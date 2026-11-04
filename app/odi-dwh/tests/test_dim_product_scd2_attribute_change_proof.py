from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest


def _noop_report(**overrides):
    report = {
        "status": "rerun_noop",
        "source_count": 72,
        "target_count": 72,
        "source_duplicate_groups": 0,
        "duplicate_product_sk_groups": 0,
        "duplicate_current_natural_key_groups": 0,
        "invalid_current_flags": 0,
        "invalid_effective_dates": 0,
        "current_target_count": 72,
        "insert_without_current_count": 0,
        "changed_current_count": 0,
        "unchanged_current_count": 72,
        "source_missing_current_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "candidate_count": 0,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    report.update(overrides)
    return report


class _Cursor:
    def __init__(self, *, baseline=None, fail_on=None, rerun_insert_count=0):
        self.values = dict(_noop_report())
        for field in (
            "status",
            "will_execute",
            "write_attempted",
            "raw_rows_included",
            "pk_values_included",
            "secret_printed",
        ):
            self.values.pop(field)
        self.values.update(baseline or {})
        self.fail_on = fail_on
        self.rerun_insert_count = rerun_insert_count
        self.result = []
        self.rowcount = -1
        self.statements = []
        self.load_pass = 0
        self.pending_expired = 0

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        upper = compact.upper()
        self.statements.append((compact, params))
        if self.fail_on and self.fail_on in upper:
            import oracledb

            raise oracledb.DatabaseError("driver credential detail")
        if upper.startswith("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"):
            self.result = []
            self.rowcount = -1
            return
        if upper.startswith("SELECT SYSDATE FROM DUAL"):
            self.result = [(datetime(2026, 7, 14, 14, 0, 0),)]
            self.rowcount = -1
            return
        marker = next((name for name in self.values if f"odi68:{name}" in sql), None)
        if marker is not None:
            self.result = [(self.values[marker],)]
            self.rowcount = -1
            return
        if upper.startswith('UPDATE "DWH"."ODS_SH_PRODUCTS"'):
            self.values["changed_current_count"] = 1
            self.values["unchanged_current_count"] = self.values["source_count"] - 1
            self.values["candidate_count"] = 1
            self.rowcount = 1
            self.result = []
            return
        if upper.startswith('UPDATE "DWH"."DIM_PRODUCT"'):
            self.pending_expired = int(self.values["changed_current_count"])
            self.rowcount = self.pending_expired
            self.result = []
            return
        if upper.startswith('INSERT INTO "DWH"."DIM_PRODUCT"'):
            if self.load_pass == 0:
                inserted = int(self.values["insert_without_current_count"]) + self.pending_expired
                self.values["target_count"] += inserted
                self.values["changed_current_count"] = 0
                self.values["unchanged_current_count"] = self.values["source_count"]
                self.values["candidate_count"] = 0
                self.rowcount = inserted
            else:
                self.rowcount = self.rerun_insert_count
            self.load_pass += 1
            self.pending_expired = 0
            self.result = []
            return
        raise AssertionError(f"Unexpected statement: {sql}")

    def fetchone(self):
        return self.result[0]

    def close(self):
        return None


class _Connection:
    def __init__(self, *, rollback_fails=False, **cursor_kwargs):
        self.cursor_instance = _Cursor(**cursor_kwargs)
        self.rollbacks = 0
        self.rollback_fails = rollback_fails
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_fails:
            import oracledb

            raise oracledb.DatabaseError("rollback detail")

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
    report=None,
    connection=None,
    *,
    review="approved",
    approved=True,
    scope="odi68_dim_product_scd2_attribute_change_rollback_proof",
    batch_id=2026071402,
):
    from dim_product_scd2_attribute_change_proof import (
        execute_dim_product_scd2_attribute_change_rollback_proof,
    )

    factory = _Factory(connection or _Connection())
    result = execute_dim_product_scd2_attribute_change_rollback_proof(
        report or _noop_report(),
        executor_review_status=review,
        approved=approved,
        approval_scope=scope,
        batch_id=batch_id,
        connection_factory=factory,
    )
    return result, factory


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"review": "pending"}, "blocked_pending_executor_review"),
        ({"approved": "yes"}, "blocked_until_exact_operator_approval"),
        ({"scope": "wrong"}, "blocked_invalid_approval_scope"),
        ({"batch_id": True}, "blocked_invalid_batch_id"),
        ({"batch_id": 0}, "blocked_invalid_batch_id"),
    ],
)
def test_change_proof_gates_block_before_connection(kwargs, expected):
    result, factory = _execute(**kwargs)

    assert result["status"] == expected
    assert factory.calls == 0


