from __future__ import annotations

from pathlib import Path

import oracledb
import pytest

from scd2_manager import PRODUCTS_SOURCE_METADATA


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ODS_AUDIT_METADATA = (
    ("ODS_CREATED_AT", "TIMESTAMP", False),
    ("ODS_UPDATED_AT", "TIMESTAMP", True),
)


def _plan():
    from dim_product_scd2_initial_create import build_dim_product_initial_create_plan

    metadata = [
        {"name": name, "data_type": data_type, "nullable": nullable}
        for name, data_type, nullable in (*PRODUCTS_SOURCE_METADATA, *ODS_AUDIT_METADATA)
    ]
    return build_dim_product_initial_create_plan(
        {
            "status": "blocked_target_missing",
            "source": {
                "exists": True,
                "column_count": 24,
                "metadata": metadata,
                "metadata_status": "metadata_ready_for_review",
                "row_count": 72,
                "duplicate_key_group_count": 0,
            },
            "target": {"exists": False},
            "sequence": {
                "owner": "DWH",
                "sequence": "SEQ_DIM_PRODUCT",
                "exists": False,
            },
        }
    )


class _Cursor:
    def __init__(self, *, tables=None, sequences=None, fail_token=None):
        self.tables = set(tables or ())
        self.sequences = set(sequences or ())
        self.fail_token = fail_token
        self.failed = False
        self.result = []
        self.statements = []
        self.closed = False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        upper = compact.upper()
        self.statements.append((compact, params))
        if self.fail_token and self.fail_token in upper and not self.failed:
            self.failed = True
            raise oracledb.DatabaseError("driver credential detail")
        if "FROM ALL_TABLES" in upper:
            self.result = [(1 if params["table_name"] in self.tables else 0,)]
        elif "FROM ALL_SEQUENCES" in upper and "INCREMENT_BY" in upper:
            if params["sequence_name"] in self.sequences:
                self.result = [(1, "N")]
            else:
                self.result = []
        elif upper.startswith("CREATE SEQUENCE"):
            self.sequences.add("ODI68_SEQ_DIM_PRODUCT_PROOF")
            self.result = []
        elif upper.startswith("CREATE TABLE"):
            self.tables.add("ODI68_DIM_PRODUCT_PROOF")
            self.result = []
        elif "FROM ALL_TAB_COLUMNS" in upper:
            from dim_product_scd2_disposable_executor import expected_target_metadata_rows

            self.result = expected_target_metadata_rows()
        elif "FROM ALL_CONSTRAINTS" in upper:
            self.result = [(1,)]
        elif upper.startswith('SELECT COUNT(*) FROM "DWH"."ODI68_DIM_PRODUCT_PROOF"'):
            self.result = [(0,)]
        elif upper.startswith('DROP TABLE "DWH"."ODI68_DIM_PRODUCT_PROOF"'):
            self.tables.discard("ODI68_DIM_PRODUCT_PROOF")
            self.result = []
        elif upper.startswith('DROP SEQUENCE "DWH"."ODI68_SEQ_DIM_PRODUCT_PROOF"'):
            self.sequences.discard("ODI68_SEQ_DIM_PRODUCT_PROOF")
            self.result = []
        else:
            raise AssertionError(f"Unexpected statement: {sql}")

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class _Factory:
    def __init__(self, connection):
        self.connection = connection
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.connection


@pytest.mark.parametrize(
    ("plan_status", "review", "approved", "scope", "expected"),
    [
        ("blocked", "approved", True, "odi68_dim_product_scd2_disposable_create_cleanup", "blocked_initial_create_contract"),
        ("ready_for_initial_create_review", "pending", True, "odi68_dim_product_scd2_disposable_create_cleanup", "blocked_pending_executor_review"),
        ("ready_for_initial_create_review", "approved", "yes", "odi68_dim_product_scd2_disposable_create_cleanup", "blocked_until_exact_operator_approval"),
        ("ready_for_initial_create_review", "approved", True, "wrong_scope", "blocked_invalid_approval_scope"),
    ],
)
def test_executor_gates_block_before_connection(plan_status, review, approved, scope, expected):
    from dim_product_scd2_disposable_executor import (
        execute_dim_product_disposable_create_cleanup_proof,
    )

    plan = _plan()
    plan["status"] = plan_status
    factory = _Factory(_Connection(_Cursor()))

    result = execute_dim_product_disposable_create_cleanup_proof(
        plan,
        executor_review_status=review,
        approved=approved,
        approval_scope=scope,
        connection_factory=factory,
    )

    assert result["status"] == expected
    assert result["connection_attempted"] is False
    assert factory.calls == 0


