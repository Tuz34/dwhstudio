"""
Monitoring MCP Server — READ-ONLY Monitoring & DQ Tools
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

6 tool:
  - mon_etl_job_metrics      : ODI session durations, row counts, error rates
  - mon_data_freshness       : latest load dates from ETL_WATERMARK + day difference
  - mon_tablespace_usage     : Oracle tablespace usage percentages
  - mon_dq_score             : Table-level DQ score (individual or aggregate)
  - mon_schema_changes       : latest changes from schema_change_detector
  - mon_alert_summary        : unapproved alerts from ALERT_LOG

All tools are READ-ONLY. INSERT/UPDATE/DELETE are prohibited.
"""

import json
import logging
from datetime import datetime

import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN, ODI_REPO_SCHEMA
from diagnostic_errors import safe_error_json, safe_error_payload

logger = logging.getLogger("monitoring_mcp")


def _repo_table(table_name: str) -> str:
    schema = (ODI_REPO_SCHEMA or "REPO_ODI_REPO").upper()
    if not schema.replace("_", "").isalnum():
        raise ValueError("Invalid ODI repository schema name")
    return f"{schema}.{table_name}"


def _get_conn():
    """Create a new Oracle connection."""
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


# ── Tool 1: ETL Job Metrics ─────────────────────────────────────────────────

def mon_etl_job_metrics(limit: int = 20) -> str:
    """
    Retrieve ODI session durations, row counts, and error rates.
    Reads from the configured ODI repository SNP_SESSION table.
    """
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT
                S.SESS_NO,
                S.SESS_NAME,
                S.SESS_BEG,
                S.SESS_END,
                ROUND((CAST(S.SESS_END AS DATE) - CAST(S.SESS_BEG AS DATE)) * 86400) AS DURATION_SEC,
                S.SESS_STATUS,
                S.NB_INS AS INSERTED,
                S.NB_UPD AS UPDATED,
                S.NB_ERR AS ERRORS
            FROM {_repo_table("SNP_SESSION")} S
            WHERE S.SESS_BEG IS NOT NULL
            ORDER BY S.SESS_BEG DESC
            FETCH FIRST :lmt ROWS ONLY
        """, {"lmt": limit})

        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return json.dumps(rows, default=_json_serial, ensure_ascii=False)

    except Exception as e:
        return safe_error_json("ETL metrics read", e)
    finally:
        cursor.close()
        conn.close()


# ── Tool 2: Data Freshness ──────────────────────────────────────────────────

def mon_data_freshness() -> str:
    """
    Retrieve latest load dates and day differences from AUDIT_USER.ETL_WATERMARK.
    """
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT
                TABLE_OWNER,
                TABLE_NAME,
                LAST_EXTRACT_DATE,
                ROUND(SYSDATE - CAST(LAST_EXTRACT_DATE AS DATE), 1) AS DAYS_SINCE
            FROM AUDIT_USER.ETL_WATERMARK
            ORDER BY LAST_EXTRACT_DATE ASC
        """)

        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return json.dumps(rows, default=_json_serial, ensure_ascii=False)

    except Exception as e:
        return safe_error_json("Data freshness read", e)
    finally:
        cursor.close()
        conn.close()


# ── Tool 3: Tablespace Usage ────────────────────────────────────────────────

