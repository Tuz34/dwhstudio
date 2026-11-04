from __future__ import annotations

from pathlib import Path

import pytest

from products_ods_initial_create import build_products_ods_initial_create_plan
from products_ods_preflight import build_products_ods_backup_restore_plan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _preflight():
    return {
        "status": "blocked_target_missing",
        "source": {
            "metadata_status": "metadata_ready_for_review",
            "duplicate_key_group_count": 0,
        },
        "target": {"exists": False},
    }


def _plan():
    preflight = _preflight()
    backup = build_products_ods_backup_restore_plan(
        preflight,
        artifact_id="ODI68_PRODUCTS_ODS_20260713T153035",
    )
    return build_products_ods_initial_create_plan(preflight, backup)


class _Cursor:
    def __init__(self, tables=None, fail_on=None):
        self.tables = set(tables or [])
        self.fail_on = fail_on
        self.statements = []
        self.result = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        upper = normalized.upper()
        if self.fail_on and self.fail_on in upper:
            import oracledb

            raise oracledb.DatabaseError("driver detail must not be returned")
        if "FROM ALL_TABLES" in upper:
            key = (params["owner"], params["table_name"])
            self.result = [(1 if key in self.tables else 0,)]
        elif upper.startswith("CREATE TABLE"):
            key = ("DWH", upper.split('"')[3])
            self.tables.add(key)
            self.result = []
        elif "FROM ALL_TAB_COLUMNS" in upper:
            from products_ods_initial_create_executor import expected_target_metadata_rows

            self.result = expected_target_metadata_rows()
        elif "FROM ALL_CONSTRAINTS" in upper:
            self.result = [(1,)]
        elif upper.startswith("SELECT COUNT(*) FROM"):
            self.result = [(0,)]
        elif upper.startswith("DROP TABLE"):
            key = ("DWH", upper.split('"')[3])
            self.tables.discard(key)
            self.result = []
        else:
            raise AssertionError(f"Unexpected statement: {sql}")

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return list(self.result)

    def close(self):
        return None


class _Connection:
    def __init__(self, tables=None, fail_on=None):
        self.cursor_instance = _Cursor(tables=tables, fail_on=fail_on)
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


def _sql(factory):
    return [statement for statement, _params in factory.connection.cursor_instance.statements]


def test_disposable_proof_rejects_truthy_approval_before_connection():
    from products_ods_initial_create_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        execute_products_ods_disposable_create_cleanup_proof,
    )

    factory = _Factory(_Connection())
    result = execute_products_ods_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved="yes",
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "blocked_until_exact_operator_approval"
    assert factory.calls == 0
    assert result["will_execute"] is False


def test_disposable_proof_creates_verifies_and_removes_only_fixed_proof_table():
    from products_ods_initial_create_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        PROOF_TABLE,
        execute_products_ods_disposable_create_cleanup_proof,
    )

    factory = _Factory(_Connection())
    result = execute_products_ods_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    statements = _sql(factory)
    assert result["status"] == "passed"
    assert result["metadata_column_count"] == 24
    assert result["unique_key_constraint_count"] == 1
    assert result["row_count"] == 0
    assert result["proof_table_exists_after_cleanup"] is False
    assert ("DWH", PROOF_TABLE) not in factory.connection.cursor_instance.tables
    assert sum(statement.startswith("CREATE TABLE") for statement in statements) == 1
    assert sum(statement.startswith("DROP TABLE") for statement in statements) == 1
    assert not any(statement.startswith(("INSERT ", "UPDATE ", "DELETE ", "MERGE ")) for statement in statements)


def test_disposable_proof_blocks_when_fixed_proof_table_already_exists():
    from products_ods_initial_create_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        PROOF_TABLE,
        execute_products_ods_disposable_create_cleanup_proof,
    )

    factory = _Factory(_Connection(tables={("DWH", PROOF_TABLE)}))
    result = execute_products_ods_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "blocked_disposable_table_exists"
    assert not any(statement.startswith(("CREATE TABLE", "DROP TABLE")) for statement in _sql(factory))


