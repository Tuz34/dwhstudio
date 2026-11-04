"""
Alert Manager — System Alert Management
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Alert CRUD operations through the AUDIT_USER.ALERT_LOG table.
Optional RAG integration: stores the alert in the business_rules collection.

Channels: UI banner + log + RAG (no email/webhook).
"""

import json
import logging
from datetime import date, datetime

import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN

logger = logging.getLogger("alert_manager")


class AlertCheckError(RuntimeError):
    """Raised when one or more alert checks could not complete reliably."""


class AlertManager:
    """System alert management class."""

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "AlertManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ── Alert Creation ──────────────────────────────────────────────────────
    def create_alert(self, alert_type: str, source: str, message: str,
                     table_owner: str = None, table_name: str = None,
                     details: dict = None, severity: str = "WARNING",
                     store_in_rag: bool = False) -> int:
        """Creates a new alert record in ALERT_LOG.

        Args:
            alert_type: Alert category (e.g. "FRESHNESS", "SCHEMA_CHANGE").
            source: Source that triggers the alert (e.g. "DWH.SALES").
            message: Short description text (max 2000 characters).
            table_owner: Related table schema (optional).
            table_name: Related table name (optional).
            details: Additional details dictionary (stored as JSON).
            severity: "INFO", "WARNING", or "CRITICAL".
            store_in_rag: If True, also adds it to the RAG business_rules collection.

        Returns:
            Created ALERT_LOG.ALERT_ID value.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        alert_id_var = cursor.var(int)

        try:
            cursor.execute("""
                INSERT INTO AUDIT_USER.ALERT_LOG
                    (ALERT_TYPE, SOURCE, MESSAGE, TABLE_OWNER, TABLE_NAME,
                     DETAILS, SEVERITY)
                VALUES
                    (:atype, :src, :msg, :towner, :tname,
                     :det, :sev)
                RETURNING ALERT_ID INTO :aid
            """, {
                "atype": alert_type,
                "src": source,
                "msg": message[:2000],
                "towner": table_owner,
                "tname": table_name,
                "det": json.dumps(details) if details else None,
                "sev": severity,
                "aid": alert_id_var,
            })
            conn.commit()
            alert_id = alert_id_var.getvalue()[0]

            logger.warning(
                "ALERT [%s/%s] %s: %s (id=%d)",
                severity, alert_type, source, message[:100], alert_id
            )

            # RAG integration
            if store_in_rag:
                try:
                    from rag_manager import add_document
                    rag_text = (
                        f"[ALERT] {alert_type} - {source}\n"
                        f"Severity: {severity}\n"
                        f"Message: {message}\n"
                        f"Table: {table_owner}.{table_name}" if table_name else ""
                    )
                    add_document(
                        "business_rules", rag_text,
                        {"type": "alert", "alert_type": alert_type, "source": source}
                    )
                except Exception as exc:
                    logger.error("Alert RAG write failed: %s", type(exc).__name__)

            return alert_id

        finally:
            cursor.close()

    # ── Get Pending Alerts ─────────────────────────────────────────────
    def get_unacknowledged(self, limit: int = 50) -> list[dict]:
        """Returns unacknowledged alerts."""
        conn = self._get_conn()
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
                FETCH FIRST :lmt ROWS ONLY
            """, {"lmt": limit})

            cols = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                for k, v in d.items():
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                rows.append(d)
            return rows

        except oracledb.DatabaseError as e:
            if "ORA-00942" in str(e):
                return []
            raise
        finally:
            cursor.close()

    # ── Acknowledge ───────────────────────────────────────────────────────────────
    def acknowledge(self, alert_id: int) -> bool:
        """Acknowledges a single alert."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE AUDIT_USER.ALERT_LOG
                SET ACKNOWLEDGED = 1, ACKNOWLEDGED_AT = SYSTIMESTAMP
                WHERE ALERT_ID = :aid AND ACKNOWLEDGED = 0
            """, {"aid": alert_id})
            conn.commit()
            return cursor.rowcount > 0
        finally:
            cursor.close()

    def acknowledge_all(self) -> int:
        """Acknowledges all pending alerts in bulk. Returns: acknowledged count."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE AUDIT_USER.ALERT_LOG
                SET ACKNOWLEDGED = 1, ACKNOWLEDGED_AT = SYSTIMESTAMP
                WHERE ACKNOWLEDGED = 0
            """)
            conn.commit()
            count = cursor.rowcount
            logger.info("Bulk acknowledgment: %d alerts acknowledged", count)
            return count
        finally:
            cursor.close()

    # ── History ───────────────────────────────────────────────────────────────
    def get_history(self, limit: int = 50) -> list[dict]:
        """Returns all alerts (including acknowledged ones)."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT ALERT_ID, ALERT_TYPE, SOURCE, MESSAGE, SEVERITY,
                       TABLE_OWNER, TABLE_NAME, ACKNOWLEDGED,
                       CREATED_AT, ACKNOWLEDGED_AT
                FROM AUDIT_USER.ALERT_LOG
                ORDER BY CREATED_AT DESC
                FETCH FIRST :lmt ROWS ONLY
            """, {"lmt": limit})

            cols = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                for k, v in d.items():
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                rows.append(d)
            return rows

        except oracledb.DatabaseError as e:
            if "ORA-00942" in str(e):
                return []
            raise
        finally:
            cursor.close()

    # ── Automatic Checks & Alert Creation ────────────────────────────────────
    def has_unacknowledged(self, alert_type: str, source: str) -> bool:
        """Return True when the same open alert is already visible to operators."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT COUNT(*)
                FROM AUDIT_USER.ALERT_LOG
                WHERE ACKNOWLEDGED = 0
                  AND ALERT_TYPE = :atype
                  AND SOURCE = :src
            """, {"atype": alert_type, "src": source})
            row = cursor.fetchone()
            return bool(row and int(row[0] or 0) > 0)
        except oracledb.DatabaseError as e:
            if "ORA-00942" in str(e):
                return False
            raise
        finally:
            cursor.close()

    def check_and_alert(self, freshness_days: int = 2) -> list[dict]:
        """Runs automatic checks and creates alerts as needed.

        Checks:
            1. Data freshness: FRESHNESS alert if LAST_EXTRACT_DATE is older than freshness_days.
            2. Schema change: SCHEMA_CHANGE alert if a change occurred in the past 24 hours.

        Args:
            freshness_days: Data freshness threshold (days). Default: 2.

        Returns:
            alert_id, type, and source fields for each created alert
            as a list of dictionaries.
        """
        created_alerts = []
        failed_checks = []

        # 1. Data freshness check
        cursor = None
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT TABLE_OWNER, TABLE_NAME,
                       ROUND(SYSDATE - CAST(LAST_EXTRACT_DATE AS DATE), 1) AS DAYS_SINCE
                FROM AUDIT_USER.ETL_WATERMARK
                WHERE SYSDATE - CAST(LAST_EXTRACT_DATE AS DATE) > :days
            """, {"days": freshness_days})

            for towner, tname, days_since in cursor.fetchall():
                source = f"{towner}.{tname}"
                if self.has_unacknowledged("FRESHNESS", source):
                    continue
                alert_id = self.create_alert(
                    alert_type="FRESHNESS",
                    source=source,
                    message=(
                        f"Data has not been refreshed for {days_since} days "
                        f"(threshold: {freshness_days} days)."
                    ),
                    table_owner=towner,
                    table_name=tname,
                    severity="WARNING" if days_since < freshness_days * 2 else "CRITICAL",
                    store_in_rag=True,
                )
                created_alerts.append({
                    "alert_id": alert_id,
                    "type": "FRESHNESS",
                    "source": source,
                    "days_since": days_since,
                })
        except Exception as exc:
            failed_checks.append("freshness")
            logger.error("Freshness alert check failed: %s", type(exc).__name__)
        finally:
            if cursor is not None:
                cursor.close()

        # 2. Schema change check
        try:
            from schema_change_detector import get_recent_changes
            changes = get_recent_changes(limit=10)
            for ch in changes:
                detected = (
                    ch.get("detected_at")
                    or ch.get("DETECTED_AT")
                    or ch.get("check_date")
                    or ch.get("CHECK_DATE")
                )
                if detected:
                    if isinstance(detected, datetime):
                        detected_date = detected.date()
                    elif isinstance(detected, date):
                        detected_date = detected
                    else:
                        continue
                    if detected_date >= date.today():
                        owner = ch.get("owner") or ch.get("TABLE_OWNER") or ""
                        table_name = ch.get("table_name") or ch.get("TABLE_NAME") or ""
                        source = f"{owner}.{table_name}"
                        if self.has_unacknowledged("SCHEMA_CHANGE", source):
                            continue
                        alert_id = self.create_alert(
                            alert_type="SCHEMA_CHANGE",
                            source=source,
                            message="Schema change detected.",
                            table_owner=owner,
                            table_name=table_name,
                            severity="WARNING",
                            store_in_rag=True,
                        )
                        created_alerts.append({
                            "alert_id": alert_id,
                            "type": "SCHEMA_CHANGE",
                            "source": source,
                        })
        except Exception as exc:
            failed_checks.append("schema_change")
            logger.error("Schema change alert check failed: %s", type(exc).__name__)

        if failed_checks:
            raise AlertCheckError(
                "One or more alert checks failed: " + ", ".join(failed_checks) + "."
            )

        return created_alerts
