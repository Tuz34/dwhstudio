"""Actual operator wrappers with synthetic SQLite counts, no live ODI or DB."""
import json

import pytest

import dim_product_audit_role_repair as audit
import dim_product_stock_cutover as cutover
import odi_orchestrator
import remote_run_contract
import execution_identity
from tests.test_dim_product_scd2_stock_checkpoint import World
from tests.test_dim_product_odi_scd2_mapping_probe import _raw as mapping_report
from tests.test_dim_product_audit_role_repair import report_line, repaired_line, ALL_OVERWRITE


class Integration:
    def __init__(self):
        self.world = World()
        self.world.db.execute('DROP TABLE ODS_SH_PRODUCTS')
        self.world.db.execute('CREATE TABLE ODS_SH_PRODUCTS AS SELECT * FROM DIM_PRODUCT')
        self.world.db.commit()
        self.stable = False
        self.calls = []
        self.fault = None
        self.runs = 0

    def script(self, name, params):
        self.calls.append((name, params))
        if name == execution_identity.SCRIPT_NAME:
            payload = dict(tool=execution_identity.TOOL, mode=execution_identity.MODE,
                status="passed", side_effects=False, repository_connection_attempted=True,
                raw_rows_included=False, pk_values_included=False, secret_printed=False,
                model_count=1, context_count=1, mapping_count=1, data_server_count=1,
                model_code="TGT_DWH", context_code="GLOBAL", schema_name="DWH",
                technology="Oracle", data_server_technology="Oracle", username="SYNTHETIC_EXEC")
            if self.fault == "identity":
                payload["mapping_count"] = 2
            return json.dumps(payload)
        if name == audit.SCRIPT_NAME:
            action = params["odi.audit.action"]
            if action == "stable_preflight":
                return report_line(action=action, status="already_repaired" if self.stable else "ready_for_repair",
                    expression_profile=audit.STABLE_PROFILE_ID if self.stable else audit.VOLATILE_PROFILE_ID,
                    **({"before_image": dict(ALL_OVERWRITE)} if self.stable else {}))
            assert action == "stable_repair"
            self.stable = True
            return repaired_line(action=action, changed_expressions=list(audit.AUDIT_COLUMNS), expression_profile=audit.STABLE_PROFILE_ID)
        if name == "dim_product_scd2_mapping_probe.groovy":
            return json.dumps(mapping_report())
        if name == "generate_scenario_metadata.groovy":
            assert params["odi.scenario.version"] == "008" and params["odi.mapping"] == cutover.MAPPING
            if self.fault == "generator":
                return "SYNTHETIC_PRIVATE_EXCEPTION"
            return json.dumps(dict(tool="odi.generate_scenario_metadata", mode="scenario_metadata_generation",
                status="passed", side_effects=True, repository_connection_attempted=True,
                scenario={"name": cutover.SCENARIO, "version": "007" if self.fault == "version" else "008", "latest_version": "008"},
                checks=[{"name": "scenario_generated", "status": "pass"}, {"name": "forbidden_operations_not_called", "status": "pass"}]))
        assert name == "run_scenario_remote.groovy"
        assert params["odi.context"] == "GLOBAL" and params["odi.scenario.version"] == "008"
        self.runs += 1
        if self.fault == "unknown":
            return "SYNTHETIC_PRIVATE_EXCEPTION"
        if self.fault == "audit_change":
            self.world.db.execute("UPDATE DIM_PRODUCT SET UPDATE_DATE=999 WHERE PRODUCT_SK=0")
            self.world.db.commit()
        return json.dumps(remote_run_contract.result("run", status="passed", stage="completed", agent=True,
            repository=True, may_have_started=True, session_status="DONE"))


@pytest.fixture
def integration(monkeypatch):
    state = Integration()
    orchestrator = odi_orchestrator.OdiOrchestrator()
    monkeypatch.setattr(orchestrator, "run_groovy", state.script)
    monkeypatch.setattr(orchestrator, "_remote_configuration_ready", lambda: True)
    yield state, orchestrator
    state.world.db.close()


def run(state, orchestrator, directory, **overrides):
    args = dict(live=True, approvals=cutover.full_approvals(), reviews=dict.fromkeys(cutover.REVIEWS, "approved"),
                exclusive_operator_attested=True, receipt_dir=directory,
                connection_factory=state.world.factory, orchestrator=orchestrator)
    args.update(overrides)
    return cutover.execute_stock_cutover(**args)


@pytest.mark.parametrize("override", [{"live": False}, {"live": "yes"}, {"exclusive_operator_attested": 1},
    {"approvals": {}}, {"reviews": {}}, {"receipt_dir": "relative"}])
