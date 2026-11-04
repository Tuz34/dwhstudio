"""AI-Q style local-first orchestration layer.

This module is intentionally conservative: the first version produces an
auditable dry-run plan and evidence pack. It does not execute DDL, create ODI
objects, or send anything to cloud providers without an explicit caller flag.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
import ctypes
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from config import LOCAL_OPENAI_API_KEY, LOCAL_OPENAI_BASE_URL, OLLAMA_TAGS_URL


class OrchestratorState(TypedDict, total=False):
    job_id: str
    prompt: str
    task_type: str
    privacy_level: str
    source_table: str
    target_layer: str
    dry_run: bool
    allow_cloud_review: bool
    odi_repository_read_only_requested: bool
    tool_plan: list[dict[str, Any]]
    local_steps: list[dict[str, Any]]
    cloud_steps: list[dict[str, Any]]
    evidence_pack: dict[str, Any]
    routing_decision: dict[str, Any]
    risk_assessment: list[dict[str, Any]]
    approval_plan: dict[str, Any]
    approved_actions: list[str]
    quality_gates: list[dict[str, Any]]
    test_plan: list[dict[str, Any]]
    odi_dry_run: dict[str, Any]
    odi_read_only_preflight: dict[str, Any]
    hardware_profile: dict[str, Any]
    final_response: dict[str, Any]
    status: Literal["running", "success", "failed"]
    warnings: list[str]
    error: str | None
    log: list[dict[str, Any]]
    observability: dict[str, Any]


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "oracle.describe_table": {
        "privacy": "local_only",
        "description": "Reads column metadata through the source connector.",
        "input": {"table": "SCHEMA.TABLE"},
        "output": {"columns": "metadata list", "pk_columns": "list[str]"},
    },
    "oracle.preview_rows": {
        "privacy": "local_only",
        "description": "Reads sample rows; raw rows are not written to the evidence pack.",
        "input": {"table": "SCHEMA.TABLE", "limit": "int"},
        "output": {"rows": "masked/local only"},
    },
    "mapping.generate_ods": {
        "privacy": "local_first",
        "description": "Produces an ODS mapping draft and target column suggestions.",
        "input": {"source_table": "SCHEMA.TABLE", "target_layer": "ODS"},
        "output": {"mapping_spec": "dict"},
    },
    "oracle.plan_load_strategy": {
        "privacy": "local_only",
        "description": "Suggests an Oracle-to-Oracle full/incremental/KM/CDC strategy.",
        "input": {"source_table": "SCHEMA.TABLE", "columns": "metadata list", "pk_columns": "list[str]"},
        "output": {"load_strategy": "dict"},
    },
    "odi.dry_run_mapping": {
        "privacy": "local_only",
        "description": "Validates ODI mapping parameters in a dry run.",
        "input": {"mapping_spec": "dict"},
        "output": {"checks": "list"},
    },
    "odi.read_only_preflight": {
        "privacy": "local_only",
        "description": "Produces a connectionless approval preflight for ODI SDK finder/read-only validation.",
        "input": {"mapping_spec": "dict", "approval_plan": "dict"},
        "output": {"status": "blocked_until_approval|ready_for_read_only_connection", "checks": "list"},
    },
    "reconcile.plan_checks": {
        "privacy": "local_only",
        "description": "Prepares a row count, PK, NULL, and sample reconciliation plan.",
        "input": {"source_table": "SCHEMA.TABLE", "target_table": "OWNER.TABLE"},
        "output": {"test_plan": "list"},
    },
    "rag.search": {
        "privacy": "local_only",
        "description": "Searches local RAG for modeling patterns and error solutions.",
        "input": {"query": "string", "collection": "optional string"},
        "output": {"documents": "list"},
    },
    "llm.local": {
        "privacy": "local_only",
        "description": "Calls a candidate local OpenAI/Ollama/Hermes/Qwen/Nemotron model.",
        "input": {"messages": "list"},
        "output": {"text": "string"},
    },
    "llm.cloud_review": {
        "privacy": "evidence_pack_only",
        "description": "Sends only a controlled evidence pack to the cloud reviewer.",
        "input": {"evidence_pack": "dict"},
        "output": {"review": "string"},
    },
}


APPROVAL_MATRIX: dict[str, dict[str, Any]] = {
    "DDL": {
        "title": "Create target DDL",
        "description": "Create a new target table, index, constraint, or other schema object.",
        "required_roles": ["DWH_ARCHITECT", "ADMIN"],
        "triggers": ["target table create", "index create", "constraint create", "schema object create"],
        "allowed_without_approval": ["generate DDL text", "lint DDL", "dry-run plan"],
        "denial_result": "do_not_execute_ddl_keep_plan_only",
        "audit_required": True,
    },
    "ODI_CREATE_MAPPING": {
        "title": "Create or update ODI mapping",
        "description": "Create, update, delete, or persist an ODI repository object.",
        "required_roles": ["DATA_ENGINEER", "DWH_ARCHITECT"],
        "triggers": ["ODI SDK create", "ODI SDK update", "ODI repository mutation"],
        "allowed_without_approval": ["logical odi.dry_run_mapping", "read generated spec", "write tests"],
        "denial_result": "keep_logical_dry_run_only",
        "audit_required": True,
    },
    "ODI_REPOSITORY_READ_ONLY": {
        "title": "Read-only ODI repository validation",
        "description": "Use ODI SDK finders or equivalent metadata reads without creating or changing ODI objects.",
        "required_roles": ["DATA_ENGINEER"],
        "triggers": ["ODI SDK finder", "read-only repository connection", "metadata validation against ODI"],
        "allowed_without_approval": ["connectionless logical validator", "static Groovy review"],
        "denial_result": "skip_repository_lookup_use_logical_validator",
        "audit_required": True,
    },
    "DROP_OR_ALTER_TARGET": {
        "title": "Drop or alter target object",
        "description": "Drop, truncate, alter, recreate, or otherwise destructively change a target object.",
        "required_roles": ["DWH_ARCHITECT", "ADMIN"],
        "triggers": ["DROP", "ALTER", "TRUNCATE", "recreate target", "destructive change"],
        "allowed_without_approval": ["identify impact", "generate rollback plan", "dry-run risk note"],
        "denial_result": "block_destructive_change",
        "audit_required": True,
    },
    "CLOUD_EVIDENCE_SEND": {
        "title": "Send evidence pack to cloud reviewer",
        "description": "Send sanitized metadata/evidence to a cloud model or external service.",
        "required_roles": ["DWH_ARCHITECT"],
        "triggers": ["allow_cloud_review=True", "cloud reviewer", "external model review"],
        "allowed_without_approval": ["local-only review", "evidence sanitization test"],
        "denial_result": "local_only_review",
        "audit_required": True,
    },
    "CREDENTIAL_OR_ENV_CHANGE": {
        "title": "Credential or environment change",
        "description": "Create, edit, rotate, reveal, or persist secrets, .env values, connection strings, or API keys.",
        "required_roles": ["ADMIN"],
        "triggers": [".env edit", "credential change", "API key", "connection string", "secret rotation"],
        "allowed_without_approval": ["redaction check", "presence check without value exposure"],
        "denial_result": "do_not_modify_configuration",
        "audit_required": True,
    },
}


def _approval_matrix() -> dict[str, dict[str, Any]]:
    return json.loads(json.dumps(APPROVAL_MATRIX))


def _legacy_approval_required_for() -> list[str]:
    return [
        "DDL",
        "ODI_CREATE_MAPPING",
        "DROP_OR_ALTER_TARGET",
        "CLOUD_EVIDENCE_SEND",
        "CREDENTIAL_OR_ENV_CHANGE",
    ]


BLOCKED_EVIDENCE_KEYS = {
    "row",
    "rows",
    "sample_row",
    "sample_rows",
    "raw_row",
    "raw_rows",
    "row_data",
    "preview",
    "preview_rows",
    "data",
    "password",
    "secret",
    "token",
    "api_key",
    "connection_string",
    "dsn_password",
}

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b")),
    ("assignment_secret", re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("oracle_conn", re.compile(r"(?i)\b(user id|uid|user|password|pwd)\s*=\s*[^;]+(;|$)")),
    ("url_credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@")),
)

PII_COLUMN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"(?i)\b(e[-_]?mail|email)\b")),
    ("phone", re.compile(r"(?i)\b(phone|telephone|mobile|gsm|tel)\b")),
    ("person_name", re.compile(r"(?i)\b(first[_ ]?name|last[_ ]?name|full[_ ]?name|namee|surname)\b")),
    ("national_id", re.compile(r"(?i)\b(tckn|tc[_ ]?kimlik|ssn|national[_ ]?id|identity)\b")),
    ("address", re.compile(r"(?i)\b(address|adres|city|street|postal|zip)\b")),
    ("financial", re.compile(r"(?i)\b(salary|commission|amount|price|cost|iban|account|card)\b")),
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(state: OrchestratorState, step: str, status: str, detail: str = "") -> list[dict[str, Any]]:
    items = list(state.get("log", []))
    items.append({"step": step, "status": status, "detail": detail, "timestamp": _now()})
    return items


def _redact_text(value: str) -> tuple[str, list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    redacted = value
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            findings.append({"type": name, "action": "redacted"})
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted, findings


def _sensitivity_tags_for_column(column: dict[str, Any]) -> list[str]:
    text = " ".join(
        str(column.get(key) or "")
        for key in ("name", "comment", "description", "data_element")
    )
    tags = [tag for tag, pattern in PII_COLUMN_PATTERNS if pattern.search(text)]
    return sorted(set(tags))


def _sanitize_evidence_value(value: Any, path: str, report: dict[str, Any]) -> Any:
    key = path.rsplit(".", 1)[-1].lower()
    if key in BLOCKED_EVIDENCE_KEYS:
        report["removed_keys"].append(path)
        return "[REMOVED_LOCAL_ONLY]"
    if isinstance(value, str):
        redacted, findings = _redact_text(value)
        if findings:
            report["redactions"].extend({"path": path, **finding} for finding in findings)
        return redacted
    if isinstance(value, list):
        return [_sanitize_evidence_value(item, f"{path}[]", report) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else str(k)
            if str(k).lower() in BLOCKED_EVIDENCE_KEYS:
                report["removed_keys"].append(child_path)
                continue
            cleaned[k] = _sanitize_evidence_value(v, child_path, report)
        return cleaned
    return value


def _build_sensitivity_summary(columns: list[dict[str, Any]]) -> dict[str, Any]:
    by_column: dict[str, list[str]] = {}
    tag_counts: dict[str, int] = {}
    for column in columns:
        name = str(column.get("name") or "")
        tags = _sensitivity_tags_for_column(column)
        if tags:
            by_column[name] = tags
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return {
        "pii_or_sensitive_columns": by_column,
        "tag_counts": tag_counts,
        "policy": "column names/comments may be shared as metadata; raw values stay local",
    }


def _oracle_load_strategy(table: str, columns: list[dict[str, Any]], pk_columns: list[str]) -> dict[str, Any]:
    names = [str(col.get("name") or "").upper() for col in columns]
    date_like = [
        name for name in names
        if any(token in name for token in ("UPDATE", "MODIFIED", "CHANGE", "TIME_ID", "DATE", "TIMESTAMP"))
    ]
    business_date = next((name for name in date_like if name in {"TIME_ID", "MODIFIEDDATE", "MODIFIED_DATE", "UPDATE_DATE", "UPDATED_AT"}), "")
    is_fact_like = table.upper().endswith(".SALES") or (not pk_columns and any(name.endswith("_ID") for name in names))

    if pk_columns and business_date:
        return {
            "strategy": "incremental_merge",
            "recommended_odi_km": "LKM Oracle to Oracle + IKM Oracle Incremental Update",
            "watermark_column": business_date,
            "merge_key": pk_columns,
            "cdc_options": ["watermark", "materialized_view_log", "goldengate"],
            "rationale": "PK and date/update columns exist; a watermark based merge can be tried first.",
            "approval_required": ["ODI_KM_SELECTION", "INCREMENTAL_LOGIC", "LIVE_ODI_CREATE"],
        }
    if pk_columns:
        return {
            "strategy": "full_load_then_merge_by_pk",
            "recommended_odi_km": "LKM Oracle to Oracle + IKM Oracle Control Append or Incremental Update",
            "watermark_column": None,
            "merge_key": pk_columns,
            "cdc_options": ["add_watermark_column", "materialized_view_log", "goldengate"],
            "rationale": "A PK exists but no reliable watermark column; incremental loading needs an additional CDC decision.",
            "approval_required": ["CDC_DESIGN", "ODI_KM_SELECTION", "LIVE_ODI_CREATE"],
        }
    if business_date:
        return {
            "strategy": "incremental_append_with_watermark",
            "recommended_odi_km": "LKM Oracle to Oracle + IKM Oracle Insert",
            "watermark_column": business_date,
            "merge_key": [],
            "cdc_options": ["watermark", "partition_exchange_if_large_fact", "goldengate"],
            "rationale": "No PK; treat as a fact/append table, requiring row count and aggregate reconciliation checks.",
            "approval_required": ["FACT_GRAIN_CONFIRMATION", "WATERMARK_CONFIRMATION", "LIVE_ODI_CREATE"],
        }
    return {
        "strategy": "full_load_snapshot",
        "recommended_odi_km": "LKM Oracle to Oracle + IKM Oracle Insert",
        "watermark_column": None,
        "merge_key": [],
        "cdc_options": ["source_watermark_required", "materialized_view_log", "goldengate"],
        "rationale": "No PK or watermark; the safe initial path is a full snapshot, and incremental loading needs a source design decision.",
        "approval_required": ["SOURCE_GRAIN_CONFIRMATION", "CDC_DESIGN", "LIVE_ODI_CREATE"],
    }


def _check(status: str, name: str, detail: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if data:
        item["data"] = data
    return item


def _odi_dry_run_mapping(mapping_spec: dict[str, Any]) -> dict[str, Any]:
    """Validate an ODI mapping plan without connecting to or mutating ODI.

    This is the first safety layer for `odi.dry_run_mapping`: it checks the
    generated contract and approval boundaries before any real ODI SDK finder
    or repository write is allowed in a later phase.
    """

    checks: list[dict[str, Any]] = []
    source_table = str(mapping_spec.get("source_table") or "").strip().upper()
    target_table = str(mapping_spec.get("target_table") or "").strip().upper()
    target_layer = str(mapping_spec.get("target_layer") or "").strip().upper()
    columns = list(mapping_spec.get("columns") or [])
    pk_columns = [str(col).upper() for col in mapping_spec.get("pk_columns") or []]
    load_strategy = dict(mapping_spec.get("oracle_load_strategy") or {})
    mapping_name = str(mapping_spec.get("mapping_name") or "").strip().upper()

    checks.append(_check("pass", "side_effects_disabled", "Logical dry-run only; ODI SDK create/update is not called."))

    if source_table and "." in source_table:
        checks.append(_check("pass", "source_table_qualified", source_table))
    else:
        checks.append(_check("fail", "source_table_qualified", "Source table must be SCHEMA.TABLE."))

    if target_table and "." in target_table:
        checks.append(_check("pass", "target_table_qualified", target_table))
    else:
        checks.append(_check("fail", "target_table_qualified", "Target table must be OWNER.TABLE."))

    if target_layer == "ODS" and ".ODS_" in target_table:
        checks.append(_check("pass", "ods_target_naming", target_table))
    elif target_layer == "ODS":
        checks.append(_check("warning", "ods_target_naming", "ODS target should use ODS_<SCHEMA>_<TABLE> naming."))
    else:
        checks.append(_check("pass", "target_layer", target_layer or "not specified"))

    if mapping_name:
        checks.append(_check("pass", "mapping_name_present", mapping_name))
    else:
        checks.append(_check("warning", "mapping_name_present", "Mapping name will need to be generated before live ODI create."))

    if columns:
        checks.append(_check("pass", "source_columns_present", f"{len(columns)} source columns available."))
    else:
        checks.append(_check("fail", "source_columns_present", "No source columns available for ODI mapping validation."))

    column_names = {str(col.get("name") or "").upper() for col in columns if isinstance(col, dict)}
    missing_pk = [col for col in pk_columns if col not in column_names]
    if pk_columns and not missing_pk:
        checks.append(_check("pass", "pk_columns_in_source", ", ".join(pk_columns)))
    elif pk_columns:
        checks.append(_check("fail", "pk_columns_in_source", "PK columns missing from metadata.", {"missing": missing_pk}))
    else:
        checks.append(_check("warning", "pk_columns_in_source", "No PK columns; fact/snapshot reconciliation must use row count and aggregates."))

    strategy = str(load_strategy.get("strategy") or "")
    recommended_km = str(load_strategy.get("recommended_odi_km") or "")
    if strategy:
        checks.append(_check("pass", "load_strategy_selected", strategy))
    else:
        checks.append(_check("fail", "load_strategy_selected", "No Oracle load strategy selected."))

    if recommended_km:
        checks.append(_check("pass", "km_recommendation_present", recommended_km))
    else:
        checks.append(_check("warning", "km_recommendation_present", "No ODI KM recommendation present."))

    approval_required = list(load_strategy.get("approval_required") or [])
    if approval_required:
        checks.append(_check("pass", "live_odi_approval_required", ", ".join(approval_required)))
    else:
        checks.append(_check("warning", "live_odi_approval_required", "Live ODI create should require explicit approval."))

    statuses = [item["status"] for item in checks]
    return {
        "tool": "odi.dry_run_mapping",
        "mode": "logical_only",
        "side_effects": False,
        "passed": "fail" not in statuses,
        "summary": {
            "pass": statuses.count("pass"),
            "warning": statuses.count("warning"),
            "fail": statuses.count("fail"),
        },
        "mapping_spec": {
            "mapping_name": mapping_name,
            "source_table": source_table,
            "target_table": target_table,
            "target_layer": target_layer,
            "load_strategy": strategy,
        },
        "checks": checks,
        "next_phase": "Optional ODI SDK read-only finder validation after approval of repository connection scope.",
    }


def _approval_gate(plan: dict[str, Any], action: str) -> dict[str, Any]:
    for gate in plan.get("gates") or []:
        if gate.get("action") == action:
            return dict(gate)
    return {}


def _odi_read_only_preflight(mapping_spec: dict[str, Any], approval_plan: dict[str, Any]) -> dict[str, Any]:
    """Build the approval-aware preflight for ODI SDK finder validation.

    This function never imports or calls ODI SDK code. It only states whether
    a later read-only repository connection is permitted by the approval plan.
    """

    action = "ODI_REPOSITORY_READ_ONLY"
    gate = _approval_gate(approval_plan, action)
    approved = gate.get("status") == "approved_for_current_run"
    checks = [
        _check("pass", "repository_connection_not_attempted", "No ODI SDK connection or finder was called."),
        _check("pass", "side_effects_disabled", "Preflight is connectionless and cannot mutate the ODI repository."),
        _check(
            "pass",
            "forbidden_operations_declared",
            "create/update/delete/mutate operations are outside this preflight scope.",
            {"forbidden": ["create", "update", "delete", "mutate", "commit", "save"]},
        ),
        _check(
            "pass" if gate else "fail",
            "approval_gate_present",
            "ODI_REPOSITORY_READ_ONLY gate found." if gate else "ODI_REPOSITORY_READ_ONLY gate missing.",
        ),
        _check(
            "pass" if approved else "warning",
            "approval_status",
            "Read-only finder validation is approved for current run."
            if approved
            else "Read-only finder validation is blocked until approval.",
        ),
    ]
    source_table = str(mapping_spec.get("source_table") or "").strip().upper()
    target_table = str(mapping_spec.get("target_table") or "").strip().upper()
    mapping_name = str(mapping_spec.get("mapping_name") or "").strip().upper()
    statuses = [item["status"] for item in checks]
    return {
        "tool": "odi.read_only_preflight",
        "mode": "connectionless_preflight",
        "status": "ready_for_read_only_connection" if approved else "blocked_until_approval",
        "ready": approved,
        "side_effects": False,
        "repository_connection_attempted": False,
        "requested_action": action,
        "approval": {
            "action": action,
            "status": gate.get("status", "missing"),
            "decision": gate.get("decision", "missing_approval_gate"),
            "required_roles": gate.get("required_roles", []),
            "auto_approved": gate.get("auto_approved", False),
        },
        "allowed_read_only_operations": [
            "find_project",
            "find_folder",
            "find_model",
            "find_datastore",
            "find_mapping",
            "inspect_knowledge_modules",
        ],
        "forbidden_operations": ["create", "update", "delete", "mutate", "commit", "save"],
        "mapping_spec": {
            "mapping_name": mapping_name,
            "source_table": source_table,
            "target_table": target_table,
        },
        "summary": {
            "pass": statuses.count("pass"),
            "warning": statuses.count("warning"),
            "fail": statuses.count("fail"),
        },
        "checks": checks,
        "next_phase": (
            "Run ODI SDK finder validation with read-only scope."
            if approved
            else "Keep logical dry-run only until ODI_REPOSITORY_READ_ONLY approval is recorded."
        ),
    }


def _finalize_evidence_pack(pack: dict[str, Any], columns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"removed_keys": [], "redactions": []}
    sanitized = _sanitize_evidence_value(pack, "", report)
    sanitized["evidence_pack_version"] = 2
    sanitized["privacy_policy"] = {
        "raw_rows": "forbidden",
        "credentials": "forbidden",
        "env_files": "forbidden",
        "cloud_payload": "metadata_and_local_findings_only",
    }
    sanitized["allowed_cloud_fields"] = [
        "job_id",
        "task_type",
        "source_table",
        "target_table",
        "target_layer",
        "columns",
        "pk_columns",
        "local_findings",
        "sensitivity_summary",
        "oracle_load_strategy",
        "approval_plan",
        "odi_read_only_preflight",
        "question_for_cloud",
        "redaction_report",
    ]
    sanitized["sensitivity_summary"] = _build_sensitivity_summary(columns or sanitized.get("columns", []))
    sanitized["redaction_report"] = report
    return sanitized


def _evidence_contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for _, pattern in SECRET_PATTERNS)
    if isinstance(value, list):
        return any(_evidence_contains_secret(item) for item in value)
    if isinstance(value, dict):
        return any(_evidence_contains_secret(item) for item in value.values())
    return False


def _evidence_has_blocked_key(value: Any, path: str = "") -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (
                str(key).lower() in BLOCKED_EVIDENCE_KEYS
                and not child_path.startswith(("privacy_policy.", "redaction_report."))
            ):
                return True
            if _evidence_has_blocked_key(item, child_path):
                return True
    if isinstance(value, list):
        return any(_evidence_has_blocked_key(item, path) for item in value)
    return False


def _is_modeling_request(prompt: str) -> bool:
    text = prompt.upper()
    keywords = (
        "ODS", "DWH", "MODEL", "MODELLE", "TABLO", "TABLE", "MAPPING",
        "MAP", "ODI", "ETL", "KATMAN", "LAYER", "RECONCILIATION",
    )
    return any(keyword in text for keyword in keywords)


def _extract_source_table(prompt: str) -> str:
    text = prompt.upper()
    match = re.search(r"\b([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)\b", text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    # Common sample-schema defaults for short user prompts.
    for schema in ("HR", "SH"):
        for table in ("EMPLOYEES", "JOB_HISTORY", "SALES", "CUSTOMERS", "PRODUCTS"):
            if table in text:
                return f"{schema}.{table}"
    return ""


def _infer_target_layer(prompt: str) -> str:
    text = prompt.upper()
    if "DWH" in text:
        return "DWH"
    return "ODS"


def _summarize_columns(table: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    warnings: list[str] = []
    try:
        from source_connector import get_source_connector

        connector = get_source_connector()
        schema_df = connector.get_table_schema(table)
        pk_columns = connector.get_pk_columns(table)
        columns: list[dict[str, Any]] = []
        for row in schema_df.to_dict(orient="records"):
            columns.append(
                {
                    "name": row.get("COLUMN_NAME"),
                    "type": row.get("FULL_DATA_TYPE") or row.get("DATA_TYPE"),
                    "nullable": row.get("NULLABLE"),
                    "comment": row.get("COMMENTS") or "",
                }
            )
        if not columns:
            warnings.append(f"Source table metadata returned empty: {table}")
        return columns, pk_columns, warnings
    except Exception as exc:
        warnings.append(f"Source metadata could not be read ({table}): {exc}")
        return [], [], warnings


def _hardware_profile() -> dict[str, Any]:
    profile: dict[str, Any] = {"gpu": None, "ram_bytes": None, "npu": None, "notes": []}
    try:
        cmd = [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,driver_version",
            "--format=csv,noheader,nounits",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            name, total, used, driver = [p.strip() for p in proc.stdout.strip().split(",", 3)]
            profile["gpu"] = {
                "name": name,
                "memory_total_mib": int(float(total)),
                "memory_used_mib": int(float(used)),
                "driver_version": driver,
            }
    except Exception as exc:
        profile["notes"].append(f"nvidia-smi okunamadi: {exc}")

    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        memory = MEMORYSTATUSEX()
        memory.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))  # type: ignore[attr-defined]
        profile["ram_bytes"] = int(memory.ullTotalPhys)
    except Exception:
        try:
            profile["ram_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        except Exception:
            pass
    return profile


def _gpu_snapshot() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"available": False, "error": proc.stderr.strip()}
        name, total, used, util, temp = [p.strip() for p in proc.stdout.strip().split(",", 4)]
        return {
            "available": True,
            "name": name,
            "memory_total_mib": int(float(total)),
            "memory_used_mib": int(float(used)),
            "gpu_util_percent": int(float(util)),
            "temperature_c": int(float(temp)),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _latest_benchmark_summary(limit: int = 5) -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parent / "logs" / "local_ai_benchmark.jsonl"
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            result = record.get("result") or {}
            before = record.get("gpu_before") or {}
            after = record.get("gpu_after") or {}
            items.append({
                "timestamp": record.get("timestamp"),
                "base_url": record.get("base_url"),
                "model": record.get("model"),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "completion_tokens": result.get("completion_tokens"),
                "approx_tokens_per_second": result.get("approx_tokens_per_second"),
                "gpu_memory_used_mib_before": before.get("memory_used_mib"),
                "gpu_memory_used_mib_after": after.get("memory_used_mib"),
            })
    except Exception:
        return []
    return items


def _initial_observability(job_id: str, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "PENDING",
        "started_at": _now(),
        "finished_at": None,
        "duration_ms": None,
        "node_metrics": [],
        "route": {
            "local_openai_models": (runtime or {}).get("local_openai_models", []),
            "ollama_models": (runtime or {}).get("ollama_models", []),
            "cloud_policy": "pending",
        },
        "model_runtime": runtime or {},
        "latest_benchmarks": _latest_benchmark_summary(),
        "gpu_before": _gpu_snapshot(),
        "gpu_after": None,
    }


def _append_node_observation(
    state: OrchestratorState,
    node_name: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    status: str,
    detail: str = "",
) -> dict[str, Any]:
    obs = dict(state.get("observability") or {})
    metrics = list(obs.get("node_metrics") or [])
    metrics.append({
        "node": node_name,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "detail": detail,
    })
    obs["node_metrics"] = metrics
    obs["status"] = "RUNNING"
    return obs


def _finalize_observability(
    obs: dict[str, Any],
    status: str,
    routing_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    finished_at = _now()
    started = obs.get("started_at")
    duration_ms = None
    if started:
        try:
            duration_ms = int((datetime.fromisoformat(finished_at) - datetime.fromisoformat(started)).total_seconds() * 1000)
        except Exception:
            duration_ms = None
    finalized = dict(obs)
    finalized["status"] = status
    finalized["finished_at"] = finished_at
    finalized["duration_ms"] = duration_ms
    finalized["gpu_after"] = _gpu_snapshot()
    if routing_decision:
        finalized["route"] = {
            "primary_worker": routing_decision.get("primary_worker"),
            "cloud_policy": routing_decision.get("cloud_policy"),
            "runtime_status": routing_decision.get("runtime_status", {}),
        }
    return finalized


def _observable_node(node_name: str, fn):
    def wrapped(state: OrchestratorState) -> dict[str, Any]:
        started_at = _now()
        start = time.perf_counter()
        try:
            update = dict(fn(state))
            status = "FAILED" if update.get("overall_status") == "failed" or update.get("status") == "failed" else "SUCCESS"
            detail = str(update.get("error") or update.get("current_step") or "")
        except Exception as exc:
            finished_at = _now()
            duration_ms = int((time.perf_counter() - start) * 1000)
            obs = _append_node_observation(state, node_name, started_at, finished_at, duration_ms, "FAILED", str(exc))
            raise
        finished_at = _now()
        duration_ms = int((time.perf_counter() - start) * 1000)
        base_state = {**state, "observability": update.get("observability", state.get("observability", {}))}
        observed = _append_node_observation(
            base_state,
            node_name,
            started_at,
            finished_at,
            duration_ms,
            status,
            detail,
        )
        if node_name == "final_composer":
            response = dict(update.get("final_response") or {})
            observed = _finalize_observability(
                observed,
                "SUCCEEDED" if status == "SUCCESS" else "FAILED",
                response.get("routing_decision", {}),
            )
            if response:
                response["observability"] = observed
                update["final_response"] = response
        update["observability"] = observed
        return update

    return wrapped


def _runtime_status() -> dict[str, Any]:
    """Probe local model runtimes without downloading or loading a model."""
    status = {
        "ollama_api": False,
        "ollama_models": [],
        "local_openai_api": False,
        "local_openai_models": [],
    }
    try:
        import requests

        res = requests.get(os.getenv("OLLAMA_TAGS_URL") or OLLAMA_TAGS_URL, timeout=2)
        if res.status_code == 200:
            status["ollama_api"] = True
            status["ollama_models"] = [m.get("name") for m in res.json().get("models", []) if m.get("name")]
    except Exception:
        pass
    try:
        import requests

        base_url = (os.getenv("LOCAL_OPENAI_BASE_URL") or LOCAL_OPENAI_BASE_URL).rstrip("/")
        headers = {}
        api_key = os.getenv("LOCAL_OPENAI_API_KEY") or LOCAL_OPENAI_API_KEY
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        res = requests.get(f"{base_url}/models", headers=headers, timeout=2)
        if res.status_code == 200:
            status["local_openai_api"] = True
            payload = res.json()
            status["local_openai_models"] = [
                item.get("id") for item in payload.get("data", []) if item.get("id")
            ]
    except Exception:
        pass
    return status


def _routing_decision(state: OrchestratorState, runtime: dict[str, Any]) -> dict[str, Any]:
    privacy = state.get("privacy_level", "metadata_only")
    has_local_model = bool(runtime.get("ollama_models") or runtime.get("local_openai_models"))
    cloud_allowed = bool(state.get("allow_cloud_review") and privacy != "restricted")
    return {
        "primary_worker": "local_llm" if has_local_model else "tool_only_until_model_ready",
        "cloud_policy": "evidence_pack_only" if cloud_allowed else "disabled",
        "reason": [
            "Sensitive data stays within the local boundary.",
            "The cloud can see only the evidence pack, not raw data.",
            "If the local model list is empty, the endpoint remains in plan/dry-run mode.",
        ],
        "runtime_status": runtime,
    }


def _build_approval_plan(
    state: OrchestratorState,
    *,
    load_strategy: dict[str, Any] | None = None,
    cloud_steps: list[dict[str, Any]] | None = None,
    approved_actions: list[str] | None = None,
) -> dict[str, Any]:
    matrix = _approval_matrix()
    prompt = str(state.get("prompt") or "").upper()
    task_type = state.get("task_type")
    dry_run = bool(state.get("dry_run", True))
    cloud_enabled = any(bool(step.get("enabled")) for step in (cloud_steps or []))
    approved = set(approved_actions or state.get("approved_actions") or [])
    requested: set[str] = set()

    if task_type in {"ods_modeling", "dwh_modeling"}:
        requested.add("ODI_CREATE_MAPPING")
        requested.add("DDL")
        if load_strategy and "LIVE_ODI_CREATE" in set(load_strategy.get("approval_required") or []):
            requested.add("ODI_CREATE_MAPPING")
        if not dry_run:
            requested.add("ODI_REPOSITORY_READ_ONLY")
        if bool(state.get("odi_repository_read_only_requested", True)):
            requested.add("ODI_REPOSITORY_READ_ONLY")

    if any(token in prompt for token in ("DROP", "ALTER", "TRUNCATE", "RECREATE", "REPLACE TARGET")):
        requested.add("DROP_OR_ALTER_TARGET")

    if cloud_enabled or bool(state.get("allow_cloud_review")):
        requested.add("CLOUD_EVIDENCE_SEND")

    if any(token in prompt for token in (".ENV", "PASSWORD", "API_KEY", "SECRET", "TOKEN", "CONNECTION STRING")):
        requested.add("CREDENTIAL_OR_ENV_CHANGE")

    gates: list[dict[str, Any]] = []
    for action, rule in matrix.items():
        active = action in requested
        is_approved = active and action in approved
        gates.append({
            "action": action,
            "title": rule["title"],
            "required_roles": rule["required_roles"],
            "status": (
                "approved_for_current_run"
                if is_approved
                else "required_before_live_execution"
                if active
                else "not_triggered"
            ),
            "auto_approved": False,
            "dry_run_allowed": True,
            "decision": (
                "approved_by_user_or_role"
                if is_approved
                else "blocked_until_user_approval"
                if active
                else "not_required_for_current_dry_run"
            ),
            "denial_result": rule["denial_result"],
            "audit_required": rule["audit_required"],
        })

    return {
        "version": 1,
        "mode": "dry_run_guarded" if dry_run else "requires_approval",
        "matrix": matrix,
        "required_actions": [
            gate
            for gate in gates
            if gate["status"] in {"required_before_live_execution", "approved_for_current_run"}
        ],
        "gates": gates,
        "summary": {
            "required_count": len(requested),
            "auto_approved_count": 0,
            "approved_count": len(requested & approved),
            "required_roles": sorted({
                role
                for gate in gates
                if gate["status"] in {"required_before_live_execution", "approved_for_current_run"}
                for role in gate["required_roles"]
            }),
        },
        "policy": "No live DDL, ODI repository write, cloud evidence send, or credential/env change runs without explicit user approval.",
    }


def _risk_assessment(state: OrchestratorState, columns: list[dict[str, Any]], pk_columns: list[str]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    if not columns:
        risks.append({"level": "high", "area": "metadata", "message": "Source column metadata could not be read."})
    if not pk_columns:
        risks.append({"level": "medium", "area": "reconciliation", "message": "No PK; reconciliation requires row count and an alternative key strategy."})
    if state.get("allow_cloud_review"):
        risks.append({"level": "medium", "area": "privacy", "message": "Cloud review is enabled; data must stay within the evidence pack."})
    load_strategy = state.get("oracle_load_strategy", {})
    if load_strategy.get("strategy") in {"full_load_snapshot", "full_load_then_merge_by_pk"}:
        risks.append({"level": "medium", "area": "cdc", "message": "The Oracle CDC/watermark decision must be clarified for incremental loading."})
    if not risks:
        risks.append({"level": "low", "area": "initial_plan", "message": "No critical risk was found for the dry-run plan."})
    return risks


def _quality_gates(columns: list[dict[str, Any]], pk_columns: list[str]) -> list[dict[str, Any]]:
    return [
        {"gate": "source_metadata", "status": "pass" if columns else "fail", "detail": f"{len(columns)} columns"},
        {"gate": "pk_strategy", "status": "pass" if pk_columns else "review", "detail": ", ".join(pk_columns) or "No PK"},
        {"gate": "cloud_payload", "status": "pass", "detail": "The evidence pack contains no raw rows or credentials"},
        {"gate": "live_side_effects", "status": "pass", "detail": "DDL/ODI creation is disabled in dry-run mode"},
    ]


def node_init(state: OrchestratorState) -> dict[str, Any]:
    job_id = state.get("job_id") or str(uuid.uuid4())
    return {
        "job_id": job_id,
        "status": "running",
        "dry_run": state.get("dry_run", True),
        "allow_cloud_review": state.get("allow_cloud_review", False),
        "warnings": list(state.get("warnings", [])),
        "log": _log(state, "init", "SUCCESS", f"job_id={job_id}"),
    }


def node_intent_classifier(state: OrchestratorState) -> dict[str, Any]:
    prompt = state.get("prompt", "")
    if not _is_modeling_request(prompt):
        warnings = list(state.get("warnings", []))
        warnings.append("The prompt was not classified as an ODS/DWH/ODI modeling request.")
        return {
            "task_type": "unsupported",
            "source_table": "",
            "target_layer": "",
            "warnings": warnings,
            "log": _log(state, "intent_classifier", "SKIPPED", "unsupported prompt"),
        }
    source_table = _extract_source_table(prompt)
    if not source_table:
        warnings = list(state.get("warnings", []))
        warnings.append("A modeling request was detected, but no source table was found.")
        return {
            "task_type": "unsupported",
            "source_table": "",
            "target_layer": "",
            "warnings": warnings,
            "log": _log(state, "intent_classifier", "FAILED", "source table missing"),
        }
    target_layer = _infer_target_layer(prompt)
    task_type = "ods_modeling" if target_layer == "ODS" else "dwh_modeling"
    return {
        "task_type": task_type,
        "source_table": source_table,
        "target_layer": target_layer,
        "log": _log(state, "intent_classifier", "SUCCESS", f"{task_type}:{source_table}->{target_layer}"),
    }


def node_privacy_classifier(state: OrchestratorState) -> dict[str, Any]:
    prompt = state.get("prompt", "")
    sensitive_patterns = (".ENV", "PASSWORD", "API_KEY", "SECRET", "TOKEN", "PRIVATE KEY")
    privacy = "restricted" if any(p in prompt.upper() for p in sensitive_patterns) else "metadata_only"
    warnings = list(state.get("warnings", []))
    if privacy == "restricted":
        warnings.append("Prompt credential/secret benzeri ifade iceriyor; cloud review kapali tutulmali.")
    return {
        "privacy_level": privacy,
        "warnings": warnings,
        "log": _log(state, "privacy_classifier", "SUCCESS", privacy),
    }


def node_tool_selector(state: OrchestratorState) -> dict[str, Any]:
    if state.get("task_type") == "unsupported":
        return {"tool_plan": [], "log": _log(state, "tool_selector", "SKIPPED", "unsupported prompt")}
    tools = [
        {"name": "oracle.describe_table", "required": True, "runs": "local"},
        {"name": "rag.search", "required": False, "runs": "local"},
        {"name": "mapping.generate_ods", "required": True, "runs": "local"},
        {"name": "oracle.plan_load_strategy", "required": True, "runs": "local"},
        {"name": "odi.dry_run_mapping", "required": True, "runs": "local"},
        {"name": "reconcile.plan_checks", "required": True, "runs": "local"},
    ]
    if state.get("allow_cloud_review") and state.get("privacy_level") != "restricted":
        tools.append({"name": "llm.cloud_review", "required": False, "runs": "cloud_evidence_only"})
    return {"tool_plan": tools, "log": _log(state, "tool_selector", "SUCCESS", f"{len(tools)} tools")}


def node_planner(state: OrchestratorState) -> dict[str, Any]:
    if state.get("task_type") == "unsupported":
        runtime = _runtime_status()
        routing = _routing_decision(state, runtime)
        warnings = list(state.get("warnings", []))
        approval_plan = _build_approval_plan(state, cloud_steps=[])
        return {
            "local_steps": [],
            "cloud_steps": [],
            "evidence_pack": _finalize_evidence_pack({
                "job_id": state.get("job_id"),
                "task_type": "unsupported",
                "approval_plan": approval_plan,
                "local_findings": ["The initial supported task type: ods_modeling/dwh_modeling"],
                "question_for_cloud": "",
            }, []),
            "routing_decision": routing,
            "risk_assessment": [
                {"level": "low", "area": "intent", "message": "No data or tools were run for an unrelated prompt."}
            ],
            "approval_plan": approval_plan,
            "quality_gates": [
                {"gate": "intent_supported", "status": "review", "detail": "Not an ODS/DWH/ODI modeling request"},
                {"gate": "live_side_effects", "status": "pass", "detail": "Tool/DDL/ODI was not run"},
            ],
            "test_plan": [{"name": "intent_supported", "expected": "ODS/DWH/ODI modeling prompt"}],
            "warnings": warnings,
            "log": _log(state, "planner", "SKIPPED", "unsupported prompt"),
        }
    table = state.get("source_table", "HR.EMPLOYEES")
    target_layer = state.get("target_layer", "ODS")
    columns, pk_columns, warnings = _summarize_columns(table)
    load_strategy = _oracle_load_strategy(table, columns, pk_columns)
    runtime = _runtime_status()
    routing = _routing_decision(state, runtime)
    risks = _risk_assessment({**state, "oracle_load_strategy": load_strategy}, columns, pk_columns)
    gates = _quality_gates(columns, pk_columns)
    target_table = f"DWH.ODS_{table.replace('.', '_')}" if target_layer == "ODS" else f"DWH.{table.replace('.', '_')}"
    mapping_spec = {
        "mapping_name": f"MAP_{table.replace('.', '_')}_TO_{target_layer}",
        "source_table": table,
        "target_table": target_table,
        "target_layer": target_layer,
        "columns": columns[:80],
        "pk_columns": pk_columns,
        "oracle_load_strategy": load_strategy,
    }
    odi_dry_run = _odi_dry_run_mapping(mapping_spec)
    gates.append({
        "gate": "odi_dry_run_mapping",
        "status": "pass" if odi_dry_run["passed"] else "fail",
        "detail": f"{odi_dry_run['summary']['pass']} pass, {odi_dry_run['summary']['warning']} warning, {odi_dry_run['summary']['fail']} fail",
    })
    cloud_steps = [
        {
            "step": "cloud_review",
            "enabled": bool(state.get("allow_cloud_review") and state.get("privacy_level") != "restricted"),
            "input": "evidence_pack_only",
        }
    ]
    approval_plan = _build_approval_plan(state, load_strategy=load_strategy, cloud_steps=cloud_steps)
    odi_read_only_preflight = _odi_read_only_preflight(mapping_spec, approval_plan)
    gates.append({
        "gate": "odi_read_only_preflight",
        "status": "pass" if odi_read_only_preflight["status"] in {"blocked_until_approval", "ready_for_read_only_connection"} else "fail",
        "detail": odi_read_only_preflight["status"],
    })
    local_steps = [
        {"step": "describe_source", "tool": "oracle.describe_table", "input": {"table": table}},
        {"step": "retrieve_patterns", "tool": "rag.search", "input": {"query": f"{table} {target_layer} modeling"}},
        {
            "step": "generate_mapping_spec",
            "tool": "mapping.generate_ods",
            "input": {"source_table": table, "target_table": target_table, "dry_run": True},
            "output": {"mapping_name": mapping_spec["mapping_name"]},
        },
        {
            "step": "plan_oracle_load_strategy",
            "tool": "oracle.plan_load_strategy",
            "input": {"source_table": table, "pk_columns": pk_columns},
        },
        {
            "step": "odi_dry_run",
            "tool": "odi.dry_run_mapping",
            "input": {"target_table": target_table, "mode": "logical_only"},
            "output": {
                "passed": odi_dry_run["passed"],
                "summary": odi_dry_run["summary"],
                "side_effects": odi_dry_run["side_effects"],
            },
        },
        {
            "step": "odi_read_only_preflight",
            "tool": "odi.read_only_preflight",
            "input": {"target_table": target_table, "requested_action": "ODI_REPOSITORY_READ_ONLY"},
            "output": {
                "status": odi_read_only_preflight["status"],
                "ready": odi_read_only_preflight["ready"],
                "repository_connection_attempted": odi_read_only_preflight["repository_connection_attempted"],
            },
        },
        {
            "step": "plan_reconciliation",
            "tool": "reconcile.plan_checks",
            "input": {"source_table": table, "target_table": target_table},
        },
    ]
    evidence_pack = _finalize_evidence_pack({
        "job_id": state.get("job_id"),
        "task_type": state.get("task_type"),
        "source_table": table,
        "target_table": target_table,
        "target_layer": target_layer,
        "columns": columns[:80],
        "pk_columns": pk_columns,
        "oracle_load_strategy": load_strategy,
        "odi_dry_run": odi_dry_run,
        "odi_read_only_preflight": odi_read_only_preflight,
        "approval_plan": approval_plan,
        "local_findings": [
            f"{len(columns)} columns of metadata were found" if columns else "Column metadata could not be read",
            f"PK: {', '.join(pk_columns)}" if pk_columns else "No PK found, or this may be a fact table",
            f"Load strategy: {load_strategy.get('strategy')}",
            f"ODI dry-run: {'passed' if odi_dry_run['passed'] else 'failed'} ({odi_dry_run['summary']})",
            f"ODI read-only preflight: {odi_read_only_preflight['status']}",
        ],
        "question_for_cloud": "Review the mapping strategy and test scope without raw data.",
    }, columns[:80])
    test_plan = [
        {"name": "metadata_available", "expected": "source columns > 0"},
        {"name": "target_naming", "expected": "ODS_<SCHEMA>_<TABLE>"},
        {"name": "pk_strategy", "expected": "Use the PK as a reconciliation key if present"},
        {"name": "oracle_load_strategy", "expected": "full/incremental/KM/CDC strategy selected"},
        {"name": "row_count_reconciliation", "expected": "source/target row count compare"},
        {"name": "nullability_check", "expected": "DQ checks for NOT NULL columns"},
    ]
    return {
        "local_steps": local_steps,
        "cloud_steps": cloud_steps,
        "evidence_pack": evidence_pack,
        "odi_dry_run": odi_dry_run,
        "odi_read_only_preflight": odi_read_only_preflight,
        "routing_decision": routing,
        "risk_assessment": risks,
        "approval_plan": approval_plan,
        "quality_gates": gates,
        "test_plan": test_plan,
        "warnings": list(state.get("warnings", [])) + warnings,
        "log": _log(state, "planner", "SUCCESS", f"{table}->{target_table}"),
    }


def node_test_checker(state: OrchestratorState) -> dict[str, Any]:
    evidence = state.get("evidence_pack", {})
    cloud_steps = state.get("cloud_steps", [])
    cloud_enabled = any(bool(step.get("enabled")) for step in cloud_steps)
    cloud_evidence_only = all(step.get("input") == "evidence_pack_only" for step in cloud_steps if step.get("enabled"))
    privacy = state.get("privacy_level")
    odi_dry_run = state.get("odi_dry_run", {})
    odi_read_only_preflight = state.get("odi_read_only_preflight", {})
    approval_plan = state.get("approval_plan", {})
    approval_gates = list(approval_plan.get("gates") or [])
    checks = [
        {"check": "dry_run_only", "ok": bool(state.get("dry_run", True))},
        {"check": "no_raw_rows_in_evidence", "ok": "rows" not in evidence and "sample_rows" not in evidence},
        {"check": "no_blocked_keys_in_evidence", "ok": not _evidence_has_blocked_key(evidence)},
        {"check": "no_secret_patterns_in_evidence", "ok": not _evidence_contains_secret(evidence)},
        {"check": "evidence_pack_v2", "ok": evidence.get("evidence_pack_version") == 2},
        {"check": "sensitivity_classified", "ok": "sensitivity_summary" in evidence},
        {"check": "no_cloud_without_flag", "ok": (not cloud_enabled) or bool(state.get("allow_cloud_review"))},
        {"check": "restricted_blocks_cloud", "ok": not (privacy == "restricted" and cloud_enabled)},
        {"check": "cloud_evidence_only", "ok": (not cloud_enabled) or cloud_evidence_only},
        {"check": "metadata_present", "ok": state.get("task_type") == "unsupported" or bool(evidence.get("columns"))},
        {"check": "odi_dry_run_side_effect_free", "ok": state.get("task_type") == "unsupported" or odi_dry_run.get("side_effects") is False},
        {"check": "odi_dry_run_passed", "ok": state.get("task_type") == "unsupported" or odi_dry_run.get("passed") is True},
        {
            "check": "odi_read_only_preflight_connectionless",
            "ok": state.get("task_type") == "unsupported"
            or (
                odi_read_only_preflight.get("repository_connection_attempted") is False
                and odi_read_only_preflight.get("side_effects") is False
            ),
        },
        {
            "check": "odi_read_only_preflight_status_valid",
            "ok": state.get("task_type") == "unsupported"
            or odi_read_only_preflight.get("status") in {"blocked_until_approval", "ready_for_read_only_connection"},
        },
        {"check": "approval_matrix_v1", "ok": approval_plan.get("version") == 1 and bool(approval_plan.get("matrix"))},
        {"check": "no_auto_approved_live_gate", "ok": all(gate.get("auto_approved") is False for gate in approval_gates)},
        {"check": "approval_roles_present", "ok": bool(approval_plan.get("summary", {}).get("required_roles") or state.get("task_type") == "unsupported")},
    ]
    return {"test_plan": list(state.get("test_plan", [])) + checks, "log": _log(state, "test_checker", "SUCCESS")}


def node_final_composer(state: OrchestratorState) -> dict[str, Any]:
    # Observability finalization is handled by _observable_node wrapper after this
    # node returns. Calling _finalize_observability here would cause a double
    # finalization (two gpu_after snapshots, wrong finished_at).
    observability = state.get("observability", {})
    response = {
        "job_id": state.get("job_id"),
        "status": "planned",
        "mode": "dry_run" if state.get("dry_run", True) else "requires_approval",
        "task_type": state.get("task_type"),
        "privacy_level": state.get("privacy_level"),
        "source_table": state.get("source_table"),
        "target_layer": state.get("target_layer"),
        "tool_plan": state.get("tool_plan", []),
        "local_steps": state.get("local_steps", []),
        "cloud_steps": state.get("cloud_steps", []),
        "evidence_pack": state.get("evidence_pack", {}),
        "odi_dry_run": state.get("odi_dry_run", {}),
        "odi_read_only_preflight": state.get("odi_read_only_preflight", {}),
        "routing_decision": state.get("routing_decision", {}),
        "risk_assessment": state.get("risk_assessment", []),
        "approval_plan": state.get("approval_plan", {}),
        "quality_gates": state.get("quality_gates", []),
        "test_plan": state.get("test_plan", []),
        "hardware_profile": state.get("hardware_profile", {}),
        "observability": observability,
        "warnings": state.get("warnings", []),
        "approval_required_for": _legacy_approval_required_for(),
    }
    return {
        "status": "success",
        "final_response": response,
        "observability": observability,
        "log": _log(state, "final_composer", "SUCCESS", "dry-run plan ready"),
    }


def build_orchestrator_graph():
    graph = StateGraph(OrchestratorState)
    graph.add_node("init", _observable_node("init", node_init))
    graph.add_node("intent_classifier", _observable_node("intent_classifier", node_intent_classifier))
    graph.add_node("privacy_classifier", _observable_node("privacy_classifier", node_privacy_classifier))
    graph.add_node("tool_selector", _observable_node("tool_selector", node_tool_selector))
    graph.add_node("planner", _observable_node("planner", node_planner))
    graph.add_node("test_checker", _observable_node("test_checker", node_test_checker))
    graph.add_node("final_composer", _observable_node("final_composer", node_final_composer))
    graph.add_edge(START, "init")
    graph.add_edge("init", "intent_classifier")
    graph.add_edge("intent_classifier", "privacy_classifier")
    graph.add_edge("privacy_classifier", "tool_selector")
    graph.add_edge("tool_selector", "planner")
    graph.add_edge("planner", "test_checker")
    graph.add_edge("test_checker", "final_composer")
    graph.add_edge("final_composer", END)
    return graph.compile(checkpointer=MemorySaver())


_GRAPH = None


def get_orchestrator_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_orchestrator_graph()
    return _GRAPH


def _persist_job(state: dict[str, Any]) -> None:
    try:
        out_dir = Path(__file__).resolve().parent / "logs"
        out_dir.mkdir(exist_ok=True)
        payload = {
            "timestamp": _now(),
            "job_id": state.get("job_id"),
            "prompt": state.get("prompt"),
            "final_response": state.get("final_response"),
            "log": state.get("log", []),
            "observability": state.get("observability", {}),
        }
        with (out_dir / "ai_orchestrator_jobs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # Audit write should never break the user-facing dry-run response.
        pass


def orchestrate(
    prompt: str,
    *,
    dry_run: bool = True,
    allow_cloud_review: bool = False,
    approved_actions: list[str] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    graph = get_orchestrator_graph()
    runtime = _runtime_status()
    state: OrchestratorState = {
        "job_id": job_id or str(uuid.uuid4()),
        "prompt": prompt.strip(),
        "dry_run": dry_run,
        "allow_cloud_review": allow_cloud_review,
        "approved_actions": list(approved_actions or []),
        "odi_repository_read_only_requested": True,
        "hardware_profile": _hardware_profile(),
        "observability": _initial_observability(job_id or str(uuid.uuid4()), runtime),
        "warnings": [],
        "log": [],
    }
    state["observability"]["job_id"] = state["job_id"]
    config = {"configurable": {"thread_id": state["job_id"]}}
    try:
        result = dict(graph.invoke(state, config=config))
        response = dict(result.get("final_response") or {})
        if not response:
            observability = _finalize_observability(
                result.get("observability", state.get("observability", {})),
                "FAILED",
                result.get("routing_decision", {}),
            )
            response = {
                "job_id": state["job_id"],
                "status": "failed",
                "error": "Orchestrator did not produce a final response.",
                "warnings": ["Internal state was not returned through the API."],
                "observability": observability,
            }
        elif "observability" not in response:
            response["observability"] = _finalize_observability(
                result.get("observability", state.get("observability", {})),
                "SUCCEEDED",
                result.get("routing_decision", {}),
            )
        _persist_job({**result, "final_response": response})
        return response
    except Exception as exc:
        observability = _finalize_observability(state.get("observability", {}), "FAILED")
        response = {
            "job_id": state["job_id"],
            "status": "failed",
            "error": str(exc),
            "mode": "dry_run" if dry_run else "requires_approval",
            "approval_required_for": _legacy_approval_required_for(),
            "approval_plan": _build_approval_plan(state),
            "warnings": ["The orchestrator error was converted to a controlled response; internal state was not returned through the API."],
            "observability": observability,
        }
        _persist_job({**state, "final_response": response, "observability": observability, "error": str(exc)})
        return response


def get_tool_registry() -> dict[str, dict[str, Any]]:
    return TOOL_REGISTRY
