from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

logger = logging.getLogger(__name__)

SchemaLookup = Callable[[str], Any]
KmOptionsLookup = Callable[[str], dict[str, Any]]


class MappingDetailPayload(TypedDict):
    MAPPING_NAME: str
    SRC_TABLE: str
    TGT_TABLE: str
    sources: list[dict[str, Any]]
    target: dict[str, Any]
    columns: list[dict[str, Any]]
    expression_rows: list[dict[str, Any]]
    source_columns: list[dict[str, Any]]
    filters: list[str]
    joins: list[str]
    km_options: dict[str, Any]
    STATUS: str


@dataclass(frozen=True)
class MappingDetailPresentation:
    kind: Literal["payload", "raw", "parse_error"]
    payload: MappingDetailPayload | None = None
    raw_json: str | None = None


def _safe_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _short_table(value: object) -> str:
    text = _safe_str(value)
    return text.split(".")[-1] if text else ""


def _normalize_schema_table(value: object) -> str:
    text = _safe_str(value)
    if not text:
        return ""
    parts = text.split(".")
    if len(parts) > 1:
        owner, table = parts[-2].upper(), parts[-1]
        aliases = {
            "TGT_DWH": "DWH",
            "LGCL_ORACLE_DWH_UNIT": "DWH",
            "ODS": "DWH",
            "SRC_HR": "HR",
            "HR": "HR",
            "SRC_SH": "SH",
            "SH": "SH",
        }
        return f"{aliases.get(owner, owner)}.{table}"
    if text.upper().startswith(("ODS_", "DWH_", "SKEY_")):
        return f"DWH.{text}"
    return text


def _format_odi_type(item: dict[str, Any]) -> str:
    data_type = _safe_str(item.get("DATA_TYPE") or item.get("FULL_DATA_TYPE"))
    length = _safe_str(item.get("COL_LENGTH") or item.get("DATA_LENGTH"))
    precision = _safe_str(item.get("DATA_PRECISION") or item.get("PRECISION"))
    scale = _safe_str(item.get("DATA_SCALE") or item.get("SCALE"))
    if data_type and "(" in data_type:
        return data_type
    if data_type:
        size = precision or length
        if scale:
            size = f"{size},{scale}" if size else scale
        return f"{data_type}({size})" if size else data_type
    return f"Length {length}" if length else "N/A"


def _is_complex_expression(value: object) -> bool:
    text = _safe_str(value)
    return bool(
        text
        and (
            text.startswith(("'", '"'))
            or " " in text
            or "(" in text
            or any(operator in text for operator in ("+", "-", "*", "/", "||"))
            or "." not in text
        )
    )


def _schema_type_map(schema_lookup: SchemaLookup, table_name: str) -> dict[str, str]:
    try:
        schema_df = schema_lookup(table_name)
        return {
            _safe_str(row["COLUMN_NAME"]).upper(): _safe_str(row["FULL_DATA_TYPE"])
            for _, row in schema_df.iterrows()
        }
    except Exception as exc:
        logger.debug("Mapping detail schema enrichment skipped: %s", type(exc).__name__)
        return {}


def _collect_rows(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], str, list[str]]:
    columns: list[dict[str, Any]] = []
    source_columns: list[dict[str, Any]] = []
    source_tables: set[str] = set()
    target_table = "?"
    filters: list[str] = []
    datastore_types = {"FILE", "XML", "DATASTORE", "XSD"}
    for item in items:
        qualified = _safe_str(item.get("TABLE_QUALIFIED_NAME"))
        component = _safe_str(item.get("COMP_NAME"))
        component_type = _safe_str(item.get("COMP_TYPE")).upper()
        attribute = _safe_str(item.get("ATTR_NAME"))
        expression = _safe_str(item.get("EXPRESSION"))
        is_target = (
            qualified.upper().startswith(("DWH.", "ODS."))
            or "ODS_" in qualified.upper()
            or component.upper().startswith(("ODS_", "TGT_"))
        )
        if is_target:
            target_table = qualified or component
        elif (qualified or component) and component_type in datastore_types:
            source_tables.add(qualified or component)
            if attribute and attribute != "FILTER_CONDITION":
                source_columns.append({
                    "table": _normalize_schema_table(qualified or component),
                    "component": component or _short_table(qualified),
                    "column": attribute,
                    "data_type": _format_odi_type(item),
                })
        if component_type == "FILTER" and attribute == "FILTER_CONDITION" and expression:
            filters.append(expression)
        if expression and attribute and attribute != "FILTER_CONDITION":
            columns.append({
                "src": expression,
                "tgt": attribute,
                "type": _format_odi_type(item),
                "is_exp": _is_complex_expression(expression),
            })
    return columns, source_columns, source_tables, target_table, filters