def test_change_proof_requires_clean_rerun_noop_baseline():
    report = _noop_report(status="ready_for_scd2_load_review", candidate_count=1)

    result, factory = _execute(report=report)

    assert result["status"] == "blocked_baseline_preflight"
    assert factory.calls == 0


def test_source_change_statement_is_fixed_one_row_and_does_not_return_key():
    from dim_product_scd2_attribute_change_proof import build_source_change_statement

    sql = build_source_change_statement()
    upper = sql.upper()

    assert upper.startswith('UPDATE "DWH"."ODS_SH_PRODUCTS"')
    assert 'SET "PROD_STATUS" = CASE' in upper
    assert 'SELECT MIN("PROD_ID")' in upper
    assert "RETURNING" not in upper


def test_proof_insert_does_not_advance_nontransactional_real_sequence():
    from dim_product_scd2_attribute_change_proof import (
        build_rollback_proof_insert_statement,
    )

    sql = build_rollback_proof_insert_statement()
    upper = sql.upper()

    assert upper.startswith('INSERT INTO "DWH"."DIM_PRODUCT"')
    assert 'MAX("PRODUCT_SK")' in upper
    assert "NEXTVAL" not in upper
    assert "SEQ_DIM_PRODUCT" not in upper


def test_change_proof_proves_one_expire_one_insert_and_always_rolls_back():
    connection = _Connection()

    result, factory = _execute(connection=connection)

    assert result["status"] == "passed_rolled_back"
    assert result["simulated_source_change_count"] == 1
    assert result["changed_candidate_count"] == 1
    assert result["expired_count"] == 1
    assert result["inserted_count"] == 1
    assert result["rerun_expired_count"] == 0
    assert result["rerun_inserted_count"] == 0
    assert result["proof_target_count"] == 73
    assert result["proof_current_target_count"] == 72
    assert result["proof_remaining_candidate_count"] == 0
    assert result["rolled_back"] is True
    assert result["persistent_state_preserved"] is True
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["batch_id_value_included"] is False
    assert connection.rollbacks == 1
    assert connection.closed is True
    assert factory.calls == 1


def test_change_proof_blocks_stale_live_baseline_before_source_update():
    connection = _Connection(baseline={"target_count": 73})

    result, _factory = _execute(connection=connection)

    assert result["status"] == "blocked_baseline_state_changed"
    assert connection.rollbacks == 1
    assert not any(
        statement.upper().startswith('UPDATE "DWH"."ODS_SH_PRODUCTS"')
        for statement, _params in connection.cursor_instance.statements
    )


def test_change_proof_rolls_back_when_rerun_is_not_noop():
    connection = _Connection(rerun_insert_count=1)

    result, _factory = _execute(connection=connection)

    assert result["status"] == "failed_rolled_back"
    assert result["error_type"] == "SCD2ChangeProofError"
    assert result["rolled_back"] is True
    assert connection.rollbacks == 1


def test_change_proof_redacts_database_error_and_rolls_back():
    connection = _Connection(fail_on='UPDATE "DWH"."DIM_PRODUCT"')

    result, _factory = _execute(connection=connection)

    assert result["status"] == "failed_rolled_back"
    assert result["error_type"] == "DatabaseError"
    assert "credential detail" not in str(result)
    assert result["rolled_back"] is True
    assert connection.rollbacks == 1


def test_change_proof_fails_closed_when_mandatory_rollback_fails():
    connection = _Connection(rollback_fails=True)

    result, _factory = _execute(connection=connection)

    assert result["status"] == "failed_rollback"
    assert result["rolled_back"] is False
    assert result["persistent_state_preserved"] is False


def test_change_proof_has_no_commit_endpoint_config_or_destructive_surface():
    source = (
        Path(__file__).parents[1] / "dim_product_scd2_attribute_change_proof.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()

    assert "odi68_dim_product_scd2_attribute_change_rollback_proof" in source
    assert "connection_factory" in source
    assert ".commit(" not in lowered
    assert "oracledb.connect" not in lowered
    assert "import config" not in lowered
    assert "@" + "app.route" not in lowered
    assert "@" + "blueprint.route" not in lowered
    for forbidden in (
        "delete from",
        "drop table",
        "drop sequence",
        "truncate table",
        "create table",
        "create sequence",
    ):
        assert forbidden not in lowered
