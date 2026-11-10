"""ODI-side steps for the generic SCD2 flow.

Every function runs one registered Groovy action (the caller never picks the
script) and returns a normalized, value-free step result. Raw Groovy output is
never returned: only fixed status fields, booleans and counts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import remote_run_contract
import repository_write_contract as write_contract
import scd2_builder
from odi_orchestrator import groovy_approval_context

IKM_NAME = "IKM Oracle Slowly Changing Dimension"
LKM_NAME = "LKM SQL to Oracle (Built-In)"
SCRIPT_PROBE = "scd2_probe.groovy"
SCRIPT_REGISTER = "scd2_datastore_register.groovy"
SCRIPT_MAPPING = "scd2_mapping_create.groovy"
SCRIPT_IKM_IMPORT = "dim_product_scd2_ikm_import.groovy"

_TOOLS = {
    SCRIPT_PROBE: ("odi.scd2_probe", "generic_scd2_readiness_probe"),
    SCRIPT_REGISTER: (
        "odi.scd2_datastore_register",
        "generic_absent_only_scd2_datastore_registration",
    ),
    SCRIPT_MAPPING: (
        "odi.scd2_mapping_create",
        "generic_absent_only_scd2_mapping_creation",
    ),
}
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
_HATA_ERROR_TYPE_RE = re.compile(r"error_type=([A-Za-z][A-Za-z0-9_$]{0,127})")
_TYPE_PARTS_RE = re.compile(r"^([A-Z][A-Z0-9]*)(?:\(([0-9]+)(?:,([0-9]+)| (CHAR|BYTE))?\))?$")

_PROBE_BOOLEANS = (
    "source_model_exists",
    "source_datastore_exists",
    "target_model_exists",
    "target_model_oracle",
    "target_datastore_exists",
    "project_folder_exists",
    "ikm_present",
    "ikm_scd_type",
    "lkm_present",
    "mapping_exists",
    "scenario_exists",
    "scenario_name_collision",
)
_REGISTER_COUNT_KEYS = (
    "target_column_count",
    "primary_key_column_count",
    "natural_key_column_count",
)
_MAPPING_COUNT_KEYS = (
    "source_component_count",
    "target_component_count",
    "target_attribute_count",
    "mapped_attribute_count",
    "access_point_count",
    "lkm_assigned_count",
    "stable_audit_expression_count",
)
# The four audit columns whose creation expressions must carry the stable profile.
STABLE_AUDIT_COLUMN_COUNT = 4
_AUDIT_PROFILE_BOOLEANS = ("stable", "role_contract_valid", "source_alias_valid")
_AUDIT_PROFILE_COUNTS = ("audit_attribute_count", "stable_expression_count", "audit_target_hint_count")
_ROLE_COUNT_KEYS = (
    "surrogate_key",
    "natural_key",
    "start_date",
    "end_date",
    "current_flag",
    "tracked_attributes",
    "overwrite_attributes",
)


def _step(step: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "step": step,
        "status": status,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        **extra,
    }


def skipped(step: str) -> dict[str, Any]:
    """Step result for an object that already exists from an earlier run."""
    return _step(step, "skipped_exists", side_effects=False)


def _failed(step: str, error_type: str, *, side_effects: bool = False) -> dict[str, Any]:
    return _step(step, "failed", error_type=error_type, side_effects=side_effects)


def _parse_envelope(script: str, step: str, output: object) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (payload, None) for a passed envelope or (None, failed_step)."""
    writing = script != SCRIPT_PROBE
    def rejected(error_type: str) -> dict[str, Any]:
        return _step(step, "failed", **write_contract.unverified(error_type)) if writing else _failed(step, error_type)
    text = str(output or "").strip()
    if text.upper().startswith("HATA:"):
        match = _HATA_ERROR_TYPE_RE.search(text)
        return None, rejected(match.group(1) if match else "GroovyActionFailed")
    try:
        payload = write_contract.parse_json(text)
    except (TypeError, ValueError):
        return None, rejected("InvalidGroovyOutput")
    tool, mode = _TOOLS[script]
    if (
        not isinstance(payload, dict)
        or payload.get("tool") != tool
        or payload.get("mode") != mode
        or payload.get("raw_rows_included") is not False
        or payload.get("pk_values_included") is not False
        or payload.get("secret_printed") is not False
        or payload.get("repository_connection_attempted") is not True
    ):
        return None, rejected("InvalidGroovyEnvelope")
    if writing and payload.get("status") != "passed":
        state = write_contract.failure(payload, tool, mode)
        return None, _step(step, "failed", **state) if state else rejected("InvalidGroovyEnvelope")
    if payload.get("status") == "error":
        error_type = payload.get("error_type")
        if not isinstance(error_type, str) or not _ERROR_TYPE_RE.fullmatch(error_type):
            return None, _failed(step, "InvalidGroovyEnvelope")
        return None, _failed(step, error_type, side_effects=payload.get("side_effects") is True)
    if payload.get("status") != "passed" or not isinstance(payload.get("result"), dict):
        return None, rejected("InvalidGroovyEnvelope")
    return payload, None


