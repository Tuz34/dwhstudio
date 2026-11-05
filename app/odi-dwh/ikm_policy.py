"""Resolve one approved IKM before target DDL; never defer selection to Groovy."""

import re

IKM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()._-]{0,127}$")
DEFAULT_CONTROL_APPEND_IKM = "IKM Oracle Insert"


def resolve_approved_ikm(
    requested: str | None, integration_type: str, approved_names: str, priority: str,
) -> str:
    approved = {
        name.strip().lower(): name.strip()
        for name in str(approved_names or "").split(",") if name.strip()
    }
    if requested not in (None, ""):
        if not isinstance(requested, str) or not IKM_NAME_RE.fullmatch(requested.strip()):
            raise ValueError("ikm_name is invalid.")
        selected = approved.get(requested.strip().lower())
        if selected is None:
            raise ValueError("ikm_name is not in the approved IKM list (ODI_APPROVED_IKMS).")
    elif integration_type == "CONTROL_APPEND":
        selected = approved.get(DEFAULT_CONTROL_APPEND_IKM.lower())
        if selected is None:
            raise ValueError("CONTROL_APPEND needs an insert IKM in the approved IKM list.")
    else:
        # Preserve the first configured preference, but resolve it locally and exactly.
        # Availability must never make the repository silently choose a different KM.
        first = str(priority or "").split(",")[0].strip().lower()
        matches = [name for key, name in approved.items() if first and first in key]
        selected = approved.get(first)
        if selected is None:
            if len(matches) != 1:
                raise ValueError("Specify ikm_name: the first IKM priority is not one unambiguous approved name.")
            selected = matches[0]
    if not IKM_NAME_RE.fullmatch(selected):
        raise ValueError("The configured ikm_name is invalid.")
    if integration_type == "CONTROL_APPEND" and is_update_ikm(selected):
        raise ValueError("CONTROL_APPEND cannot use a merge or update IKM.")
    return selected


def is_update_ikm(name: str) -> bool:
    return "merge" in name.lower() or "update" in name.lower()


def verified_source_identity(source: object, owner: str, table: str, context: str) -> dict:
    """Accept physical-schema evidence, never a scored model-name hint."""
    if not isinstance(source, dict):
        raise ValueError("Source identity is unverified")
    for key in ("model_code", "datastore_name", "resource_name", "physical_schema", "context"):
        if not isinstance(source.get(key), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", source[key]):
            raise ValueError("Source identity is unverified")
    if (source["physical_schema"], source["resource_name"], source["context"]) != (owner, table, context):
        raise ValueError("Source identity does not match the request")
    exists = source.get("datastore_exists")
    identifier = source.get("datastore_id")
    if ("datastore_id" not in source or type(exists) is not bool
            or (exists and (not isinstance(identifier, str) or not re.fullmatch(r"[0-9]+", identifier)))):
        raise ValueError("Source datastore identity is unverified")
    if not exists and (identifier is not None or source["datastore_name"] != table):
        raise ValueError("Absent source datastore identity is invalid")
    keys = ("model_code", "datastore_name", "resource_name", "physical_schema", "context", "datastore_exists", "datastore_id")
    return {key: source[key] for key in keys}


def normalize_repository_preflight(
    payload: object, ikm_name: str, owner: str | None, table: str | None, context: str,
) -> dict:
    """Project a strict metadata-only proof; unknown/malformed output never authorizes writes."""
    base = {"tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False,
            "repository_connection_attempted": True}
    failure = {**base, "code": "invalid_preflight_output"}
    if not isinstance(payload, dict) or payload.get("tool") != base["tool"] or payload.get("side_effects") is not False:
        return failure
    if payload.get("status") != "passed":
        code = payload.get("code")
        allowed = {"approval_required", "ikm_not_approved", "ikm_not_imported", "ikm_ambiguous",
                   "repository_lookup_failed",
                   "source_identity_missing", "source_identity_ambiguous", "source_identity_unverified"}
        return {**base, "code": code if isinstance(code, str) and code in allowed else "repository_lookup_failed"}
    if (payload.get("repository_connection_attempted") is not True
            or type(payload.get("ikm_match_count")) is not int or payload["ikm_match_count"] != 1
            or payload.get("ikm_name") != ikm_name
            or not isinstance(payload.get("ikm_id"), str) or not re.fullmatch(r"[0-9]+", payload["ikm_id"])):
        return failure
    source = None
    if owner is not None:
        try:
            source = verified_source_identity(payload.get("source"), owner, table, context)
        except ValueError:
            return {**base, "code": "source_identity_unverified"}
    elif payload.get("source") is not None:
        return failure
    return {**base, "status": "passed", "ikm_name": ikm_name, "ikm_id": payload["ikm_id"],
            "ikm_match_count": 1, "source": source}
