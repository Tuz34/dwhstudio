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


def _live_preflight():
    return {
        "status": "blocked_target_missing",
        "source": {
            "exists": True,
            "column_count": 24,
            "metadata_status": "metadata_ready_for_review",
            "row_count": 72,
            "duplicate_key_group_count": 0,
        },
        "target": {"owner": "DWH", "table": "DIM_PRODUCT", "exists": False},
        "sequence": {
            "owner": "DWH",
            "sequence": "SEQ_DIM_PRODUCT",
            "exists": False,
        },
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
    }


def _backup_evidence():
    return {
        "owner": "DWH",
        "table": "ODI68_ODS_PRODUCTS_BKP",
        "exists": True,
        "column_count": 24,
        "row_count": 0,
    }


class _Cursor:
    def __init__(
        self,
        *,
        tables=None,
        sequences=None,
        backup_exists=True,
        backup_columns=24,
        backup_rows=0,
        fail_token=None,
    ):
        self.tables = set(tables or ())
        self.sequences = set(sequences or ())
        self.backup_exists = backup_exists
        self.backup_columns = backup_columns
        self.backup_rows = backup_rows
        self.fail_token = fail_token
        self.failed = False
        self.result = []
        self.statements = []

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        upper = compact.upper()
        self.statements.append((compact, params))
        if self.fail_token and self.fail_token in upper and not self.failed:
            self.failed = True
            raise oracledb.DatabaseError("driver credential detail")
        if "FROM ALL_TABLES" in upper:
            if params["table_name"] == "ODI68_ODS_PRODUCTS_BKP":
                self.result = [(1 if self.backup_exists else 0,)]
            else:
                self.result = [(1 if params["table_name"] in self.tables else 0,)]
        elif "FROM ALL_SEQUENCES" in upper and "INCREMENT_BY" in upper:
            if params["sequence_name"] in self.sequences:
                self.result = [(1, "N")]
            else:
                self.result = []
        elif upper.startswith('CREATE SEQUENCE "DWH"."SEQ_DIM_PRODUCT"'):
            self.sequences.add("SEQ_DIM_PRODUCT")
            self.result = []
        elif upper.startswith('CREATE TABLE "DWH"."DIM_PRODUCT"'):
            self.tables.add("DIM_PRODUCT")
            self.result = []
        elif upper.startswith("SELECT COUNT(*) FROM ALL_TAB_COLUMNS"):
            self.result = [(self.backup_columns,)]
        elif "FROM ALL_TAB_COLUMNS" in upper:
            from dim_product_scd2_initial_create_executor import expected_target_metadata_rows

            self.result = expected_target_metadata_rows()
        elif "FROM ALL_CONSTRAINTS" in upper:
            self.result = [(1,)]
        elif upper.startswith('SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT"'):
            self.result = [(0,)]
        elif upper.startswith('SELECT COUNT(*) FROM "DWH"."ODI68_ODS_PRODUCTS_BKP"'):
            self.result = [(self.backup_rows,)]
        else:
            raise AssertionError(f"Unexpected statement: {sql}")

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0] if self.result else None

    def close(self):
        return None


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


def _execute(
    factory,
    *,
    plan=None,
    preflight=None,
    backup=None,
    review="approved",
    approved=True,
    scope="odi68_dim_product_scd2_initial_create",
    proof="passed",
):
    from dim_product_scd2_initial_create_executor import execute_dim_product_initial_create

    return execute_dim_product_initial_create(
        plan or _plan(),
        live_preflight=preflight or _live_preflight(),
        ods_backup_evidence=backup or _backup_evidence(),
        executor_review_status=review,
        approved=approved,
        approval_scope=scope,
        disposable_proof_status=proof,
        connection_factory=factory,
    )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("plan", {"status": "blocked"}, "blocked_initial_create_contract"),
        ("review", "pending", "blocked_pending_executor_review"),
        ("approved", "yes", "blocked_until_exact_operator_approval"),
        ("scope", "wrong_scope", "blocked_invalid_approval_scope"),
        ("proof", "pending", "blocked_pending_disposable_proof"),
        ("preflight", {"status": "ready"}, "blocked_live_preflight"),
        ("backup", {"exists": False}, "blocked_ods_backup_evidence"),
    ],
)
def test_all_gates_block_before_connection(field, value, expected):
    factory = _Factory(_Connection(_Cursor()))
    kwargs = {field: value}

    result = _execute(factory, **kwargs)

    assert result["status"] == expected
    assert result["connection_attempted"] is False
    assert factory.calls == 0


