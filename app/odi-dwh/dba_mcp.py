"""
DBA MCP Server — Oracle Veritabani Yonetici Araclari (READ-ONLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

10 tool:
  - dba_active_sessions       : Aktif oturumlari ve calistirdiklari SQL'i goster
  - dba_blocking_locks        : Kilit bekleyen ve tutan oturumlari tespit et
  - dba_tablespace_detail     : Tablespace doluluk, autoextend, datafile detayi
  - dba_user_privileges       : Kullanici/rol yetki matrisi sorgula
  - dba_object_privileges     : Tablo/view bazli GRANT durumu
  - dba_invalid_objects       : Gecersiz (INVALID) Oracle nesneleri listele
  - dba_redo_archive_status   : Redo log ve archive log durumu
  - dba_rman_backup_status    : Son RMAN yedekleme bilgisi
  - dba_top_sql               : En cok kaynak tuketilen SQL'ler (V$SQL)
  - dba_wait_events           : Bekleme olaylari (wait event istatistikleri)

Tum araclar READ-ONLY'dir. DML/DDL yasaktir.
SYSDBA veya DBA rolune sahip kullanici ile calisir.
"""

import json
import logging
from datetime import datetime

import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN
from diagnostic_errors import safe_error_json

logger = logging.getLogger("dba_mcp")


def _get_conn():
    """Oracle DBA baglantisi olusturur."""
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


def _json_serial(obj):
    """datetime vb. JSON'a cevirilemeyen tipleri string yapar."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


def _safe_query(sql, params=None, fallback_sql=None, fallback_params=None):
    """
    Guvenli sorgu calistirici. DBA_ gorunumleri icin yetki yoksa
    fallback_sql (genellikle USER_ gorunumu) denenir.
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
    Aktif Oracle oturumlarini, calistirdiklari SQL'i ve kaynak kullanimini gosterir.

    Args:
        limit: Maksimum satir sayisi (varsayilan 30).

    Returns:
        JSON listesi: SID, SERIAL, USERNAME, STATUS, SQL_TEXT, MACHINE, PROGRAM, LOGON_TIME
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
    Kilit bekleyen ve kilit tutan oturumlari tespit eder.
    Bloklayan -> bloklanan zincirini gosterir.

    Returns:
        JSON listesi: BLOCKER_SID, BLOCKER_USER, WAITER_SID, WAITER_USER,
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
        SELECT 'Yetki yetersiz — V$LOCK/DBA_OBJECTS erisimi gerekli' AS MESSAGE FROM DUAL
    """
    return _safe_query(sql, fallback_sql=fallback)


# ── Tool 3: Tablespace Detail ────────────────────────────────────────────────

def dba_tablespace_detail() -> str:
    """
    Tablespace bazli detayli doluluk: datafile, autoextend, max size.

    Returns:
        JSON listesi: TABLESPACE, DATAFILE, SIZE_MB, USED_MB, FREE_MB,
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
    Kullanici/rol bazli sistem yetkilerini sorgular.

    Args:
        username: Belirli bir kullanici adi (bossa tum DWH kullanicilari).

    Returns:
        JSON listesi: GRANTEE, PRIVILEGE, ADMIN_OPTION
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
    Tablo/view bazli GRANT durumunu sorgular. Kim hangi tabloya ne yetkisi vermis.

    Args:
        owner: Nesne sahibi schema (varsayilan DWH).
        object_name: Belirli bir tablo/view adi (bossa tum nesneler).

    Returns:
        JSON listesi: GRANTEE, OWNER, TABLE_NAME, PRIVILEGE, GRANTABLE
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
    INVALID durumundaki Oracle nesnelerini listeler (bozuk view, paket vb.).

    Args:
        owner: Schema filtresi (bossa DWH ile ilgili tum schemalar).

    Returns:
        JSON listesi: OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS, LAST_DDL_TIME
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
    Redo log gruplari ve archive log modunu gosterir.

    Returns:
        JSON: redo_logs listesi + archive_mode bilgisi
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
    Son X gun icindeki RMAN yedekleme islemlerini gosterir.

    Args:
        days: Kac gunluk gecmis (varsayilan 7).

    Returns:
        JSON listesi: SESSION_KEY, INPUT_TYPE, STATUS, START_TIME, END_TIME,
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
        SELECT 'V$RMAN_BACKUP_JOB_DETAILS erisimi yok veya RMAN yedekleme yapilmamis' AS MESSAGE FROM DUAL
    """
    return _safe_query(sql, {"days": days}, fallback_sql=fallback)


# ── Tool 9: Top SQL by Resources ─────────────────────────────────────────────

def dba_top_sql(order_by: str = "elapsed", limit: int = 15) -> str:
    """
    En cok kaynak tuketen SQL sorgularini listeler.

    Args:
        order_by: Siralama kriteri — 'elapsed' (sure), 'cpu', 'disk', 'executions'
        limit: Maksimum satir (varsayilan 15).

    Returns:
        JSON listesi: SQL_ID, SQL_TEXT, ELAPSED_SEC, CPU_SEC, DISK_READS,
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
    En sik karsilasilan bekleme olaylarini (wait event) gosterir.
    Performans darbogazlarini tespitte kritiktir.

    Args:
        limit: Maksimum satir (varsayilan 20).

    Returns:
        JSON listesi: EVENT, WAIT_CLASS, TOTAL_WAITS, TIME_WAITED_SEC,
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
