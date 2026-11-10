from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

import oracledb

import remote_run_contract
import repository_write_contract


class MappingRunSuccess(TypedDict):
    status: Literal["SUCCESS"]
    batch_id: str
    scenario: str
    version: str
    context: str
    truncate_target_table: bool
    remote: dict


class MappingRunFailure(TypedDict):
    error: str
    batch_id: str


class MappingRunUnsupported(TypedDict):
    status: Literal["ikm_option_unsupported"]
    option: str
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
    payload: dict[str, Any]
    status_code: int


@dataclass
class RemoteRunPhase:
    """Request-local phase. A repository write and the remote call set it explicitly.

    history_uncommitted is set immediately before a history write and cleared
    only when the state manager reports committed is True.
    """

    attempted: bool = False
    kind: str = ""
    batch_id: str = ""
    scenario_name: str = ""
    remote: dict[str, Any] | None = None
    persist_failed: bool = False
    history_uncommitted: bool = False
    pending_outcome: MappingRunOutcome | None = None
    options: MappingRunOptions | None = None

    def response_for_escape(self, exc: BaseException) -> MappingRunOutcome | None:
        if self.options is None:
            return None
        return preserved_remote_outcome(self.options, self, exc)


_AUDIT_WRITE_ERRORS = (oracledb.Error, OSError)
_PROGRAMMING_FAULTS = (AttributeError, KeyError, TypeError)
_HISTORY_WARNINGS = {
    "SUCCESS": "The run completed but its history record could not be saved.",
    "failed": "The run failed but its history record could not be saved.",
    "blocked": "The run was blocked but its history record could not be saved.",
    "unknown": "The run outcome is unknown but its history record could not be saved.",
}


# ODI-87: temporary gate on this generic entry only. Low-level runners are not covered.
# Remove or replace it only after a separately reviewed stock cutover proof.
_CUTOVER_GATED_MAPPING = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
_CUTOVER_GATE_STATUS = "blocked_fixed_mapping_cutover_pending"
_CUTOVER_GATE_ERROR = (
    "The fixed DIM_PRODUCT mapping cannot run through the generic mapping run path "
    "until a separately reviewed cutover proof replaces this gate."
)


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


def _options_updated(output: object) -> bool:
    from odi_orchestrator import _groovy_output_failed

    return (isinstance(output, str) and not _groovy_output_failed(output)
            and any(line.startswith("SUCCESS:") for line in output.splitlines()))


def _scenario_ready(value: object, name: str, version: str) -> bool:
    if not isinstance(value, dict):
        return False
    scenario, checks = value.get("scenario"), value.get("checks")
    return (value.get("tool") == "odi.generate_scenario_metadata"
            and value.get("mode") == "scenario_metadata_generation"
            and isinstance(value.get("status"), str) and value["status"] in {"passed", "already_ready"}
            and type(value.get("side_effects")) is bool
            and value.get("repository_connection_attempted") is True
            and isinstance(scenario, dict) and scenario.get("name") == name and scenario.get("version") == version
            and isinstance(checks, list) and bool(checks)
            and all(isinstance(check, dict) and check.get("status") == "pass" for check in checks))


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


def _explicitly_committed(result: Any) -> bool:
    return getattr(result, "committed", None) is True


def _accept_commit(phase: RemoteRunPhase | None, result: Any) -> bool:
    """The in-flight history marker ends only when COMMIT was observed."""
    if not _explicitly_committed(result):
        return False
    if phase is not None:
        phase.history_uncommitted = False
    return True


