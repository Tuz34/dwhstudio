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
_TARGET_FLAG_VALUES = frozenset({"Y", "YES", "TRUE", "1"})
# Highest name rank wins. A structural target adds 1000 and beats every name.
_NAME_RANKS = (
    ("DM_", 70),
    ("DIM_", 60),
    ("EDW_", 50),
    ("ODS_", 40),
    ("STG_", 30),
    ("DWH_", 10),
)
_LAYER_BY_PREFIX = (
    ("DM_", "DM"),
    ("DIM_", "EDW"),
    ("EDW_", "EDW"),
    ("ODS_", "ODS"),
    ("STG_", "STG"),
    ("SKEY_", "SKEY"),
)
_SCHEMA_LAYERS = {"ODS": "ODS", "STG": "STG", "EDW": "EDW", "DM": "DM"}


def _text(value: object) -> str:
    return str(value).strip() if value else ""


def _remember_project_fields(entry: dict, item: dict) -> None:
    folder_name = _text(item.get("FOLDER_NAME") or item.get("folder_name"))
    project_code = _text(item.get("PROJECT_CODE") or item.get("project_code"))
    project_name = _text(item.get("PROJECT_NAME") or item.get("project_name"))
    if folder_name and not entry.get("folder_name"):
        entry["FOLDER_NAME"] = folder_name
        entry["folder_name"] = folder_name
    if project_code and not entry.get("project_code"):
        entry["PROJECT_CODE"] = project_code
        entry["project_code"] = project_code
        entry["project"] = project_code
    if project_name and not entry.get("project_name"):
        entry["PROJECT_NAME"] = project_name
        entry["project_name"] = project_name
        entry["project"] = entry.get("project") or project_name


def _flag_is_target(value: object) -> bool:
    if value is True or value == 1:
        return True
    return str(value or "").strip().upper() in _TARGET_FLAG_VALUES


def _row_is_structural_target(item: dict) -> bool:
    component_type = str(item.get("COMP_TYPE") or "").upper()
    if component_type not in _DATASTORE_TYPES:
        return False
    if _flag_is_target(item.get("IS_TARGET")) or _flag_is_target(item.get("is_target")):
        return True
    expression = item.get("EXPRESSION")
    return isinstance(expression, str) and bool(expression.strip())


def _target_name_rank(short_name: str, comp_name: str, qualified: str, mapping_name: str) -> int:
    ranks = [
        rank
        for prefix, rank in _NAME_RANKS
        if short_name.startswith(prefix) or comp_name.startswith(prefix)
    ]
    if qualified.upper().startswith("ODS."):
        ranks.append(40)
    mapping = mapping_name.upper()
    skey_requested = "_SKEY_" in mapping or mapping.startswith("MAP_SKEY")
    if skey_requested and (short_name.startswith("SKEY_") or comp_name.startswith("SKEY_")):
        ranks.append(20)
    if comp_name.startswith("TGT_"):
        ranks.append(5)
    return max(ranks) if ranks else 0


def _choose_mapping_target(rows: list[dict], mapping_name: str) -> dict | None:
    structural = [row for row in rows if row["structural"]]
    pool = structural or rows
    best: dict | None = None
    best_key = (-1, -1)
    for row in pool:
        qualified = row["qualified"]
        if not qualified:
            continue
        name_rank = _target_name_rank(row["short"], row["comp"], qualified, mapping_name)
        if row["structural"]:
            rank = 1000 + name_rank
        elif name_rank <= 0:
            continue
        else:
            rank = name_rank
        key = (rank, row["index"])
        if key > best_key:
            best = row
            best_key = key
    return best


def _layer_for_target(qualified: str) -> str:
    parts = qualified.split(".")
    short = (parts[1] if len(parts) > 1 else qualified).upper()
    owner = parts[0].upper() if len(parts) > 1 else ""
    for prefix, layer in _LAYER_BY_PREFIX:
        if short.startswith(prefix):
            return layer
    return _SCHEMA_LAYERS.get(owner, "")


