from __future__ import annotations

import inspect

import pytest

import scd2_builder
import scd2_target_executor as executor
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    connection_factory,
    oracle_error,
    plan_catalog_rows,
)


@pytest.fixture
def plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


def handler_for(plan, *, table_exists=0, sequence_exists=0, catalog=None, fail_on=None, error="ORA-01031: x"):
    rows = plan_catalog_rows(plan) if catalog is None else catalog

    def handler(sql, params):
        if fail_on and fail_on in sql:
            return oracle_error(error)
        if "FROM ALL_TABLES" in sql:
            return [(table_exists,)]
        if "FROM ALL_SEQUENCES" in sql:
            return [(sequence_exists,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return rows
        return []

    return handler


def test_blocked_without_exact_boolean_approval(plan):
    for approved in (False, None, "true", 1, "yes"):
        log = []
        result = executor.execute_target_ddl(
            plan, approved=approved, connection_factory=connection_factory(log, handler_for(plan))
        )
        assert result["status"] == "blocked_until_approval"
        assert result["connection_attempted"] is False
        assert log == []


def test_creates_sequence_then_table_then_grants(plan):
    log = []
    result = executor.execute_target_ddl(
        plan, approved=True, connection_factory=connection_factory(log, handler_for(plan)), ddl_user="DWH_DDL"
    )
    assert result["status"] == "created"
    ddl = [sql for sql in log if sql.startswith(("CREATE", "GRANT"))]
    assert ddl[0].startswith('CREATE SEQUENCE "DM"."SEQ_DIM_CUSTOMER"')
    assert ddl[1].startswith('CREATE TABLE "DM"."DIM_CUSTOMER"')
    assert all(sql.startswith("GRANT") for sql in ddl[2:])
    assert result["write_operations_attempted"] == ["create_sequence", "create_target", "grants"]
    assert result["grants_applied"] == len(ddl) - 2 and result["grants_failed"] == 0
    assert result["drop_supported"] is False
    assert not any("DROP" in sql.upper() or "ALTER" in sql.upper() for sql in log)


def test_existing_matching_table_is_not_touched(plan):
    log = []
    result = executor.execute_target_ddl(
        plan, approved=True, connection_factory=connection_factory(log, handler_for(plan, table_exists=1))
    )
    assert result["status"] == "already_exists_matching"
    assert not any(sql.startswith(("CREATE", "GRANT", "DROP", "ALTER")) for sql in log)


def test_existing_mismatching_table_is_blocked_not_altered(plan):
    log = []
    wrong = plan_catalog_rows(plan)[:-1]
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, table_exists=1, catalog=wrong)),
    )
    assert result["status"] == "blocked_target_exists_mismatch"
    assert not any(sql.startswith(("CREATE", "GRANT", "DROP", "ALTER")) for sql in log)


def test_existing_sequence_is_reused_and_reported(plan):
    log = []
    result = executor.execute_target_ddl(
        plan, approved=True, connection_factory=connection_factory(log, handler_for(plan, sequence_exists=1))
    )
    assert result["status"] == "created"
    assert result["sequence_already_existed"] is True
    assert result["sequence_created"] is False
    assert not any(sql.startswith("CREATE SEQUENCE") for sql in log)


def test_table_failure_after_sequence_reports_partial_state(plan):
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, fail_on="CREATE TABLE")),
    )
    assert result["status"] == "partial_failure"
    assert result["sequence_created"] is True
    assert result["target_created"] is False
    assert result["stage"] == "create_target"
    assert result["cleanup_attempted"] is False
    assert result["cleanup_requires_separate_approval"] is True
    assert not any("DROP" in sql.upper() for sql in log)


def test_privilege_failure_before_any_write_is_a_clean_block(plan):
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory([], handler_for(plan, fail_on="CREATE SEQUENCE")),
    )
    assert result["status"] == "blocked_ddl_privilege"
    assert result["target_created"] is False and result["sequence_created"] is False


def test_verification_mismatch_after_create_is_partial(plan):
    calls = {"n": 0}
    good = plan_catalog_rows(plan)

    def handler(sql, params):
        if "FROM ALL_TABLES" in sql:
            return [(0,)]
        if "FROM ALL_SEQUENCES" in sql:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            calls["n"] += 1
            return good[:-1]
        return []

    result = executor.execute_target_ddl(plan, approved=True, connection_factory=connection_factory([], handler))
    assert result["status"] == "partial_failure"
    assert result["stage"] == "verify_target"
    assert result["target_created"] is True and result["sequence_created"] is True


def test_failed_grant_is_counted_not_hidden(plan):
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory([], handler_for(plan, fail_on="TO ODI_EXEC")),
    )
    assert result["status"] == "created_grants_incomplete"
    assert result["grants_failed"] > 0


def test_connection_failure_is_reported_without_details(plan):
    def broken():
        raise oracle_error("ORA-12541: secret host detail")

    result = executor.execute_target_ddl(plan, approved=True, connection_factory=broken)
    assert result["status"] == "failed"
    assert result["connection_attempted"] is False
    assert "host detail" not in str(result)


def test_executor_source_has_no_drop_or_alter_path():
    source = inspect.getsource(executor).upper()
    assert "DROP TABLE" not in source and "DROP SEQUENCE" not in source
    assert "ALTER TABLE" not in source