def test_ddl_renderers_are_fixed_to_disposable_objects():
    from dim_product_scd2_disposable_executor import (
        build_disposable_sequence_statement,
        build_disposable_table_statement,
    )

    table_sql = build_disposable_table_statement()
    sequence_sql = build_disposable_sequence_statement()

    assert '"DWH"."ODI68_DIM_PRODUCT_PROOF"' in table_sql
    assert 'PRIMARY KEY ("PRODUCT_SK")' in table_sql
    assert '"DWH"."ODI68_SEQ_DIM_PRODUCT_PROOF"' in sequence_sql
    assert "NOCYCLE" in sequence_sql
    assert '"DIM_PRODUCT"' not in table_sql
    assert '"SEQ_DIM_PRODUCT"' not in sequence_sql


@pytest.mark.parametrize(
    ("tables", "sequences", "expected"),
    [
        ({"ODI68_DIM_PRODUCT_PROOF"}, set(), "blocked_disposable_table_exists"),
        (set(), {"ODI68_SEQ_DIM_PRODUCT_PROOF"}, "blocked_disposable_sequence_exists"),
    ],
)
def test_executor_never_removes_preexisting_proof_objects(tables, sequences, expected):
    from dim_product_scd2_disposable_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        execute_dim_product_disposable_create_cleanup_proof,
    )

    cursor = _Cursor(tables=tables, sequences=sequences)
    result = execute_dim_product_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=_Factory(_Connection(cursor)),
    )

    assert result["status"] == expected
    assert not any(sql.startswith("DROP ") for sql, _params in cursor.statements)


def test_executor_creates_verifies_and_removes_both_disposable_objects():
    from dim_product_scd2_disposable_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        execute_dim_product_disposable_create_cleanup_proof,
    )

    cursor = _Cursor()
    factory = _Factory(_Connection(cursor))

    result = execute_dim_product_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "passed"
    assert result["metadata_column_count"] == 26
    assert result["product_sk_constraint_count"] == 1
    assert result["row_count"] == 0
    assert result["sequence_increment_by"] == 1
    assert result["sequence_cycle"] is False
    assert result["proof_table_exists_after_cleanup"] is False
    assert result["proof_sequence_exists_after_cleanup"] is False
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert cursor.tables == set()
    assert cursor.sequences == set()
    assert factory.calls == 1


def test_executor_cleans_only_owned_objects_and_redacts_driver_failure():
    from dim_product_scd2_disposable_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        execute_dim_product_disposable_create_cleanup_proof,
    )

    cursor = _Cursor(fail_token="FROM ALL_TAB_COLUMNS")
    result = execute_dim_product_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=_Factory(_Connection(cursor)),
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "driver credential detail" not in str(result)
    assert result["cleanup_attempted"] is True
    assert result["proof_table_exists_after_cleanup"] is False
    assert result["proof_sequence_exists_after_cleanup"] is False
    assert cursor.tables == set()
    assert cursor.sequences == set()


def test_executor_module_cannot_drop_real_dim_objects_or_open_configured_connection():
    source = (
        PROJECT_ROOT / "dim_product_scd2_disposable_executor.py"
    ).read_text(encoding="utf-8")

    assert 'DROP TABLE "{OWNER}"."{TARGET_TABLE}"' not in source
    assert 'DROP SEQUENCE "{OWNER}"."{TARGET_SEQUENCE}"' not in source
    assert 'DROP TABLE "DWH"."DIM_PRODUCT"' not in source
    assert 'DROP SEQUENCE "DWH"."SEQ_DIM_PRODUCT"' not in source
    assert "run_configured" not in source
    assert "import config" not in source


def test_disposable_executor_doc_keeps_live_proof_unclaimed():
    text = (
        PROJECT_ROOT / "docs" / "dwh-dim-product-scd2-disposable-proof-executor.md"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    for required in (
        "disposable target and sequence proof executor",
        "odi68_dim_product_scd2_disposable_create_cleanup",
        "independent review",
        "exact operator approval",
        "fixed proof objects",
        "same approved call",
        "no raw rows",
        "no pk values",
        "live proof has not been run",
    ):
        assert required in lowered
    assert "live-proven" not in lowered
