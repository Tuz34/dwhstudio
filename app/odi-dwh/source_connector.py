"""
Source System Abstraction (PHASE 1)
==================================
The old system relied on an MSSQL (AdventureWorks) source. In the new system,
the source is Oracle (schemas such as HR and SH). This module abstracts source
reads; mapping_generator / reconciliator / db.py do not call pyodbc directly.

Architecture:
    SourceConnector (ABC)          -> shared interface
      |-- OracleSourceConnector    -> PRIMARY PATH (Oracle source: ALL_TAB_*)
      |-- MssqlSourceConnector     -> LEGACY adapter (with explicit flag)
    get_source_connector()         -> correct connector via SOURCE_DB_TYPE (singleton)

Interface methods (based on current db.py usage):
    get_tables()                 -> list[str]   ("SCHEMA.TABLE")
    get_table_schema(table)      -> DataFrame   [COLUMN_NAME, FULL_DATA_TYPE, COMMENTS]
    get_table_comment(table)     -> str
    get_pk_columns(table)        -> list[str]   (composite PK supported)
    sample_data(table, limit)    -> DataFrame
    execute_select(sql, params)  -> DataFrame
    type_to_oracle(src_type)     -> str         (Oracle target type)
"""
import logging
from abc import ABC, abstractmethod
from functools import lru_cache

import pandas as pd

import config
from exceptions import DatabaseQueryError

logger = logging.getLogger("dwh_app.source_connector")


def _display_dsn(dsn: str | None) -> str:
    if not dsn:
        return "configured"
    return "***"


# ============================================================
# Abstract interface.
# ============================================================
class SourceConnector(ABC):
    """Shared interface for all source systems."""

    db_type: str = "base"

    @abstractmethod
    def get_tables(self) -> list[str]:
        """Return source tables in 'SCHEMA.TABLE' format."""
        ...

    @abstractmethod
    def get_table_schema(self, table_name: str) -> pd.DataFrame:
        """Columns: COLUMN_NAME, FULL_DATA_TYPE, COMMENTS."""
        ...

    @abstractmethod
    def get_table_comment(self, table_name: str) -> str:
        ...

    @abstractmethod
    def get_pk_columns(self, table_name: str) -> list[str]:
        """Primary key column names (multiple for a composite PK)."""
        ...

    @abstractmethod
    def sample_data(self, table_name: str, limit: int = 100) -> pd.DataFrame:
        ...

    @abstractmethod
    def execute_select(self, sql: str, params=None) -> pd.DataFrame:
        ...

    @abstractmethod
    def type_to_oracle(self, src_type: str, length=None, precision=None, scale=None) -> str:
        """Convert the source data type to the Oracle target type."""
        ...

    @staticmethod
    def _split(table_name: str, default_schema: str):
        if "." in table_name:
            schema, tname = table_name.split(".", 1)
        else:
            schema, tname = default_schema, table_name
        return schema, tname


