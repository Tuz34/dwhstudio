"""Application-path acceptance driver for ODI-81 F1-F5.

Importing this module does not open a connection or call an endpoint. The
optional CLI refuses a live run and never builds an HTTP client.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import Any

import odi81_isolated_fixtures as isolated
import odi81_scd2_fixtures as fixtures

HttpClient = Callable[[str, Mapping[str, Any]], Any]
Factory = fixtures.Factory
NAMESPACE = isolated.NAMESPACE
GENERATE_PATH = "/api/scd2/generate"
RUN_PATH = "/api/scd2/run"
CLASSIC_GENERATE_PATH = "/api/mapping/generate"
CLASSIC_RUN_PATH = "/api/mapping/run"
CLASSIC_VERSION = "001"
CLASSIC_ENGINE = "classic"
CANONICAL_CALLS = (
    "prepare_source",
    "app_generate",
    "backup_target_r1",
    "verify_before_r1",
    "app_run_1",
    "verify_after_r1",
    "backup_source",
    "mutate_source",
    "backup_target_r2",
    "verify_before_r2",
    "app_run_2",
    "verify_after_r2",
    "backup_target_r3",
    "verify_before_r3",
    "app_run_3",
    "verify_after_r3",
)
LEGACY_CALLS = (
    "prepare_source",
    "app_generate",
    "backup_target_r1",
    "verify_before_r1",
    "app_run_1",
    "verify_after_r1",
    "backup_source",
    "backup_target_r2",
    "mutate_source",
    "verify_before_r2",
    "app_run_2",
    "verify_after_r2",
    "backup_target_r3",
    "verify_before_r3",
    "app_run_3",
    "verify_after_r3",
)
LEGACY: dict[str, dict[str, str]] = {
    "F1": {
        "source_owner": "DWH",
        "source_table": "ODI81N_SRC_F1",
        "target_owner": "DWH",
        "target_table": "ODS_ODI81N_F1",
        "mapping_name": "MAP_ODI81N_F1",
        "integration_type": "IU",
        "load_mode": "incremental",
    },
    "F2": {
        "source_owner": "DWH",
        "source_table": "ODI81N_SRC_F2",
        "target_owner": "DWH",
        "target_table": "ODS_ODI81N_F2",
        "mapping_name": "MAP_ODI81N_F2",
        "integration_type": "CONTROL_APPEND",
        "load_mode": "full_reload",
    },
    "F3": {
        "source_owner": "DWH",
        "source_table": "ODI81N_SRC_F3",
        "target_owner": "DWH",
        "target_table": "DWH_ODI81N_F3",
        "mapping_name": "MAP_ODI81N_F3",
        "integration_type": "IU",
        "load_mode": "type1",
    },
}
def _freeze_namespace() -> None:
    # Driver, helpers and backups share one fixed fresh namespace; no override path exists.
    if NAMESPACE != "ODI81N" or set(LEGACY) != set(isolated._FLOWS):
        raise RuntimeError("namespace")
    for flow, spec in LEGACY.items():
        if (spec["source_table"], spec["target_table"]) != isolated._FLOWS[flow]:
            raise RuntimeError("namespace")
        if spec["mapping_name"] != f"MAP_{NAMESPACE}_{flow}" or len(spec["mapping_name"] + "_SCN") > 30:
            raise RuntimeError("namespace")
    for flow, spec in fixtures.FLOWS.items():
        if spec["mapping_name"] != f"MAP_{NAMESPACE}_{flow}":
            raise RuntimeError("namespace")


_freeze_namespace()


_WRITE_FLAGS = (
    "connection_attempted",
    "ddl_attempted",
    "object_may_exist",
    "dml_attempted",
    "commit_attempted",
    "commit_completed",
    "outcome_unknown",
    "manual_review_required",
    "rollback_failed",
    "cursor_close_failed",
    "connection_close_failed",
    "http_write_attempted",
)


def required_scopes(flow: str) -> tuple[str, ...]:
    """Exact approvals that must already be true before the first callback."""
    name = flow.lower()
    if flow in LEGACY:
        return tuple(f"odi81_{name}_{suffix}" for suffix in (
            "prepare_source", "generate", "backup_target_r1", "verify_before_r1", "run_1",
            "verify_after_r1", "backup_source", "backup_target_r2", "mutate_source",
            "verify_before_r2", "run_2", "verify_after_r2", "backup_target_r3",
            "verify_before_r3", "run_3", "verify_after_r3",
        ))
    return (
        f"odi81_{name}_prepare_source",
        f"odi81_{name}_generate",
        f"odi81_{name}_backup_target_r1",
        f"odi81_{name}_verify_before_r1",
        f"odi81_{name}_run_1",
        f"odi81_{name}_verify_after_r1",
        f"odi81_{name}_backup_source",
        f"odi81_{name}_mutate_source",
        f"odi81_{name}_backup_target_r2",
        f"odi81_{name}_verify_before_r2",
        f"odi81_{name}_run_2",
        f"odi81_{name}_verify_after_r2",
        f"odi81_{name}_backup_target_r3",
        f"odi81_{name}_verify_before_r3",
        f"odi81_{name}_run_3",
        f"odi81_{name}_verify_after_r3",
    )


def full_approvals(flow: str) -> dict[str, bool]:
    """Literal True for every scope of one flow. Tests and callers copy this."""
    return {scope: True for scope in required_scopes(flow)}


def generate_body(flow: str) -> dict[str, Any]:
    """SCD2 generate request. Model codes and the execution user are not fields."""
    spec = fixtures.FLOWS[flow]
    return {
        "approved": True,
        "source_owner": spec["source_owner"],
        "source_table": spec["source_table"],
        "target_owner": spec["target_owner"],
        "target_table": spec["target_table"],
        "mapping_name": spec["mapping_name"],
        "natural_key": ["ENTITY_ID"],
        "tracked_columns": ["ATTR_TEXT", "VALUE_AMT"],
        "grant_select_to_app_user": False,
    }


def run_body(flow: str, context: str) -> dict[str, Any]:
    """SCD2 run request bound to scenario version 001 and the proved context."""
    body = generate_body(flow)
    body["scenario_version"] = fixtures.SCENARIO_VERSION
    body["context"] = context
    return body


def classic_params(flow: str) -> dict[str, Any]:
    """Locked classic generate parameters. F2 truncates; F1 and F3 merge on ENTITY_ID."""
    spec = LEGACY[flow]
    incremental = spec["integration_type"] == "IU"
    return {
        "mapping_name": spec["mapping_name"],
        "src_model": "ORACLE_DWH",
        "src_owner": spec["source_owner"],
        "src_table": spec["source_table"],
        "tgt_model": "ORACLE_DWH" if flow == "F3" else "ORACLE_ODS",
        "tgt_owner": spec["target_owner"],
        "tgt_table": spec["target_table"],
        "integration_type": spec["integration_type"],
        "update_key_columns": ["ENTITY_ID"] if incremental else [],
        "update_key_kind": "primary" if incremental else "unique",
        "needs_skey": False,
        "drop_target_if_exists": False,
        "replace_existing_mapping": False,
        "context": "GLOBAL",
        "description": "",
    }


def classic_generate_body(flow: str) -> dict[str, Any]:
    """POST /api/mapping/generate body. The engine is classic and approval is literal."""
    return {"approved": True, "engine": CLASSIC_ENGINE, "params": classic_params(flow)}


def classic_run_body(flow: str, context: str) -> dict[str, Any]:
    """POST /api/mapping/run body. Only the documented full reload truncates the target."""
    spec = LEGACY[flow]
    return {
        "approved": True,
        "mapping_name": spec["mapping_name"],
        "scenario_version": CLASSIC_VERSION,
        "context": context,
        "truncate_target_table": spec["load_mode"] == "full_reload",
    }


def _report() -> dict[str, Any]:
    report = fixtures._report("run_acceptance")
    report["tool"] = "odi81.app_acceptance"
    report["live"] = False
    report["calls"] = []
    report["steps"] = []
    report["http_log"] = []
    report["downstream_stopped"] = False
    report["http_write_attempted"] = False
    return report


def _owners(flow: str) -> tuple[str, ...]:
    if flow in LEGACY:
        return ("DWH",)
    return fixtures._owners_for(flow)


def _approvals_ok(flow: str, approvals: object) -> bool:
    if type(approvals) is not dict:
        return False
    return all(approvals.get(scope) is True for scope in required_scopes(flow))


def _deps_ok(
    flow: str,
    http_client: object,
    writer_factories: object,
    reader_factories: object,
    source_reader_factory: object,
) -> bool:
    if not callable(http_client) or not callable(source_reader_factory):
        return False
    if not isinstance(writer_factories, Mapping) or not isinstance(reader_factories, Mapping):
        return False
    for owner in _owners(flow):
        if not callable(writer_factories.get(owner)) or not callable(reader_factories.get(owner)):
            return False
    return True


def _copy_flags(report: dict[str, Any], child: Mapping[str, Any]) -> None:
    for key in _WRITE_FLAGS:
        if child.get(key):
            report[key] = True


def _accept_step(report: dict[str, Any], action: str, child: Mapping[str, Any]) -> bool:
    report["calls"].append(action)
    status = child.get("status")
    report["steps"].append({"action": action, "status": status, "error_code": child.get("error_code")})
    _copy_flags(report, child)
    if status == "passed":
        if str(action).startswith("verify_"):
            report["counts"] = dict(child.get("counts") or {})
            report["full_target_snapshot_equal"] = child.get("full_target_snapshot_equal") is True
            report["logical_business_comparison_only"] = child.get("logical_business_comparison_only") is True
            report["rerun_semantics"] = child.get("rerun_semantics")
            report["physical_dml_count_proven"] = False
        return True
    report["status"] = status if status in {"failed", "blocked"} else "failed"
    report["error_code"] = child.get("error_code") or "VerificationFailed"
    report["stage"] = action
    report["downstream_stopped"] = True
    if report.get("http_write_attempted") is True:
        report["manual_review_required"] = True
    fixtures._finish(report)
    return False


def _parts(response: Any) -> tuple[int, dict[str, Any]]:
    if isinstance(response, tuple) and len(response) == 2 and type(response[0]) is int and type(response[1]) is dict:
        return response
    status = getattr(response, "status_code", None)
    payload = getattr(response, "json", None)
    if callable(payload):
        payload = payload()
    if type(status) is not int or type(payload) is not dict:
        raise isolated._Blocked("HttpFailed")
    return status, payload


def _accept_generate(status: int, payload: Mapping[str, Any]) -> None:
    if status != 200 or payload.get("status") != "generated":
        raise isolated._Blocked("HttpFailed")
    if payload.get("scenario_version") != fixtures.SCENARIO_VERSION:
        raise isolated._Blocked("ScenarioVersionMismatch")


def _accept_run(flow: str, context: str, status: int, payload: Mapping[str, Any]) -> None:
    if status != 200 or payload.get("status") != "run_completed":
        raise isolated._Blocked("HttpFailed")
    scenario = payload.get("scenario")
    expected = f"{fixtures.FLOWS[flow]['mapping_name']}_SCN"
    if type(scenario) is not dict or scenario.get("name") != expected:
        raise isolated._Blocked("HttpFailed")
    if scenario.get("version") != fixtures.SCENARIO_VERSION:
        raise isolated._Blocked("ScenarioVersionMismatch")
    if scenario.get("context") != context:
        raise isolated._Blocked("ContextMismatch")


def _http_step(
    report: dict[str, Any],
    client: HttpClient,
    path: str,
    body: dict[str, Any],
    accept: Callable[[int, dict[str, Any]], None],
) -> dict[str, Any]:
    scratch = fixtures._report("http")
    scratch["verification_failed"] = True
    scratch["http_write_attempted"] = True
    scratch["outcome_unknown"] = True
    scratch["manual_review_required"] = True
    box: dict[str, Any] = {}

    def invoke() -> None:
        box["value"] = client(path, body)

    with isolated._operation(scratch, [], failure=fixtures._apply_failure):
        invoke()
        status, payload = _parts(box["value"])
        report["http_log"].append({"path": path, "http_status": status})
        accept(status, payload)
        scratch.update(status="passed", error_code=None, verification_failed=False,
                       outcome_unknown=False, manual_review_required=False)
    if scratch["status"] != "passed":
        scratch["status"] = "failed" if scratch["status"] == "blocked" else scratch["status"]
    return scratch


def _audit_names(flow: str) -> tuple[str, ...]:
    if flow == "F3":
        return isolated._EDW_EXTRA
    return isolated._ODS_EXTRA


def _accept_classic_generate(flow: str, status: int, payload: Mapping[str, Any]) -> None:
    """Accept only a successful classic generation whose key and audit shape match."""
    if status != 200 or payload.get("overall_status") != "SUCCESS":
        raise isolated._Blocked("HttpFailed")
    params = payload.get("params")
    steps = payload.get("steps")
    if type(params) is not dict or type(steps) is not dict:
        raise isolated._Blocked("HttpFailed")
    expected = classic_params(flow)
    for key in (
        "mapping_name", "src_model", "src_owner", "src_table", "tgt_model", "tgt_owner",
        "tgt_table", "integration_type", "update_key_columns", "update_key_kind",
        "needs_skey", "drop_target_if_exists", "replace_existing_mapping", "context",
    ):
        if params.get(key) != expected[key]:
            raise isolated._Blocked("ColumnContractMismatch")
    step = steps.get("create_target_table")
    if type(step) is not dict or step.get("status") not in {"CREATED", "ALREADY_IN_SYNC"}:
        raise isolated._Blocked("HttpFailed")
    if step.get("table") != f"DWH.{expected['tgt_table']}":
        raise isolated._Blocked("ColumnContractMismatch")
    key = step.get("key")
    if type(key) is not dict:
        raise isolated._Blocked("TargetPrimaryKeyMismatch")
    if expected["integration_type"] == "CONTROL_APPEND":
        if key.get("status") != "NONE" or key.get("columns") not in ([], None):
            raise isolated._Blocked("ColumnContractMismatch")
    elif key.get("kind") != "primary" or key.get("columns") != ["ENTITY_ID"]:
        raise isolated._Blocked("TargetPrimaryKeyMismatch")
    elif key.get("status") not in {"CREATED", "ALREADY_PRESENT"}:
        raise isolated._Blocked("TargetPrimaryKeyMismatch")
    if step["status"] == "CREATED" and list(step.get("default_columns_added") or []) != list(_audit_names(flow)):
        raise isolated._Blocked("ColumnContractMismatch")


def _accept_classic_run(flow: str, context: str, status: int, payload: Mapping[str, Any]) -> None:
    """Accept the generic scenario payload only when version, context and mode match."""
    if status != 200 or payload.get("status") != "SUCCESS":
        raise isolated._Blocked("HttpFailed")
    expected = classic_run_body(flow, context)
    if payload.get("scenario") != f"{expected['mapping_name']}_SCN":
        raise isolated._Blocked("HttpFailed")
    if payload.get("version") != CLASSIC_VERSION:
        raise isolated._Blocked("ScenarioVersionMismatch")
    if payload.get("context") != context:
        raise isolated._Blocked("ContextMismatch")
    if payload.get("truncate_target_table") is not expected["truncate_target_table"]:
        raise isolated._Blocked("HttpFailed")


def _run_legacy(
    report: dict[str, Any],
    flow: str,
    http_client: HttpClient,
    writer_factories: Mapping[str, Factory],
    reader_factories: Mapping[str, Factory],
    source_reader_factory: Factory,
    context: str,
) -> dict[str, Any]:
    """Drive F1-F3 through classic generate/run. Backups precede the source change."""
    report["native_odi_proven"] = False
    name = flow.lower()
    writer = writer_factories["DWH"]
    reader = reader_factories["DWH"]

    def scope(suffix: str) -> str:
        return f"odi81_{name}_{suffix}"

    def generate_ok(status: int, payload: dict[str, Any]) -> None:
        _accept_classic_generate(flow, status, payload)

    def run_ok(status: int, payload: dict[str, Any]) -> None:
        _accept_classic_run(flow, context, status, payload)

    if not _accept_step(report, "prepare_source", isolated.prepare_source(
        flow, writer, source_reader_factory, approved=True, approval_scope=scope("prepare_source"),
    )):
        return report
    if not _accept_step(report, "app_generate", _http_step(
        report, http_client, CLASSIC_GENERATE_PATH, classic_generate_body(flow), generate_ok,
    )):
        return report
    if not _accept_step(report, "backup_target_r1", isolated.create_retained_backup(
        flow, "target", 1, writer, approved=True, approval_scope=scope("backup_target_r1"),
    )):
        return report
    if not _accept_step(report, "verify_before_r1", isolated.verify_flow(
        flow, 1, "before_run", reader, source_reader_factory,
        approved=True, approval_scope=scope("verify_before_r1"),
    )):
        return report
    if not _accept_step(report, "app_run_1", _http_step(
        report, http_client, CLASSIC_RUN_PATH, classic_run_body(flow, context), run_ok,
    )):
        return report
    if not _accept_step(report, "verify_after_r1", isolated.verify_flow(
        flow, 1, "after_run", reader, source_reader_factory,
        approved=True, approval_scope=scope("verify_after_r1"),
    )):
        return report
    if not _accept_step(report, "backup_source", isolated.create_retained_backup(
        flow, "source", 2, writer, approved=True, approval_scope=scope("backup_source"),
    )):
        return report
    if not _accept_step(report, "backup_target_r2", isolated.create_retained_backup(
        flow, "target", 2, writer, approved=True, approval_scope=scope("backup_target_r2"),
    )):
        return report
    if not _accept_step(report, "mutate_source", isolated.mutate_source(
        flow, writer, source_reader_factory, approved=True, approval_scope=scope("mutate_source"),
    )):
        return report
    if not _accept_step(report, "verify_before_r2", isolated.verify_flow(
        flow, 2, "before_run", reader, source_reader_factory,
        approved=True, approval_scope=scope("verify_before_r2"),
    )):
        return report
    if not _accept_step(report, "app_run_2", _http_step(
        report, http_client, CLASSIC_RUN_PATH, classic_run_body(flow, context), run_ok,
    )):
        return report
    if not _accept_step(report, "verify_after_r2", isolated.verify_flow(
        flow, 2, "after_run", reader, source_reader_factory,
        approved=True, approval_scope=scope("verify_after_r2"),
    )):
        return report
    if not _accept_step(report, "backup_target_r3", isolated.create_retained_backup(
        flow, "target", 3, writer, approved=True, approval_scope=scope("backup_target_r3"),
    )):
        return report
    if not _accept_step(report, "verify_before_r3", isolated.verify_flow(
        flow, 3, "before_run", reader, source_reader_factory,
        approved=True, approval_scope=scope("verify_before_r3"),
    )):
        return report
    if not _accept_step(report, "app_run_3", _http_step(
        report, http_client, CLASSIC_RUN_PATH, classic_run_body(flow, context), run_ok,
    )):
        return report
    if not _accept_step(report, "verify_after_r3", isolated.verify_flow(
        flow, 3, "after_run", reader, source_reader_factory,
        approved=True, approval_scope=scope("verify_after_r3"),
    )):
        return report
    report.update(status="passed", stage="completed", error_code=None, physical_dml_count_proven=False)
    return report


def _run_scd2(
    report: dict[str, Any],
    flow: str,
    http_client: HttpClient,
    writer_factories: Mapping[str, Factory],
    reader_factories: Mapping[str, Factory],
    source_reader_factory: Factory,
    context: str,
    source_model_bindings: object,
) -> dict[str, Any]:
    common = {
        "approved": True,
        "context": context,
        "source_model_bindings": source_model_bindings,
    }
    name = flow.lower()
    if not _accept_step(report, "prepare_source", fixtures.prepare_source(
        flow, writer_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_prepare_source", **common,
    )):
        return report
    if not _accept_step(report, "app_generate", _http_step(
        report, http_client, GENERATE_PATH, generate_body(flow), _accept_generate,
    )):
        return report
    if not _accept_step(report, "backup_target_r1", fixtures.create_backup(
        flow, "target", 1, writer_factories,
        approval_scope=f"odi81_{name}_backup_target_r1", **common,
    )):
        return report
    if not _accept_step(report, "verify_before_r1", fixtures.verify_target(
        flow, 1, "before_run", reader_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_verify_before_r1", **common,
    )):
        return report
    if not _accept_step(report, "app_run_1", _http_step(
        report, http_client, RUN_PATH, run_body(flow, context),
        lambda status, payload: _accept_run(flow, context, status, payload),
    )):
        return report
    if not _accept_step(report, "verify_after_r1", fixtures.verify_target(
        flow, 1, "after_run", reader_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_verify_after_r1", **common,
    )):
        return report
    if not _accept_step(report, "backup_source", fixtures.create_backup(
        flow, "source", 2, writer_factories,
        approval_scope=f"odi81_{name}_backup_source", **common,
    )):
        return report
    if not _accept_step(report, "mutate_source", fixtures.mutate_source(
        flow, writer_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_mutate_source", **common,
    )):
        return report
    if not _accept_step(report, "backup_target_r2", fixtures.create_backup(
        flow, "target", 2, writer_factories,
        approval_scope=f"odi81_{name}_backup_target_r2", **common,
    )):
        return report
    if not _accept_step(report, "verify_before_r2", fixtures.verify_target(
        flow, 2, "before_run", reader_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_verify_before_r2", **common,
    )):
        return report
    if not _accept_step(report, "app_run_2", _http_step(
        report, http_client, RUN_PATH, run_body(flow, context),
        lambda status, payload: _accept_run(flow, context, status, payload),
    )):
        return report
    if not _accept_step(report, "verify_after_r2", fixtures.verify_target(
        flow, 2, "after_run", reader_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_verify_after_r2", **common,
    )):
        return report
    if not _accept_step(report, "backup_target_r3", fixtures.create_backup(
        flow, "target", 3, writer_factories,
        approval_scope=f"odi81_{name}_backup_target_r3", **common,
    )):
        return report
    if not _accept_step(report, "verify_before_r3", fixtures.verify_target(
        flow, 3, "before_run", reader_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_verify_before_r3", **common,
    )):
        return report
    if not _accept_step(report, "app_run_3", _http_step(
        report, http_client, RUN_PATH, run_body(flow, context),
        lambda status, payload: _accept_run(flow, context, status, payload),
    )):
        return report
    if not _accept_step(report, "verify_after_r3", fixtures.verify_target(
        flow, 3, "after_run", reader_factories, source_reader_factory,
        approval_scope=f"odi81_{name}_verify_after_r3", **common,
    )):
        return report
    report.update(status="passed", stage="completed", error_code=None, physical_dml_count_proven=False)
    return report


def run_acceptance(
    flow: str,
    *,
    live: object = False,
    approvals: object = None,
    http_client: HttpClient | None = None,
    writer_factories: Mapping[str, Factory] | None = None,
    reader_factories: Mapping[str, Factory] | None = None,
    source_reader_factory: Factory | None = None,
    context: object = "GLOBAL",
    source_model_bindings: object = None,
) -> dict[str, Any]:
    """Run one flow through injected app endpoints. Default ``live`` is off."""
    report = _report()
    if live is not True:
        report["error_code"] = "LiveDisabled"
        return report
    report["live"] = True
    if not isinstance(flow, str) or flow not in {**LEGACY, **fixtures.FLOWS}:
        return report
    report["flow"] = flow
    if not _approvals_ok(flow, approvals):
        report["error_code"] = "ApprovalScopeMismatch"
        return report
    if not _deps_ok(flow, http_client, writer_factories, reader_factories, source_reader_factory):
        report["error_code"] = "DependencyMissing"
        return report
    if not isinstance(context, str) or not fixtures._gate_models(
        report, _owners(flow), context, source_model_bindings,
    ):
        if isinstance(context, str):
            return report
        report["error_code"] = "ContextMismatch"
        report["status"] = "blocked"
        return report
    assert http_client is not None
    assert writer_factories is not None
    assert reader_factories is not None
    assert source_reader_factory is not None
    if flow in LEGACY:
        return _run_legacy(
            report, flow, http_client, writer_factories, reader_factories,
            source_reader_factory, context,
        )
    return _run_scd2(
        report, flow, http_client, writer_factories, reader_factories,
        source_reader_factory, context, source_model_bindings,
    )


def main(argv: list[str] | None = None) -> int:
    """Refuse an uninjected live run. Import does not call this."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--live" in args:
        print("live_requires_injected_client")
        return 2
    print("live_disabled")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