def mon_tablespace_usage() -> str:
    """Oracle tablespace usage percentages."""
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT
                df.TABLESPACE_NAME,
                ROUND(df.TOTAL_MB, 1)    AS TOTAL_MB,
                ROUND(df.TOTAL_MB - NVL(fs.FREE_MB, 0), 1) AS USED_MB,
                ROUND(NVL(fs.FREE_MB, 0), 1) AS FREE_MB,
                ROUND((1 - NVL(fs.FREE_MB, 0)/df.TOTAL_MB) * 100, 1) AS USED_PCT
            FROM
                (SELECT TABLESPACE_NAME, SUM(BYTES)/1024/1024 AS TOTAL_MB
                 FROM DBA_DATA_FILES GROUP BY TABLESPACE_NAME) df
            LEFT JOIN
                (SELECT TABLESPACE_NAME, SUM(BYTES)/1024/1024 AS FREE_MB
                 FROM DBA_FREE_SPACE GROUP BY TABLESPACE_NAME) fs
              ON df.TABLESPACE_NAME = fs.TABLESPACE_NAME
            ORDER BY USED_PCT DESC
        """)

        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return json.dumps(rows, default=_json_serial, ensure_ascii=False)

    except Exception as e:
        # Try USER_ if DBA_ view permissions are unavailable
        try:
            cursor2 = conn.cursor()
            cursor2.execute("""
                SELECT TABLESPACE_NAME, ROUND(SUM(BYTES)/1024/1024, 1) AS TOTAL_MB
                FROM USER_SEGMENTS
                GROUP BY TABLESPACE_NAME
                ORDER BY TOTAL_MB DESC
            """)
            cols = [d[0] for d in cursor2.description]
            rows = [dict(zip(cols, row)) for row in cursor2.fetchall()]
            cursor2.close()
            return json.dumps(rows, default=_json_serial, ensure_ascii=False)
        except Exception as e2:
            return safe_error_json("Tablespace read", e2)
    finally:
        cursor.close()
        conn.close()


# ── Tool 4: DQ Score ────────────────────────────────────────────────────────

def mon_dq_score(owner: str = "DWH", table_name: str = "") -> str:
    """
    Calculate a table-level DQ score.
    If table_name is empty, return an aggregate score for all tables.
    Skor = (1 - ihlal_sayisi / toplam_satir) * 100
    """
    from data_quality import DataQualityManager

    dqm = DataQualityManager()
    try:
        if table_name:
            tables = [table_name.upper()]
        else:
            conn = dqm._get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT TABLE_NAME FROM ALL_TABLES
                WHERE OWNER = :owner
                  AND TABLE_NAME NOT LIKE 'ERR\\_%' ESCAPE '\\'
                  AND TABLE_NAME NOT LIKE 'BIN$%'
                  AND TABLE_NAME NOT LIKE 'SYS_%'
                ORDER BY TABLE_NAME
            """, {"owner": owner.upper()})
            tables = [row[0] for row in cursor.fetchall()]
            cursor.close()

        scores = []
        for tname in tables:
            try:
                result = dqm.validate_table(owner, tname)
                summary = result.summary()
                total = summary["total_rows"]
                violations = sum(v["violation_count"] for v in summary["violations"])
                score = round((1 - violations / total) * 100, 1) if total > 0 else 100.0
                scores.append({
                    "table": f"{owner}.{tname}",
                    "total_rows": total,
                    "violations": violations,
                    "errors": summary["errors"],
                    "warnings": summary["warnings"],
                    "score": score,
                    "status": "GREEN" if score >= 95 else ("YELLOW" if score >= 80 else "RED"),
                })
            except Exception as e:
                scores.append({
                    "table": f"{owner}.{tname}",
                    "score": None,
                    **safe_error_payload("DQ score read", e),
                    "status": "UNKNOWN",
                })

        return json.dumps(scores, default=_json_serial, ensure_ascii=False)

    except Exception as e:
        return safe_error_json("DQ score read", e)
    finally:
        dqm.close()


# ── Tool 5: Schema Changes ──────────────────────────────────────────────────

def mon_schema_changes(limit: int = 20) -> str:
    """Returns recent schema changes from AUDIT_USER.SCHEMA_HASH."""
    try:
        from schema_change_detector import get_recent_changes
        changes = get_recent_changes(limit=limit)
        for c in changes:
            for k, v in c.items():
                if hasattr(v, "isoformat"):
                    c[k] = v.isoformat()
        return json.dumps(changes, default=_json_serial, ensure_ascii=False)
    except Exception as e:
        return safe_error_json("Schema changes read", e)


# ── Tool 6: Alert Summary ───────────────────────────────────────────────────

def mon_alert_summary() -> str:
    """Returns unapproved alerts from AUDIT_USER.ALERT_LOG."""
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT ALERT_ID, ALERT_TYPE, SOURCE, MESSAGE, SEVERITY,
                   TABLE_OWNER, TABLE_NAME, CREATED_AT
            FROM AUDIT_USER.ALERT_LOG
            WHERE ACKNOWLEDGED = 0
            ORDER BY
                CASE SEVERITY
                    WHEN 'CRITICAL' THEN 1
                    WHEN 'WARNING'  THEN 2
                    ELSE 3
                END,
                CREATED_AT DESC
            FETCH FIRST 50 ROWS ONLY
        """)

        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return json.dumps(rows, default=_json_serial, ensure_ascii=False)

    except oracledb.DatabaseError as e:
        if "ORA-00942" in str(e):
            # ALERT_LOG table has not been created yet
            return json.dumps([])
        return safe_error_json("Alert summary read", e)
    except Exception as e:
        return safe_error_json("Alert summary read", e)
    finally:
        cursor.close()
        conn.close()
