"""Closed, status-only contract for the common remote ODI runner."""

import json

SCRIPTS = {
    "remote_agent_health.groovy": "health",
    "run_scenario_remote.groovy": "run",
}
ERRORS = frozenset({
    "ApprovalRequired", "InvalidRunIdentity", "AgentConfigurationUnavailable",
    "AgentUnavailable", "ScenarioOwnershipUnavailable", "RepositoryCleanupFailed",
    "ScenarioRunFailed", "ScenarioOutcomeUnknown", "RemoteOutputInvalid",
    "RemoteProcessFailed", "RemoteTimeout",
})
STAGES = frozenset({"approval", "validation", "configuration", "health", "ownership", "run", "completed"})


def result(action: str, *, status: str = "blocked", stage: str = "configuration",
           error_code: str | None = None, agent: bool = False, repository: bool = False,
           may_have_started: bool = False, unknown: bool = False,
           session_status: str | None = None) -> dict:
    return {
        "tool": "odi.remote_agent_health" if action == "health" else "odi.remote_scenario_run",
        "status": status, "stage": stage, "error_code": error_code,
        "agent_contact_attempted": agent, "repository_connection_attempted": repository,
        "run_may_have_started": may_have_started, "side_effects": may_have_started,
        "outcome_unknown": unknown, "session_status": session_status,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
    }


def transport_failure(action: str, code: str) -> dict:
    # Once the run process was launched, absence of a valid result cannot prove no start.
    running = action == "run"
    return result(action, status="unknown" if running else "blocked", stage="run" if running else "health",
                  error_code=code, agent=True, repository=running,
                  may_have_started=running, unknown=running)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("DuplicateJsonKey")
        payload[key] = value
    return payload


def _reject_constant(_value: str) -> None:
    raise ValueError("NonfiniteJsonConstant")


def parse(value: object, action: str) -> dict:
    """Reject extra fields, coercions and contradictory outcomes; never return raw output."""
    invalid = transport_failure(action, "RemoteOutputInvalid")
    try:
        payload = (json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                   if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return invalid
    expected = result(action)
    if not isinstance(payload, dict) or set(payload) != set(expected):
        return invalid
    if payload["tool"] != expected["tool"]:
        return invalid
    for key in ("agent_contact_attempted", "repository_connection_attempted", "run_may_have_started",
                "side_effects", "outcome_unknown", "raw_rows_included", "pk_values_included", "secret_printed"):
        if type(payload[key]) is not bool:
            return invalid
    if any(payload[key] for key in ("raw_rows_included", "pk_values_included", "secret_printed")):
        return invalid
    status, stage, code = payload["status"], payload["stage"], payload["error_code"]
    if not isinstance(status, str) or status not in {"passed", "blocked", "failed", "unknown"}:
        return invalid
    if not isinstance(stage, str) or stage not in STAGES:
        return invalid
    if code is not None and (not isinstance(code, str) or code not in ERRORS):
        return invalid
    session = payload["session_status"]
    if session is not None and (not isinstance(session, str) or session not in {"DONE", "ERROR"}):
        return invalid
    started = payload["run_may_have_started"]
    if payload["side_effects"] != started or (started and not payload["agent_contact_attempted"]):
        return invalid
    if action == "health" and (started or payload["repository_connection_attempted"] or session is not None):
        return invalid
    if status == "passed":
        if (stage != "completed" or code is not None or payload["outcome_unknown"]
                or not payload["agent_contact_attempted"]):
            return invalid
        if action == "run" and (not started or not payload["repository_connection_attempted"] or session != "DONE"):
            return invalid
    elif status == "unknown":
        if action != "run" or not started or not payload["outcome_unknown"] or session is not None or code is None:
            return invalid
    elif status == "failed":
        if action != "run" or not started or payload["outcome_unknown"] or session != "ERROR" or code != "ScenarioRunFailed":
            return invalid
    elif started or payload["outcome_unknown"] or session is not None or code is None:
        return invalid
    return dict(payload)