def test_gates_block_before_any_factory_or_repository_call(integration, tmp_path, override):
    state, orchestrator = integration
    result = run(state, orchestrator, tmp_path, **override)
    assert result["status"] == "blocked" and state.world.log == [] and state.calls == []


def test_real_wrappers_prepare_fresh_008_and_two_verified_runs(integration, tmp_path):
    state, orchestrator = integration
    result = run(state, orchestrator, tmp_path)
    assert result["status"] == "passed" and result["rerun_full_snapshot_equal"] is True
    assert result["business_parity_proven"] is True and result["physical_dml_count_proven"] is False
    assert state.runs == 2
    assert not result["outcome_unknown"] and not result["manual_review_required"]
    assert (tmp_path / audit.RECEIPT_NAME).is_file()
    assert all('SELECT' not in path.read_text() for path in tmp_path.glob('*.json'))


@pytest.mark.parametrize("fault", ["generator", "version", "unknown", "audit_change"])
def test_failed_or_uncertain_writes_stop_and_require_manual_review(integration, tmp_path, fault):
    state, orchestrator = integration
    state.fault = fault
    report = run(state, orchestrator, tmp_path)
    assert report["status"] != "passed" and report["manual_review_required"] is True
    assert state.runs <= 1
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report)
    assert not report["rerun_full_snapshot_equal"]


def test_business_difference_is_counted_before_repository_mutation(integration, tmp_path):
    state, orchestrator = integration
    state.world.db.execute("UPDATE ODS_SH_PRODUCTS SET PROD_NAME='DIFFERENT' WHERE PROD_ID=0")
    state.world.db.commit()
    report = run(state, orchestrator, tmp_path)
    assert report["status"] != "passed" and [name for name, _ in state.calls] == [execution_identity.SCRIPT_NAME]
    assert "DIFFERENT" not in json.dumps(report)


def test_existing_stable_profile_does_not_need_a_new_repair_receipt(integration, tmp_path):
    state, orchestrator = integration
    state.stable = True
    (tmp_path / audit.RECEIPT_NAME).write_text("retained receipt; must not be overwritten")
    result = run(state, orchestrator, tmp_path)
    assert result["status"] == "passed"
    assert not any(params.get("odi.audit.action") == "stable_repair" for _, params in state.calls)


def test_configured_adapter_must_gate_before_constructing_orchestrator(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(odi_orchestrator.OdiOrchestrator, "__init__", lambda self: calls.append(1))
    result = cutover.run_configured_stock_cutover(live=True, approvals={}, reviews={}, receipt_dir=tmp_path)
    assert result["status"] == "blocked" and calls == []


def test_unproven_execution_identity_blocks_before_any_database_connection(integration, tmp_path):
    state, orchestrator = integration
    state.fault = "identity"
    result = run(state, orchestrator, tmp_path)
    assert result["status"] == "failed" and state.world.log == []
    assert "SYNTHETIC_EXEC" not in json.dumps(result)


@pytest.mark.parametrize("scope", cutover.SCOPES)
@pytest.mark.parametrize("value", [False, "yes", 1])
def test_each_distinct_scope_requires_literal_true_before_callbacks(integration, tmp_path, scope, value):
    state, orchestrator = integration
    approvals = cutover.full_approvals()
    approvals[scope] = value
    assert run(state, orchestrator, tmp_path, approvals=approvals)["status"] == "blocked"
    assert state.calls == [] and state.world.log == []


@pytest.mark.parametrize("name", cutover.REVIEWS)
def test_all_reviews_precede_callbacks(integration, tmp_path, name):
    state, orchestrator = integration
    reviews = dict.fromkeys(cutover.REVIEWS, "approved")
    reviews[name] = "pending"
    assert run(state, orchestrator, tmp_path, reviews=reviews)["status"] == "blocked"
    assert state.calls == [] and state.world.log == []


def test_business_proof_is_read_only_aggregate_only_and_cleanup_sensitive(integration):
    state, _ = integration
    report = cutover.business_proof(state.world.factory)
    assert report["status"] == "passed" and report["counts"]["source_count"] == 72
    assert state.world.log[0] == "SET TRANSACTION READ ONLY"
    assert all(sql == "SET TRANSACTION READ ONLY" or sql.startswith("SELECT COUNT(*)") for sql in state.world.log)
    state.world.fail = "connection_close"
    report = cutover.business_proof(state.world.factory)
    assert report["status"] == "failed" and report["counts"] == {}
