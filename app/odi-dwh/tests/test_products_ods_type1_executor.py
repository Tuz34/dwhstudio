from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _metadata_preflight():
    return {
        "status": "ready_for_named_backup",
        "source": {
            "metadata_status": "metadata_ready_for_review",
            "duplicate_key_group_count": 0,
        },
        "target": {
            "exists": True,
            "metadata_status": "metadata_ready_for_review",
            "duplicate_key_group_count": 0,
        },
    }


class _Cursor:
    def __init__(self, *, target_count=0, backup_exists=False, merge_counts=None, fail_on=None):
        from products_ods_type1_executor import BACKUP_TABLE, TARGET_TABLE

        self.source_count = 72
        self.target_count = target_count
        self.insert_count = 72 - target_count
        self.update_count = 0
        self.unchanged_count = target_count
        self.source_duplicates = 0
        self.target_duplicates = 0
        self.tables = {TARGET_TABLE: target_count}
        if backup_exists:
            self.tables[BACKUP_TABLE] = target_count
        self.merge_counts = list(merge_counts or [72, 0])
        self.fail_on = fail_on
        self.result = []
        self.rowcount = -1
        self.statements = []

    def execute(self, sql, params=None):
        from products_ods_type1_executor import SOURCE_TABLE, TARGET_TABLE

        normalized = " ".join(sql.split())
        upper = normalized.upper()
        self.statements.append((normalized, params))
        self.rowcount = -1
        if self.fail_on and self.fail_on in upper:
            import oracledb

            raise oracledb.DatabaseError("driver detail must not be returned")
        if upper.startswith("SET TRANSACTION"):
            self.result = []
        elif "FROM ALL_TABLES" in upper:
            self.result = [(1 if params["table_name"] in self.tables else 0,)]
        elif "FROM ALL_TAB_COLUMNS" in upper:
            self.result = [(24 if params["table_name"] in self.tables else 0,)]
        elif upper.startswith("CREATE TABLE"):
            names = re.findall(r'"DWH"\."([A-Z0-9_]+)"', upper)
            self.tables[names[0]] = self.tables[names[1]]
            self.result = []
        elif upper.startswith("DROP TABLE"):
            table = re.findall(r'"DWH"\."([A-Z0-9_]+)"', upper)[0]
            self.tables.pop(table, None)
            self.result = []
        elif upper.startswith("MERGE INTO"):
            self.rowcount = self.merge_counts.pop(0)
            self.target_count = self.source_count
            self.tables[TARGET_TABLE] = self.target_count
            self.insert_count = 0
            self.update_count = 0
            self.unchanged_count = self.source_count
            self.result = []
        elif "HAVING COUNT(*) > 1" in upper:
            self.result = [(
                self.source_duplicates if f'"SH"."{SOURCE_TABLE}"' in upper else self.target_duplicates,
            )]
        elif "LEFT JOIN" in upper and 'IS NULL' in upper:
            self.result = [(self.insert_count,)]
        elif "WHERE NOT (" in upper:
            self.result = [(self.unchanged_count,)]
        elif "WHERE DECODE(" in upper:
            self.result = [(self.update_count,)]
        elif upper == f'SELECT COUNT(*) FROM "SH"."{SOURCE_TABLE}"':
            self.result = [(self.source_count,)]
        elif upper == f'SELECT COUNT(*) FROM "DWH"."{TARGET_TABLE}"':
            self.result = [(self.target_count,)]
        elif upper.startswith('SELECT COUNT(*) FROM "DWH".'):
            table = re.findall(r'"DWH"\."([A-Z0-9_]+)"', upper)[0]
            self.result = [(self.tables[table],)]
        else:
            raise AssertionError(f"Unexpected statement: {normalized}")

    def fetchone(self):
        return self.result[0]

    def close(self):
        return None


class _Connection:
    def __init__(self, **cursor_kwargs):
        self.cursor_instance = _Cursor(**cursor_kwargs)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _Factory:
    def __init__(self, connection):
        self.connection = connection
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.connection


def _candidate_preflight(factory=None):
    from products_ods_type1_executor import run_products_ods_type1_read_only_preflight

    factory = factory or _Factory(_Connection())
    return run_products_ods_type1_read_only_preflight(
        _metadata_preflight(),
        connection_factory=factory,
    )


def test_type1_read_only_preflight_blocks_before_connection_on_metadata_state():
    from products_ods_type1_executor import run_products_ods_type1_read_only_preflight

    factory = _Factory(_Connection())
    result = run_products_ods_type1_read_only_preflight(
        {"status": "blocked_target_metadata_drift"},
        connection_factory=factory,
    )

    assert result["status"] == "blocked_metadata_preflight"
    assert factory.calls == 0


def test_type1_read_only_preflight_returns_counts_without_key_values():
    factory = _Factory(_Connection())
    result = _candidate_preflight(factory)

    assert result["status"] == "ready_for_type1_backup"
    assert result["source_count"] == 72
    assert result["target_count"] == 0
    assert result["insert_candidate_count"] == 72
    assert result["update_candidate_count"] == 0
    assert result["unchanged_count"] == 0
    assert result["pk_values_included"] is False
    assert factory.connection.rollbacks == 1


