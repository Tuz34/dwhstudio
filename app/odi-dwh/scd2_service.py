"""Orchestration for the generic SCD2 routes: plan, generate, run, postflight.

All Oracle and ODI access goes through ``Scd2Services`` so tests can inject
mocks. Responses carry statuses, counts and booleans only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import oracledb

import remote_run_contract
import scd2_builder
import scd2_odi
import scd2_postflight
import scd2_target_executor

logger = logging.getLogger("dwh_app.scd2_service")

SCD2_REQUIRED_COLUMNS = frozenset(name for name, _t, _n in scd2_builder.SCD2_COLUMNS)

DM_TOPOLOGY_STEPS = [
    "In ODI Topology, create (or reuse) the physical schema for the DM Oracle schema on the "
    "existing Oracle data server and bind it to a new logical schema (for example ORACLE_DM).",
    "In ODI Designer, create an Oracle model with code TGT_DM bound to that logical schema.",
    "Re-run POST /api/scd2/generate. The app does not create topology objects silently.",
]


@dataclass(frozen=True)
class Scd2Services:
    catalog_connection: Callable[[], Any]
    ddl_connection: Callable[[], Any]
    ddl_user: str
    orchestrator_factory: Callable[[], Any]


def default_services() -> Scd2Services:
    from config import ORACLE_DDL_USER
    from db import get_connection
    from odi_orchestrator import OdiOrchestrator

    return Scd2Services(
        catalog_connection=get_connection,
        ddl_connection=lambda: get_connection(use_ddl=True),
        ddl_user=(ORACLE_DDL_USER or "APP_USER").upper(),
        orchestrator_factory=OdiOrchestrator,
    )


def _invalid(exc: scd2_builder.Scd2PlanError) -> tuple[dict[str, Any], int]:
    return {"status": "invalid_request", "error_code": exc.code}, 400


def _source_columns(
    body: Mapping[str, Any], services: Scd2Services, *, allow_request_columns: bool
) -> tuple[list[dict[str, Any]] | None, str]:
    supplied = body.get("source_columns")
    if allow_request_columns and isinstance(supplied, list) and supplied:
        return supplied, "request"
    try:
        owner = str(body.get("source_owner") or "").strip().upper()
        table = str(body.get("source_table") or "").strip().upper()
        if owner not in scd2_builder.SOURCE_MODEL_BY_OWNER or not scd2_builder.IDENTIFIER_RE.fullmatch(table):
            return None, "invalid"
        return (
            scd2_target_executor.fetch_source_columns(owner, table, services.catalog_connection),
            "live_catalog",
        )
    except oracledb.Error as exc:
        logger.warning("SCD2 source metadata read failed: %s", type(exc).__name__)
        return None, "unavailable"


def _build_plan(
    body: Mapping[str, Any], services: Scd2Services, *, allow_request_columns: bool
) -> tuple[dict[str, Any] | None, str, tuple[dict[str, Any], int] | None]:
    try:
        scd2_builder.validate_request(body)
    except scd2_builder.Scd2PlanError as exc:
        return None, "", _invalid(exc)
    columns, origin = _source_columns(body, services, allow_request_columns=allow_request_columns)
    if origin == "unavailable":
        return None, origin, ({"status": "source_metadata_unavailable"}, 503)
    if not columns:
        return None, origin, ({"status": "blocked_source_not_found"}, 404)
    try:
        return scd2_builder.build_scd2_plan(body, columns), origin, None
    except scd2_builder.Scd2PlanError as exc:
        return None, origin, _invalid(exc)


def plan_preview(body: Mapping[str, Any], services: Scd2Services) -> tuple[dict[str, Any], int]:
    """Connectionless preview unless the caller omits ``source_columns``."""
    plan, origin, error = _build_plan(body, services, allow_request_columns=True)
    if error is not None or plan is None:
        return error or ({"status": "invalid_request"}, 400)
    return (
        {
            "status": "planned",
            "metadata_source": origin,
            "plan": plan,
            "ddl": {
                "create_sequence": scd2_builder.render_create_sequence(plan),
                "create_table": scd2_builder.render_create_table(plan),
                "grants": scd2_builder.render_grant_statements(plan),
            },
            "odi_expressions": scd2_builder.odi_expressions(plan),
            "write_operations_attempted": [],
        },
        200,
    )


def _generate_response(
    status: str,
    plan: Mapping[str, Any] | None,
    steps: list[dict[str, Any]],
    http_status: int,
    **extra: Any,
) -> tuple[dict[str, Any], int]:
    return (
        {
            "status": status,
            "mapping_name": plan["mapping_name"] if plan else None,
            "scenario_name": plan["scenario_name"] if plan else None,
            "steps": steps,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            **extra,
        },
        http_status,
    )


def _probe_block(plan: Mapping[str, Any], checks: Mapping[str, bool]) -> tuple[str, dict[str, Any]] | None:
    """Return (blocked_status, extra) when ODI is not ready for generation."""
    target_owner = plan["target"]["owner"]
    if not checks["target_model_exists"]:
        if target_owner == "DM":
            return "blocked_dm_model_missing", {
                "required_model_code": plan["models"]["target"],
                "required_topology_steps": DM_TOPOLOGY_STEPS,
            }
        return "blocked_target_model_missing", {"required_model_code": plan["models"]["target"]}
    if not checks["target_model_oracle"]:
        return "blocked_target_model_not_oracle", {}
    if not checks["source_model_exists"]:
        return "blocked_source_model_missing", {"required_model_code": plan["models"]["source"]}
    if not checks["source_datastore_exists"]:
        return "blocked_source_datastore_missing", {
            "required_action": "Register the source table as a datastore in its ODI model, then retry."
        }
    if not checks["project_folder_exists"]:
        return "blocked_project_folder_missing", {}
    if not checks["lkm_present"]:
        return "blocked_lkm_missing", {"required_km": plan["knowledge_modules"]["lkm"]}
    if checks["scenario_name_collision"]:
        return "blocked_scenario_name_collision", {}
    if checks["mapping_exists"] and not checks["target_datastore_exists"]:
        return "blocked_mapping_exists_unrelated", {}
    return None


def generate(
    body: Mapping[str, Any], services: Scd2Services, *, approved: object
) -> tuple[dict[str, Any], int]:
    """Approved flow: probe -> target DDL -> datastore -> mapping -> scenario."""
    if approved is not True:
        return {"status": "blocked_until_approval"}, 400
    plan, _origin, error = _build_plan(body, services, allow_request_columns=False)
    if error is not None or plan is None:
        return error or ({"status": "invalid_request"}, 400)

    orchestrator = services.orchestrator_factory()
    steps: list[dict[str, Any]] = []

    probe_step = scd2_odi.probe(
        orchestrator,
        {
            **scd2_builder.probe_params(
                plan["source"]["owner"],
                plan["source"]["table"],
                plan["target"]["owner"],
                plan["target"]["table"],
                plan["mapping_name"],
            ),
            "odi.scd2.scenario_version": plan["scenario_version"],
        },
    )
    steps.append(probe_step)
    if probe_step["status"] != "passed":
        return _generate_response("failed_odi_probe", plan, steps, 502)
    checks = probe_step["checks"]
    blocked = _probe_block(plan, checks)
    if blocked is not None:
        status, extra = blocked
        return _generate_response(status, plan, steps, 409, **extra)

    mismatches = scd2_odi.existing_mismatches(plan, probe_step)
    if mismatches:
        return _generate_response(
            "blocked_existing_object_mismatch", plan, steps, 409, mismatches=mismatches
        )

    ddl = scd2_target_executor.execute_target_ddl(
        plan, approved=True, connection_factory=services.ddl_connection, ddl_user=services.ddl_user
    )
    steps.append({"step": "target_ddl", **ddl})
    if ddl["status"] not in {"created", "already_exists_matching", "repaired_incomplete_target"}:
        failure = {
            "partial_failure": "partial_failure_target_ddl",
            "created_grants_incomplete": "partial_failure_target_grants",
        }.get(ddl["status"], ddl["status"])
        return _generate_response(
            failure, plan, steps, 409 if failure.startswith("blocked") else 502
        )

    if not checks["ikm_present"]:
        ikm_step = scd2_odi.import_ikm(orchestrator, True)
        steps.append(ikm_step)
        if ikm_step["status"] != "passed":
            return _generate_response("partial_failure_ikm_import", plan, steps, 502)

    if checks["target_datastore_exists"]:
        steps.append(scd2_odi.skipped("odi_datastore_register"))
    else:
        register_step = scd2_odi.register_datastore(orchestrator, plan, True)
        steps.append(register_step)
        if register_step["status"] != "passed":
            return _generate_response("partial_failure_datastore_register", plan, steps, 502)

    if checks["mapping_exists"]:
        steps.append(scd2_odi.skipped("odi_mapping_create"))
    else:
        mapping_step = scd2_odi.create_mapping(orchestrator, plan, True)
        steps.append(mapping_step)
        if mapping_step["status"] != "passed":
            return _generate_response("partial_failure_mapping_create", plan, steps, 502)

    scenario_step = scd2_odi.generate_scenario(orchestrator, plan, True)
    steps.append(scenario_step)
    if scenario_step["status"] not in {"passed", "skipped_exists"}:
        return _generate_response("partial_failure_scenario_generate", plan, steps, 502)

    return _generate_response(
        "generated",
        plan,
        steps,
        200,
        target={"owner": plan["target"]["owner"], "table": plan["target"]["table"]},
        scenario_version=plan["scenario_version"],
    )


def validate_run_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate identifiers for a run; raises ``Scd2PlanError``."""
    probe = scd2_builder.probe_params(
        str(body.get("source_owner") or ""),
        str(body.get("source_table") or ""),
        str(body.get("target_owner") or ""),
        str(body.get("target_table") or ""),
        str(body.get("mapping_name") or ""),
    )
    natural_key = scd2_builder.identifier_list(body.get("natural_key"), "invalid_natural_key")
    version = str(body.get("scenario_version") or "").strip()
    context = str(body.get("context") or "GLOBAL").strip().upper()
    if not scd2_builder.IDENTIFIER_RE.fullmatch(context):
        raise scd2_builder.Scd2PlanError("invalid_context")
    if version and (len(version) > 12 or not all(ch.isalnum() or ch in "._-" for ch in version)):
        raise scd2_builder.Scd2PlanError("invalid_scenario_version")
    return {
        "probe": probe,
        "source_owner": str(body["source_owner"]).strip().upper(),
        "source_table": probe["odi.scd2.source_table"],
        "source_model": probe["odi.scd2.source_model"],
        "target_owner": str(body["target_owner"]).strip().upper(),
        "target_table": probe["odi.scd2.target_table"],
        "target_model": probe["odi.scd2.target_model"],
        "mapping_name": probe["odi.scd2.mapping"],
        "natural_key": natural_key,
        "scenario_name": f"{probe['odi.scd2.mapping']}_SCN",
        "scenario_version": version,
        "context": context,
    }


