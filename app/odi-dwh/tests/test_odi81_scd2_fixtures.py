"""Offline F4/F5 source, backup, and SCD2 witness tests."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import odi81_isolated_fixtures as isolated
import odi81_scd2_fixtures as fixture
import scd2_builder
from tests.odi81_harness_support import Database

BINDINGS = json.dumps({"GLOBAL": {"DWH": "TGT_DWH", "DM": "TGT_DM"}})
ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("FIXTURE_A", "FIXTURE_B", "LEAK", "SELECT", "password", "101")


@pytest.fixture
def db():
    database = Database()
    yield database
    database.close()


def writers(db, flow="F4"):
    spec = fixture.FLOWS[flow]
    return {
        spec["source_owner"]: db.factory("writer", spec["source_owner"]),
        spec["target_owner"]: db.factory("dm" if spec["target_owner"] == "DM" else "writer", spec["target_owner"]),
    }


def readers(db, flow="F4"):
    spec = fixture.FLOWS[flow]
    return {spec["target_owner"]: db.factory("catalog", spec["target_owner"])}


def source_reader(db):
    return db.factory("source", "APP")


def prepare(db, flow="F4", **kwargs):
    return fixture.prepare_source(
        flow, writers(db, flow), source_reader(db), approved=True,
        approval_scope=f"odi81_{flow.lower()}_prepare_source", context="GLOBAL",
        source_model_bindings=BINDINGS, **kwargs,
    )


def mutate(db, flow="F4"):
    return fixture.mutate_source(
        flow, writers(db, flow), source_reader(db), approved=True,
        approval_scope=f"odi81_{flow.lower()}_mutate_source", context="GLOBAL",
        source_model_bindings=BINDINGS,
    )


def backup(db, flow="F4", kind="target", run=1):
    action = "backup_source" if kind == "source" else f"backup_target_r{run}"
    return fixture.create_backup(
        flow, kind, run, writers(db, flow), approved=True,
        approval_scope=f"odi81_{flow.lower()}_{action}", context="GLOBAL",
        source_model_bindings=BINDINGS,
    )


def verify(db, flow="F4", run=1, phase="after_run"):
    kind = "before" if phase == "before_run" else "after"
    return fixture.verify_target(
        flow, run, phase, readers(db, flow), source_reader(db), approved=True,
        approval_scope=f"odi81_{flow.lower()}_verify_{kind}_r{run}", context="GLOBAL",
        source_model_bindings=BINDINGS,
    )


def redacted(report):
    blob = json.dumps(report)
    for token in FORBIDDEN:
        assert token not in blob, token
    assert report["raw_rows_included"] is False
    assert report["pk_values_included"] is False
    assert report["secret_printed"] is False
    assert report["physical_dml_count_proven"] is False


def test_frozen_manifest_and_surrogate_names():
    assert fixture.SK["F4"] == "ODI81_F4_SK"
    assert fixture.SK["F5"] == "ODI81_F5_SK"
    assert fixture.SK["F4"] == scd2_builder.surrogate_key_name("DIM_ODI81_F4", fixture.BUSINESS)
    assert fixture.SK["F5"] == scd2_builder.surrogate_key_name("DIM_ODI81_F5", fixture.BUSINESS)
    assert fixture.FLOWS["F4"]["source_table"] == "ODS_ODI81_F4"
    assert fixture.FLOWS["F4"]["target_table"] == "DIM_ODI81_F4"
    assert fixture.FLOWS["F5"]["source_table"] == "DWH_ODI81_F5"
    assert fixture.FLOWS["F5"]["target_owner"] == "DM"
    assert fixture.FLOWS["F5"]["target_table"] == "DIM_ODI81_F5"
    assert len(fixture.target_catalog("F4")) == 11
    assert fixture.SOURCE_CATALOG[0] == (1, "ENTITY_ID", "NUMBER", 22, 10, 0, None, None, "N")
    assert fixture.SOURCE_CATALOG[1][1:8] == ("ATTR_TEXT", "VARCHAR2", 40, None, None, "B", 40)
    assert fixture.target_catalog("F4")[0][1] == "ODI81_F4_SK"
    update_date = next(row for row in fixture.target_catalog("F4") if row[1] == "UPDATE_DATE")
    assert update_date[2] == "TIMESTAMP(6)"
    assert update_date[8] == "Y"
    assert "PRIMARY KEY (ENTITY_ID)" in fixture.source_create_sql("F4")
    assert "CONSTRAINT" not in fixture.backup_create_sql("F5", "target", 1)
    assert 'NOT NULL' in fixture.backup_create_sql("F4", "target", 1)
    assert '"UPDATE_DATE" TIMESTAMP,' in fixture.backup_create_sql("F4", "target", 1) or (
        '"UPDATE_DATE" TIMESTAMP)' in fixture.backup_create_sql("F4", "target", 1)
    )
    for flow, (source, target) in isolated._FLOWS.items():
        assert source.startswith("ODI81_SRC_")
        assert flow in {"F1", "F2", "F3"}


def test_new_modules_do_not_add_a_broad_exception_handler():
    banned = {"socket", "urllib", "requests", "oracledb", "config", "dotenv", "db", "app"}
    for name in ("odi81_scd2_fixtures.py", "odi81_app_acceptance.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None
                assert not (
                    isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
                )
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in banned for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned


@pytest.mark.parametrize("approved", [False, None, 1, "true"])
@pytest.mark.parametrize("action", ["prepare_source", "mutate_source", "create_backup", "verify_target"])
def test_bad_approval_does_not_call_factory(db, approved, action):
    scope = {
        "prepare_source": "odi81_f4_prepare_source",
        "mutate_source": "odi81_f4_mutate_source",
        "create_backup": "odi81_f4_backup_target_r1",
        "verify_target": "odi81_f4_verify_after_r1",
    }[action]
    if action == "prepare_source":
        result = fixture.prepare_source(
            "F4", writers(db), source_reader(db), approved=approved, approval_scope=scope,
            context="GLOBAL", source_model_bindings=BINDINGS,
        )
    elif action == "mutate_source":
        result = fixture.mutate_source(
            "F4", writers(db), source_reader(db), approved=approved, approval_scope=scope,
            context="GLOBAL", source_model_bindings=BINDINGS,
        )
    elif action == "create_backup":
        result = fixture.create_backup(
            "F4", "target", 1, writers(db), approved=approved, approval_scope=scope,
            context="GLOBAL", source_model_bindings=BINDINGS,
        )
    else:
        result = fixture.verify_target(
            "F4", 1, "after_run", readers(db), source_reader(db), approved=approved, approval_scope=scope,
            context="GLOBAL", source_model_bindings=BINDINGS,
        )
    assert result["status"] == "blocked"
    assert db.events == []
    redacted(result)


@pytest.mark.parametrize("scope", ["odi81_f5_prepare_source", "odi81_f4_mutate_source", None])
def test_cross_scope_blocks_before_factory(db, scope):
    result = fixture.prepare_source(
        "F4", writers(db), source_reader(db), approved=True, approval_scope=scope,
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["error_code"] == "ApprovalScopeMismatch"
    assert db.events == []


@pytest.mark.parametrize("flow", ["F1", "F6", "f4", "DWH", None])
def test_identifier_redirect_never_reaches_factory(db, flow):
    result = fixture.prepare_source(
        flow, writers(db), source_reader(db), approved=True, approval_scope="odi81_f4_prepare_source",
        context="GLOBAL", source_model_bindings=BINDINGS,
    )
    assert result["status"] == "blocked" and db.events == []


@pytest.mark.parametrize("bindings,context,code", [
    ("{", "GLOBAL", "source_model_config_invalid"),
    (json.dumps({"DEV": {"DWH": "TGT_DWH", "DM": "TGT_DM"}}), "GLOBAL", "SourceModelUnbound"),
    (json.dumps({"GLOBAL": {"DWH": "SRC_HR", "DM": "TGT_DM"}}), "GLOBAL", "source_model_disagrees_with_app"),
])
def test_model_binding_blocks_before_factory(db, bindings, context, code):
    result = fixture.prepare_source(
        "F4", writers(db), source_reader(db), approved=True, approval_scope="odi81_f4_prepare_source",
        context=context, source_model_bindings=bindings,
    )
    assert result["error_code"] == code
    assert db.events == []


@pytest.mark.parametrize("flow", ["F4", "F5"])
def test_prepare_seeds_three_rows_and_quotes_only_frozen_names(db, flow):
    result = prepare(db, flow)
    assert result["status"] == "passed", result
    assert result["counts"]["rows"] == 3
    assert result["source_reader_verified"] is True
    assert result["ddl_attempted"] is True and result["object_may_exist"] is True
    assert result["commit_completed"] is True and result["created"] is True
    spec = fixture.FLOWS[flow]
    allowed = {
        spec["source_owner"], spec["source_table"], *fixture.BUSINESS, f"PK_{spec['source_table']}",
    }
    saw_readonly = False
    for role, sql in db.events:
        if role == "source" and sql == "SET TRANSACTION READ ONLY":
            saw_readonly = True
        if not isinstance(sql, str) or sql in {"connect", "commit", "rollback", "close", "cursor_close"}:
            continue
        for name in re.findall(r'"([A-Z0-9_$#]+)"', sql):
            assert name in allowed
        assert "DWH\".\"DIM_" not in sql
    assert saw_readonly
    assert prepare(db, flow)["error_code"] == "SourceAlreadyExists"
    redacted(result)


def test_writer_select_any_is_not_a_fallback(db):
    db.select_any = True
    result = fixture.prepare_source(
        "F4", {"DWH": db.factory("writer", "APP"), "DM": db.factory("dm", "DM")}, source_reader(db),
        approved=True, approval_scope="odi81_f4_prepare_source", context="GLOBAL",
        source_model_bindings=BINDINGS,
    )
    assert result["error_code"] == "WriterOwnerMismatch"
    assert not any(isinstance(sql, str) and sql.startswith("CREATE") for _role, sql in db.events)
    redacted(result)


def test_reader_without_access_blocks_before_create(db):
    db.select_any = False
    result = prepare(db)
    assert result["error_code"] == "SourceReaderCannotReadNewFixture"
    assert not any(isinstance(sql, str) and sql.startswith("CREATE") for _role, sql in db.events)


def test_commit_failure_is_unknown_not_a_false_no_effect(db):
    db.fail_commit = True
    result = prepare(db)
    assert result["status"] == "failed"
    assert result["commit_attempted"] is True and result["commit_completed"] is False
    assert result["outcome_unknown"] is True and result["manual_review_required"] is True
    redacted(result)


def test_cleanup_failure_is_redacted_and_continues(db):
    db.fail_cleanup = "cursor"
    result = prepare(db)
    assert result["error_code"] == "CleanupFailed"
    assert result["cursor_close_failed"] is True
    assert any(event == "close" for _role, event in db.events)
    redacted(result)


def test_process_control_still_closes_remaining_resources(db):
    db.fail_cleanup = "interrupt"
    db.interrupt_role = "writer"
    with pytest.raises(KeyboardInterrupt):
        prepare(db)
    assert ("writer", "cursor_close") in db.events
    assert ("writer", "close") in db.events


def test_source_backup_and_mutate_are_one_update_and_one_insert(db):
    assert prepare(db)["status"] == "passed"
    db.install_target("F4", "seed")
    created = backup(db, kind="source", run=2)
    assert created["status"] == "passed" and created["created"] is True
    again = backup(db, kind="source", run=2)
    assert again["preexisting"] is True and again["created"] is False
    assert sum(isinstance(sql, str) and sql.startswith("CREATE") for _role, sql in db.events) == 2
    result = mutate(db)
    assert result["status"] == "passed", result
    assert result["counts"]["rows"] == 4
    assert result["counts"]["updated"] == 1 and result["counts"]["inserted"] == 1
    redacted(result)


def test_retained_backup_mismatch_is_not_replaced(db):
    assert prepare(db)["status"] == "passed"
    assert backup(db, kind="source", run=2)["status"] == "passed"
    db.db.execute('UPDATE "ODS_ODI81_F4" SET "ATTR_TEXT"=?', ("OTHER",))
    result = backup(db, kind="source", run=2)
    assert result["status"] != "passed"
    assert result["error_code"] in {"InvalidCheckpoint", "FixtureStateMismatch", "BackupMetadataMismatch"}
    assert not any(isinstance(sql, str) and any(token in sql for token in ("DROP ", "TRUNCATE", "ALTER "))
                   for _role, sql in db.events)
    redacted(result)


def test_f5_target_backup_uses_dm_writer(db):
    db.install_target("F5", "empty")
    result = backup(db, "F5", "target", 1)
    assert result["status"] == "passed", result
    sql = "\n".join(item for _role, item in db.events if isinstance(item, str))
    assert '"DM"."DIM_ODI81_F5"' in sql
    assert '"DWH"."DIM_ODI81_F5"' not in sql
    assert any(role == "dm" and item == "connect" for role, item in db.events)


def _ready(db, flow="F4", run=3, phase="after_run", target="changed", backup="changed", source="changed"):
    db.install_source(flow, source)
    db.install_target(flow, target)
    db.install_backup(flow, "target", run, backup)


def test_before_run_match_is_not_an_after_run_witness(db):
    _ready(db, run=1, phase="before_run", target="empty", backup="empty", source="seed")
    result = verify(db, run=1, phase="before_run")
    assert result["status"] == "passed", result
    assert result["full_target_snapshot_equal"] is False
    assert result["rerun_semantics"] is None
    assert result["logical_business_comparison_only"] is True
    assert result["counts"]["current_rows"] == 0
    assert result["counts"]["target_rows"] == 0
    redacted(result)


@pytest.mark.parametrize("run,current,historical,total", [(1, 3, 0, 3), (2, 4, 1, 5)])
def test_after_run_counts_and_business_parity(db, run, current, historical, total):
    state = "seed" if run == 1 else "changed"
    _ready(db, run=run, target=state, backup="empty" if run == 1 else "seed", source=state)
    result = verify(db, run=run, phase="after_run")
    assert result["status"] == "passed", result
    assert result["counts"]["current_rows"] == current
    assert result["counts"]["historical_rows"] == historical
    assert result["counts"]["target_rows"] == total
    assert result["full_target_snapshot_equal"] is False
    assert result["counts"]["current_minus_source"] == 0
    redacted(result)


def test_run3_full_snapshot_witness(db):
    _ready(db)
    db.install_backup("F4", "target", 2, "seed")
    result = verify(db, run=3, phase="after_run")
    assert result["status"] == "passed", result
    assert result["full_target_snapshot_equal"] is True
    assert result["rerun_semantics"] == "incremental"
    assert result["logical_business_comparison_only"] is False
    assert result["physical_dml_count_proven"] is False
    redacted(result)


def test_metadata_drift_fails_closed(db):
    _ready(db)
    db.db.execute(
        "UPDATE ALL_TAB_COLUMNS SET DATA_LENGTH=1 WHERE TABLE_NAME='DIM_ODI81_F4' AND COLUMN_NAME='ATTR_TEXT'"
    )
    result = verify(db, run=3)
    assert result["error_code"] == "ColumnContractMismatch"
    assert result["full_target_snapshot_equal"] is False
    redacted(result)


def test_null_audit_is_not_a_witness(db):
    _ready(db)
    db.db.execute('UPDATE "DIM_ODI81_F4" SET "UPDATE_DATE"=NULL WHERE "ODI81_F4_SK"=1')
    result = verify(db, run=3)
    assert result["error_code"] == "NullAuditWitness"
    assert result["full_target_snapshot_equal"] is False
    redacted(result)


def test_same_count_wrong_business_rows_fail(db):
    _ready(db)
    db.db.execute('UPDATE "DIM_ODI81_F4" SET "ATTR_TEXT"=? WHERE "ODI81_F4_SK"=2', ("OTHER",))
    result = verify(db, run=3)
    assert result["error_code"] == "BusinessParityMismatch"
    assert result["full_target_snapshot_equal"] is False
    redacted(result)


def test_duplicate_surrogate_fails(db):
    _ready(db)
    db.db.execute('UPDATE "DIM_ODI81_F4" SET "ODI81_F4_SK"=1 WHERE "ODI81_F4_SK"=2')
    result = verify(db, run=3)
    assert result["error_code"] == "DuplicateSurrogate"
    assert result["full_target_snapshot_equal"] is False


def test_duplicate_natural_key_fails(db):
    _ready(db)
    row = db.db.execute('SELECT * FROM "DIM_ODI81_F4" WHERE "ODI81_F4_SK"=2').fetchone()
    values = (9, *row[1:])
    placeholders = ",".join("?" for _ in values)
    db.db.execute(f'INSERT INTO "DIM_ODI81_F4" VALUES ({placeholders})', values)
    result = verify(db, run=3)
    assert result["error_code"] == "DuplicateNaturalKey"
    assert result["full_target_snapshot_equal"] is False


def test_history_mismatch_fails(db):
    _ready(db)
    db.db.execute(
        'UPDATE "DIM_ODI81_F4" SET "EFFECTIVE_TO"=? WHERE "CURRENT_FLAG"=? AND "ENTITY_ID"=101',
        ("2024-07-01", "0"),
    )
    result = verify(db, run=3)
    assert result["error_code"] == "HistoryMismatch"
    assert result["full_target_snapshot_equal"] is False


def test_invalid_flag_and_date_range_fail(db):
    _ready(db)
    db.db.execute('UPDATE "DIM_ODI81_F4" SET "CURRENT_FLAG"=? WHERE "ODI81_F4_SK"=2', ("Y",))
    assert verify(db, run=3)["error_code"] == "CurrentFlagInvalid"
    db.db.execute('UPDATE "DIM_ODI81_F4" SET "CURRENT_FLAG"=? WHERE "ODI81_F4_SK"=2', ("1",))
    db.db.execute(
        'UPDATE "DIM_ODI81_F4" SET "EFFECTIVE_TO"=? WHERE "ODI81_F4_SK"=2',
        ("2099-01-01",),
    )
    result = verify(db, run=3)
    assert result["error_code"] == "DateRangeInvalid"
    assert result["full_target_snapshot_equal"] is False
    redacted(result)


def test_stale_backup_snapshot_is_not_a_pass(db):
    _ready(db, backup="changed")
    db.install_backup("F4", "target", 2, "seed")
    db.db.execute('UPDATE "DIM_ODI81_F4" SET "UPDATE_DATE"=? WHERE "ODI81_F4_SK"=2', ("2024-07-01T00:00:00",))
    result = verify(db, run=3)
    assert result["status"] == "failed"
    assert result["error_code"] == "SnapshotMismatch"
    assert result["full_target_snapshot_equal"] is False
    assert result["rerun_semantics"] is None
    redacted(result)


def test_cleanup_failure_clears_a_snapshot_witness(db):
    _ready(db)
    db.fail_cleanup = "cursor"
    db.fail_cleanup_role = "catalog"
    result = verify(db, run=3)
    assert result["error_code"] == "CleanupFailed"
    assert result["full_target_snapshot_equal"] is False
    assert result["logical_business_comparison_only"] is False
    assert result["rerun_semantics"] is None
    redacted(result)


def test_read_failure_stops_without_leaking(db):
    _ready(db)
    db.fail_sql = "ALL_TAB_COLUMNS"
    result = verify(db, run=3)
    assert result["status"] == "failed"
    assert result["error_code"] == "OperationFailed"
    assert result["full_target_snapshot_equal"] is False
    redacted(result)