def _with_history_warning(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body["state_persist_failed"] = True
    status = body.get("status")
    key = status if isinstance(status, str) and status in _HISTORY_WARNINGS else "failed"
    body["warning"] = _HISTORY_WARNINGS[key]
    return body


def _log_persist_failure(app_module: Any, batch_id: str, options: MappingRunOptions, exc: BaseException) -> None:
    app_module.logger.error(
        "Mapping run state persist failed (batch=%s mapping=%s): %s",
        batch_id,
        options.mapping_name,
        type(exc).__name__,
    )


def _persist_run_state(
    app_module: Any,
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    current_step: str,
    overall_status: str,
    completed_steps: list[str],
    error: object = None,
    *,
    surface: bool = False,
    phase: RemoteRunPhase | None = None,
) -> bool:
    """Return True when the audit row was committed, or no result object came back.

    Post-run callers pass surface=True. A database or OS failure then returns
    False. Building or redacting the state lets programming faults propagate.
    A close error after COMMIT still counts as committed. Callers set
    phase.history_uncommitted before this call; it is cleared only here, and
    only when committed is True.
    """
    safe_error = None
    result = None
    try:
        safe_error = app_module.redact_sensitive_text(str(error or ""), max_len=2000)
        state = _state_payload(
            options, batch_id, scenario_name, current_step, overall_status, completed_steps, safe_error
        )
        with app_module.MappingStateManager() as manager:
            result = manager.upsert_state(state, thread_id=batch_id)
    except _AUDIT_WRITE_ERRORS as exc:
        if _accept_commit(phase, result):
            return True
        _log_persist_failure(app_module, batch_id, options, exc)
        return False
    except Exception as exc:
        if _accept_commit(phase, result):
            return True
        if surface or safe_error is None or isinstance(exc, _PROGRAMMING_FAULTS):
            raise
        _log_persist_failure(app_module, batch_id, options, exc)
        return False
    if _accept_commit(phase, result):
        return True
    if result is None:
        return True
    return False


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
    phase: RemoteRunPhase,
) -> MappingRunOutcome:
    outcome = MappingRunOutcome({"error": public_error, "batch_id": batch_id}, status_code)
    phase.pending_outcome = outcome
    phase.history_uncommitted = True
    persisted = _persist_run_state(
        app_module, options, batch_id, scenario_name, step, "RUN_FAILED", completed_steps, error,
        phase=phase,
    )
    _create_failure_alert(app_module, options, batch_id, scenario_name, step, error)
    if persisted:
        return outcome
    return MappingRunOutcome(_with_history_warning(outcome.payload), status_code)


