import logging
import re
import threading

import oracledb
import pandas as pd

from config import ORACLE_DDL_PASSWORD, ORACLE_DDL_USER, ORACLE_DSN, ORACLE_PASSWORD, ORACLE_USER
from exceptions import DatabaseQueryError

logger = logging.getLogger("dwh_app.db")

# Y3: Lazy-initialized connection pool (min=1, max=5)
# Pooling is unsupported in SYSDBA mode — use a direct connection for the sys user.
_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()
_LEADING_SQL_NOISE = re.compile(r"^(?:(?:\s+)|(?:--[^\r\n]*(?:\r?\n|$))|(?:/\*.*?\*/))*", re.DOTALL)
_DDL_KEYWORDS = {"COMMENT", "CREATE", "ALTER", "DROP", "GRANT", "REVOKE"}


def _requires_ddl_connection(sql: str) -> bool:
    """Select elevated credentials only for an actual leading DDL statement."""

    statement = _LEADING_SQL_NOISE.sub("", str(sql or ""), count=1)
    keyword_match = re.match(r"[A-Za-z]+", statement)
    return bool(keyword_match and keyword_match.group(0).upper() in _DDL_KEYWORDS)

def _get_pool() -> oracledb.ConnectionPool | None:
    global _pool
    if _pool is not None:
        return _pool
    if not ORACLE_USER or ORACLE_USER.lower() == "sys":
        return None   # SYSDBA or missing credentials — do not create a pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            _pool = oracledb.create_pool(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
                min=1,
                max=5,
                increment=1,
            )
            logger.info("Oracle connection pool created (min=1, max=5)")
        except Exception as e:
            logger.warning("Oracle pool initialization failed: %s", type(e).__name__)
    return _pool

def get_connection(use_ddl=False):
    """Returns a pooled connection. Connects directly for SYSDBA or when no pool exists."""
    if use_ddl and ORACLE_DDL_USER and ORACLE_DDL_PASSWORD:
        return oracledb.connect(user=ORACLE_DDL_USER, password=ORACLE_DDL_PASSWORD, dsn=ORACLE_DSN)
        
    if ORACLE_USER and ORACLE_USER.lower() == "sys":
        return oracledb.connect(
            user=ORACLE_USER, password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN, mode=oracledb.AUTH_MODE_SYSDBA
        )
    pool = _get_pool()
    if pool:
        return pool.acquire()
    return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)


def test_connection() -> bool:
    try:
        conn = get_connection()
        conn.close()
        return True
    except Exception:
        return False


def execute_dml(sql: str, params: dict = None) -> str:
    sql = sql.strip()
    upper_sql = sql.upper()
    
    # Elevated credentials are selected from the actual leading statement,
    # never from keywords inside comments, literals, or PL/SQL bodies.
    is_ddl = _requires_ddl_connection(sql)
    
    conn = get_connection(use_ddl=is_ddl)
    cursor = None
    try:
        cursor = conn.cursor()
        # In Oracle, a semicolon causes an error in ordinary DML,
        # but is required in PL/SQL blocks starting with BEGIN / DECLARE.
        upper_sql = sql.upper()
        if not (upper_sql.startswith("BEGIN") or upper_sql.startswith("DECLARE")):
            if sql.endswith(";"):
                sql = sql[:-1].strip()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        affected = cursor.rowcount
        conn.commit()
        return f"{affected} rows affected."
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.error("Database rollback failed: %s", type(rollback_exc).__name__)
        raise DatabaseQueryError("Database write failed.") from exc
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()