def test_real_ddl_renderers_are_fixed_and_have_no_cleanup_surface():
    from dim_product_scd2_initial_create_executor import (
        build_real_sequence_statement,
        build_real_table_statement,
    )

    table_sql = build_real_table_statement()
    sequence_sql = build_real_sequence_statement()

    assert 'CREATE TABLE "DWH"."DIM_PRODUCT"' in table_sql
    assert 'PRIMARY KEY ("PRODUCT_SK")' in table_sql
    assert 'CREATE SEQUENCE "DWH"."SEQ_DIM_PRODUCT"' in sequence_sql
    assert "INCREMENT BY 1 NOCYCLE" in sequence_sql
    assert "DROP " not in table_sql + sequence_sql


@pytest.mark.parametrize(
    ("tables", "sequences", "expected"),
    [
        ({"DIM_PRODUCT"}, set(), "blocked_target_exists"),
        (set(), {"SEQ_DIM_PRODUCT"}, "blocked_sequence_exists"),
    ],
)
def test_rerun_guard_blocks_existing_real_objects_without_ddl(tables, sequences, expected):
    cursor = _Cursor(tables=tables, sequences=sequences)

    result = _execute(_Factory(_Connection(cursor)))

    assert result["status"] == expected
    assert not any(sql.startswith("CREATE ") for sql, _params in cursor.statements)


def test_executor_rechecks_fixed_ods_backup_on_live_connection_before_ddl():
    cursor = _Cursor(backup_exists=False)

    result = _execute(_Factory(_Connection(cursor)))

    assert result["status"] == "blocked_ods_backup_changed"
    assert result["connection_attempted"] is True
    assert not any(sql.startswith("CREATE ") for sql, _params in cursor.statements)


def test_executor_creates_and_verifies_empty_real_target_and_sequence():
    cursor = _Cursor()
    factory = _Factory(_Connection(cursor))

    result = _execute(factory)

    assert result["status"] == "created_pending_scd2_load"
    assert result["metadata_column_count"] == 26
    assert result["product_sk_constraint_count"] == 1
    assert result["row_count"] == 0
    assert result["sequence_increment_by"] == 1
    assert result["sequence_cycle"] is False
    assert result["target_exists_after_create"] is True
    assert result["sequence_exists_after_create"] is True
    assert result["cleanup_attempted"] is False
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert cursor.tables == {"DIM_PRODUCT"}
    assert cursor.sequences == {"SEQ_DIM_PRODUCT"}
    assert factory.calls == 1


@pytest.mark.parametrize(
    ("fail_token", "target_may_exist", "sequence_may_exist"),
    [
        ('CREATE TABLE "DWH"."DIM_PRODUCT"', False, True),
        ("ORDER BY COLUMN_ID", True, True),
    ],
)
def test_failure_is_redacted_and_never_auto_cleans_real_objects(
    fail_token, target_may_exist, sequence_may_exist
):
    cursor = _Cursor(fail_token=fail_token)

    result = _execute(_Factory(_Connection(cursor)))

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "driver credential detail" not in str(result)
    assert result["target_may_exist"] is target_may_exist
    assert result["sequence_may_exist"] is sequence_may_exist
    assert result["cleanup_attempted"] is False
    assert result["cleanup_requires_separate_approval"] is True
    assert not any(sql.startswith("DROP ") for sql, _params in cursor.statements)


def test_real_executor_module_has_no_drop_endpoint_or_configured_connection():
    source = (
        PROJECT_ROOT / "dim_product_scd2_initial_create_executor.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()

    assert "drop table" not in lowered
    assert "drop sequence" not in lowered
    assert "run_configured" not in lowered
    assert "import config" not in lowered
    assert "@" + "app.route" not in lowered
    assert "@" + "bp.route" not in lowered


def test_real_executor_doc_is_honest_about_review_and_live_state():
    text = (
        PROJECT_ROOT / "docs" / "dwh-dim-product-scd2-initial-create-executor.md"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    for required in (
        "real dim_product target and sequence executor",
        "odi68_dim_product_scd2_initial_create",
        "independent review",
        "exact operator approval",
        "disposable proof",
        "ods backup",
        "no automatic cleanup",
        "no raw rows",
        "no pk values",
        "live real-object setup has not been run",
    ):
        assert required in lowered
    assert "live-proven" not in lowered