@pytest.mark.parametrize(
    ("review", "approved", "scope", "proof", "expected"),
    [
        ("pending", True, "odi68_products_ods_initial_target_create", "passed", "blocked_pending_executor_review"),
        ("approved", "yes", "odi68_products_ods_initial_target_create", "passed", "blocked_until_exact_operator_approval"),
        ("approved", True, "wrong_scope", "passed", "blocked_invalid_approval_scope"),
        ("approved", True, "odi68_products_ods_initial_target_create", "pending", "blocked_pending_disposable_cleanup_proof"),
    ],
)
def test_initial_create_gates_open_no_connection_until_every_requirement_passes(
    review, approved, scope, proof, expected
):
    from products_ods_initial_create_executor import execute_products_ods_initial_target_create

    factory = _Factory(_Connection())
    result = execute_products_ods_initial_target_create(
        _plan(),
        executor_review_status=review,
        approved=approved,
        approval_scope=scope,
        disposable_cleanup_proof_status=proof,
        connection_factory=factory,
    )

    assert result["status"] == expected
    assert factory.calls == 0


def test_initial_create_creates_and_verifies_empty_target_after_all_gates():
    from products_ods_initial_create_executor import (
        INITIAL_CREATE_APPROVAL_SCOPE,
        TARGET_TABLE,
        execute_products_ods_initial_target_create,
    )

    factory = _Factory(_Connection())
    result = execute_products_ods_initial_target_create(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=INITIAL_CREATE_APPROVAL_SCOPE,
        disposable_cleanup_proof_status="passed",
        connection_factory=factory,
    )

    assert result["status"] == "created_pending_type1_load"
    assert result["metadata_column_count"] == 24
    assert result["unique_key_constraint_count"] == 1
    assert result["row_count"] == 0
    assert result["rerun_guard"] == "target_exists_blocks_repeat_create"
    assert ("DWH", TARGET_TABLE) in factory.connection.cursor_instance.tables
    assert sum(statement.startswith("CREATE TABLE") for statement in _sql(factory)) == 1
    assert not any(statement.startswith("DROP TABLE") for statement in _sql(factory))


def test_initial_create_rerun_blocks_before_ddl_when_target_exists():
    from products_ods_initial_create_executor import (
        INITIAL_CREATE_APPROVAL_SCOPE,
        TARGET_TABLE,
        execute_products_ods_initial_target_create,
    )

    factory = _Factory(_Connection(tables={("DWH", TARGET_TABLE)}))
    result = execute_products_ods_initial_target_create(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=INITIAL_CREATE_APPROVAL_SCOPE,
        disposable_cleanup_proof_status="passed",
        connection_factory=factory,
    )

    assert result["status"] == "blocked_target_exists"
    assert not any(statement.startswith("CREATE TABLE") for statement in _sql(factory))


def test_executor_redacts_oracle_driver_error_and_cleans_own_proof_object():
    from products_ods_initial_create_executor import (
        DISPOSABLE_PROOF_APPROVAL_SCOPE,
        PROOF_TABLE,
        execute_products_ods_disposable_create_cleanup_proof,
    )

    factory = _Factory(_Connection(fail_on="FROM ALL_TAB_COLUMNS"))
    result = execute_products_ods_disposable_create_cleanup_proof(
        _plan(),
        executor_review_status="approved",
        approved=True,
        approval_scope=DISPOSABLE_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "driver detail" not in str(result)
    assert result["proof_table_exists_after_cleanup"] is False
    assert ("DWH", PROOF_TABLE) not in factory.connection.cursor_instance.tables


def test_create_statement_builder_rejects_every_unreviewed_table():
    from products_ods_initial_create_executor import build_create_table_statement

    with pytest.raises(ValueError, match="table"):
        build_create_table_statement("DWH", "UNREVIEWED_TABLE")


def test_oracle_default_timestamp_precision_matches_frozen_contract():
    from products_ods_initial_create_executor import _full_type

    assert _full_type("TIMESTAMP(6)", 11, None, None) == "TIMESTAMP"


def test_executor_has_no_endpoint_or_type1_data_load_surface():
    source = (PROJECT_ROOT / "products_ods_initial_create_executor.py").read_text(encoding="utf-8")
    lowered = source.lower()

    assert "@app.route" not in lowered
    assert "blueprint" not in lowered
    assert "insert into" not in lowered
    assert "merge into" not in lowered
    assert "update dwh" not in lowered
    assert "delete from" not in lowered
    assert "truncate table" not in lowered


def test_executor_doc_requires_exact_live_approval_and_denies_live_proof():
    text = (PROJECT_ROOT / "docs" / "dwh-products-ods-initial-create-executor.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()

    for required in (
        "disposable create/cleanup proof",
        "exact operator approval",
        "fixed table allowlist",
        "no endpoint",
        "oracle ddl auto-commits",
        "count/status/metadata only",
        "no raw rows",
        "no pk values",
        "live disposable ddl proof passed",
        "the real target was created empty",
    ):
        assert required in lowered