def _format_dates(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        # datetime columns → dd.MM.yyyy
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            if (df[col].dt.hour != 0).any() or (df[col].dt.minute != 0).any():
                df[col] = df[col].dt.strftime("%d.%m.%Y %H:%M")
            else:
                df[col] = df[col].dt.strftime("%d.%m.%Y")
        # large numbers such as epoch milliseconds (columns that may contain dates)
        elif pd.api.types.is_numeric_dtype(df[col]):
            col_lower = col.lower()
            if any(k in col_lower for k in ("date", "tarih", "time", "zaman", "created", "updated")):
                try:
                    converted = pd.to_datetime(df[col], unit="ms")
                    if (converted.dt.hour != 0).any() or (converted.dt.minute != 0).any():
                        df[col] = converted.dt.strftime("%d.%m.%Y %H:%M")
                    else:
                        df[col] = converted.dt.strftime("%d.%m.%Y")
                except Exception:
                    pass
    return df


def execute_select(sql: str) -> pd.DataFrame:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        df = pd.DataFrame(rows, columns=columns)
        return _format_dates(df)
    except Exception as exc:
        raise DatabaseQueryError("Database query failed.") from exc
    finally:
        conn.close()


def get_user_tables() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
                       SELECT OWNER || '.' || TABLE_NAME
                       FROM ALL_TABLES
                       WHERE OWNER IN ('DWH', 'ODS', 'AUDIT_USER')
                       ORDER BY OWNER, TABLE_NAME
                       """)
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()

# ============================================================
# SOURCE access — PHASE 1: through source_connector
# SOURCE_DB_TYPE=oracle (primary path) | mssql (fallback). Pure MSSQL/Oracle
# code is in source_connector.py; these are thin delegates.
# ============================================================

def get_source_connector_lazy():
    # Lazy import (for circular dependencies and configuration order)
    from source_connector import get_source_connector
    return get_source_connector()

def get_source_tables() -> list:
    """Returns active source (SOURCE_DB_TYPE) tables as 'SCHEMA.TABLE'."""
    return get_source_connector_lazy().get_tables()

def get_source_table_schema(table_name: str) -> pd.DataFrame:
    """Columns: COLUMN_NAME, FULL_DATA_TYPE, COMMENTS."""
    return get_source_connector_lazy().get_table_schema(table_name)

def get_source_table_comment(table_name: str) -> str:
    return get_source_connector_lazy().get_table_comment(table_name)

def get_source_pk_columns(table_name: str) -> list:
    """Source PK columns with composite PK support."""
    return get_source_connector_lazy().get_pk_columns(table_name)

def source_sample_data(table_name: str, limit: int = 100) -> pd.DataFrame:
    return get_source_connector_lazy().sample_data(table_name, limit)

def execute_source_select(sql: str, params=None) -> pd.DataFrame:
    return get_source_connector_lazy().execute_select(sql, params)


# --- Backward compatibility: old get_mssql_* names (now delegated to connector) ---
# NOTE: These names route to an Oracle OR MSSQL source according to SOURCE_DB_TYPE.
# In PHASE 2, callers will switch to get_source_* names; these will then be removed.

def get_mssql_tables() -> list:
    return get_source_tables()

def get_mssql_table_schema(table_name: str) -> pd.DataFrame:
    return get_source_table_schema(table_name)

def execute_mssql_select(sql: str, params: tuple = None) -> pd.DataFrame:
    return execute_source_select(sql, params)

def get_mssql_table_comment(table_name: str) -> str:
    return get_source_table_comment(table_name)



def get_table_schema(table_name: str) -> pd.DataFrame:
    # Split the DWH.TABLO_ADI format
    if '.' in table_name:
        owner, tname = table_name.split('.', 1)
    else:
        owner, tname = 'DWH', table_name

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH,
                   c.DATA_PRECISION, c.DATA_SCALE, c.NULLABLE, c.DATA_DEFAULT,
                   cm.COMMENTS
            FROM ALL_TAB_COLUMNS c
            LEFT JOIN ALL_COL_COMMENTS cm
              ON c.OWNER = cm.OWNER AND c.TABLE_NAME = cm.TABLE_NAME AND c.COLUMN_NAME = cm.COLUMN_NAME
            WHERE c.OWNER = :owner
              AND c.TABLE_NAME = :tname
            ORDER BY c.COLUMN_ID
        """, {"owner": owner.upper(), "tname": tname.upper()})
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        df = pd.DataFrame(rows, columns=columns)
        
        # Reconstruct full types like VARCHAR2(50), NUMBER(10,2)
        def format_type(r):
            base = str(r['DATA_TYPE'])
            prec = r['DATA_PRECISION']
            scale = r['DATA_SCALE']
            length = r['DATA_LENGTH']
            
            if base in ['VARCHAR2', 'CHAR', 'NVARCHAR2', 'NCHAR']:
                return f"{base}({length})"
            if base == 'NUMBER':
                if not pd.isna(prec) and not pd.isna(scale) and int(scale) > 0:
                    return f"{base}({int(prec)},{int(scale)})"
                if not pd.isna(prec):
                    return f"{base}({int(prec)})"
            return base
            
        df['FULL_DATA_TYPE'] = df.apply(format_type, axis=1)
        return df
    finally:
        cursor.close()
        conn.close()


def get_table_constraints(table_name: str) -> pd.DataFrame:
    if '.' in table_name:
        owner, tname = table_name.split('.', 1)
    else:
        owner, tname = 'DWH', table_name

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT
                c.CONSTRAINT_NAME,
                c.CONSTRAINT_TYPE,
                cc.COLUMN_NAME,
                c.STATUS
            FROM ALL_CONSTRAINTS c
            JOIN ALL_CONS_COLUMNS cc
                ON c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME AND c.OWNER = cc.OWNER
            WHERE c.TABLE_NAME = :tname AND c.OWNER = :owner
            ORDER BY c.CONSTRAINT_TYPE, cc.POSITION
        """, {"tname": tname.upper(), "owner": owner.upper()})
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=columns)
    finally:
        cursor.close()
        conn.close()


def get_schema_for_ai(table_names: list) -> str:
    lines = []
    for tbl in table_names:
        try:
            df = get_table_schema(tbl)
            cols = ", ".join(
                f"{row['COLUMN_NAME']} {row['DATA_TYPE']}"
                for _, row in df.iterrows()
            )
            lines.append(f"TABLE {tbl} ({cols})")
        except Exception as e:
            lines.append(f"-- TABLE {tbl}: schema unavailable ({e})")
    return "\n".join(lines)


def get_table_comment(table_name: str) -> str:
    if '.' in table_name:
        owner, tname = table_name.split('.', 1)
    else:
        owner, tname = 'DWH', table_name

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COMMENTS 
            FROM ALL_TAB_COMMENTS 
            WHERE OWNER = :owner AND TABLE_NAME = :tname
        """, {"owner": owner.upper(), "tname": tname.upper()})
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""
    finally:
        cursor.close()
        conn.close()