def _project_params() -> dict[str, str]:
    from config import ODI_FOLDER, ODI_PROJECT

    return {"odi.project": ODI_PROJECT, "odi.folder": ODI_FOLDER}


def _run(orchestrator: Any, script: str, params: Mapping[str, str], approved: object) -> str:
    with groovy_approval_context(approved is True):
        return orchestrator.run_groovy(script, dict(params))


def _target_owner(probe_params: Mapping[str, str]) -> str | None:
    """The physical target owner of the validated target model, for the stable audit profile."""
    owners = [owner for owner, model in scd2_builder.TARGET_MODEL_BY_OWNER.items()
              if model == probe_params.get("odi.scd2.target_model")]
    return owners[0] if len(owners) == 1 else None


def probe(orchestrator: Any, probe_params: Mapping[str, str]) -> dict[str, Any]:
    """Read-only ODI readiness probe: booleans and counts only."""
    owner = _target_owner(probe_params)
    if owner is None:
        return _failed("odi_probe", "InvalidProbeParameters")
    params = {**_project_params(), **probe_params, "odi.scd2.target_owner": owner}
    output = _run(orchestrator, SCRIPT_PROBE, params, approved=False)
    payload, failure = _parse_envelope(SCRIPT_PROBE, "odi_probe", output)
    if failure is not None:
        return failure
    result = payload["result"]
    if any(type(result.get(key)) is not bool for key in _PROBE_BOOLEANS):
        return _failed("odi_probe", "InvalidGroovyEnvelope")
    count = result.get("source_column_count")
    if type(count) is not int or count < 0:
        return _failed("odi_probe", "InvalidGroovyEnvelope")
    details = _probe_details(result)
    if details is None:
        return _failed("odi_probe", "InvalidGroovyEnvelope")
    return _step(
        "odi_probe",
        "passed",
        side_effects=False,
        checks={key: result[key] for key in _PROBE_BOOLEANS},
        source_column_count=count,
        **details,
    )


_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,12}$")
_ROLES = frozenset(
    {
        "SURROGATE_KEY",
        "NATURAL_KEY",
        "ADD_ROW_ON_CHANGE",
        "OVERWRITE_ON_CHANGE",
        "START_TIMESTAMP",
        "END_TIMESTAMP",
        "CURRENT_RECORD_FLAG",
        "NONE",
    }
)
_MAPPING_KEYS = frozenset(
    {
        "target_count",
        "target_table",
        "source_tables",
        "target_component",
        "source_components",
        "integration_type",
        "target_ikm",
        "lkm_names",
        "access_point_count",
        "target_attribute_count",
        "mapped_attribute_count",
        "audit_profile",
    }
)


def _name_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) and len(v) <= 128 for v in value):
        return None
    return list(value)


def _component(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"model", "table"}:
        return None
    if not all(isinstance(value[key], str) and scd2_builder.IDENTIFIER_RE.fullmatch(value[key]) for key in ("model", "table")):
        return None
    return {"model": value["model"], "table": value["table"]}