# ============================================================
# ORACLE source — PRIMARY PATH
# ============================================================
class OracleSourceConnector(SourceConnector):
    db_type = "oracle"

    # Oracle->Oracle: most types are preserved. The source FULL type is already Oracle.
    _TYPE_MAP = {
        "VARCHAR2": "VARCHAR2", "NVARCHAR2": "VARCHAR2", "CHAR": "CHAR",
        "NCHAR": "CHAR", "CLOB": "CLOB", "NCLOB": "CLOB", "BLOB": "BLOB",
        "NUMBER": "NUMBER", "FLOAT": "FLOAT", "BINARY_FLOAT": "BINARY_FLOAT",
        "BINARY_DOUBLE": "BINARY_DOUBLE", "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP", "RAW": "RAW", "LONG": "CLOB",
    }

    def __init__(self):
        self.user = config.SOURCE_ORACLE_USER
        self.password = config.SOURCE_ORACLE_PASSWORD
        self.dsn = config.SOURCE_ORACLE_DSN
        self.schemas = config.SOURCE_ORACLE_SCHEMAS  # list[str] (UPPER), may be empty
        self._pool = None

    # --- connection ---
    def _connect(self):
        import oracledb
        if self._pool is None:
            try:
                self._pool = oracledb.create_pool(
                    user=self.user, password=self.password, dsn=self.dsn,
                    min=1, max=3, increment=1,
                )
                logger.info("Oracle SOURCE pool created (%s@%s)", self.user, _display_dsn(self.dsn))
            except Exception as e:
                logger.warning("Oracle SOURCE pool could not be created; direct connection: %s", e)
        if self._pool:
            return self._pool.acquire()
        return oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)

    # --- interface ---
    def get_tables(self) -> list[str]:
        conn = self._connect()
        cur = conn.cursor()
        try:
            # Exclude Oracle internal tables: text indexes (DR$), recycle bin (BIN$),
            # nested tables, and secondary (domain index) tables.
            _filter = ("AND TABLE_NAME NOT LIKE 'DR$%' "
                       "AND TABLE_NAME NOT LIKE 'BIN$%' "
                       "AND NESTED = 'NO' AND SECONDARY = 'N'")
            if self.schemas:
                placeholders = ", ".join(f":s{i}" for i in range(len(self.schemas)))
                binds = {f"s{i}": s for i, s in enumerate(self.schemas)}
                cur.execute(
                    f"""SELECT OWNER || '.' || TABLE_NAME
                        FROM ALL_TABLES WHERE OWNER IN ({placeholders})
                        {_filter}
                        ORDER BY OWNER, TABLE_NAME""", binds)
            else:
                cur.execute(
                    f"""SELECT OWNER || '.' || TABLE_NAME FROM ALL_TABLES
                       WHERE OWNER NOT IN ('SYS','SYSTEM','XDB','MDSYS','CTXSYS',
                           'OUTLN','DBSNMP','APPQOSSYS','ORDSYS','WMSYS','AUDIT_USER')
                       {_filter}
                       ORDER BY OWNER, TABLE_NAME""")
            return [r[0] for r in cur.fetchall()]
        except Exception as e:
            logger.warning("Oracle SOURCE get_tables error: %s", e)
            return []
        finally:
            cur.close(); conn.close()

    def get_table_schema(self, table_name: str) -> pd.DataFrame:
        schema, tname = self._split(table_name, self.schemas[0] if self.schemas else "HR")
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT c.COLUMN_NAME, c.DATA_TYPE, c.DATA_LENGTH,
                       c.DATA_PRECISION, c.DATA_SCALE, c.NULLABLE, cm.COMMENTS
                FROM ALL_TAB_COLUMNS c
                LEFT JOIN ALL_COL_COMMENTS cm
                  ON c.OWNER=cm.OWNER AND c.TABLE_NAME=cm.TABLE_NAME AND c.COLUMN_NAME=cm.COLUMN_NAME
                WHERE c.OWNER=:o AND c.TABLE_NAME=:t
                ORDER BY c.COLUMN_ID
            """, {"o": schema.upper(), "t": tname.upper()})
            cols = [d[0] for d in cur.description]
            df = pd.DataFrame(cur.fetchall(), columns=cols)
            if df.empty:
                return pd.DataFrame(columns=["COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH",
                "DATA_PRECISION", "DATA_SCALE", "NULLABLE", "FULL_DATA_TYPE", "COMMENTS"])

            def _full(r):
                base = str(r["DATA_TYPE"]); prec = r["DATA_PRECISION"]
                scale = r["DATA_SCALE"]; length = r["DATA_LENGTH"]
                if base in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"):
                    return f"{base}({int(length)})"
                if base == "NUMBER":
                    if pd.notna(prec) and pd.notna(scale) and int(scale) > 0:
                        return f"NUMBER({int(prec)},{int(scale)})"
                    if pd.notna(prec):
                        return f"NUMBER({int(prec)})"
                return base
            df["FULL_DATA_TYPE"] = df.apply(_full, axis=1)
            # Extended contract (PHASE 2.0): all metadata for mapping_generator.
            return df[["COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION",
                       "DATA_SCALE", "NULLABLE", "FULL_DATA_TYPE", "COMMENTS"]]
        finally:
            cur.close(); conn.close()

    def get_table_comment(self, table_name: str) -> str:
        schema, tname = self._split(table_name, self.schemas[0] if self.schemas else "HR")
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("""SELECT COMMENTS FROM ALL_TAB_COMMENTS
                           WHERE OWNER=:o AND TABLE_NAME=:t""",
                        {"o": schema.upper(), "t": tname.upper()})
            row = cur.fetchone()
            return row[0] if row and row[0] else ""
        except Exception:
            return ""
        finally:
            cur.close(); conn.close()

    def get_pk_columns(self, table_name: str) -> list[str]:
        schema, tname = self._split(table_name, self.schemas[0] if self.schemas else "HR")
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT cc.COLUMN_NAME
                FROM ALL_CONSTRAINTS c
                JOIN ALL_CONS_COLUMNS cc
                  ON c.CONSTRAINT_NAME=cc.CONSTRAINT_NAME AND c.OWNER=cc.OWNER
                WHERE c.CONSTRAINT_TYPE='P' AND c.OWNER=:o AND c.TABLE_NAME=:t
                ORDER BY cc.POSITION
            """, {"o": schema.upper(), "t": tname.upper()})
            return [r[0] for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            cur.close(); conn.close()

    def sample_data(self, table_name: str, limit: int = 100) -> pd.DataFrame:
        schema, tname = self._split(table_name, self.schemas[0] if self.schemas else "HR")
        sql = f'SELECT * FROM "{schema.upper()}"."{tname.upper()}" FETCH FIRST {int(limit)} ROWS ONLY'
        return self.execute_select(sql)

    def execute_select(self, sql: str, params=None) -> pd.DataFrame:
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql, params or {})
            cols = [d[0] for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
        finally:
            cur.close(); conn.close()

    def type_to_oracle(self, src_type, length=None, precision=None, scale=None) -> str:
        # Oracle source: if the type arrives in full form (VARCHAR2(20), NUMBER(10,2)), keep it unchanged.
        if src_type and "(" in src_type:
            return src_type
        base = (src_type or "").upper().split("(")[0].strip()
        return self._TYPE_MAP.get(base, src_type or "VARCHAR2(4000)")


# ============================================================
# MSSQL source — LEGACY adapter (enabled with an explicit flag)
# ============================================================
class MssqlSourceConnector(SourceConnector):
    db_type = "mssql"

    # MSSQL -> Oracle type mapping.
    _TYPE_MAP = {
        "int": "NUMBER(10)", "bigint": "NUMBER(19)", "smallint": "NUMBER(5)",
        "tinyint": "NUMBER(3)", "bit": "NUMBER(1)",
        "decimal": "NUMBER", "numeric": "NUMBER", "money": "NUMBER(19,4)",
        "smallmoney": "NUMBER(10,4)", "float": "FLOAT", "real": "BINARY_FLOAT",
        "varchar": "VARCHAR2", "nvarchar": "VARCHAR2", "char": "CHAR",
        "nchar": "CHAR", "text": "CLOB", "ntext": "CLOB",
        "date": "DATE", "datetime": "DATE", "datetime2": "TIMESTAMP",
        "smalldatetime": "DATE", "time": "TIMESTAMP",
        "uniqueidentifier": "VARCHAR2(36)", "varbinary": "BLOB", "binary": "RAW",
        "image": "BLOB", "xml": "CLOB",
    }

    def _conn_str(self):
        server = config.MSSQL_SERVER or config.MSSQL_DEFAULT_SERVER
        database = config.MSSQL_DATABASE or config.MSSQL_DEFAULT_DATABASE
        if not server or not database:
            raise RuntimeError(
                "MSSQL source connector requires MSSQL_SERVER/MSSQL_DATABASE "
                "or MSSQL_DEFAULT_SERVER/MSSQL_DEFAULT_DATABASE."
            )
        if config.MSSQL_USER and config.MSSQL_PASSWORD:
            return (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
                    f"DATABASE={database};UID={config.MSSQL_USER};PWD={config.MSSQL_PASSWORD}")
        return (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
                f"DATABASE={database};Trusted_Connection=yes")

    def _connect(self, timeout=5):
        import pyodbc
        return pyodbc.connect(self._conn_str(), timeout=timeout)

    def get_tables(self) -> list[str]:
        try:
            conn = self._connect(timeout=3)
            cur = conn.cursor()
            cur.execute("SELECT TABLE_SCHEMA + '.' + TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_TYPE='BASE TABLE' ORDER BY 1")
            tables = [r[0] for r in cur.fetchall()]
            conn.close()
            return tables
        except Exception as e:
            logger.warning("MSSQL get_tables error: %s", e)
            return []

    def get_table_schema(self, table_name: str) -> pd.DataFrame:
        try:
            schema, tname = self._split(table_name, "dbo")
            conn = self._connect(timeout=3)
            query = """
                SELECT c.COLUMN_NAME,
                  c.DATA_TYPE,
                  c.CHARACTER_MAXIMUM_LENGTH AS DATA_LENGTH,
                  c.NUMERIC_PRECISION AS DATA_PRECISION,
                  c.NUMERIC_SCALE AS DATA_SCALE,
                  c.IS_NULLABLE AS NULLABLE,
                  c.DATA_TYPE + ISNULL('(' + CAST(NULLIF(c.CHARACTER_MAXIMUM_LENGTH,-1) AS VARCHAR)+')','') AS FULL_DATA_TYPE,
                  CAST(ep.value AS VARCHAR(4000)) AS COMMENTS
                FROM INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN sys.tables st ON st.name=c.TABLE_NAME AND schema_name(st.schema_id)=c.TABLE_SCHEMA
                LEFT JOIN sys.columns sc ON sc.object_id=st.object_id AND sc.name=c.COLUMN_NAME
                LEFT JOIN sys.extended_properties ep ON ep.major_id=st.object_id AND ep.minor_id=sc.column_id AND ep.name='MS_Description'
                WHERE c.TABLE_SCHEMA=? AND c.TABLE_NAME=? ORDER BY c.ORDINAL_POSITION
            """
            df = pd.read_sql(query, conn, params=(schema, tname))
            conn.close()
            return df
        except Exception as e:
            logger.warning("MSSQL get_table_schema error (%s): %s", table_name, e)
            return pd.DataFrame(columns=["COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH",
                "DATA_PRECISION", "DATA_SCALE", "NULLABLE", "FULL_DATA_TYPE", "COMMENTS"])

    def get_table_comment(self, table_name: str) -> str:
        try:
            schema, tname = self._split(table_name, "dbo")
            conn = self._connect(timeout=3)
            cur = conn.cursor()
            cur.execute("""SELECT CAST(ep.value AS VARCHAR(4000)) FROM sys.tables st
                LEFT JOIN sys.extended_properties ep ON ep.major_id=st.object_id AND ep.minor_id=0 AND ep.name='MS_Description'
                WHERE schema_name(st.schema_id)=? AND st.name=?""", (schema, tname))
            row = cur.fetchone()
            conn.close()
            return row[0] if row and row[0] else ""
        except Exception:
            return ""

    def get_pk_columns(self, table_name: str) -> list[str]:
        try:
            schema, tname = self._split(table_name, "dbo")
            conn = self._connect(timeout=3)
            cur = conn.cursor()
            cur.execute("""
                SELECT kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME=kcu.CONSTRAINT_NAME
                WHERE tc.CONSTRAINT_TYPE='PRIMARY KEY'
                  AND tc.TABLE_SCHEMA=? AND tc.TABLE_NAME=?
                ORDER BY kcu.ORDINAL_POSITION
            """, (schema, tname))
            pks = [r[0] for r in cur.fetchall()]
            conn.close()
            return pks
        except Exception:
            return []

    def sample_data(self, table_name: str, limit: int = 100) -> pd.DataFrame:
        schema, tname = self._split(table_name, "dbo")
        return self.execute_select(f"SELECT TOP {int(limit)} * FROM [{schema}].[{tname}]")

    def execute_select(self, sql: str, params=None) -> pd.DataFrame:
        try:
            conn = self._connect(timeout=10)
            df = pd.read_sql(sql, conn, params=params)
            conn.close()
            return df
        except Exception as exc:
            logger.error("MSSQL execute_select failed: %s", type(exc).__name__)
            raise DatabaseQueryError("Source query failed.") from exc

    def type_to_oracle(self, src_type, length=None, precision=None, scale=None) -> str:
        base = (src_type or "").lower().split("(")[0].strip()
        mapped = self._TYPE_MAP.get(base, "VARCHAR2(4000)")
        if mapped == "VARCHAR2" and length is not None and int(length) < 0:
            return "CLOB"
        if mapped in ("VARCHAR2", "CHAR") and length and int(length) > 0:
            return f"{mapped}({length})"
        return mapped


# ============================================================
# Factory (singleton, via SOURCE_DB_TYPE)
# ============================================================
@lru_cache(maxsize=2)
def get_source_connector(db_type: str | None = None) -> SourceConnector:
    """Return the connector for SOURCE_DB_TYPE. Oracle is primary; MSSQL is legacy."""
    dt = (db_type or config.SOURCE_DB_TYPE or "oracle").lower()
    if dt == "mssql":
        if not getattr(config, "ENABLE_MSSQL_SOURCE", False):
            raise RuntimeError(
                "MSSQL source connector is disabled by default. "
                "Set ENABLE_MSSQL_SOURCE=true explicitly to use the legacy adapter."
            )
        logger.info("Source connector: MSSQL (legacy adapter)")
        return MssqlSourceConnector()
    logger.info("Source connector: Oracle (primary path)")
    return OracleSourceConnector()
