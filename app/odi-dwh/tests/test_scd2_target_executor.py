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


def handler_for(
    plan, *, table_exists=0, sequence_exists=0, catalog=None, fail_on=None, error="ORA-01031: x", grants="all", users=None
):
    users = users or {}
    rows = plan_catalog_rows(plan) if catalog is None else catalog

    def handler(sql, params):
        if fail_on and fail_on in sql:
            return oracle_error(error)
        if "FROM ALL_USERS" in sql:
            return [(users.get(params.get("u"), 1),)]
        if "FROM ALL_TABLES" in sql:
            return [(table_exists,)]
        if "FROM ALL_SEQUENCES" in sql:
            return [(sequence_exists,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return rows
        if "FROM ALL_TAB_PRIVS" in sql:
            if grants == "all":
                return [(g, p, t) for t, g, p in scd2_builder.required_grants(plan)]
            return list(grants)
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
        plan, approved=True, connection_factory=connection_factory(log, handler_for(plan, table_exists=1, sequence_exists=1))
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
        if "FROM ALL_USERS" in sql:
            return [(1,)]
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


def test_existing_table_with_missing_sequence_and_grants_is_repaired_without_touching_the_table(plan):
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, table_exists=1, grants=[])),
    )
    assert result["status"] == "repaired_incomplete_target"
    assert result["repaired"] == ["sequence", "grants"]
    writes = [sql for sql in log if sql.startswith(("CREATE", "GRANT", "DROP", "ALTER"))]
    assert writes[0].startswith("CREATE SEQUENCE")
    assert all(sql.startswith("GRANT") for sql in writes[1:]) and len(writes) == 6
    assert not any("CREATE TABLE" in sql or "DROP" in sql or "ALTER" in sql for sql in log)


def test_existing_table_only_missing_grants_are_granted(plan):
    present = [(g, p, t) for t, g, p in scd2_builder.required_grants(plan) if p != "DELETE"]
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, table_exists=1, sequence_exists=1, grants=present)),
    )
    assert result["status"] == "repaired_incomplete_target" and result["repaired"] == ["grants"]
    assert [sql for sql in log if sql.startswith("GRANT")] == [
        'GRANT DELETE ON "DM"."DIM_CUSTOMER" TO ODI_EXEC'
    ]


def test_existing_complete_target_needs_no_writes(plan):
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, table_exists=1, sequence_exists=1)),
    )
    assert result["status"] == "already_exists_matching" and result["repaired"] == []
    assert not any(sql.startswith(("CREATE", "GRANT")) for sql in log)


def test_existing_table_repair_failure_is_blocked_target_incomplete(plan):
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(
            [], handler_for(plan, table_exists=1, sequence_exists=1, grants=[], fail_on="GRANT ")
        ),
    )
    assert result["status"] == "blocked_target_incomplete" and result["missing"] == ["grants"]
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory([], handler_for(plan, table_exists=1, fail_on="CREATE SEQUENCE")),
    )
    assert result["status"] == "blocked_target_incomplete" and result["missing"] == ["sequence"]


def test_multibyte_national_types_use_char_length_not_data_length():
    # NVARCHAR2(50) has DATA_LENGTH 100 bytes; VARCHAR2(50 CHAR) has DATA_LENGTH 200.
    assert executor._full_type("NVARCHAR2", 100, None, None, 50, "C") == "NVARCHAR2(50)"
    assert executor._full_type("NCHAR", 20, None, None, 10, "C") == "NCHAR(10)"
    assert executor._full_type("VARCHAR2", 200, None, None, 50, "C") == "VARCHAR2(50 CHAR)"
    assert executor._full_type("VARCHAR2", 50, None, None, 50, "B") == "VARCHAR2(50 BYTE)"
    assert executor._full_type("VARCHAR2", 100, None, None, 50, "B") == "VARCHAR2(100 BYTE)"
    assert executor._full_type("CHAR", 4, None, None, 4, "B") == "CHAR(4 BYTE)"


def test_catalog_read_maps_multibyte_rows():
    rows = [
        ("A", "NVARCHAR2", 100, None, None, "N", 50, "C"),
        ("B", "VARCHAR2", 200, None, None, "Y", 50, "C"),
    ]

    def handler(sql, params):
        return rows

    columns = executor.fetch_source_columns("DWH", "T", connection_factory([], handler))
    assert [c["data_type"] for c in columns] == ["NVARCHAR2(50)", "VARCHAR2(50 CHAR)"]


