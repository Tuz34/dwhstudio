"""
SKEY Manager — Surrogate Key Management
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DWH dimension tables use the following Surrogate Key (SKEY) objects:
- SKEY_MAP_<TABLE> table (Natural Key → Surrogate Key mapping)
- SEQ_<TABLE> sequence
- SKEY expression for ODI mapping

All DDL operations are logged in MAPPING_LOG (with rollback support).
"""

import json
import logging
import uuid
from datetime import datetime

import oracledb
from config import ORACLE_DDL_USER, ORACLE_DDL_PASSWORD, ORACLE_DSN

logger = logging.getLogger("skey_manager")


class SkeyManager:
    """Manage Surrogate Key objects."""

    SKEY_TABLE_TEMPLATE = """
CREATE TABLE {owner}.SKEY_MAP_{table} (
    SKEY         NUMBER NOT NULL,
    NATURAL_KEY  VARCHAR2(500) NOT NULL,
    INSERT_DATE  DATE DEFAULT SYSDATE,
    UPDATE_DATE  DATE,
    DELETE_DATE  DATE,
    ETL_BATCH_ID NUMBER,
    CONSTRAINT PK_SKEY_MAP_{table} PRIMARY KEY (SKEY),
    CONSTRAINT UK_SKEY_MAP_{table}_NK UNIQUE (NATURAL_KEY)
)
""".strip()

    SEQ_TEMPLATE = """
CREATE SEQUENCE {owner}.SEQ_{table}
    START WITH 1
    INCREMENT BY 1
    NOCACHE
    NOCYCLE
""".strip()

    EXPRESSION_TEMPLATE = """CASE
    WHEN EXISTS (
        SELECT 1 FROM {owner}.SKEY_MAP_{table} m
        WHERE m.NATURAL_KEY = {nk_expr}
    )
    THEN (
        SELECT m.SKEY FROM {owner}.SKEY_MAP_{table} m
        WHERE m.NATURAL_KEY = {nk_expr}
    )
    ELSE {owner}.SEQ_{table}.NEXTVAL
END""".strip()

    # Privileges to grant to APP_USER.
    APP_USER = "APP_USER"

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        """Connect as the DDL user (CREATE/ALTER privileges required)."""
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_DDL_USER,
                password=ORACLE_DDL_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def _auto_grant(self, conn, owner: str, object_name: str, object_type: str = "TABLE"):
        """Automatically GRANT APP_USER access to the newly created object."""
        try:
            cur = conn.cursor()
            if object_type == "TABLE":
                cur.execute(f"GRANT SELECT, INSERT, UPDATE ON {owner}.{object_name} TO {self.APP_USER}")
            elif object_type == "SEQUENCE":
                cur.execute(f"GRANT SELECT ON {owner}.{object_name} TO {self.APP_USER}")
            conn.commit()
            cur.close()
            logger.info("AUTO-GRANT: %s.%s -> %s", owner, object_name, self.APP_USER)
        except Exception as e:
            logger.warning("AUTO-GRANT failed (continuing): %s.%s — %s", owner, object_name, e)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "SkeyManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def _log_operation(self, conn, batch_id: str, operation: str,
                       object_owner: str, object_name: str,
                       ddl_text: str, status: str = "SUCCESS",
                       error_message: str = None):
        """Log the DDL operation in MAPPING_LOG."""
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO AUDIT_USER.MAPPING_LOG
                    (BATCH_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME,
                     DDL_TEXT, STATUS, ERROR_MESSAGE)
                VALUES
                    (:bid, :op, :oowner, :oname, :ddl, :st, :err)
            """, {
                "bid": batch_id,
                "op": operation,
                "oowner": object_owner,
                "oname": object_name,
                "ddl": ddl_text,
                "st": status,
                "err": error_message,
            })
            conn.commit()
        except Exception as e:
            logger.error("MAPPING_LOG write error: %s", e)
        finally:
            cursor.close()

    def create_skey_objects(self, owner: str, table: str) -> dict:
        """Create the SKEY_MAP table and SEQ sequence with DDL.

        Args:
            owner: Target schema name (e.g. "DWH").
            table: Dimension table name (e.g. "PRODUCT").

        Returns:
            Dictionary containing batch_id, created (created objects), skipped
            (objects already present), and errors lists.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        batch_id = str(uuid.uuid4())[:8]
        result = {"batch_id": batch_id, "created": [], "skipped": [], "errors": []}
        owner = owner.upper()
        table = table.upper()

        # 1. SKEY_MAP table.
        skey_ddl = self.SKEY_TABLE_TEMPLATE.format(owner=owner, table=table)
        try:
            cursor.execute(skey_ddl)
            conn.commit()
            result["created"].append(f"SKEY_MAP_{table}")
            self._log_operation(conn, batch_id, "CREATE_TABLE", owner,
                                f"SKEY_MAP_{table}", skey_ddl)
            logger.info("SKEY table created: %s.SKEY_MAP_%s", owner, table)
            # Grant access automatically.
            self._auto_grant(conn, owner, f"SKEY_MAP_{table}", "TABLE")
        except oracledb.DatabaseError as e:
            if "ORA-00955" in str(e):
                result["skipped"].append(f"SKEY_MAP_{table}")
                # Check the grant even if the table already exists.
                self._auto_grant(conn, owner, f"SKEY_MAP_{table}", "TABLE")
            else:
                result["errors"].append({"object": f"SKEY_MAP_{table}",
                                         "error": "SKEY object creation failed.",
                                         "error_type": type(e).__name__})
                self._log_operation(conn, batch_id, "CREATE_TABLE", owner,
                                    f"SKEY_MAP_{table}", skey_ddl, "FAILED", str(e))

        # 2. Sequence
        seq_ddl = self.SEQ_TEMPLATE.format(owner=owner, table=table)
        try:
            cursor.execute(seq_ddl)
            conn.commit()
            result["created"].append(f"SEQ_{table}")
            self._log_operation(conn, batch_id, "CREATE_SEQUENCE", owner,
                                f"SEQ_{table}", seq_ddl)
            logger.info("SKEY sequence created: %s.SEQ_%s", owner, table)
            # Grant access automatically.
            self._auto_grant(conn, owner, f"SEQ_{table}", "SEQUENCE")
        except oracledb.DatabaseError as e:
            if "ORA-00955" in str(e):
                result["skipped"].append(f"SEQ_{table}")
                self._auto_grant(conn, owner, f"SEQ_{table}", "SEQUENCE")
            else:
                result["errors"].append({"object": f"SEQ_{table}",
                                         "error": "SKEY object creation failed.",
                                         "error_type": type(e).__name__})
                self._log_operation(conn, batch_id, "CREATE_SEQUENCE", owner,
                                    f"SEQ_{table}", seq_ddl, "FAILED", str(e))

        cursor.close()
        return result

    def get_skey_expression(self, owner: str, table: str, nk_expr: str) -> str:
        """Return the SKEY CASE WHEN expression for ODI mapping.

        Args:
            owner: Schema containing the SKEY_MAP table.
            table: Dimension table name.
            nk_expr: Natural key SQL expression
                (e.g. "SRC.PRODUCT_ID || '-' || SRC.CATEGORY_ID").

        Returns:
            CASE WHEN EXISTS ... THEN ... ELSE SEQ.NEXTVAL END expression.
        """
        expr = self.EXPRESSION_TEMPLATE.format(
            owner=owner.upper(),
            table=table.upper(),
            nk_expr=nk_expr,
        )
        # Remove newlines to pass the regex filter and command-line parameters.
        return expr.replace("\n", " ").replace("\r", " ").replace("  ", " ").strip()