def _target_is_scd2(spec: Mapping[str, Any], services: Scd2Services) -> bool | None:
    try:
        columns = scd2_target_executor.fetch_source_columns(
            spec["target_owner"], spec["target_table"], services.catalog_connection
        )
    except oracledb.Error as exc:
        logger.warning("SCD2 target metadata read failed: %s", type(exc).__name__)
        return None
    return SCD2_REQUIRED_COLUMNS <= {column["name"] for column in columns}


def _version_key(version: str) -> tuple[Any, ...]:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[._-]", version))


def _select_version(requested: str, available: list[str]) -> str | None:
    """Bind a run to a version verified to exist for this mapping's scenario."""
    if requested:
        return requested if requested in available else None
    if not available:
        return None
    return max(available, key=lambda v: tuple((0, p) if isinstance(p, int) else (1, p) for p in _version_key(v)))


def run(
    body: Mapping[str, Any], services: Scd2Services, *, approved: object
) -> tuple[dict[str, Any], int]:
    """Approved flow: guards -> scenario run -> count-only postflight."""
    if approved is not True:
        return {"status": "blocked_until_approval"}, 400
    try:
        spec = validate_run_request(body)
    except scd2_builder.Scd2PlanError as exc:
        return _invalid(exc)

    steps: list[dict[str, Any]] = []
    orchestrator = services.orchestrator_factory()
    health = remote_run_contract.parse(orchestrator.remote_agent_health(approved=True), "health")
    steps.append({"step": "remote_agent_health", **health})
    if health["status"] != "passed":
        return {"status": "blocked_agent_preflight", "steps": steps}, 503
    is_scd2 = _target_is_scd2(spec, services)
    if is_scd2 is None:
        return {"status": "target_metadata_unavailable", "steps": steps}, 503
    if not is_scd2:
        return {"status": "blocked_target_not_scd2", "steps": steps}, 409

    probe_step = scd2_odi.probe(orchestrator, spec["probe"])
    steps.append(probe_step)
    if probe_step["status"] != "passed":
        return {"status": "failed_odi_probe", "steps": steps}, 502
    checks = probe_step["checks"]
    if checks["scenario_name_collision"]:
        return {"status": "blocked_scenario_name_collision", "steps": steps}, 409
    if not (checks["mapping_exists"] and checks["scenario_exists"]):
        return {"status": "blocked_scenario_missing", "steps": steps}, 409
    mismatches = scd2_odi.mapping_mismatches(
        probe_step["mapping"],
        {"source_model": spec["source_model"], "source_table": spec["source_table"],
         "target_model": spec["target_model"], "target_table": spec["target_table"]},
    )
    if mismatches:
        return {"status": "blocked_existing_object_mismatch", "mismatches": mismatches, "steps": steps}, 409
    version = _select_version(spec["scenario_version"], probe_step["scenario_versions"])
    if version is None:
        return {
            "status": "blocked_scenario_version_missing",
            "available_versions": probe_step["scenario_versions"],
            "steps": steps,
        }, 409
    spec["scenario_version"] = version

    source = (spec["source_owner"], spec["source_table"])
    before = scd2_postflight.collect_postflight(
        spec["target_owner"], spec["target_table"], spec["natural_key"], services.catalog_connection, source=source
    )
    if before.get("status") != "ok":
        return {"status": "blocked_count_preflight", "steps": steps, "preflight": before}, 503
    run_step = scd2_odi.run_scenario(
        orchestrator, spec["scenario_name"], spec["scenario_version"], spec["context"], True
    )
    steps.append(run_step)
    after = scd2_postflight.collect_postflight(
        spec["target_owner"], spec["target_table"], spec["natural_key"], services.catalog_connection, source=source
    )
    run_ok = run_step["status"] == "passed"
    status = "run_completed" if run_ok else "run_unknown" if run_step.get("outcome_unknown") else "run_failed"
    return (
        {
            "status": status,
            "scenario": {
                "name": spec["scenario_name"],
                "version": spec["scenario_version"],
                "context": spec["context"],
            },
            "steps": steps,
            "preflight": before,
            "postflight": after,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
        },
        200 if run_ok else 502,
    )


def postflight(args: Mapping[str, str], services: Scd2Services) -> tuple[dict[str, Any], int]:
    """Count-only evidence for an existing SCD2 target."""
    owner = str(args.get("owner") or "").strip().upper()
    table = str(args.get("table") or "").strip().upper()
    raw_key = str(args.get("natural_key") or "")
    natural_key = [part.strip().upper() for part in raw_key.split(",") if part.strip()]
    source = None
    source_owner = str(args.get("source_owner") or "").strip().upper()
    source_table = str(args.get("source_table") or "").strip().upper()
    if source_owner and source_table:
        source = (source_owner, source_table)
    result = scd2_postflight.collect_postflight(
        owner, table, natural_key, services.catalog_connection, source=source
    )
    if result["status"] == "invalid_request":
        return result, 400
    return result, 200 if result["status"] == "ok" else 502
