from __future__ import annotations

from pathlib import Path

import pytest


def _metadata_preflight():
    return {
        "status": "ready_for_scd2_candidate_preflight",
        "source": {
            "exists": True,
            "metadata_status": "metadata_ready_for_review",
            "duplicate_key_group_count": 0,
        },
        "target": {
            "exists": True,
            "metadata_status": "metadata_ready_for_review",
            "duplicate_product_sk_group_count": 0,
            "duplicate_current_natural_key_group_count": 0,
            "invalid_current_flag_count": 0,
            "invalid_effective_date_order_count": 0,
        },
        "sequence": {"exists": True},
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


class _Cursor:
    def __init__(self, **overrides):
        self.values = {
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
        }
        self.values.update(overrides)
        self.result = []
        self.statements = []

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.statements.append((compact, params))
        if compact.upper().startswith("SET TRANSACTION READ ONLY"):
            self.result = []
            return
        marker = next((name for name in self.values if f"odi68:{name}" in sql), None)
        if marker is None:
            raise AssertionError(f"Unexpected statement: {sql}")
        self.result = [(self.values[marker],)]

    def fetchone(self):
        return self.result[0]

    def close(self):
        return None


class _Connection:
    def __init__(self, **overrides):
        self.cursor_instance = _Cursor(**overrides)
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

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


def _run(connection=None, metadata=None):
    from dim_product_scd2_candidate_preflight import (
        run_dim_product_scd2_candidate_preflight,
    )

    factory = _Factory(connection or _Connection())
    result = run_dim_product_scd2_candidate_preflight(
        metadata or _metadata_preflight(),
        connection_factory=factory,
    )
    return result, factory


def test_candidate_preflight_blocks_before_connection_on_metadata_state():
    metadata = _metadata_preflight()
    metadata["status"] = "blocked_target_metadata_drift"

    result, factory = _run(metadata=metadata)

    assert result["status"] == "blocked_metadata_preflight"
    assert factory.calls == 0


def test_candidate_preflight_reports_initial_load_counts_only():
    connection = _Connection()

    result, factory = _run(connection)

    assert result["status"] == "ready_for_scd2_load_review"
    assert result["source_count"] == 72
    assert result["target_count"] == 0
    assert result["current_target_count"] == 0
    assert result["insert_without_current_count"] == 72
    assert result["changed_current_count"] == 0
    assert result["unchanged_current_count"] == 0
    assert result["candidate_count"] == 72
    assert result["source_missing_current_count"] == 0
    assert result["backup_column_count"] == 24
    assert result["backup_row_count"] == 0
    assert result["will_execute"] is False
    assert result["write_attempted"] is False
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["secret_printed"] is False
    assert factory.calls == 1
    assert connection.cursor_instance.statements[0][0] == "SET TRANSACTION READ ONLY"
    combined = "\n".join(sql for sql, _params in connection.cursor_instance.statements)
    assert '"CURRENT_FLAG" = \'1\'' in combined
    assert "NOT IN ('1', '0')" in combined
    assert "DATE '2400-01-01'" in combined
    assert '"CURRENT_FLAG" = \'Y\'' not in combined
    assert "DATE '9999-12-31'" in combined
    assert connection.rollbacks == 1
    assert connection.closed is True


def test_candidate_preflight_detects_rerun_noop():
    result, _factory = _run(
        _Connection(
            target_count=72,
            current_target_count=72,
            insert_without_current_count=0,
            unchanged_current_count=72,
        )
    )

    assert result["status"] == "rerun_noop"
    assert result["candidate_count"] == 0
    assert result["unchanged_current_count"] == 72


def test_candidate_preflight_classifies_new_changed_and_unchanged_counts():
    result, _factory = _run(
        _Connection(
            target_count=70,
            current_target_count=70,
            insert_without_current_count=2,
            changed_current_count=3,
            unchanged_current_count=67,
        )
    )

    assert result["status"] == "ready_for_scd2_load_review"
    assert result["candidate_count"] == 5
    assert result["insert_without_current_count"] == 2
    assert result["changed_current_count"] == 3
    assert result["unchanged_current_count"] == 67


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"source_duplicate_groups": 1}, "blocked_source_duplicate_keys"),
        ({"duplicate_product_sk_groups": 1}, "blocked_target_duplicate_surrogate_keys"),
        ({"duplicate_current_natural_key_groups": 1}, "blocked_target_multiple_current_rows"),
        ({"invalid_current_flags": 1}, "blocked_target_invalid_current_flags"),
        ({"invalid_effective_dates": 1}, "blocked_target_invalid_effective_dates"),
        ({"source_missing_current_count": 1}, "blocked_delete_policy_required"),
        ({"backup_exists": 0}, "blocked_ods_backup_missing"),
        ({"backup_column_count": 23}, "blocked_ods_backup_metadata"),
        ({"backup_row_count": 1}, "blocked_ods_backup_count"),
        ({"unchanged_current_count": 71}, "blocked_candidate_count_mismatch"),
    ],
)
def test_candidate_preflight_fails_closed(overrides, expected):
    result, _factory = _run(_Connection(**overrides))

    assert result["status"] == expected


def test_candidate_preflight_redacts_database_errors():
    import oracledb

    class _FailingCursor(_Cursor):
        def execute(self, sql, params=None):
            if "odi68:changed_current_count" in sql:
                raise oracledb.DatabaseError("credential detail must not escape")
            super().execute(sql, params)

    connection = _Connection()
    connection.cursor_instance = _FailingCursor()

    result, _factory = _run(connection)

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "credential detail" not in str(result)
    assert connection.rollbacks == 1


def test_candidate_preflight_does_not_query_missing_backup_object():
    class _MissingBackupCursor(_Cursor):
        def execute(self, sql, params=None):
            if "odi68:backup_column_count" in sql or "odi68:backup_row_count" in sql:
                raise AssertionError("Missing backup metadata must not be queried.")
            super().execute(sql, params)

    connection = _Connection()
    connection.cursor_instance = _MissingBackupCursor(backup_exists=0)

    result, _factory = _run(connection)

    assert result["status"] == "blocked_ods_backup_missing"
    assert result["backup_column_count"] == 0
    assert result["backup_row_count"] == 0


def test_candidate_preflight_source_has_fixed_read_only_surface():
    source = (
        Path(__file__).parents[1] / "dim_product_scd2_candidate_preflight.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()

    assert "set transaction read only" in lowered
    assert 'source_owner = "dwh"' in lowered
    assert 'source_table = "ods_sh_products"' in lowered
    assert 'target_owner = "dwh"' in lowered
    assert 'target_table = "dim_product"' in lowered
    assert 'ods_backup_table = "odi68_ods_products_bkp"' in lowered
    for forbidden in (
        "create table",
        "create sequence",
        "insert into",
        "merge into",
        "update \"dwh\"",
        "delete from",
        "truncate table",
        "drop table",
        "drop sequence",
    ):
        assert forbidden not in lowered