def _audit_profile(value: object) -> dict[str, Any] | None:
    """Exact boolean/count shape of the probe's stable audit profile; anything else is rejected."""
    if not isinstance(value, dict) or set(value) != set(_AUDIT_PROFILE_BOOLEANS + _AUDIT_PROFILE_COUNTS):
        return None
    if any(type(value[key]) is not bool for key in _AUDIT_PROFILE_BOOLEANS):
        return None
    if any(type(value[key]) is not int or value[key] < 0 for key in _AUDIT_PROFILE_COUNTS):
        return None
    return dict(value)


def audit_profile_stable(mapping: Mapping[str, Any] | None) -> bool:
    """True only for a complete, internally consistent stable audit profile."""
    profile = mapping.get("audit_profile") if isinstance(mapping, Mapping) else None
    return (
        isinstance(profile, Mapping)
        and profile.get("stable") is True
        and profile.get("role_contract_valid") is True
        and profile.get("source_alias_valid") is True
        and profile.get("audit_attribute_count") == STABLE_AUDIT_COLUMN_COUNT
        and profile.get("stable_expression_count") == STABLE_AUDIT_COLUMN_COUNT
        and profile.get("audit_target_hint_count") == 0
    )


def _probe_details(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate contract-relevant metadata returned by the probe."""
    versions = _name_list(result.get("scenario_versions"))
    pk = _name_list(result.get("target_primary_key"))
    ak = _name_list(result.get("target_alternate_key"))
    columns = result.get("target_columns")
    if versions is None or pk is None or ak is None or not isinstance(columns, list):
        return None
    if not all(_VERSION_RE.fullmatch(v) for v in versions):
        return None
    clean_columns = []
    for column in columns:
        if (
            not isinstance(column, dict)
            or set(column) != {"name", "role", "mandatory", "data_format", "type_code", "length", "scale", "length_semantics"}
            or not isinstance(column["name"], str)
            or column["role"] not in _ROLES
            or type(column["mandatory"]) is not bool
            or not isinstance(column["data_format"], str)
            or _TYPE_PARTS_RE.fullmatch(column["data_format"]) is None
            or column["type_code"] != column["data_format"].split("(", 1)[0]
            or type(column["length"]) not in (int, type(None))
            or type(column["scale"]) not in (int, type(None))
            or (column["length"] is not None and column["length"] < 0)
            or (column["scale"] is not None and column["scale"] < 0)
            or column["length_semantics"] not in (None, "CHAR", "BYTE")
        ):
            return None
        clean_columns.append(dict(column))
    mapping = result.get("mapping")
    if mapping is not None:
        if not isinstance(mapping, dict) or set(mapping) != _MAPPING_KEYS:
            return None
        lkms = _name_list(mapping["lkm_names"])
        sources = _name_list(mapping["source_tables"])
        target_component = _component(mapping["target_component"]) if mapping["target_component"] is not None else None
        source_components_raw = mapping["source_components"]
        if not isinstance(source_components_raw, list):
            return None
        source_components = [_component(item) for item in source_components_raw]
        counts_ok = all(
            type(mapping[key]) is int and mapping[key] >= 0
            for key in ("target_count", "target_attribute_count", "mapped_attribute_count", "access_point_count")
        )
        text_ok = all(mapping[key] is None or isinstance(mapping[key], str) for key in ("target_table", "integration_type", "target_ikm"))
        profile = _audit_profile(mapping["audit_profile"])
        if (lkms is None or sources is None or any(item is None for item in source_components) or not counts_ok
                or not text_ok or profile is None):
            return None
        mapping = {**mapping, "lkm_names": lkms, "source_tables": sources, "target_component": target_component,
                   "source_components": source_components, "audit_profile": profile}
    return {
        "scenario_versions": versions,
        "target_datastore": {"columns": clean_columns, "primary_key": pk, "alternate_key": ak},
        "mapping": mapping,
    }


def _lkms_ok(mapping: Mapping[str, Any]) -> bool:
    """No access point means no LKM is required (same-server mapping); otherwise only the built-in LKM is accepted."""
    names = mapping["lkm_names"]
    if mapping["access_point_count"] == 0:
        return not names
    return len(names) == mapping["access_point_count"] and all(n == LKM_NAME for n in names)


def km_mismatch_details(mapping: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Class-only KM names (no repository ids) so a tester can see which KM differs."""
    if mapping is None or "mapping_knowledge_modules" not in mapping_mismatches(
        mapping, {"source_table": None, "source_model": None, "target_table": None, "target_model": None}
    ):
        return None
    return {
        "expected_ikm": IKM_NAME,
        "found_ikm": mapping["target_ikm"],
        "expected_lkm": LKM_NAME if mapping["access_point_count"] else None,
        "found_lkms": list(mapping["lkm_names"]),
        "access_point_count": mapping["access_point_count"],
    }


def mapping_mismatches(
    mapping: Mapping[str, Any] | None,
    plan_like: Mapping[str, Any],
    expected_attributes: int | None = None,
) -> list[str]:
    """Compare an existing mapping with the requested source/target/KMs."""
    if mapping is None:
        return ["mapping_unreadable"]
    found: list[str] = []
    if (
        mapping["target_count"] != 1
        or mapping["target_table"] != plan_like["target_table"]
        or mapping["target_component"] != {"model": plan_like["target_model"], "table": plan_like["target_table"]}
    ):
        found.append("mapping_target")
    if (
        mapping["source_tables"] != [plan_like["source_table"]]
        or mapping["source_components"] != [{"model": plan_like["source_model"], "table": plan_like["source_table"]}]
    ):
        found.append("mapping_source")
    if mapping["integration_type"] not in {"SCD", "SLOWLY_CHANGING_DIMENSION"}:
        found.append("mapping_integration_type")
    if mapping["target_ikm"] != IKM_NAME or not _lkms_ok(mapping):
        found.append("mapping_knowledge_modules")
    if mapping["mapped_attribute_count"] != mapping["target_attribute_count"] or (
        expected_attributes is not None and mapping["target_attribute_count"] != expected_attributes
    ):
        found.append("mapping_coverage")
    # A reused mapping must already carry the stable audit profile; it is never repaired here.
    if not audit_profile_stable(mapping):
        found.append("mapping_audit_profile")
    return found


def _column_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if any(actual[key] != expected[key] for key in expected):
        return False
    parts = _TYPE_PARTS_RE.fullmatch(expected["data_format"])
    if parts is None or actual["type_code"] != parts.group(1):
        return False
    precision = int(parts.group(2)) if parts.group(2) is not None else None
    scale = int(parts.group(3)) if parts.group(3) is not None else None
    if parts.group(1) == "NUMBER":
        # ODI stores data_format independently of precision/scale. Bare NUMBER is
        # unconstrained; NUMBER(p) defaults to scale zero, including SDK null scale.
        expected_scale = (scale or 0) if precision is not None else None
        actual_scale = (actual["scale"] or 0) if precision is not None else actual["scale"]
        if actual["length"] != precision or actual_scale != expected_scale:
            return False
    elif parts.group(1) == "FLOAT":
        expected_precision = precision if precision is not None else 126
        actual_precision = actual["length"] if actual["length"] is not None else 126
        if actual_precision != expected_precision or actual["scale"] is not None:
            return False
    elif parts.group(1) == "TIMESTAMP":
        # TIMESTAMP length is the fractional-seconds precision (default 6), never a size.
        expected_precision = precision if precision is not None else 6
        actual_precision = actual["length"] if actual["length"] is not None else 6
        if actual_precision != expected_precision or not 0 <= actual_precision <= 9 or actual["scale"] is not None:
            return False
    elif precision is not None and actual["length"] != precision:
        return False
    elif scale is not None and actual["scale"] != scale:
        return False
    return actual["length_semantics"] == parts.group(4)


def existing_mismatches(plan: Mapping[str, Any], step: Mapping[str, Any]) -> list[str]:
    """Mismatch codes between existing ODI objects and the requested plan."""
    checks = step["checks"]
    found: list[str] = []
    if checks["target_datastore_exists"]:
        role_by_name = {
            c["name"]: ("NONE" if c["scd_role"] == "NONE" else c["scd_role"]) for c in plan["target_columns"]
        }
        expected = [
            {"name": c["name"], "role": role_by_name[c["name"]], "mandatory": not c["nullable"],
             "data_format": scd2_builder.oracle_ddl_type(c["data_type"])}
            for c in plan["target_columns"]
        ]
        store = step["target_datastore"]
        if len(store["columns"]) != len(expected) or any(
            not _column_matches(actual, wanted)
            for actual, wanted in zip(store["columns"], expected)
        ):
            found.append("datastore_columns")
        if store["primary_key"] != plan["primary_key"]["columns"] or store["alternate_key"] != plan["natural_key"]:
            found.append("datastore_keys")
    if checks["mapping_exists"]:
        found.extend(
            mapping_mismatches(
                step["mapping"],
                {"source_model": plan["models"]["source"], "source_table": plan["source"]["table"],
                 "target_model": plan["models"]["target"], "target_table": plan["target"]["table"]},
                len(plan["target_columns"]),
            )
        )
    return found


def _counts_ok(result: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return all(type(result.get(key)) is int and result[key] >= 0 for key in keys)


def register_datastore(
    orchestrator: Any, plan: Mapping[str, Any], approved: object, *, phase: Any = None,
) -> dict[str, Any]:
    params = scd2_builder.groovy_params(plan)
    write_contract.mark_write_attempted(phase)
    output = _run(orchestrator, SCRIPT_REGISTER, params, approved)
    payload, failure = _parse_envelope(SCRIPT_REGISTER, "odi_datastore_register", output)
    if failure is not None:
        return failure
    result = payload["result"]
    roles = result.get("scd_role_column_counts")
    expected_roles = dict(surrogate_key=1, natural_key=len(plan["natural_key"]), start_date=1, end_date=1,
                          current_flag=1, tracked_attributes=len(plan["tracked_attributes"]),
                          overwrite_attributes=len(plan["overwrite_attributes"]) + 4)
    if (
        payload.get("side_effects") is not True
        or result.get("commit_completed") is not True
        or not _counts_ok(result, _REGISTER_COUNT_KEYS)
        or not isinstance(roles, dict)
        or set(roles) != set(_ROLE_COUNT_KEYS)
        or not _counts_ok(roles, _ROLE_COUNT_KEYS)
        or roles != expected_roles
        or result["primary_key_column_count"] != 1
        or result["natural_key_column_count"] != len(plan["natural_key"])
        or result["target_column_count"] != len(plan["target_columns"])
    ):
        return _failed("odi_datastore_register", "InvalidGroovyEnvelope", side_effects=True)
    return _step(
        "odi_datastore_register",
        "passed",
        side_effects=True,
        target_column_count=result["target_column_count"],
        scd_role_column_counts={key: roles[key] for key in _ROLE_COUNT_KEYS},
    )


def create_mapping(
    orchestrator: Any, plan: Mapping[str, Any], approved: object, *, phase: Any = None,
) -> dict[str, Any]:
    params = {**_project_params(), **scd2_builder.groovy_params(plan)}
    write_contract.mark_write_attempted(phase)
    output = _run(orchestrator, SCRIPT_MAPPING, params, approved)
    payload, failure = _parse_envelope(SCRIPT_MAPPING, "odi_mapping_create", output)
    if failure is not None:
        return failure
    result = payload["result"]
    expected = len(plan["target_columns"])
    if (
        payload.get("side_effects") is not True
        or result.get("commit_completed") is not True
        or result.get("integration_type") != "SCD"
        or result.get("target_ikm") != plan["knowledge_modules"]["ikm"]
        or not _counts_ok(result, _MAPPING_COUNT_KEYS)
        or result["target_attribute_count"] != expected
        or result["mapped_attribute_count"] != expected
        or result["source_component_count"] != 1
        or result["target_component_count"] != 1
        or result["access_point_count"] != result["lkm_assigned_count"]
        or result["stable_audit_expression_count"] != STABLE_AUDIT_COLUMN_COUNT
    ):
        return _failed("odi_mapping_create", "InvalidGroovyEnvelope", side_effects=True)
    return _step(
        "odi_mapping_create",
        "passed",
        side_effects=True,
        integration_type="SCD",
        target_ikm=result["target_ikm"],
        target_attribute_count=result["target_attribute_count"],
        mapped_attribute_count=result["mapped_attribute_count"],
        access_point_count=result["access_point_count"],
        lkm_assigned_count=result["lkm_assigned_count"],
        stable_audit_expression_count=result["stable_audit_expression_count"],
    )


def import_ikm(orchestrator: Any, approved: object, *, phase: Any = None) -> dict[str, Any]:
    """Reuse the existing fixed, absent-only SDK-reference IKM importer."""
    from config import ODI_PROJECT
    from dim_product_odi_scd2_ikm_import import normalize_dim_product_odi_scd2_ikm_import

    params = {"odi.oracle_home": orchestrator.odi_home, "odi.project": ODI_PROJECT}
    write_contract.mark_write_attempted(phase)
    output = _run(orchestrator, SCRIPT_IKM_IMPORT, params, approved)
    try:
        normalized = normalize_dim_product_odi_scd2_ikm_import(json.loads(str(output)))
    except (TypeError, ValueError):
        return _step("odi_ikm_import", "failed", **write_contract.unverified("InvalidGroovyOutput"))
    if normalized.get("status") == "passed":
        return _step("odi_ikm_import", "passed", side_effects=True)
    error_type = normalized.get("error_type")
    if normalized.get("status") != "error":
        return _step("odi_ikm_import", "failed", **write_contract.unverified("InvalidImportOutput"))
    return _failed(
        "odi_ikm_import",
        error_type if isinstance(error_type, str) else "IkmImportFailed",
        side_effects=normalized.get("side_effects") is True,
    )


def generate_scenario(
    orchestrator: Any, plan: Mapping[str, Any], approved: object, *, phase: Any = None,
) -> dict[str, Any]:
    """Absent-only scenario generation through the existing generator."""
    write_contract.mark_write_attempted(phase)
    result = orchestrator.generate_scenario_metadata(
        approved=approved is True,
        mapping_name=plan["mapping_name"],
        scenario_name=plan["scenario_name"],
        version=plan["scenario_version"],
    )
    status = str(result.get("status") or "").lower() if isinstance(result, Mapping) else ""
    if status == "passed":
        return _step("odi_scenario_generate", "passed", side_effects=True)
    if status == "already_ready":
        return _step("odi_scenario_generate", "skipped_exists", side_effects=False)
    error_type = result.get("error_type") if isinstance(result, Mapping) else None
    safe = error_type if isinstance(error_type, str) and _ERROR_TYPE_RE.fullmatch(error_type) else "ScenarioGenerationFailed"
    if status == "blocked_until_approval":
        return _failed("odi_scenario_generate", safe)
    state = write_contract.failure(result, "odi.generate_scenario_metadata", "scenario_metadata_generation", generator=True)
    return _step("odi_scenario_generate", "failed", **(state or write_contract.unverified(safe)))


def run_scenario(
    orchestrator: Any,
    scenario_name: str,
    version: str,
    context: str,
    approved: object,
    *,
    phase: Any = None,
) -> dict[str, Any]:
    """Run one `<mapping>_SCN` scenario through the existing safe wrapper."""
    write_contract.mark_write_attempted(phase)
    result = remote_run_contract.parse(orchestrator.run_scenario_remote(
        approved=approved is True,
        mapping_name=scenario_name[:-4] if scenario_name.endswith("_SCN") else "",
        scenario_name=scenario_name,
        version=version,
        context=context,
    ), "run")
    return _step(
        "odi_scenario_run",
        result["status"],
        side_effects=result["side_effects"],
        scenario={"name": scenario_name, "version": version, "context": context},
        session_status=result["session_status"],
        outcome_unknown=result["outcome_unknown"],
        error_code=result["error_code"],
        remote=result,
    )