def test_named_backup_rejects_truthy_approval_before_connection():
    from products_ods_type1_executor import (
        BACKUP_PROOF_APPROVAL_SCOPE,
        execute_products_ods_named_backup_restore_proof,
    )

    factory = _Factory(_Connection())
    result = execute_products_ods_named_backup_restore_proof(
        _candidate_preflight(),
        executor_review_status="approved",
        approved="yes",
        approval_scope=BACKUP_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "blocked_until_exact_operator_approval"
    assert factory.calls == 0


def test_named_backup_proves_restore_copy_and_keeps_only_fixed_backup():
    from products_ods_type1_executor import (
        BACKUP_PROOF_APPROVAL_SCOPE,
        BACKUP_TABLE,
        RESTORE_PROOF_TABLE,
        execute_products_ods_named_backup_restore_proof,
    )

    factory = _Factory(_Connection())
    result = execute_products_ods_named_backup_restore_proof(
        _candidate_preflight(),
        executor_review_status="approved",
        approved=True,
        approval_scope=BACKUP_PROOF_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "passed"
    assert result["backup_column_count"] == 24
    assert result["backup_row_count"] == 0
    assert result["restore_proof_row_count"] == 0
    assert BACKUP_TABLE in factory.connection.cursor_instance.tables
    assert RESTORE_PROOF_TABLE not in factory.connection.cursor_instance.tables


@pytest.mark.parametrize(
    ("review", "approved", "scope", "backup", "expected"),
    [
        ("pending", True, "odi68_products_ods_type1_load_and_rerun_proof", "passed", "blocked_pending_executor_review"),
        ("approved", "yes", "odi68_products_ods_type1_load_and_rerun_proof", "passed", "blocked_until_exact_operator_approval"),
        ("approved", True, "wrong", "passed", "blocked_invalid_approval_scope"),
        ("approved", True, "odi68_products_ods_type1_load_and_rerun_proof", "pending", "blocked_pending_named_backup_restore_proof"),
    ],
)
def test_type1_load_gates_open_no_connection(review, approved, scope, backup, expected):
    from products_ods_type1_executor import execute_products_ods_type1_load_and_rerun_proof

    factory = _Factory(_Connection(backup_exists=True))
    result = execute_products_ods_type1_load_and_rerun_proof(
        _candidate_preflight(),
        backup_proof_status=backup,
        executor_review_status=review,
        approved=approved,
        approval_scope=scope,
        connection_factory=factory,
    )

    assert result["status"] == expected
    assert factory.calls == 0


def test_type1_load_merges_and_proves_same_transaction_rerun_is_noop():
    from products_ods_type1_executor import (
        TYPE1_LOAD_APPROVAL_SCOPE,
        execute_products_ods_type1_load_and_rerun_proof,
    )

    factory = _Factory(_Connection(backup_exists=True, merge_counts=[72, 0]))
    result = execute_products_ods_type1_load_and_rerun_proof(
        _candidate_preflight(),
        backup_proof_status="passed",
        executor_review_status="approved",
        approved=True,
        approval_scope=TYPE1_LOAD_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "passed"
    assert result["inserted_count"] == 72
    assert result["refreshed_count"] == 0
    assert result["rerun_affected_count"] == 0
    assert result["target_count"] == 72
    assert result["remaining_insert_candidate_count"] == 0
    assert factory.connection.commits == 1
    assert factory.connection.rollbacks == 0


def test_type1_load_rolls_back_when_rerun_is_not_noop():
    from products_ods_type1_executor import (
        TYPE1_LOAD_APPROVAL_SCOPE,
        execute_products_ods_type1_load_and_rerun_proof,
    )

    factory = _Factory(_Connection(backup_exists=True, merge_counts=[72, 1]))
    result = execute_products_ods_type1_load_and_rerun_proof(
        _candidate_preflight(),
        backup_proof_status="passed",
        executor_review_status="approved",
        approved=True,
        approval_scope=TYPE1_LOAD_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "Type1VerificationError"
    assert result["rolled_back"] is True
    assert factory.connection.commits == 0
    assert factory.connection.rollbacks == 1


def test_type1_load_redacts_oracle_error_and_rolls_back():
    from products_ods_type1_executor import (
        TYPE1_LOAD_APPROVAL_SCOPE,
        execute_products_ods_type1_load_and_rerun_proof,
    )

    factory = _Factory(_Connection(backup_exists=True, fail_on="MERGE INTO"))
    result = execute_products_ods_type1_load_and_rerun_proof(
        _candidate_preflight(),
        backup_proof_status="passed",
        executor_review_status="approved",
        approved=True,
        approval_scope=TYPE1_LOAD_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "driver detail" not in str(result)
    assert result["rolled_back"] is True


def test_type1_merge_is_fixed_to_reviewed_tables_and_columns():
    from products_ods_type1_executor import build_type1_merge_statement
    from scd2_manager import PRODUCTS_SOURCE_METADATA

    sql = build_type1_merge_statement()

    assert 'MERGE INTO "DWH"."ODS_SH_PRODUCTS"' in sql
    assert 'USING "SH"."PRODUCTS"' in sql
    assert 't."PROD_ID" = s."PROD_ID"' in sql
    for name, _data_type, _nullable in PRODUCTS_SOURCE_METADATA:
        assert f'"{name}"' in sql
    assert "SYSTIMESTAMP" in sql


def test_type1_executor_has_no_endpoint_config_or_dynamic_identifier_surface():
    text = (PROJECT_ROOT / "products_ods_type1_executor.py").read_text(encoding="utf-8")
    lowered = text.lower()

    assert "@app.route" not in lowered
    assert "blueprint" not in lowered
    assert "import config" not in lowered
    assert "connection string" not in lowered
    assert '"raw_rows_included": false' in lowered
    assert '"pk_values_included": false' in lowered


def test_type1_executor_doc_keeps_live_steps_separately_gated():
    text = (PROJECT_ROOT / "docs" / "dwh-products-ods-type1-executor.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()

    for required in (
        "read-only candidate preflight",
        "named backup/restore proof",
        "same transaction",
        "exact operator approval",
        "backup remains",
        "no endpoint",
        "no raw rows",
        "no pk values",
        "no live backup or type 1 load was run",
    ):
        assert required in lowered
