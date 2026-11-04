from __future__ import annotations

import json
from pathlib import Path

import mapping_state_manager
import mapping_snapshot_manager
from audit_schema_contract import AUDIT_SCHEMA_TABLES, build_audit_schema_preflight_plan


ROOT = Path(__file__).resolve().parents[1]
DDL_ROOT = ROOT / "ddl"
CANONICAL = DDL_ROOT / "audit_schema_contract.sql"
LEGACY_DDL = (
    DDL_ROOT / "init_audit_infra.sql",
    DDL_ROOT / "faz_1_2_3_objects.sql",
    DDL_ROOT / "faz_5_langgraph_state.sql",
)


def test_audit_schema_has_one_fail_closed_canonical_contract():
    sql = CANONICAL.read_text(encoding="utf-8").upper()

    for table in (
        "ALERT_LOG",
        "MAPPING_LOG",
        "DATA_LINEAGE",
        "MAPPING_SNAPSHOT",
        "MAPPING_RUN_STATE",
    ):
        assert f"CREATE TABLE AUDIT_USER.{table}" in sql
        assert f"ASSERT_COLUMN('{table}'" in sql

    assert "CREATED_AT" in sql
    assert "OP_DATE" not in sql
    assert "SQLERRM" not in sql
    assert "ERROR EXECUTING" not in sql
    assert sql.count("AUDIT_SCHEMA_CONTRACT_READY") == 1
    assert "RAISE;" in sql


def test_legacy_audit_ddl_files_fail_closed_and_do_not_redefine_objects():
    for path in LEGACY_DDL:
        text = path.read_text(encoding="utf-8").upper()
        assert "CREATE TABLE" not in text, path.name
        assert "RAISE_APPLICATION_ERROR" in text, path.name
        assert "AUDIT_SCHEMA_CONTRACT.SQL" in text, path.name

    setup_source = (ROOT / "setup_oracle_users.py").read_text(encoding="utf-8").upper()
    assert "CREATE TABLE AUDIT_USER.DATA_LINEAGE" not in setup_source


class _MetadataCursor:
    def __init__(self, exists=0):
        self.exists = exists
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (self.exists,)

    def close(self):
        return None


class _MetadataConnection:
    def __init__(self, exists=0):
        self.cursor_obj = _MetadataCursor(exists)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        raise AssertionError("metadata-only table checks must not commit")


def test_runtime_table_checks_never_attempt_ddl(monkeypatch):
    state_connection = _MetadataConnection(exists=0)
    state_manager = mapping_state_manager.MappingStateManager()
    mapping_state_manager.MappingStateManager._table_ensured = False
    monkeypatch.setattr(state_manager, "_get_conn", lambda: state_connection)

    snapshot_connection = _MetadataConnection(exists=0)
    snapshot_manager = mapping_snapshot_manager.MappingSnapshotManager()
    monkeypatch.setattr(snapshot_manager, "_get_conn", lambda: snapshot_connection)

    for manager, connection in (
        (state_manager, state_connection),
        (snapshot_manager, snapshot_connection),
    ):
        try:
            manager.ensure_table()
        except RuntimeError as exc:
            assert "audit schema" in str(exc).lower()
        else:
            raise AssertionError("missing canonical audit tables must fail closed")
        executed_sql = "\n".join(sql for sql, _params in connection.cursor_obj.executed).upper()
        assert "CREATE TABLE" not in executed_sql
        assert "CREATE INDEX" not in executed_sql


def test_persisted_mapping_state_is_allowlisted_and_redacted():
    serialized = mapping_state_manager._state_json({
        "batch_id": "RUN-001",
        "mapping_name": "MAP_STATUS",
        "current_step": "run_scenario",
        "overall_status": "RUN_FAILED",
        "completed_steps": ["parse", "validate"],
        "scenario_name": "MAP_STATUS_SCN",
        "scenario_version": "001",
        "context": "GLOBAL",
        "truncate_target_table": False,
        "validated_params": {
            "src_schema": "HR",
            "src_table": "STATUS",
            "tgt_owner": "DWH",
            "tgt_table": "ODS_STATUS",
            "prompt": "must not be persisted",
        },
        "raw_params": {"raw_rows": ["must not be persisted"]},
        "error": "RuntimeError password=blocked-test-value jdbc:oracle:thin:@blocked/service raw detail",
        "result_rows": [{"STATUS_CODE": "must not be persisted"}],
    })
    state = json.loads(serialized)

    assert set(state) <= mapping_state_manager.PERSISTED_STATE_FIELDS
    assert state["validated_params"] == {
        "src_schema": "HR",
        "src_table": "STATUS",
        "tgt_owner": "DWH",
        "tgt_table": "ODS_STATUS",
    }
    assert state["error"] == "Operation failed. RuntimeError"
    assert "raw_params" not in state
    assert "result_rows" not in state
    assert "blocked-test-value" not in serialized
    assert "jdbc:oracle" not in serialized.lower()
    assert "must not be persisted" not in serialized


def test_audit_schema_preflight_plan_is_static_and_metadata_only():
    plan = build_audit_schema_preflight_plan()

    assert set(plan["required_tables"]) == set(AUDIT_SCHEMA_TABLES)
    assert plan["connection_attempted"] is False
    assert plan["write_operations_attempted"] == []
    assert plan["raw_rows_included"] is False
    assert plan["pk_values_included"] is False
    assert plan["live_proven"] is False
    for query in plan["queries"].values():
        assert query.lstrip().upper().startswith("SELECT ")
        assert not any(token in query.upper() for token in ("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "CREATE ", "ALTER ", "DROP "))


def test_mapping_generator_audit_log_persists_metadata_and_fixed_errors_only():
    source = (ROOT / "mapping_generator.py").read_text(encoding="utf-8")

    assert "audit_metadata = {" in source
    assert "ddl_text=json.dumps(audit_metadata" in source
    assert 'error_message="Mapping operation failed." if error_message else None' in source
    assert "error_message=error_message" not in source
