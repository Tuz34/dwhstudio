from __future__ import annotations

import math

import pandas as pd

def df_to_jsonable(df: pd.DataFrame) -> list:
    """Convert a DataFrame to a JSON-safe list of dicts (NaN/Inf to None, numpy types to native values)."""
    df = df.where(pd.notna(df), None)
    records = df.to_dict(orient="records")

    def clean(val):
        if val is None:
            return None
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        if hasattr(val, "item"):   # numpy scalar (int64, float64, etc.)
            return val.item()
        return val

    return [{k: clean(v) for k, v in row.items()} for row in records]


# Page helpers

_DATASTORE_TYPES = frozenset({"FILE", "XML", "DATASTORE", "XSD"})
_TARGET_COMPONENT_TYPES = frozenset({"DATASTORE", "FILE"})
_LAYER_BY_PREFIX = (
    ("DM_", "DM"),
    ("DIM_", "EDW"),
    ("EDW_", "EDW"),
    ("ODS_", "ODS"),
    ("STG_", "STG"),
    ("SKEY_", "SKEY"),
)
_SCHEMA_LAYERS = {"ODS": "ODS", "STG": "STG", "EDW": "EDW", "DM": "DM"}


def _is_target_flag(item: dict) -> bool:
    if "IS_TARGET" in item:
        raw = item.get("IS_TARGET")
    elif "is_target" in item:
        raw = item.get("is_target")
    else:
        return False
    return str(raw if raw is not None else "").strip().upper() == "Y"


def _component_key(item: dict) -> tuple[str, str]:
    comp = str(item.get("COMP_NAME") or "").strip().upper()
    qualified = str(item.get("TABLE_QUALIFIED_NAME") or "").strip()
    return comp, qualified


def _evidence_target(items: list) -> dict | None:
    """The one flagged DATASTORE/FILE row. Two rows are unknown even when the names match."""
    flagged = [
        item for item in items
        if str(item.get("COMP_TYPE") or "").strip().upper() in _TARGET_COMPONENT_TYPES
        and _is_target_flag(item)
    ]
    if len(flagged) != 1:
        return None
    return flagged[0]


def _table_and_owner(item: dict) -> tuple[str, str]:
    qualified = str(item.get("TABLE_QUALIFIED_NAME") or "").strip()
    if not qualified:
        return "DWH", str(item.get("COMP_NAME") or "")
    parts = qualified.split(".")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "DWH", qualified


def _layer_for_target(table: str, owner: str) -> str:
    short = table.upper()
    for prefix, layer in _LAYER_BY_PREFIX:
        if short.startswith(prefix):
            return layer
    return _SCHEMA_LAYERS.get(owner.upper(), "")


def _source_label(item: dict) -> str:
    qualified = str(item.get("TABLE_QUALIFIED_NAME") or "").strip()
    if qualified:
        return qualified
    return str(item.get("COMP_NAME") or "").upper()


def _formatted_sources(items: list, excluded: tuple[str, str] | None) -> str:
    names = set()
    for item in items:
        comp_type = str(item.get("COMP_TYPE") or "").upper()
        if comp_type not in _DATASTORE_TYPES:
            continue
        if excluded is not None and _component_key(item) == excluded:
            continue
        label = _source_label(item)
        if label:
            names.add(label)
    return ", ".join(sorted(names))


def _base_route_source_table(items: list) -> str:
    """Source list the base mappings route produced for these rows.

    When no single IS_TARGET is present, SRC_TABLE stays byte-equal to that
    list. The name checks below are only that historical source partition.
    They do not choose TGT_TABLE, TGT_OWNER, or LAYER.
    """
    sources = set()
    for item in items:
        name = item.get("MAPPING_NAME")
        if not name:
            continue
        qualified_name = str(item.get("TABLE_QUALIFIED_NAME") or "").strip()
        comp_type = str(item.get("COMP_TYPE") or "").upper()
        comp_name = str(item.get("COMP_NAME") or "").upper()
        mapping_name_upper = str(name).upper()
        qualified_short = qualified_name.split(".")[-1].upper() if qualified_name else ""
        named_target = (
            qualified_name.upper().startswith("ODS.")
            or qualified_short.startswith("ODS_")
            or qualified_short.startswith("DWH_")
            or comp_name.startswith("ODS_")
            or comp_name.startswith("DWH_")
            or comp_name.startswith("TGT_")
            or (
                qualified_short.startswith("SKEY_")
                and ("_SKEY_" in mapping_name_upper or mapping_name_upper.startswith("MAP_SKEY"))
            )
        )
        if named_target and qualified_name:
            continue
        if not qualified_name and not comp_name:
            continue
        if comp_type not in _DATASTORE_TYPES:
            continue
        sources.add(qualified_name if qualified_name else comp_name)
    return ", ".join(sorted(sources))


class OdiMappingRankError(ValueError):
    """A lineage row has no integer MAPPING_RANK from the SQL cap."""


def _require_mapping_rank(item: object) -> int:
    if isinstance(item, dict) and "MAPPING_RANK" in item:
        rank = item["MAPPING_RANK"]
        if type(rank) is int:
            return rank
    raise OdiMappingRankError("mapping rank is missing or not an integer")


