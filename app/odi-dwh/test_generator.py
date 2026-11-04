"""
Test Generator — DWH Table Test Templates
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Generate automatic SQL test templates for dimension and fact tables.
5 test templates:
  1. SKEY Uniqueness — surrogate key uniqueness
  2. NOT NULL — required fields are populated
  3. SCD2 Date Order — START_DATE < END_DATE
  4. CURRENT_FLAG Values — only 'Y' or 'N'
  5. CURRENT_FLAG Unique per NK — one 'Y' record per NK

Optionally store generated tests in the RAG business_rules collection.
"""

import logging

import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN

logger = logging.getLogger("test_generator")


class TestGenerator:
    """Generate DWH table test templates."""

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

    def _get_pk_columns(self, owner: str, table_name: str) -> list[str]:
        """Fetch PK columns."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT cc.COLUMN_NAME
                FROM ALL_CONSTRAINTS c
                JOIN ALL_CONS_COLUMNS cc
                  ON c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME AND c.OWNER = cc.OWNER
                WHERE c.TABLE_NAME = :tname AND c.OWNER = :owner
                  AND c.CONSTRAINT_TYPE = 'P'
                ORDER BY cc.POSITION
            """, {"tname": table_name.upper(), "owner": owner.upper()})
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _get_not_null_columns(self, owner: str, table_name: str) -> list[str]:
        """Returns columns with a NOT NULL constraint (nullable='N')."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS
                WHERE OWNER = :owner AND TABLE_NAME = :tname AND NULLABLE = 'N'
                ORDER BY COLUMN_ID
            """, {"owner": owner.upper(), "tname": table_name.upper()})
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _has_column(self, owner: str, table_name: str, col_name: str) -> bool:
        """Check whether the specified column exists in the table."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM ALL_TAB_COLUMNS
                WHERE OWNER = :owner AND TABLE_NAME = :tname AND COLUMN_NAME = :cname
            """, {"owner": owner.upper(), "tname": table_name.upper(), "cname": col_name.upper()})
            return cursor.fetchone()[0] > 0
        finally:
            cursor.close()

    def generate_tests(self, owner: str, table_name: str) -> list[dict]:
        """
        Generate applicable test templates for the table.
        Returns: [{"test_name": "...", "sql": "...", "description": "..."}]
        """
        owner = owner.upper()
        table_name = table_name.upper()
        full_name = f"{owner}.{table_name}"
        tests = []

        # 1. SKEY Uniqueness
        pk_cols = self._get_pk_columns(owner, table_name)
        if pk_cols:
            pk_col = pk_cols[0]
            tests.append({
                "test_name": f"SKEY_UNIQUE_{table_name}",
                "description": f"{pk_col} column must be unique",
                "sql": f"SELECT {pk_col}, COUNT(*) AS CNT FROM {full_name} GROUP BY {pk_col} HAVING COUNT(*) > 1",
                "expected": "Result must be empty (0 rows)",
            })

        # 2. NOT NULL
        nn_cols = self._get_not_null_columns(owner, table_name)
        for col in nn_cols[:5]:  # At most 5 columns.
            tests.append({
                "test_name": f"NOT_NULL_{table_name}_{col}",
                "description": f"{col} column must not be NULL",
                "sql": f"SELECT COUNT(*) AS NULL_COUNT FROM {full_name} WHERE {col} IS NULL",
                "expected": "NULL_COUNT = 0",
            })

        # 3. SCD2 Date Order
        has_start = self._has_column(owner, table_name, "START_DATE")
        has_end = self._has_column(owner, table_name, "END_DATE")
        if has_start and has_end:
            tests.append({
                "test_name": f"SCD2_DATE_ORDER_{table_name}",
                "description": "START_DATE must always be earlier than END_DATE",
                "sql": f"SELECT COUNT(*) AS BAD_DATES FROM {full_name} WHERE START_DATE > END_DATE",
                "expected": "BAD_DATES = 0",
            })

        # 4. CURRENT_FLAG Values
        has_flag = self._has_column(owner, table_name, "CURRENT_FLAG")
        if has_flag:
            tests.append({
                "test_name": f"CURRENT_FLAG_VALUES_{table_name}",
                "description": "CURRENT_FLAG must only be 'Y' or 'N'",
                "sql": f"SELECT DISTINCT CURRENT_FLAG FROM {full_name} WHERE CURRENT_FLAG NOT IN ('Y', 'N')",
                "expected": "Result must be empty (0 rows)",
            })

            # 5. CURRENT_FLAG Unique per NK
            # Use a UNIQUE constraint other than the PK for NK, or all NOT NULL columns otherwise.
            if len(pk_cols) > 0 and len(nn_cols) > 1:
                # Treat the first NOT NULL column other than the PK as NK.
                nk_candidates = [c for c in nn_cols if c not in pk_cols and c != "CURRENT_FLAG"]
                if nk_candidates:
                    nk_col = nk_candidates[0]
                    tests.append({
                        "test_name": f"CURRENT_FLAG_UNIQUE_NK_{table_name}",
                        "description": f"Each {nk_col} must have only one CURRENT_FLAG='Y'",
                        "sql": (
                            f"SELECT {nk_col}, COUNT(*) AS Y_COUNT "
                            f"FROM {full_name} WHERE CURRENT_FLAG = 'Y' "
                            f"GROUP BY {nk_col} HAVING COUNT(*) > 1"
                        ),
                        "expected": "Result must be empty (0 rows)",
                    })

        logger.info("%s: %d test templates generated", full_name, len(tests))
        return tests

    def generate_and_store_in_rag(self, owner: str, table_name: str) -> list[dict]:
        """
        Generate tests and store them in the RAG business_rules collection.
        """
        tests = self.generate_tests(owner, table_name)

        if tests:
            try:
                from rag_manager import add_document
                for t in tests:
                    rag_text = (
                        f"[DQ TEST] {t['test_name']}\n"
                        f"Description: {t['description']}\n"
                        f"SQL: {t['sql']}\n"
                        f"Expected: {t['expected']}"
                    )
                    add_document(
                        "business_rules", rag_text,
                        {"type": "dq_test", "table": f"{owner}.{table_name}",
                         "test_name": t["test_name"]}
                    )
                logger.info("%d tests stored in RAG: %s.%s", len(tests), owner, table_name)
            except Exception as e:
                logger.error("RAG record failed: %s", e)

        return tests
