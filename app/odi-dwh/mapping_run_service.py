from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict


class MappingRunSuccess(TypedDict):
    status: Literal["SUCCESS"]
    batch_id: str
    scenario: str
    version: str
    truncate_target_table: bool
    message: str
    option_log: str
    generation_log: str
    log: str


class MappingRunFailure(TypedDict):
    error: str
    batch_id: str


@dataclass(frozen=True)
class MappingRunOptions:
    mapping_name: str
    version: str
    context: str
    truncate_target_table: bool


@dataclass(frozen=True)
class MappingRunOutcome:
    payload: MappingRunSuccess | MappingRunFailure
    status_code: int


def parse_required_bool(value: object) -> tuple[bool | None, str | None]:
    if isinstance(value, bool):
        return value, None
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true", None
    return None, "truncate_target_table must be selected as true or false before running a scenario."


def validate_mapping_run_options(
    body: dict[str, Any],
    mapping_name: str,
    valid_version: Callable[[str], bool],
    valid_identifier: Callable[[str], bool],
) -> tuple[MappingRunOptions | None, str | None]:
    truncate_target_table, bool_error = parse_required_bool(body.get("truncate_target_table"))
    if bool_error or truncate_target_table is None:
        return None, bool_error
    version = str(body.get("scenario_version") or "001").strip() or "001"
    context = str(body.get("context") or "GLOBAL").strip().upper() or "GLOBAL"
    if not valid_version(version):
        return None, "Invalid scenario_version."
    if not valid_identifier(context):
        return None, "Invalid context."
    return MappingRunOptions(mapping_name, version, context, truncate_target_table), None


def _is_odi_failure(output: object) -> bool:
    upper = str(output or "").upper()
    return any(token in upper for token in ("HATA:", "FATAL ERROR", "LKM VERIFY FAILED")) or any(
        upper.startswith(token) or f"\n{token}" in upper for token in ("FATAL:", "ERROR:")
    )


def _state_payload(
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    current_step: str,
    overall_status: str,
    completed_steps: list[str],
    safe_error: str,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "mapping_name": options.mapping_name,
        "scenario_name": scenario_name,
        "scenario_version": options.version,
        "context": options.context,
        "truncate_target_table": options.truncate_target_table,
        "current_step": current_step,
        "overall_status": overall_status,
        "completed_steps": completed_steps,
        "error": safe_error,
        "validated_params": {
            "mapping_name": options.mapping_name,
            "scenario_name": scenario_name,
            "scenario_version": options.version,
            "context": options.context,
            "truncate_target_table": options.truncate_target_table,
        },
    }


def _persist_run_state(
    app_module: Any,
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    current_step: str,
    overall_status: str,
    completed_steps: list[str],
    error: object = None,
) -> None:
    safe_error = app_module.redact_sensitive_text(str(error or ""), max_len=2000)
    state = _state_payload(
        options, batch_id, scenario_name, current_step, overall_status, completed_steps, safe_error
    )
    try:
        with app_module.MappingStateManager() as manager:
            manager.upsert_state(state, thread_id=batch_id)
    except Exception as exc:
        app_module.logger.error(
            "Mapping run state persist failed (batch=%s mapping=%s): %s",
            batch_id,
            options.mapping_name,
            type(exc).__name__,
        )


def _create_failure_alert(
    app_module: Any,
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    current_step: str,
    error: object,
) -> None:
    try:
        safe_error = app_module.redact_sensitive_text(error, max_len=500)
        with app_module.AlertManager() as manager:
            manager.create_alert(
                alert_type="ODI_RUN_FAILED",
                source=options.mapping_name,
                message=f"ODI scenario run failed for {options.mapping_name} at {current_step} (batch {batch_id}).",
                details={
                    "batch_id": batch_id,
                    "mapping_name": options.mapping_name,
                    "scenario_name": scenario_name,
                    "scenario_version": options.version,
                    "context": options.context,
                    "current_step": current_step,
                    "truncate_target_table": options.truncate_target_table,
                    "error": safe_error,
                },
                severity="CRITICAL",
                store_in_rag=False,
            )
    except Exception as exc:
        app_module.logger.error(
            "Mapping run failure alert failed (batch=%s mapping=%s): %s",
            batch_id,
            options.mapping_name,
            type(exc).__name__,
        )


def _failure_outcome(
    app_module: Any,
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    step: str,
    completed_steps: list[str],
    error: str,
    public_error: str,
    status_code: int,
) -> MappingRunOutcome:
    _persist_run_state(
        app_module, options, batch_id, scenario_name, step, "RUN_FAILED", completed_steps, error
    )
    _create_failure_alert(app_module, options, batch_id, scenario_name, step, error)
    return MappingRunOutcome({"error": public_error, "batch_id": batch_id}, status_code)


def execute_mapping_run(app_module: Any, options: MappingRunOptions) -> MappingRunOutcome:
    from odi_orchestrator import OdiOrchestrator

    batch_id = f"RUN-{uuid.uuid4().hex[:12].upper()}"
    scenario_name = f"{options.mapping_name}_SCN"
    completed_steps: list[str] = []
    orchestrator = OdiOrchestrator()

    option_result = orchestrator.update_mapping(
        mapping_name=options.mapping_name,
        operation="update_km",
        truncate_target_table=options.truncate_target_table,
    )
    if _is_odi_failure(option_result):
        return _failure_outcome(
            app_module, options, batch_id, scenario_name, "update_km", completed_steps,
            f"Scenario option update failed: {option_result}", "Scenario option update failed.", 500,
        )
    completed_steps.append("update_km")

    generation_result = orchestrator.generate_scenario(scenario_name, options.mapping_name, options.version)
    if _is_odi_failure(generation_result):
        return _failure_outcome(
            app_module, options, batch_id, scenario_name, "generate_scenario", completed_steps,
            f"Scenario generation failed: {generation_result}", "Scenario generation failed.", 400,
        )
    completed_steps.append("generate_scenario")

    run_result = orchestrator.run_scenario(scenario_name, options.version, options.context)
    if _is_odi_failure(run_result):
        return _failure_outcome(
            app_module, options, batch_id, scenario_name, "run_scenario", completed_steps,
            f"Scenario run failed:\n{run_result}", "Scenario run failed.", 500,
        )
    completed_steps.append("run_scenario")
    _persist_run_state(
        app_module, options, batch_id, scenario_name, "run_scenario", "SUCCEEDED", completed_steps
    )
    payload: MappingRunSuccess = {
        "status": "SUCCESS",
        "batch_id": batch_id,
        "scenario": scenario_name,
        "version": options.version,
        "truncate_target_table": options.truncate_target_table,
        "message": f"{scenario_name} scenario ran successfully.",
        "option_log": app_module.redact_sensitive_text(option_result, max_len=12000),
        "generation_log": app_module.redact_sensitive_text(generation_result, max_len=12000),
        "log": app_module.redact_sensitive_text(run_result, max_len=12000),
    }
    return MappingRunOutcome(payload, 200)