def _component_details(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
    components: dict[str, dict[str, Any]] = {}
    target_counts: dict[str, int] = {}
    joins: list[str] = []
    for item in items:
        qualified = _safe_str(item.get("TABLE_QUALIFIED_NAME"))
        component = _safe_str(item.get("COMP_NAME"))
        component_type = _safe_str(item.get("COMP_TYPE")).upper()
        attribute = _safe_str(item.get("ATTR_NAME"))
        expression = _safe_str(item.get("EXPRESSION"))
        table_name = qualified or component
        if table_name and component_type in {"FILE", "XML", "DATASTORE", "XSD"}:
            key = component.upper() or table_name.upper()
            components[key] = {
                "name": table_name,
                "component": component or _short_table(table_name),
                "type": component_type,
            }
            if expression:
                target_counts[key] = target_counts.get(key, 0) + 1
        if component_type in {"JOIN", "LOOKUP"} and attribute in {"JOIN_CONDITION", "LOOKUP_CONDITION"} and expression:
            joins.append(expression)
    return components, max(target_counts, key=target_counts.get, default=""), joins


def _build_source_details(
    components: dict[str, dict[str, Any]],
    target_key: str,
    columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    details = []
    for key, table_info in components.items():
        if key == target_key:
            continue
        component = _safe_str(table_info.get("component"))
        table_short = _short_table(table_info.get("name")).upper()
        usage_count = sum(
            1 for column in columns if component and f"{component.upper()}." in _safe_str(column.get("src")).upper()
        )
        details.append({
            "name": table_info.get("name"),
            "component": component,
            "type": table_info.get("type"),
            "role": "Lookup / auxiliary source" if table_short.startswith("SKEY_") else "Main source",
            "usage_count": usage_count,
        })
    return sorted(details, key=lambda row: (row.get("role") != "Main source", row.get("name") or ""))


def _enrich_types(
    target_table: str,
    source_details: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    source_columns: list[dict[str, Any]],
    schema_lookup: SchemaLookup,
) -> None:
    target_types = _schema_type_map(schema_lookup, _normalize_schema_table(target_table))
    for column in columns:
        enriched = target_types.get(_safe_str(column.get("tgt")).upper())
        if enriched:
            column["type"] = enriched

    source_type_maps: dict[str, dict[str, str]] = {}
    for source in source_details:
        source_name = _normalize_schema_table(source.get("name"))
        table_key = _short_table(source_name).upper()
        component_key = _safe_str(source.get("component")).upper()
        type_map = _schema_type_map(schema_lookup, source_name)
        for key in (table_key, component_key):
            if key:
                source_type_maps[key] = type_map
        for source_column in source_columns:
            if _short_table(source_column.get("table")).upper() == table_key or _safe_str(source_column.get("component")).upper() == component_key:
                enriched = type_map.get(_safe_str(source_column.get("column")).upper())
                if enriched:
                    source_column["data_type"] = enriched

    for column in columns:
        current_type = _safe_str(column.get("type"))
        expression = _safe_str(column.get("src"))
        if current_type and not current_type.startswith("Length ") and current_type != "N/A":
            continue
        upper_expression = expression.upper()
        if upper_expression in {"SYSDATE", "CURRENT_DATE", "CURRENT_TIMESTAMP"}:
            column["type"] = "TIMESTAMP" if upper_expression == "CURRENT_TIMESTAMP" else "DATE"
        elif len(expression) >= 2 and expression[0] == expression[-1] == "'":
            column["type"] = f"VARCHAR2({max(len(expression) - 2, 1)})"
        elif "." in expression and not _is_complex_expression(expression):
            source_component, source_attribute = expression.rsplit(".", 1)
            lookup = source_type_maps.get(source_component.upper(), {})
            if lookup.get(source_attribute.upper()):
                column["type"] = lookup[source_attribute.upper()]


def present_mapping_detail(
    mapping_name: str,
    data_json: str,
    schema_lookup: SchemaLookup,
    km_options_lookup: KmOptionsLookup,
) -> MappingDetailPresentation:
    try:
        items = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return MappingDetailPresentation(kind="parse_error", raw_json=data_json)
    if not isinstance(items, list):
        return MappingDetailPresentation(kind="raw", raw_json=data_json)

    columns, source_columns, source_tables, target_table, filters = _collect_rows(items)
    components, target_key, joins = _component_details(items)
    target_info = components.get(target_key, {})
    target_table = _safe_str(target_info.get("name")) or target_table
    source_details = _build_source_details(components, target_key, columns)
    _enrich_types(target_table, source_details, columns, source_columns, schema_lookup)
    columns = [column for column in columns if column.get("tgt") not in {"JOIN_CONDITION", "LOOKUP_CONDITION"}]
    payload: MappingDetailPayload = {
        "MAPPING_NAME": mapping_name,
        "SRC_TABLE": ", ".join(row["name"] for row in source_details if row.get("name")) or ", ".join(sorted(source_tables)) or "?",
        "TGT_TABLE": target_table,
        "sources": source_details,
        "target": target_info,
        "columns": columns,
        "expression_rows": [
            {
                "target_column": column.get("tgt"),
                "expression": column.get("src"),
                "data_type": column.get("type"),
                "is_expression": bool(column.get("is_exp")),
            }
            for column in columns
        ],
        "source_columns": source_columns,
        "filters": filters,
        "joins": joins,
        "km_options": km_options_lookup(mapping_name),
        "STATUS": "Active",
    }
    return MappingDetailPresentation(kind="payload", payload=payload)