def _json_line(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _unsupported_ikm_option(output: object) -> str | None:
    """Return the option name when update_mapping reported ikm_option_unsupported."""
    for line in str(output or "").splitlines():
        payload = _json_line(line)
        if payload is not None and payload.get("status") == "ikm_option_unsupported":
            option = str(payload.get("option") or "TRUNCATE_TARGET_TABLE")
            return option if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", option) else "TRUNCATE_TARGET_TABLE"
    return None


def _unsupported_option_outcome(
    app_module: Any,
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    completed_steps: list[str],
    option: str,
    phase: RemoteRunPhase,
) -> MappingRunOutcome:
    message = (
        f"The mapping's IKM does not expose the {option} option. "
        "Run with truncate_target_table=false or change the IKM."
    )
    outcome = MappingRunOutcome(
        {
            "status": "ikm_option_unsupported",
            "option": option,
            "error": message,
            "batch_id": batch_id,
        },
        409,
    )
    phase.pending_outcome = outcome
    phase.history_uncommitted = True
    persisted = _persist_run_state(
        app_module, options, batch_id, scenario_name, "update_km", "RUN_FAILED",
        completed_steps, message, phase=phase,
    )
    if persisted:
        return outcome
    return MappingRunOutcome(_with_history_warning(outcome.payload), 409)


def _begin_remote_run(
    orchestrator: Any,
    options: MappingRunOptions,
    scenario_name: str,
    batch_id: str,
    phase: RemoteRunPhase,
) -> dict[str, Any]:
    """Mark the remote call, then invoke it. The marker is not inferred later."""
    from odi_orchestrator import groovy_is_approved

    phase.kind = "remote"
    phase.attempted = True
    phase.batch_id = batch_id
    phase.scenario_name = scenario_name
    parsed = remote_run_contract.parse(orchestrator.run_scenario_remote(
        approved=groovy_is_approved(), mapping_name=options.mapping_name,
        scenario_name=scenario_name, version=options.version, context=options.context,
    ), "run")
    phase.remote = parsed
    return parsed


def _remote_run_payload(
    options: MappingRunOptions,
    batch_id: str,
    scenario_name: str,
    remote: dict[str, Any],
    *,
    persist_failed: bool,
) -> tuple[dict[str, Any], int]:
    if remote["status"] == "passed":
        payload: dict[str, Any] = {
            "status": "SUCCESS",
            "batch_id": batch_id,
            "scenario": scenario_name,
            "version": options.version,
            "context": options.context,
            "truncate_target_table": options.truncate_target_table,
            "remote": remote,
        }
        code = 200
    else:
        payload = {"status": remote["status"], "remote": remote, "batch_id": batch_id}
        code = 504 if remote.get("error_code") == "RemoteTimeout" else 502
    if persist_failed:
        payload["state_persist_failed"] = True
        payload["warning"] = _HISTORY_WARNINGS[str(payload["status"])]
    return payload, code


def preserved_remote_outcome(
    options: MappingRunOptions,
    phase: RemoteRunPhase,
    exc: BaseException | None = None,
) -> MappingRunOutcome | None:
    """Keep a marked outcome when later work raises. Unmarked calls return None."""
    if phase.attempted:
        remote = phase.remote
        if isinstance(remote, dict):
            payload, code = _remote_run_payload(
                options, phase.batch_id, phase.scenario_name, remote,
                persist_failed=phase.persist_failed or phase.history_uncommitted,
            )
            return MappingRunOutcome(payload, code)
        return MappingRunOutcome(
            {
                "status": "unknown",
                "batch_id": phase.batch_id,
                "run_may_have_started": True,
                "outcome_unknown": True,
            },
            502,
        )
    pending = phase.pending_outcome
    if phase.history_uncommitted and pending is not None:
        return MappingRunOutcome(_with_history_warning(pending.payload), pending.status_code)
    if phase.kind == "repository" and exc is not None:
        return MappingRunOutcome(
            {
                "batch_id": phase.batch_id,
                "status": "repository_outcome_unknown",
                "repository_write_may_have_happened": True,
                "run_may_have_started": False,
                "outcome_unknown": True,
                "error": "mapping run failed.",
                "error_type": type(exc).__name__,
            },
            500,
        )
    return None


def _mark_repository_write(phase: RemoteRunPhase, batch_id: str, scenario_name: str) -> None:
    phase.kind = "repository"
    phase.batch_id = batch_id
    phase.scenario_name = scenario_name


def execute_mapping_run(
    app_module: Any, options: MappingRunOptions, phase: RemoteRunPhase | None = None,
) -> MappingRunOutcome:
    if phase is None:
        phase = RemoteRunPhase()
    phase.options = options
    repository_write_contract.bind_request_phase(phase)
    # Same normalization as the route. Runs before any import, construction or state write.
    if str(options.mapping_name).strip().upper() == _CUTOVER_GATED_MAPPING:
        return MappingRunOutcome({"status": _CUTOVER_GATE_STATUS, "error": _CUTOVER_GATE_ERROR}, 409)

    from odi_orchestrator import OdiOrchestrator, groovy_is_approved

    batch_id = f"RUN-{uuid.uuid4().hex[:12].upper()}"
    scenario_name = f"{options.mapping_name}_SCN"
    completed_steps: list[str] = []
    orchestrator = OdiOrchestrator()

    health = remote_run_contract.parse(orchestrator.remote_agent_health(approved=groovy_is_approved()), "health")
    if health["status"] != "passed":
        # No audit/state/option write is permitted before the health gate.
        return MappingRunOutcome({"status": "blocked", "remote": health},
                                 400 if health["error_code"] == "ApprovalRequired" else 503)

    _mark_repository_write(phase, batch_id, scenario_name)
    option_result = orchestrator.update_mapping(
        mapping_name=options.mapping_name,
        operation="update_km",
        truncate_target_table=options.truncate_target_table,
    )
    unsupported_option = _unsupported_ikm_option(option_result)
    if unsupported_option is not None:
        return _unsupported_option_outcome(
            app_module, options, batch_id, scenario_name, completed_steps, unsupported_option, phase,
        )
    if not _options_updated(option_result):
        return _failure_outcome(
            app_module, options, batch_id, scenario_name, "update_km", completed_steps,
            "MappingOptionsFailed", "Scenario option update failed.", 500, phase,
        )
    completed_steps.append("update_km")

    _mark_repository_write(phase, batch_id, scenario_name)
    generation_result = orchestrator.generate_scenario_metadata(
        approved=groovy_is_approved(), scenario_name=scenario_name,
        mapping_name=options.mapping_name, version=options.version,
    )
    if not _scenario_ready(generation_result, scenario_name, options.version):
        return _failure_outcome(
            app_module, options, batch_id, scenario_name, "generate_scenario", completed_steps,
            "ScenarioGenerationFailed", "Scenario generation failed.", 400, phase,
        )
    completed_steps.append("generate_scenario")

    run_result = _begin_remote_run(orchestrator, options, scenario_name, batch_id, phase)
    if run_result["status"] != "passed":
        phase.history_uncommitted = True
        phase.persist_failed = _persist_run_state(
            app_module, options, batch_id, scenario_name, "run_scenario",
            "RUN_UNKNOWN" if run_result["outcome_unknown"] else "RUN_FAILED", completed_steps,
            run_result["error_code"], surface=True, phase=phase,
        ) is False
        _create_failure_alert(app_module, options, batch_id, scenario_name, "run_scenario",
                              run_result["error_code"])
        payload, code = _remote_run_payload(
            options, batch_id, scenario_name, run_result, persist_failed=phase.persist_failed,
        )
        return MappingRunOutcome(payload, code)
    completed_steps.append("run_scenario")
    phase.history_uncommitted = True
    phase.persist_failed = _persist_run_state(
        app_module, options, batch_id, scenario_name, "run_scenario", "SUCCEEDED", completed_steps,
        surface=True, phase=phase,
    ) is False
    payload, code = _remote_run_payload(
        options, batch_id, scenario_name, run_result, persist_failed=phase.persist_failed,
    )
    return MappingRunOutcome(payload, code)
