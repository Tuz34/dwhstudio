"""
Rollback Manager — Undo DDL Operations
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Uses batch records in the MAPPING_LOG table to undo
DDL operations in reverse order.

Supported operations:
  CREATE_TABLE    → DROP TABLE
  CREATE_SEQUENCE → DROP SEQUENCE
"""

import logging
import re

import oracledb
from config import ORACLE_DDL_USER, ORACLE_DDL_PASSWORD, ORACLE_DSN

logger = logging.getLogger("rollback_manager")
_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _qualified_object_name(owner: str, name: str) -> str:
    owner_text = str(owner or "").strip().upper()
    name_text = str(name or "").strip().upper()
    if not _IDENTIFIER_RE.fullmatch(owner_text) or not _IDENTIFIER_RE.fullmatch(name_text):
        raise ValueError("Invalid rollback object identifier.")
    return f'"{owner_text}"."{name_text}"'


class RollbackManager:
    """Manage the reversal of DDL operations."""

    # Reverse operation map.
    REVERSE_OPS = {
        "CREATE_TABLE":    "DROP TABLE",
        "CREATE_SEQUENCE": "DROP SEQUENCE",
    }

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_DDL_USER,
                password=ORACLE_DDL_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "RollbackManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def get_batch_log(self, batch_id: str) -> list[dict]:
        """Return all MAPPING_LOG records in the batch.

        Args:
            batch_id: Batch identifier for SKEY or another DDL operation.

        Returns:
            Each record contains LOG_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME,
            STATUS, ERROR_MESSAGE, and CREATED_AT fields.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT LOG_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME,
                       STATUS, ERROR_MESSAGE, CREATED_AT
                FROM AUDIT_USER.MAPPING_LOG
                WHERE BATCH_ID = :bid
                ORDER BY CREATED_AT ASC
            """, {"bid": batch_id})

            cols = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                for k, v in d.items():
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                rows.append(d)
            return rows
        finally:
            cursor.close()

    def rollback_batch(self, batch_id: str) -> dict:
        """Undo successful CREATE operations in the batch in reverse chronological order.

        Args:
            batch_id: Identifier of the DDL batch to undo.

        Returns:
            Dictionary containing batch_id, rolled_back (undone objects),
            skipped (skipped objects), and errors lists.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        result = {"batch_id": batch_id, "rolled_back": [], "skipped": [], "errors": []}

        try:
            # Fetch successful operations in reverse chronological order.
            cursor.execute("""
                SELECT LOG_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME
                FROM AUDIT_USER.MAPPING_LOG
                WHERE BATCH_ID = :bid AND STATUS = 'SUCCESS'
                ORDER BY CREATED_AT DESC
            """, {"bid": batch_id})

            operations = cursor.fetchall()

            if not operations:
                result["skipped"].append("No operations to undo were found in the batch")
                return result

            for log_id, operation, obj_owner, obj_name in operations:
                reverse_op = self.REVERSE_OPS.get(operation)
                if not reverse_op:
                    result["skipped"].append(f"{operation} {obj_owner}.{obj_name} (reverse operation undefined)")
                    continue

                try:
                    qualified_name = _qualified_object_name(obj_owner, obj_name)
                except ValueError as exc:
                    result["errors"].append({
                        "object": "invalid_identifier",
                        "error": "Rollback object validation failed.",
                        "error_type": type(exc).__name__,
                    })
                    continue

                ddl = f"{reverse_op} {qualified_name}"
                try:
                    cursor.execute(ddl)
                    conn.commit()

                    # Mark the original MAPPING_LOG record as ROLLED_BACK.
                    cursor.execute("""
                        UPDATE AUDIT_USER.MAPPING_LOG
                        SET STATUS = 'ROLLED_BACK'
                        WHERE LOG_ID = :lid
                    """, {"lid": log_id})
                    conn.commit()

                    result["rolled_back"].append(f"{reverse_op} {obj_owner}.{obj_name}")
                    logger.info("Rollback succeeded: %s", ddl)

                except oracledb.DatabaseError as e:
                    err_str = str(e)
                    if "ORA-00942" in err_str or "ORA-02289" in err_str:
                        # Table/sequence already absent.
                        result["skipped"].append(f"{obj_owner}.{obj_name} (already absent)")
                    else:
                        result["errors"].append({
                            "object": f"{obj_owner}.{obj_name}",
                            "error": "Rollback operation failed.",
                            "error_type": type(e).__name__,
                        })
                        logger.error("Rollback operation failed: %s", type(e).__name__)

        finally:
            cursor.close()

        logger.info(
            "Rollback result batch=%s: %d undone, %d skipped, %d errors",
            batch_id, len(result["rolled_back"]), len(result["skipped"]), len(result["errors"])
        )
        return result
