import logging
from pathlib import Path

import odi_orchestrator
from odi_orchestrator import OdiOrchestrator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GROOVY_ROOT = PROJECT_ROOT / "odi_automation"


class _CompletedProcess:
    def __init__(self, *, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_generate_mapping_transports_strict_replace_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )

    OdiOrchestrator().generate_mapping(
        "MAP_X",
        "ORACLE_SH",
        "PRODUCTS",
        "ORACLE_DWH",
        "ODS_SH_PRODUCTS",
        replace_existing_mapping=False,
    )

    assert calls[0][0] == "create_mapping.groovy"
    assert calls[0][1]["odi.replace_existing_mapping"] == "false"


def test_generate_mapping_transports_required_update_key_columns(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OdiOrchestrator,
        "run_groovy",
        lambda _self, script, params: calls.append((script, params)) or "SUCCESS",
    )

    OdiOrchestrator().generate_mapping(
        "MAP_SH_PRODUCTS_TO_ODS",
        "SRC_SH",
        "PRODUCTS",
        "TGT_DWH",
        "ODS_SH_PRODUCTS",
        required_update_key_columns=["PROD_ID"],
    )

    assert calls[0][1]["odi.required_update_key_columns"] == "PROD_ID"


def _prepare_runtime(monkeypatch, completed_process):
    monkeypatch.setattr(odi_orchestrator, "_JAVA_BIN", r"C:\Java\bin\java.exe")
    monkeypatch.setattr(odi_orchestrator.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        odi_orchestrator,
        "_ensure_local_props_file",
        lambda: r"D:\safe\odi_connection.local.properties",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed_process

    monkeypatch.setattr(odi_orchestrator.subprocess, "run", fake_run)
    return calls


def test_registry_covers_every_allowlisted_script_with_explicit_policy():
    scripts = {path.name for path in GROOVY_ROOT.glob("*.groovy")}

    assert set(odi_orchestrator.GROOVY_ACTION_REGISTRY) == scripts
    assert odi_orchestrator._ALLOWED_SCRIPTS == frozenset(scripts)
    assert {
        spec.category for spec in odi_orchestrator.GROOVY_ACTION_REGISTRY.values()
    } == {
        "read_only",
        "repository_write",
        "scenario_run",
        "load_plan_run",
        "topology_write",
        "credential_change",
    }
    for spec in odi_orchestrator.GROOVY_ACTION_REGISTRY.values():
        assert spec.approval_required is (spec.category != "read_only")
        assert spec.failure_contract == "fixed_status_action_error_type"
        assert spec.output_policy == "central_redaction_before_log_parse_return"


def test_persistent_script_is_blocked_at_common_boundary_without_approval(monkeypatch):
    calls = _prepare_runtime(monkeypatch, _CompletedProcess(stdout="SUCCESS"))

    result = OdiOrchestrator().run_groovy("create_mapping.groovy", {})

    assert result == "Hata: ODI Groovy action blocked. error_type=ApprovalRequired"
    assert calls == []


def test_truthy_non_boolean_approval_is_rejected(monkeypatch):
    calls = _prepare_runtime(monkeypatch, _CompletedProcess(stdout="SUCCESS"))

    with odi_orchestrator.groovy_approval_context("yes"):
        result = OdiOrchestrator().run_groovy("create_mapping.groovy", {})

    assert result.endswith("error_type=ApprovalRequired")
    assert calls == []


def test_approved_context_allows_persistent_script_without_exposing_command(monkeypatch, caplog):
    calls = _prepare_runtime(monkeypatch, _CompletedProcess(stdout="SUCCESS: completed"))
    caplog.set_level(logging.INFO, logger="odi_orchestrator")

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy("create_mapping.groovy", {"odi.mapping": "MAP_X"})

    assert result == "SUCCESS: completed"
    assert len(calls) == 1
    assert "-Dodi.mapping=MAP_X" in calls[0][0]
    assert "-Dodi.mapping=MAP_X" not in caplog.text
    assert "param_keys=odi.mapping" in caplog.text


def test_secret_like_system_property_is_rejected_before_process_launch(monkeypatch):
    calls = _prepare_runtime(monkeypatch, _CompletedProcess(stdout="SUCCESS"))

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy(
            "update_topology_pass.groovy",
            {"odi.new_password": "not-a-real-secret"},
        )

    assert result == "Hata: ODI Groovy action blocked. error_type=SecretTransportRejected"
    assert calls == []


def test_stdout_and_stderr_are_redacted_before_logging_or_return(monkeypatch, caplog):
    completed = _CompletedProcess(
        stdout=(
            "status=passed password=hidden-value jdbc:oracle:thin:@internal-host "
            "Authorization: Bearer private-bearer"
        ),
        stderr="user=private-user token=private-token dsn=private-dsn",
    )
    _prepare_runtime(monkeypatch, completed)
    caplog.set_level(logging.INFO, logger="odi_orchestrator")

    result = OdiOrchestrator().run_groovy("read_only_probe.groovy", {})

    combined = result + caplog.text
    assert "hidden-value" not in combined
    assert "internal-host" not in combined
    assert "private-user" not in combined
    assert "private-token" not in combined
    assert "private-bearer" not in combined
    assert "private-dsn" not in combined
    assert "status=passed" in result


def test_zero_exit_legacy_error_marker_fails_closed_without_returning_body(monkeypatch):
    _prepare_runtime(
        monkeypatch,
        _CompletedProcess(stdout="ERROR: password=hidden-value jdbc:oracle:thin:@internal-host"),
    )

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy("add_column.groovy", {})

    assert result == "Hata: ODI Groovy action failed. error_type=GroovyFatalOutput"
    assert "hidden-value" not in result
    assert "internal-host" not in result


def test_strict_normalized_writer_preserves_only_fixed_structured_error(monkeypatch):
    safe_error = (
        '{"tool":"odi.dim_product_scd2_ikm_import","status":"error",'
        '"mode":"fixed_sdk_reference_absent_only_ikm_import",'
        '"side_effects":false,"repository_connection_attempted":true,'
        '"raw_rows_included":false,"pk_values_included":false,'
        '"secret_printed":false,"error_type":"IllegalStateException",'
        '"import_attempted":false,"repository_state_may_have_changed":false}'
    )
    _prepare_runtime(monkeypatch, _CompletedProcess(stdout=safe_error))

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy(
            "dim_product_scd2_ikm_import.groovy",
            {"odi.oracle_home": r"C:\Oracle\Middleware\Oracle_Home"},
        )

    assert result == safe_error


def test_strict_normalized_writer_rejects_error_detail_or_extra_fields(monkeypatch):
    unsafe_error = (
        '{"tool":"odi.dim_product_scd2_ikm_import","status":"error",'
        '"mode":"fixed_sdk_reference_absent_only_ikm_import",'
        '"side_effects":false,"repository_connection_attempted":true,'
        '"raw_rows_included":false,"pk_values_included":false,'
        '"secret_printed":false,"error_type":"IllegalStateException",'
        '"import_attempted":false,"repository_state_may_have_changed":false,'
        '"error_detail":"internal path"}'
    )
    _prepare_runtime(monkeypatch, _CompletedProcess(stdout=unsafe_error))

    with odi_orchestrator.groovy_approval_context(True):
        result = OdiOrchestrator().run_groovy(
            "dim_product_scd2_ikm_import.groovy",
            {"odi.oracle_home": r"C:\Oracle\Middleware\Oracle_Home"},
        )

    assert result == "Hata: ODI Groovy action failed. error_type=GroovyFatalOutput"
    assert "internal path" not in result


def test_structured_error_status_fails_closed():
    assert odi_orchestrator._groovy_output_failed(
        '{"status":"error","action":"create_mapping","error_type":"RuntimeError"}'
    )
    assert odi_orchestrator._groovy_output_failed(
        '{"status":"failed","action":"create_mapping","error_type":"RuntimeError"}'
    )
    assert not odi_orchestrator._groovy_output_failed(
        '{"status":"passed","action":"read_only_probe"}'
    )


def test_approved_high_level_wrapper_binds_and_resets_common_context(monkeypatch):
    observed = []

    def fake_run(_script_name, _params):
        observed.append(odi_orchestrator._GROOVY_APPROVED.get())
        return '{"status":"success"}'

    orchestrator = OdiOrchestrator()
    monkeypatch.setattr(orchestrator, "run_groovy", fake_run)

    result = orchestrator.generate_scenario_metadata(
        approved=True,
        mapping_name="MAP_X",
        scenario_name="MAP_X_SCN",
    )

    assert result["status"] == "success"
    assert observed == [True]
    assert odi_orchestrator._GROOVY_APPROVED.get() is False


def test_no_allowlisted_script_prints_stack_traces():
    offenders = []
    for script_name in odi_orchestrator._ALLOWED_SCRIPTS:
        text = (GROOVY_ROOT / script_name).read_text(encoding="utf-8")
        if "printStackTrace(" in text:
            offenders.append(script_name)

    assert offenders == []