def test_missing_exec_user_blocks_before_any_ddl_with_exact_message(plan):
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, users={"ODI_EXEC": 0})),
    )
    assert result["status"] == "blocked_grantee_missing"
    assert result["missing_grantees"] == ["ODI_EXEC"]
    assert "ODI_EXEC_USER" in result["message"] and "No DDL was executed" in result["message"]
    assert not any(sql.startswith(("CREATE", "GRANT", "DROP", "ALTER")) for sql in log)
    assert result["target_created"] is False and result["sequence_created"] is False


def test_missing_grantee_also_blocks_the_existing_table_repair_path(plan):
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, table_exists=1, users={"ODI_EXEC": 0})),
    )
    assert result["status"] == "blocked_grantee_missing"
    assert not any(sql.startswith(("CREATE", "GRANT")) for sql in log)


def test_configured_exec_user_is_used_for_grants_and_existence_check():
    custom = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS, exec_user="etl_runner")
    assert custom["options"]["exec_user"] == "ETL_RUNNER"
    log = []
    asked = []

    def handler(sql, params):
        if "FROM ALL_USERS" in sql:
            asked.append(params["u"])
            return [(1,)]
        if "FROM ALL_TABLES" in sql or "FROM ALL_SEQUENCES" in sql:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(custom)
        return []

    result = executor.execute_target_ddl(custom, approved=True, connection_factory=connection_factory(log, handler))
    assert result["status"] == "created" and asked == ["ETL_RUNNER"]
    grants = [sql for sql in log if sql.startswith("GRANT")]
    assert grants and all(sql.endswith("TO ETL_RUNNER") for sql in grants)
    assert not any("ODI_EXEC" in sql for sql in log)


def test_exec_user_must_be_a_valid_identifier():
    with pytest.raises(scd2_builder.Scd2PlanError):
        scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS, exec_user="X; DROP USER Y")


def test_default_exec_user_comes_from_config(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_USER", "CFG_EXEC")
    assert scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)["options"]["exec_user"] == "CFG_EXEC"


def test_grants_disabled_by_config_issue_no_grant_sql_and_no_user_check(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    log = []
    result = executor.execute_target_ddl(
        plan, approved=True, connection_factory=connection_factory(log, handler_for(plan, users={"ODI_EXEC": 0}))
    )
    assert result["status"] == "created"
    assert result["grants_skipped_by_config"] is True
    assert result["grants_applied"] == 0 and result["grants_failed"] == 0
    assert result["write_operations_attempted"] == ["create_sequence", "create_target"]
    assert not any(sql.startswith("GRANT") or "ALL_USERS" in sql for sql in log)
    assert any(sql.startswith("CREATE TABLE") for sql in log)


def test_grants_disabled_by_config_also_skips_repair_grants(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler_for(plan, table_exists=1, sequence_exists=1, grants=[])),
    )
    assert result["status"] == "already_exists_matching" and result["grants_skipped_by_config"] is True
    assert not any(sql.startswith("GRANT") or "ALL_USERS" in sql or "ALL_TAB_PRIVS" in sql for sql in log)


def test_plan_preview_reports_grants_skipped_by_config(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    assert scd2_builder.render_grant_statements(plan) == []
    assert scd2_builder.grant_skip_flags(plan) == {"grants_skipped_by_config": True}


def test_grantee_equal_to_owner_is_skipped_not_granted():
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS, exec_user="dm")
    log = []
    result = executor.execute_target_ddl(
        plan, approved=True, connection_factory=connection_factory(log, handler_for(plan))
    )
    assert result["status"] == "created"
    assert result["grants_skipped_owner_is_grantee"] is True
    assert "grants_skipped_by_config" not in result
    assert not any(sql.startswith("GRANT") for sql in log)
    assert not any("ALL_USERS" in sql for sql in log)


def test_default_enabled_has_no_skip_flags(plan):
    assert scd2_builder.grant_skip_flags(plan) == {}
    result = executor.execute_target_ddl(
        plan, approved=True, connection_factory=connection_factory([], handler_for(plan))
    )
    assert "grants_skipped_by_config" not in result and "grants_skipped_owner_is_grantee" not in result
