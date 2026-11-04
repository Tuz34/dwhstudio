from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest


def _candidate_report(**overrides):
    report = {
        "status": "ready_for_scd2_load_review",
        "source_count": 72,
        "target_count": 0,
        "source_duplicate_groups": 0,
        "duplicate_product_sk_groups": 0,
        "duplicate_current_natural_key_groups": 0,
        "invalid_current_flags": 0,
        "invalid_effective_dates": 0,
        "current_target_count": 0,
        "insert_without_current_count": 72,
        "changed_current_count": 0,
        "unchanged_current_count": 0,
        "source_missing_current_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "candidate_count": 72,
        "will_execute": False,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    report.update(overrides)
    return report


class _Cursor:
    def __init__(
        self,
        *,
        candidate=None,
        fail_on=None,
        rerun_insert_count=0,
        post_invalid_flags=0,
    ):
        self.values = dict(_candidate_report())
        self.values.pop("status")
        for field in (
            "will_execute",
            "write_attempted",
            "raw_rows_included",
            "pk_values_included",
            "secret_printed",
        ):
            self.values.pop(field)
        self.values.update(candidate or {})
        self.fail_on = fail_on
        self.rerun_insert_count = rerun_insert_count
        self.post_invalid_flags = post_invalid_flags
        self.result = []
        self.rowcount = -1
        self.statements = []
        self.pass_number = 0
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
            self.result = [(datetime(2026, 7, 14, 12, 0, 0),)]
            self.rowcount = -1
            return
        marker = next((name for name in self.values if f"odi68:{name}" in sql), None)
        if marker is not None:
            self.result = [(self.values[marker],)]
            self.rowcount = -1
            return
        if upper.startswith('UPDATE "DWH"."DIM_PRODUCT"'):
            self.pending_expired = int(self.values["changed_current_count"])
            self.rowcount = self.pending_expired
            self.result = []
            return
        if upper.startswith('INSERT INTO "DWH"."DIM_PRODUCT"'):
            if self.pass_number == 0:
                inserted = int(self.values["insert_without_current_count"]) + self.pending_expired
                self.values["target_count"] += inserted
                self.values["current_target_count"] = self.values["source_count"]
                self.values["insert_without_current_count"] = 0
                self.values["changed_current_count"] = 0
                self.values["unchanged_current_count"] = self.values["source_count"]
                self.values["candidate_count"] = 0
                self.values["invalid_current_flags"] = self.post_invalid_flags
                self.rowcount = inserted
            else:
                self.rowcount = self.rerun_insert_count
            self.pass_number += 1
            self.pending_expired = 0
            self.result = []
            return
        raise AssertionError(f"Unexpected statement: {sql}")

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


def _execute(
    report=None,
    connection=None,
    *,
    review="approved",
    approved=True,
    scope="odi68_dim_product_scd2_load_and_rerun_proof",
    batch_id=2026071401,
):
    from dim_product_scd2_load_executor import execute_dim_product_scd2_load_and_rerun_proof

    factory = _Factory(connection or _Connection())
    result = execute_dim_product_scd2_load_and_rerun_proof(
        report or _candidate_report(),
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
def test_load_executor_gates_block_before_connection(kwargs, expected):
    result, factory = _execute(**kwargs)

    assert result["status"] == expected
    assert factory.calls == 0


def test_load_executor_rejects_inconsistent_candidate_report_before_connection():
    report = _candidate_report(candidate_count=71)

    result, factory = _execute(report=report)

    assert result["status"] == "blocked_candidate_preflight"
    assert factory.calls == 0


def test_fixed_expire_and_insert_statements_use_only_reviewed_objects():
    from dim_product_scd2_load_executor import build_expire_statement, build_insert_statement

    expire = build_expire_statement()
    insert = build_insert_statement()
    combined = f"{expire}\n{insert}".lower()

    assert expire.startswith('UPDATE "DWH"."DIM_PRODUCT"')
    assert '"CURRENT_FLAG" = \'Y\'' in expire
    assert "decode(" in expire.lower()
    assert insert.startswith('INSERT INTO "DWH"."DIM_PRODUCT"')
    assert '"DWH"."SEQ_DIM_PRODUCT".NEXTVAL' in insert
    assert 'FROM "DWH"."ODS_SH_PRODUCTS" s' in insert
    assert "not exists" in insert.lower()
    for forbidden in ("delete from", "drop table", "truncate table", "create table"):
        assert forbidden not in combined


def test_initial_load_commits_only_after_same_transaction_rerun_noop():
    connection = _Connection()

    result, factory = _execute(connection=connection)

    assert result["status"] == "passed"
    assert result["expired_count"] == 0
    assert result["inserted_count"] == 72
    assert result["rerun_expired_count"] == 0
    assert result["rerun_inserted_count"] == 0
    assert result["source_count"] == 72
    assert result["target_count"] == 72
    assert result["current_target_count"] == 72
    assert result["remaining_candidate_count"] == 0
    assert result["unchanged_count"] == 72
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["batch_id_value_included"] is False
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed is True
    assert factory.calls == 1


def test_changed_row_is_expired_and_reinserted_as_one_new_version():
    report = _candidate_report(
        target_count=72,
        current_target_count=72,
        insert_without_current_count=0,
        changed_current_count=1,
        unchanged_current_count=71,
        candidate_count=1,
    )
    connection = _Connection(
        candidate={
            "target_count": 72,
            "current_target_count": 72,
            "insert_without_current_count": 0,
            "changed_current_count": 1,
            "unchanged_current_count": 71,
            "candidate_count": 1,
        }
    )

    result, _factory = _execute(report=report, connection=connection)

    assert result["status"] == "passed"
    assert result["expired_count"] == 1
    assert result["inserted_count"] == 1
    assert result["target_count"] == 73
    assert result["current_target_count"] == 72
    assert result["remaining_candidate_count"] == 0


def test_load_executor_blocks_when_live_candidate_state_changed():
    connection = _Connection(candidate={"source_count": 73, "insert_without_current_count": 73, "candidate_count": 73})

    result, _factory = _execute(connection=connection)

    assert result["status"] == "blocked_candidate_state_changed"
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(
        statement.upper().startswith(("UPDATE ", "INSERT "))
        for statement, _params in connection.cursor_instance.statements
    )


def test_load_executor_rolls_back_on_post_load_dq_failure():
    connection = _Connection(post_invalid_flags=1)

    result, _factory = _execute(connection=connection)

    assert result["status"] == "failed"
    assert result["error_type"] == "SCD2VerificationError"
    assert result["rolled_back"] is True
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_load_executor_rolls_back_when_rerun_is_not_noop():
    connection = _Connection(rerun_insert_count=1)

    result, _factory = _execute(connection=connection)

    assert result["status"] == "failed"
    assert result["error_type"] == "SCD2VerificationError"
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_load_executor_redacts_database_error_and_rolls_back():
    connection = _Connection(fail_on="INSERT INTO")

    result, _factory = _execute(connection=connection)

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "credential detail" not in str(result)
    assert result["write_operations_attempted"] == [
        "expire_changed_current_versions",
        "insert_new_current_versions",
    ]
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_load_executor_has_no_endpoint_configured_connection_or_destructive_surface():
    source = (
        Path(__file__).parents[1] / "dim_product_scd2_load_executor.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()

    assert "odi68_dim_product_scd2_load_and_rerun_proof" in source
    assert "connection_factory" in source
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
