"""Read-only source-to-ODS flow inventory helpers.

The functions in this module inspect metadata only. They never create, update,
drop, truncate, or execute ODI SDK operations.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from db import execute_select, get_source_tables
from odi_repository import OdiRepositoryReader


_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_$]{0,127}$")


def _clean_identifier(value: str, field_name: str) -> str:
    text = (value or "").strip().upper()
    if not _IDENTIFIER_RE.match(text):
        raise ValueError(f"Invalid {field_name}.")
    return text


def _short_name(value: str | None) -> str:
    text = (value or "").strip().upper()
    if not text:
        return ""
    return text.split(".")[-1]


def _tokens(*values: Any) -> set[str]:
    result: set[str] = set()
    for value in values:
        for token in re.split(r"[^A-Z0-9]+", str(value or "").upper()):
            if token:
                result.add(token)
    return result


def _score_datastore(row: dict[str, Any], table_name: str, schema_hint: str, role: str) -> int:
    datastore = _short_name(str(row.get("datastore_name") or ""))
    resource = _short_name(str(row.get("resource_name") or ""))
    if table_name not in {datastore, resource}:
        return -100

    row_tokens = _tokens(
        row.get("model_code"),
        row.get("model_name"),
        row.get("logical_schema"),
        row.get("datastore_name"),
        row.get("resource_name"),
    )
    schema = schema_hint.upper()
    score = 50
    if schema in row_tokens or f"SRC_{schema}" in row_tokens or f"TGT_{schema}" in row_tokens:
        score += 30

    role_key = role.lower()
    if role_key == "source":
        if "SRC" in row_tokens:
            score += 15
        if "TGT" in row_tokens:
            score -= 20
    elif role_key == "target":
        if "TGT" in row_tokens:
            score += 15
        if schema in {"DWH", "ODS"} and ({"DWH", "ODS"} & row_tokens):
            score += 10
        if "SRC" in row_tokens:
            score -= 20
    return score


def _has_datastore(datastore_rows: list[dict[str, Any]], table_name: str, schema_hint: str, role: str) -> bool:
    scores = [
        _score_datastore(row, table_name.upper(), schema_hint.upper(), role)
        for row in datastore_rows
    ]
    return bool(scores) and max(scores) > 0


def _ref_matches(row: dict[str, Any], *, table_name: str, schema_hint: str | None = None) -> bool:
    table = table_name.upper()
    qualified = str(row.get("table_qualified_name") or "").upper()
    comp = str(row.get("comp_name") or "").upper()
    if table not in {_short_name(qualified), _short_name(comp)}:
        return False

    if not schema_hint:
        return True

    schema = schema_hint.upper()
    ref_tokens = _tokens(qualified, comp)
    if "." not in qualified:
        return True
    return schema in ref_tokens or f"SRC_{schema}" in ref_tokens or f"TGT_{schema}" in ref_tokens


def _mapping_matches_flow(
    refs: Iterable[dict[str, Any]],
    *,
    source_schema: str,
    source_table: str,
    target_owner: str,
    target_table: str,
) -> tuple[bool, str, str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ref in refs:
        mapping_name = str(ref.get("mapping_name") or "").strip()
        if not mapping_name:
            continue
        grouped.setdefault(mapping_name, []).append(ref)

    def _project_folder(mapping_refs: list[dict[str, Any]]) -> tuple[str, str]:
        for ref in mapping_refs:
            project = str(ref.get("project_code") or ref.get("project_name") or "").strip()
            folder = str(ref.get("folder_name") or "").strip()
            if project or folder:
                return project, folder
        return "", ""

    expected_mapping_name = f"MAP_{source_schema}_{source_table}_TO_ODS"
    for mapping_name, mapping_refs in grouped.items():
        has_source = any(
            str(ref.get("is_target") or "N").upper() != "Y"
            and _ref_matches(ref, table_name=source_table, schema_hint=source_schema)
            for ref in mapping_refs
        )
        has_target = any(
            str(ref.get("is_target") or "N").upper() == "Y"
            and _ref_matches(ref, table_name=target_table, schema_hint=target_owner)
            for ref in mapping_refs
        )
        if has_source and has_target:
            project, folder = _project_folder(mapping_refs)
            return True, mapping_name, project, folder

    # Fallback for repositories where component port metadata is incomplete.
    for mapping_name, mapping_refs in grouped.items():
        if mapping_name.upper() != expected_mapping_name:
            continue
        has_source_name = any(_ref_matches(ref, table_name=source_table, schema_hint=source_schema) for ref in mapping_refs)
        has_target_name = any(_ref_matches(ref, table_name=target_table, schema_hint=target_owner) for ref in mapping_refs)
        if has_source_name and has_target_name:
            project, folder = _project_folder(mapping_refs)
            return True, mapping_name, project, folder

    return False, "", "", ""


def _status_for_row(
    *,
    mapping_exists: bool,
    source_datastore_exists: bool,
    target_datastore_exists: bool,
    target_table_exists: bool,
) -> str:
    if mapping_exists:
        return "FLOW_OK"
    if not source_datastore_exists:
        return "SOURCE_DATASTORE_MISSING"
    if not target_table_exists:
        return "TARGET_TABLE_MISSING"
    if not target_datastore_exists:
        return "TARGET_DATASTORE_MISSING"
    return "MAPPING_MISSING"


def expected_ods_table_name(source_schema: str, source_table: str) -> str:
    return f"ODS_{source_schema.upper()}_{source_table.upper()}"


def build_ods_flow_inventory(
    *,
    source_tables: list[str],
    target_table_names: set[str],
    datastore_rows: list[dict[str, Any]],
    mapping_ref_rows: list[dict[str, Any]],
    source_schema: str = "HR",
    target_owner: str = "DWH",
) -> list[dict[str, Any]]:
    source_schema = _clean_identifier(source_schema, "source_schema")
    target_owner = _clean_identifier(target_owner, "target_owner")
    target_names = {name.upper() for name in target_table_names}

    rows: list[dict[str, Any]] = []
    for full_source in sorted({str(t).upper() for t in source_tables}):
        if "." in full_source:
            owner, table = full_source.split(".", 1)
        else:
            owner, table = source_schema, full_source
        if owner != source_schema:
            continue

        table = _clean_identifier(table, "source_table")
        expected_target = expected_ods_table_name(source_schema, table)
        source_ds_exists = _has_datastore(datastore_rows, table, source_schema, "source")
        target_ds_exists = _has_datastore(datastore_rows, expected_target, target_owner, "target")
        target_table_exists = expected_target in target_names
        mapping_exists, mapping_name, odi_project, odi_folder = _mapping_matches_flow(
            mapping_ref_rows,
            source_schema=source_schema,
            source_table=table,
            target_owner=target_owner,
            target_table=expected_target,
        )
        odi_path = " / ".join(part for part in [odi_project, odi_folder] if part)

        rows.append({
            "SOURCE_TABLE": f"{source_schema}.{table}",
            "EXPECTED_TARGET_TABLE": f"{target_owner}.{expected_target}",
            "TARGET_TABLE_EXISTS": target_table_exists,
            "SOURCE_DATASTORE_EXISTS": source_ds_exists,
            "TARGET_DATASTORE_EXISTS": target_ds_exists,
            "ODI_MAPPING_EXISTS": mapping_exists,
            "MAPPING_NAME": mapping_name,
            "ODI_PROJECT": odi_project,
            "ODI_FOLDER": odi_folder,
            "ODI_PATH": odi_path,
            "STATUS": _status_for_row(
                mapping_exists=mapping_exists,
                source_datastore_exists=source_ds_exists,
                target_datastore_exists=target_ds_exists,
                target_table_exists=target_table_exists,
            ),
        })
    return rows


def _load_target_table_names(target_owner: str, expected_targets: list[str]) -> set[str]:
    if not expected_targets:
        return set()
    owner = _clean_identifier(target_owner, "target_owner")
    clean_targets = [_clean_identifier(name, "target_table") for name in expected_targets]
    literals = ", ".join("'" + name.replace("'", "''") + "'" for name in clean_targets)
    sql = f"""
        SELECT TABLE_NAME
        FROM ALL_TABLES
        WHERE OWNER = '{owner}'
          AND TABLE_NAME IN ({literals})
    """
    df = execute_select(sql)
    if df.empty or "TABLE_NAME" not in df.columns:
        return set()
    return {str(value).upper() for value in df["TABLE_NAME"].tolist()}


def get_ods_flow_inventory(source_schema: str = "HR", target_owner: str = "DWH") -> list[dict[str, Any]]:
    source_schema = _clean_identifier(source_schema, "source_schema")
    target_owner = _clean_identifier(target_owner, "target_owner")

    source_tables = [
        table.upper()
        for table in get_source_tables()
        if table.upper().startswith(f"{source_schema}.")
    ]
    source_short_names = [_short_name(table) for table in source_tables]
    expected_targets = [expected_ods_table_name(source_schema, name) for name in source_short_names]
    target_table_names = _load_target_table_names(target_owner, expected_targets)

    reader = OdiRepositoryReader()
    datastore_rows = reader.list_datastores_by_names(source_short_names + expected_targets)
    mapping_ref_rows = reader.list_mapping_table_refs()

    return build_ods_flow_inventory(
        source_tables=source_tables,
        target_table_names=target_table_names,
        datastore_rows=datastore_rows,
        mapping_ref_rows=mapping_ref_rows,
        source_schema=source_schema,
        target_owner=target_owner,
    )
