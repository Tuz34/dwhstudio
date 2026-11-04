"""
DBA MCP Server — Oracle Database Administrator Tools (READ-ONLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

10 tool:
  - dba_active_sessions       : Show active sessions and their SQL
  - dba_blocking_locks        : Identify sessions waiting for and holding locks
  - dba_tablespace_detail     : Tablespace usage, autoextend, and datafile details
  - dba_user_privileges       : Query the user/role privilege matrix
  - dba_object_privileges     : Table/view GRANT status
  - dba_invalid_objects       : List invalid (INVALID) Oracle objects
  - dba_redo_archive_status   : Redo log and archive log status
  - dba_rman_backup_status    : Latest RMAN backup information
  - dba_top_sql               : SQL statements consuming the most resources (V$SQL)
  - dba_wait_events           : Wait events (wait event statistics)

All tools are READ-ONLY. DML/DDL is prohibited.
Runs with a user having SYSDBA or the DBA role.
"""

import json
import logging
from datetime import datetime

import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN
from diagnostic_errors import safe_error_json

logger = logging.getLogger("dba_mcp")


def _get_conn():
    """Creates an Oracle DBA connection."""
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


def _json_serial(obj):
    """Converts types that cannot be serialized to JSON, such as datetime, to strings."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


def _safe_query(sql, params=None, fallback_sql=None, fallback_params=None):
    """
    Safe query executor. If access to DBA_ views is unavailable,
    fallback_sql (usually a USER_ view) is tried.
    """
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params or {})
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return json.dumps(rows, default=_json_serial, ensure_ascii=False)
    except oracledb.DatabaseError as e:
        if fallback_sql and ("ORA-00942" in str(e) or "ORA-01031" in str(e)):
            try:
                cursor2 = conn.cursor()
                cursor2.execute(fallback_sql, fallback_params or {})
                cols = [d[0] for d in cursor2.description]
                rows = [dict(zip(cols, row)) for row in cursor2.fetchall()]
                cursor2.close()
                return json.dumps(rows, default=_json_serial, ensure_ascii=False)
            except Exception as e2:
                return safe_error_json("DBA fallback read", e2)
        return safe_error_json("DBA read", e)
    except Exception as e:
        return safe_error_json("DBA read", e)
    finally:
        cursor.close()
        conn.close()


# ── Tool 1: Active Sessions ──────────────────────────────────────────────────

def dba_active_sessions(limit: int = 30) -> str:
    """
    Shows active Oracle sessions, their SQL, and resource usage.

    Args:
        limit: Maximum row count (default 30).

    Returns:
        JSON list: SID, SERIAL, USERNAME, STATUS, SQL_TEXT, MACHINE, PROGRAM, LOGON_TIME
    """
    sql = """
        SELECT
            s.SID,
            s.SERIAL# AS SERIAL_NO,
            s.USERNAME,
            s.STATUS,
            s.MACHINE,
            s.PROGRAM,
            s.LOGON_TIME,
            s.LAST_CALL_ET AS IDLE_SECONDS,
            s.SQL_ID,
            SUBSTR(q.SQL_TEXT, 1, 200) AS SQL_TEXT
        FROM V$SESSION s
        LEFT JOIN V$SQL q ON s.SQL_ID = q.SQL_ID AND s.SQL_CHILD_NUMBER = q.CHILD_NUMBER
        WHERE s.TYPE = 'USER'
          AND s.USERNAME IS NOT NULL
        ORDER BY
            CASE s.STATUS WHEN 'ACTIVE' THEN 0 ELSE 1 END,
            s.LOGON_TIME DESC
        FETCH FIRST :lmt ROWS ONLY
    """
    return _safe_query(sql, {"lmt": limit})


# ── Tool 2: Blocking Locks ───────────────────────────────────────────────────

def dba_blocking_locks() -> str:
    """
    Identifies sessions waiting for and holding locks.
    Shows the blocker -> blocked chain.

    Returns:
        JSON list: BLOCKER_SID, BLOCKER_USER, WAITER_SID, WAITER_USER,
                       LOCKED_OBJECT, LOCK_TYPE, WAIT_SECONDS
    """
    sql = """
        SELECT
            lh.SID        AS BLOCKER_SID,
            sh.USERNAME   AS BLOCKER_USER,
            sh.PROGRAM    AS BLOCKER_PROGRAM,
            lw.SID        AS WAITER_SID,
            sw.USERNAME   AS WAITER_USER,
            sw.PROGRAM    AS WAITER_PROGRAM,
            do.OBJECT_NAME AS LOCKED_OBJECT,
            do.OBJECT_TYPE AS OBJECT_TYPE,
            lh.TYPE       AS LOCK_TYPE,
            sw.SECONDS_IN_WAIT AS WAIT_SECONDS
        FROM V$LOCK lh
        JOIN V$LOCK lw  ON lh.ID1 = lw.ID1 AND lh.ID2 = lw.ID2
                       AND lh.BLOCK > 0 AND lw.REQUEST > 0
        JOIN V$SESSION sh ON lh.SID = sh.SID
        JOIN V$SESSION sw ON lw.SID = sw.SID
        LEFT JOIN DBA_OBJECTS do ON lh.ID1 = do.OBJECT_ID
        ORDER BY sw.SECONDS_IN_WAIT DESC
    """
    fallback = """
        SELECT 'Insufficient privileges — V$LOCK/DBA_OBJECTS access required' AS MESSAGE FROM DUAL
    """
    return _safe_query(sql, fallback_sql=fallback)


# ── Tool 3: Tablespace Detail ────────────────────────────────────────────────

def dba_tablespace_detail() -> str:
    """
    Detailed tablespace usage: datafile, autoextend, max size.

    Returns:
        JSON list: TABLESPACE, DATAFILE, SIZE_MB, USED_MB, FREE_MB,
                       USED_PCT, AUTOEXTENSIBLE, MAX_SIZE_MB
    """
    sql = """
        SELECT
            df.TABLESPACE_NAME AS TABLESPACE,
            df.FILE_NAME       AS DATAFILE,
            ROUND(df.BYTES / 1024 / 1024, 1) AS SIZE_MB,
            ROUND((df.BYTES - NVL(fs.FREE_BYTES, 0)) / 1024 / 1024, 1) AS USED_MB,
            ROUND(NVL(fs.FREE_BYTES, 0) / 1024 / 1024, 1) AS FREE_MB,
            ROUND((1 - NVL(fs.FREE_BYTES, 0) / df.BYTES) * 100, 1) AS USED_PCT,
            df.AUTOEXTENSIBLE,
            ROUND(df.MAXBYTES / 1024 / 1024, 1) AS MAX_SIZE_MB
        FROM DBA_DATA_FILES df
        LEFT JOIN (
            SELECT FILE_ID, SUM(BYTES) AS FREE_BYTES
            FROM DBA_FREE_SPACE
            GROUP BY FILE_ID
        ) fs ON df.FILE_ID = fs.FILE_ID
        ORDER BY USED_PCT DESC
    """
    fallback = """
        SELECT
            TABLESPACE_NAME AS TABLESPACE,
            ROUND(SUM(BYTES) / 1024 / 1024, 1) AS TOTAL_MB
        FROM USER_SEGMENTS
        GROUP BY TABLESPACE_NAME
        ORDER BY TOTAL_MB DESC
    """
    return _safe_query(sql, fallback_sql=fallback)


# ── Tool 4: User Privileges ──────────────────────────────────────────────────

def dba_user_privileges(username: str = "") -> str:
    """
    Queries system privileges by user/role.

    Args:
        username: Specific username (all DWH users if empty).

    Returns:
        JSON list: GRANTEE, PRIVILEGE, ADMIN_OPTION
    """
    if username:
        sql = """
            SELECT GRANTEE, PRIVILEGE, ADMIN_OPTION
            FROM DBA_SYS_PRIVS
            WHERE GRANTEE = UPPER(:uname)
            UNION ALL
            SELECT GRANTEE, GRANTED_ROLE AS PRIVILEGE, ADMIN_OPTION
            FROM DBA_ROLE_PRIVS
            WHERE GRANTEE = UPPER(:uname)
            ORDER BY 1, 2
        """
        return _safe_query(sql, {"uname": username})
    else:
        sql = """
            SELECT GRANTEE, PRIVILEGE, ADMIN_OPTION
            FROM DBA_SYS_PRIVS
            WHERE GRANTEE IN ('ODS_OWNER', 'DWH_OWNER', 'ODI_EXEC', 'APP_USER', 'AUDIT_USER', 'DWH')
            UNION ALL
            SELECT GRANTEE, GRANTED_ROLE AS PRIVILEGE, ADMIN_OPTION
            FROM DBA_ROLE_PRIVS
            WHERE GRANTEE IN ('ODS_OWNER', 'DWH_OWNER', 'ODI_EXEC', 'APP_USER', 'AUDIT_USER', 'DWH')
            ORDER BY 1, 2
        """
        return _safe_query(sql)


# ── Tool 5: Object Privileges ────────────────────────────────────────────────

def dba_object_privileges(owner: str = "DWH", object_name: str = "") -> str:
    """
    Queries table/view GRANT status: who granted which privilege on which table.

    Args:
        owner: Object owner schema (default DWH).
        object_name: Specific table/view name (all objects if empty).

    Returns:
        JSON list: GRANTEE, OWNER, TABLE_NAME, PRIVILEGE, GRANTABLE
    """
    if object_name:
        sql = """
            SELECT GRANTEE, OWNER, TABLE_NAME, PRIVILEGE, GRANTABLE
            FROM DBA_TAB_PRIVS
            WHERE OWNER = UPPER(:own) AND TABLE_NAME = UPPER(:obj)
            ORDER BY GRANTEE, PRIVILEGE
        """
        return _safe_query(sql, {"own": owner, "obj": object_name})
    else:
        sql = """
            SELECT GRANTEE, OWNER, TABLE_NAME, PRIVILEGE, GRANTABLE
            FROM DBA_TAB_PRIVS
            WHERE OWNER = UPPER(:own)
            ORDER BY TABLE_NAME, GRANTEE, PRIVILEGE
            FETCH FIRST 200 ROWS ONLY
        """
        return _safe_query(sql, {"own": owner})


# ── Tool 6: Invalid Objects ──────────────────────────────────────────────────

def dba_invalid_objects(owner: str = "") -> str:
    """
    Lists Oracle objects with INVALID status (broken views, packages, etc.).

    Args:
        owner: Schema filter (all DWH related schemas if empty).

    Returns:
        JSON list: OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS, LAST_DDL_TIME
    """
    if owner:
        sql = """
            SELECT OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS, LAST_DDL_TIME
            FROM DBA_OBJECTS
            WHERE OWNER = UPPER(:own) AND STATUS = 'INVALID'
            ORDER BY OBJECT_TYPE, OBJECT_NAME
        """
        params = {"own": owner}
    else:
        sql = """
            SELECT OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS, LAST_DDL_TIME
            FROM DBA_OBJECTS
            WHERE OWNER IN ('ODS_OWNER', 'DWH_OWNER', 'ODI_EXEC', 'APP_USER', 'AUDIT_USER', 'DWH')
              AND STATUS = 'INVALID'
            ORDER BY OWNER, OBJECT_TYPE, OBJECT_NAME
        """
        params = {}
    fallback = """
        SELECT OBJECT_NAME, OBJECT_TYPE, STATUS, LAST_DDL_TIME
        FROM USER_OBJECTS WHERE STATUS = 'INVALID'
        ORDER BY OBJECT_TYPE, OBJECT_NAME
    """
    return _safe_query(sql, params, fallback_sql=fallback)


# ── Tool 7: Redo & Archive Log Status ────────────────────────────────────────

def dba_redo_archive_status() -> str:
    """
    Shows redo log groups and archive log mode.

    Returns:
        JSON: redo_logs list + archive_mode information
    """
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        # Redo logs
        cursor.execute("""
            SELECT GROUP#, THREAD#, BYTES / 1024 / 1024 AS SIZE_MB,
                   MEMBERS, STATUS
            FROM V$LOG
            ORDER BY GROUP#
        """)
        cols = [d[0] for d in cursor.description]
        redo_logs = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Archive mode
        cursor.execute("SELECT LOG_MODE, FORCE_LOGGING FROM V$DATABASE")
        row = cursor.fetchone()
        archive_info = {"LOG_MODE": row[0], "FORCE_LOGGING": row[1]} if row else {}

        result = {
            "archive_mode": archive_info,
            "redo_logs": redo_logs,
        }
        return json.dumps(result, default=_json_serial, ensure_ascii=False)

    except Exception as e:
        return safe_error_json("Redo archive status read", e)
    finally:
        cursor.close()
        conn.close()


# ── Tool 8: RMAN Backup Status ───────────────────────────────────────────────

def dba_rman_backup_status(days: int = 7) -> str:
    """
    Shows RMAN backup operations in the last X days.

    Args:
        days: How many days back (default 7).

    Returns:
        JSON list: SESSION_KEY, INPUT_TYPE, STATUS, START_TIME, END_TIME,
                       INPUT_BYTES_DISPLAY, OUTPUT_BYTES_DISPLAY, ELAPSED_SECONDS
    """
    sql = """
        SELECT
            SESSION_KEY,
            INPUT_TYPE,
            STATUS,
            START_TIME,
            END_TIME,
            INPUT_BYTES_DISPLAY,
            OUTPUT_BYTES_DISPLAY,
            ELAPSED_SECONDS
        FROM V$RMAN_BACKUP_JOB_DETAILS
        WHERE START_TIME > SYSDATE - :days
        ORDER BY START_TIME DESC
        FETCH FIRST 50 ROWS ONLY
    """
    fallback = """
        SELECT 'No access to V$RMAN_BACKUP_JOB_DETAILS or no RMAN backup has been performed' AS MESSAGE FROM DUAL
    """
    return _safe_query(sql, {"days": days}, fallback_sql=fallback)


# ── Tool 9: Top SQL by Resources ─────────────────────────────────────────────

def dba_top_sql(order_by: str = "elapsed", limit: int = 15) -> str:
    """
    Lists SQL queries that consume the most resources.

    Args:
        order_by: Sort criterion — 'elapsed' (duration), 'cpu', 'disk', 'executions'
        limit: Maximum rows (default 15).

    Returns:
        JSON list: SQL_ID, SQL_TEXT, ELAPSED_SEC, CPU_SEC, DISK_READS,
                       BUFFER_GETS, EXECUTIONS, ROWS_PROCESSED
    """
    order_map = {
        "elapsed": "ELAPSED_TIME DESC",
        "cpu": "CPU_TIME DESC",
        "disk": "DISK_READS DESC",
        "executions": "EXECUTIONS DESC",
    }
    order_clause = order_map.get(order_by, "ELAPSED_TIME DESC")

    sql = f"""
        SELECT
            SQL_ID,
            SUBSTR(SQL_TEXT, 1, 200) AS SQL_TEXT,
            ROUND(ELAPSED_TIME / 1000000, 2) AS ELAPSED_SEC,
            ROUND(CPU_TIME / 1000000, 2) AS CPU_SEC,
            DISK_READS,
            BUFFER_GETS,
            EXECUTIONS,
            ROWS_PROCESSED,
            PARSING_SCHEMA_NAME AS SCHEMA_NAME
        FROM V$SQL
        WHERE PARSING_SCHEMA_NAME NOT IN ('SYS', 'SYSTEM', 'DBSNMP', 'MDSYS', 'XDB')
          AND EXECUTIONS > 0
        ORDER BY {order_clause}
        FETCH FIRST :lmt ROWS ONLY
    """
    return _safe_query(sql, {"lmt": limit})


# ── Tool 10: Wait Events ─────────────────────────────────────────────────────

def dba_wait_events(limit: int = 20) -> str:
    """
    Shows the most frequent wait events.
    Critical for identifying performance bottlenecks.

    Args:
        limit: Maximum rows (default 20).

    Returns:
        JSON list: EVENT, WAIT_CLASS, TOTAL_WAITS, TIME_WAITED_SEC,
                       AVG_WAIT_MS, PCT_OF_TOTAL
    """
    sql = """
        SELECT
            EVENT,
            WAIT_CLASS,
            TOTAL_WAITS,
            ROUND(TIME_WAITED / 100, 2) AS TIME_WAITED_SEC,
            ROUND(AVERAGE_WAIT * 10, 2) AS AVG_WAIT_MS
        FROM V$SYSTEM_EVENT
        WHERE WAIT_CLASS != 'Idle'
          AND TOTAL_WAITS > 0
        ORDER BY TIME_WAITED DESC
        FETCH FIRST :lmt ROWS ONLY
    """
    return _safe_query(sql, {"lmt": limit})