def _cap_odi_lineage_rows_by_mapping_id(items: list, cap: int) -> tuple[list, bool]:
    """Keep component rows whose SQL MAPPING_RANK is within the cap.

    The lineage CTE assigns that rank. Names and ids are not ordered here.
    A missing or non-integer rank fails the request. Callers group after this.
    """
    ranks = [_require_mapping_rank(item) for item in items]
    kept = [item for item, rank in zip(items, ranks) if rank <= cap]
    return kept, any(rank > cap for rank in ranks)


def _group_odi_mapping_lineage_items(items: list) -> list:
    grouped = {}
    grouped_items = {}
    for item in items:
        name = item.get("MAPPING_NAME")
        if not name:
            continue
        key = str(name).strip().upper()
        if key not in grouped:
            folder_name = item.get("FOLDER_NAME") or item.get("folder_name") or ""
            project_code = item.get("PROJECT_CODE") or item.get("project_code") or ""
            project_name = item.get("PROJECT_NAME") or item.get("project_name") or ""
            grouped[key] = {
                "MAPPING_NAME": str(name),
                "FOLDER_NAME": str(folder_name) if folder_name else "",
                "folder_name": str(folder_name) if folder_name else "",
                "PROJECT_CODE": str(project_code) if project_code else "",
                "project_code": str(project_code) if project_code else "",
                "PROJECT_NAME": str(project_name) if project_name else "",
                "project_name": str(project_name) if project_name else "",
                "project": str(project_code or project_name) if (project_code or project_name) else "",
                "SRC_TABLE": "?",
                "TGT_TABLE": "?",
                "TGT_OWNER": "DWH",
                "LAYER": "",
                "DESC": "",
                "COMP_COUNT": 0,
                "JOIN_COUNT": 0,
            }
            grouped_items[key] = []
        else:
            folder_name = item.get("FOLDER_NAME") or item.get("folder_name")
            project_code = item.get("PROJECT_CODE") or item.get("project_code")
            project_name = item.get("PROJECT_NAME") or item.get("project_name")
            if folder_name and not grouped[key].get("folder_name"):
                grouped[key]["FOLDER_NAME"] = str(folder_name)
                grouped[key]["folder_name"] = str(folder_name)
            if project_code and not grouped[key].get("project_code"):
                grouped[key]["PROJECT_CODE"] = str(project_code)
                grouped[key]["project_code"] = str(project_code)
                grouped[key]["project"] = str(project_code)
            if project_name and not grouped[key].get("project_name"):
                grouped[key]["PROJECT_NAME"] = str(project_name)
                grouped[key]["project_name"] = str(project_name)
                grouped[key]["project"] = grouped[key].get("project") or str(project_name)

        grouped[key]["COMP_COUNT"] += 1
        comp_type = str(item.get("COMP_TYPE") or "").upper()
        if comp_type == "JOIN" or comp_type == "LOOKUP":
            grouped[key]["JOIN_COUNT"] += 1
        expression = item.get("EXPRESSION")
        if expression:
            expression_text = str(expression).strip()
            if any(char in expression_text for char in ["'", "(", " ", "+", "||"]) or "." not in expression_text:
                grouped[key]["HAS_EXP"] = True
        if comp_type == "FILTER":
            grouped[key]["HAS_FILTER"] = True

        mapping_name_display = str(name).upper()
        if "FLAT_FILE" in mapping_name_display and "DWH" in mapping_name_display:
            grouped[key]["DESC"] = "Flat File -> DWH"
        elif "_TO_ODS" in mapping_name_display:
            src_part = mapping_name_display.split("_TO_ODS")[0].replace("MAP_", "").replace("_", " ")
            grouped[key]["DESC"] = f"{src_part.title()} -> ODS"
        elif "_SKEY_" in mapping_name_display or mapping_name_display.startswith("MAP_SKEY"):
            src_part = mapping_name_display.replace("MAP_SKEY_", "").replace("MAP_", "").replace("_", " ")
            grouped[key]["DESC"] = f"{src_part.title()} -> SKEY"
        elif "_TO_EDW" in mapping_name_display:
            grouped[key]["DESC"] = "ODS -> EDW (Core)"
        elif "_TO_DM" in mapping_name_display:
            grouped[key]["DESC"] = "EDW -> DM (Mart)"
        else:
            grouped[key]["DESC"] = mapping_name_display.replace("MAP_", "").replace("_", " ").title()
        grouped_items[key].append(item)

    for key, entry in grouped.items():
        rows = grouped_items[key]
        chosen = _evidence_target(rows)
        if chosen is None:
            entry["TGT_TABLE"] = "?"
            entry["TGT_OWNER"] = "DWH"
            entry["LAYER"] = ""
            entry["SRC_TABLE"] = _base_route_source_table(rows)
        else:
            owner, table = _table_and_owner(chosen)
            entry["TGT_OWNER"] = owner
            entry["TGT_TABLE"] = table
            entry["LAYER"] = _layer_for_target(table, owner)
            entry["SRC_TABLE"] = _formatted_sources(rows, _component_key(chosen))
    return list(grouped.values())