def _source_names(rows: list[dict], chosen: dict | None) -> str:
    names = []
    for row in rows:
        if chosen is not None and row["index"] == chosen["index"]:
            continue
        if row["component_type"] not in _DATASTORE_TYPES:
            continue
        source_name = row["qualified"] or row["comp_display"]
        if source_name:
            names.append(source_name)
    return ", ".join(sorted(set(names)))


def _group_odi_mapping_lineage_items(items: list) -> list:
    grouped = {}
    for it in items:
        name = it.get("MAPPING_NAME")
        if not name:
            continue
        k = str(name).strip().upper()
        if k not in grouped:
            grouped[k] = {
                "MAPPING_NAME": str(name),
                "FOLDER_NAME": "",
                "folder_name": "",
                "PROJECT_CODE": "",
                "project_code": "",
                "PROJECT_NAME": "",
                "project_name": "",
                "project": "",
                "SRC_TABLE": "",
                "TGT_TABLE": "?",
                "TGT_OWNER": "DWH",
                "LAYER": "",
                "DESC": "",
                "COMP_COUNT": 0,
                "JOIN_COUNT": 0,
                "_ROWS": [],
            }
        _remember_project_fields(grouped[k], it)

        grouped[k]["COMP_COUNT"] += 1
        qname = str(it.get("TABLE_QUALIFIED_NAME") or "").strip()
        ctype = str(it.get("COMP_TYPE") or "").upper()
        cname = str(it.get("COMP_NAME") or "").upper()

        if ctype in ("JOIN", "LOOKUP"):
            grouped[k]["JOIN_COUNT"] += 1
        expr = it.get("EXPRESSION")
        if expr:
            expr_s = str(expr).strip()
            if any(c in expr_s for c in ["'", "(", " ", "+", "||"]) or "." not in expr_s:
                grouped[k]["HAS_EXP"] = True
        if ctype == "FILTER":
            grouped[k]["HAS_FILTER"] = True

        grouped[k]["_ROWS"].append({
            "index": len(grouped[k]["_ROWS"]),
            "qualified": qname,
            "short": qname.split(".")[-1].upper() if qname else "",
            "comp": cname,
            "comp_display": str(it.get("COMP_NAME") or ""),
            "component_type": ctype,
            "structural": _row_is_structural_target(it),
        })

        mn = str(name).upper()
        if "FLAT_FILE" in mn and "DWH" in mn:
            grouped[k]["DESC"] = "Flat File -> DWH"
        elif "_TO_ODS" in mn:
            src_part = mn.split("_TO_ODS")[0].replace("MAP_", "").replace("_", " ")
            grouped[k]["DESC"] = f"{src_part.title()} -> ODS"
        elif "_SKEY_" in mn or mn.startswith("MAP_SKEY"):
            src_part = mn.replace("MAP_SKEY_", "").replace("MAP_", "").replace("_", " ")
            grouped[k]["DESC"] = f"{src_part.title()} -> SKEY"
        elif "_TO_EDW" in mn:
            grouped[k]["DESC"] = "ODS -> EDW (Core)"
        elif "_TO_DM" in mn:
            grouped[k]["DESC"] = "EDW -> DM (Mart)"
        else:
            grouped[k]["DESC"] = mn.replace("MAP_", "").replace("_", " ").title()

    for item in grouped.values():
        rows = item.pop("_ROWS")
        chosen = _choose_mapping_target(rows, item["MAPPING_NAME"])
        if chosen is not None:
            qualified = chosen["qualified"]
            parts = qualified.split(".")
            item["TGT_OWNER"] = parts[0] if len(parts) > 1 else "DWH"
            item["TGT_TABLE"] = parts[1] if len(parts) > 1 else qualified
            item["LAYER"] = _layer_for_target(qualified)
        item["SRC_TABLE"] = _source_names(rows, chosen)
    return list(grouped.values())


