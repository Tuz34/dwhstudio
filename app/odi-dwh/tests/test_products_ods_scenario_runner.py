from __future__ import annotations


def test_products_runner_blocks_truthy_approval_before_runtime(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orch = OdiOrchestrator()
    called = []
    monkeypatch.setattr(odi_orchestrator.subprocess, "run", lambda *args, **kwargs: called.append((args, kwargs)))

    result = orch.run_products_ods_scenario_safe(
        approved="yes",
        approval_scope="odi68_products_ods_scenario_run_and_rerun_proof",
        scenario_name="MAP_SH_PRODUCTS_TO_ODS_SCN",
        version="001",
        context="GLOBAL",
    )

    assert result["status"] == "blocked_until_exact_operator_approval"
    assert called == []


def test_products_runner_blocks_wrong_scope_and_identity_before_runtime(monkeypatch):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    orch = OdiOrchestrator()
    called = []
    monkeypatch.setattr(odi_orchestrator.subprocess, "run", lambda *args, **kwargs: called.append((args, kwargs)))

    wrong_scope = orch.run_products_ods_scenario_safe(
        approved=True,
        approval_scope="odi68_products_ods_scenario_generation",
        scenario_name="MAP_SH_PRODUCTS_TO_ODS_SCN",
        version="001",
        context="GLOBAL",
    )
    wrong_identity = orch.run_products_ods_scenario_safe(
        approved=True,
        approval_scope="odi68_products_ods_scenario_run_and_rerun_proof",
        scenario_name="MAP_SH_PRODUCTS_TO_ODS_SCN",
        version="002",
        context="GLOBAL",
    )

    assert wrong_scope["status"] == "blocked_approval_scope"
    assert wrong_identity["status"] == "blocked_scenario_identity"
    assert called == []


def test_products_runner_returns_fixed_success_without_session_identifier(monkeypatch):
    from odi_orchestrator import OdiOrchestrator
    from tests.test_remote_run_contract import done, mock_process

    orch = OdiOrchestrator()
    called = mock_process(monkeypatch)
    result = orch.run_products_ods_scenario_safe(
        approved=True,
        approval_scope="odi68_products_ods_scenario_run_and_rerun_proof",
        scenario_name="MAP_SH_PRODUCTS_TO_ODS_SCN",
        version="001",
        context="GLOBAL",
    )

    assert result == {
        **done(),
        "scenario": {
            "name": "MAP_SH_PRODUCTS_TO_ODS_SCN",
            "version": "001",
            "context": "GLOBAL",
        },
        "raw_output_included": False,
        "session_id_included": False,
    }
    assert called == ["run_scenario_remote.groovy"]
    assert "session_id" not in result


def test_products_runner_fails_closed_on_error_or_incomplete_success(monkeypatch):
    from odi_orchestrator import OdiOrchestrator
    from tests.test_remote_run_contract import mock_process

    orch = OdiOrchestrator()
    outputs = [
        "Hata: ODI Groovy action failed. error_type=GroovyProcessError",
        "Status: RUNNING",
        "Session ID: 12345\nStatus: DONE\nSUCCESS: Scenario completed successfully.",
    ]

    for output in outputs:
        called = mock_process(monkeypatch, stdout=output)
        result = orch.run_products_ods_scenario_safe(
            approved=True,
            approval_scope="odi68_products_ods_scenario_run_and_rerun_proof",
            scenario_name="MAP_SH_PRODUCTS_TO_ODS_SCN",
            version="001",
            context="GLOBAL",
        )

        assert result["status"] == "unknown"
        assert result["error_code"] == "RemoteOutputInvalid"
        assert result["outcome_unknown"] is True
        assert result["raw_output_included"] is False
        assert result["session_id_included"] is False
        assert called == ["run_scenario_remote.groovy"]
        assert "12345" not in str(result)
        assert output not in str(result)


def test_products_runner_does_not_expand_the_generic_odi24_allowlist():
    import odi_orchestrator

    assert "MAP_SH_PRODUCTS_TO_ODS_SCN" not in odi_orchestrator._ODI24_ALLOWED_SCENARIOS
