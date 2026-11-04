from __future__ import annotations

import json
import os
import re

from flask import has_request_context, session

import ai_egress as requests
import config
from ai_egress import local_openai_settings
from app_helpers import _group_odi_mapping_lineage_items
from config import ORACLE_DSN
from db import execute_select
from health_helpers import _chk_claude, _chk_llm, _chk_local_openai
from odi_mcp import odi_get_mapping_lineage


def df_to_jsonable(df):
    rows = []
    for record in df.to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if hasattr(value, "item"):
                value = value.item()
            clean[key] = value
        rows.append(clean)
    return rows

# English forms of a mapping-creation request ("create mapping", "create a mapping",
# "make a mapping", "build a mapping", ...). Turkish forms are matched by token lists.
_CREATE_MAPPING_EN_RE = re.compile(r"\b(?:create|make|build)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?mapping")
# Whole-word "table" (English) / "tablo..." (Turkish); avoids "stable", "suitable".
_TABLE_WORD_RE = re.compile(r"\b(?:tablo\w*|tables?)\b")


def _is_modeled_tables_question(message: str) -> bool:
    msg = (message or "").lower()
    msg_ascii = msg
    for _a, _b in (("Ã„Â±", "i"), ("iÃŒâ€¡", "i"), ("Ã„Å¸", "g"), ("ÃƒÂ¼", "u"),
                   ("Ã…Å¸", "s"), ("ÃƒÂ¶", "o"), ("ÃƒÂ§", "c"),
                   ("Ãƒâ€Ã‚Â±", "i"), ("Ãƒâ€Ã‚Â°", "i"), ("Ãƒâ€Ã…Â¸", "g"),
                   ("ÃƒÆ’Ã‚Â¼", "u"), ("Ãƒâ€¦Ã…Â¸", "s"), ("ÃƒÆ’Ã‚Â¶", "o"), ("ÃƒÆ’Ã‚Â§", "c")):
        msg_ascii = msg_ascii.replace(_a, _b)

    # NEW MODELING REQUEST (create) -> NOT the list route; let it fall through to the LLM/mapping flow.
    # Turkish: "modellemek istiyorum", "X'i modelle", "ods katmanina modelle", "mapping olustur" ...
    # English: "create/make/build (a) mapping" via _CREATE_MAPPING_EN_RE.
    create_intent = (
        "modellemek" in msg_ascii
        or "modelleyel" in msg_ascii
        or "olustur" in msg_ascii
        or "ods katmanina" in msg_ascii
        or "ods a tasi" in msg_ascii
        or "ods'a tasi" in msg_ascii
        or bool(_CREATE_MAPPING_EN_RE.search(msg_ascii))
        or ("mapping" in msg_ascii and any(v in msg_ascii for v in ["olustur", "yap", "kur", "uret", "yeni"]))
        or (
            "modelle" in msg_ascii
            and "modellenmis" not in msg_ascii
            and "modellenen" not in msg_ascii
            and "modelli " not in msg_ascii
        )
    )
    if create_intent:
        return False

    # Intent to LIST/ASK ABOUT tables that are ALREADY modeled
    has_table = any(w in msg_ascii for w in ["tablo", "tablolar", "table", "tables"])
    has_list = any(w in msg_ascii for w in ["modelli", "modellenmis", "modellenen", "modeled", "modelled", "mapping"])
    return has_table and has_list


def _modeled_tables_answer(message: str | None = None):
    def display_db_table(value):
        if not value or value == "?":
            return value or "?"
        model_to_schema = {
            "SRC_HR": "HR",
            "SRC_SH": "SH",
            "TGT_DWH": "DWH",
            "LGCL_ORACLE_DWH_UNIT": "DWH",
        }
        parts = []
        for part in str(value).split(","):
            text = part.strip()
            parts.append(re.sub(
                r"^([A-Z0-9_]+)\.",
                lambda match: f"{model_to_schema.get(match.group(1).upper(), match.group(1))}.",
                text,
                flags=re.IGNORECASE,
            ))
        return ", ".join(parts)

    source_schema = None
    if message:
        msg = _normalize_chat_text(message)
        for schema in (config.SOURCE_ORACLE_SCHEMAS or ["HR", "SH"]):
            clean = str(schema or "").strip().upper()
            if clean and re.search(rf"\b{re.escape(clean.lower())}\b", msg):
                source_schema = clean
                break

    if source_schema:
        import flow_inventory

        inventory_rows = flow_inventory.get_ods_flow_inventory(source_schema=source_schema, target_owner="DWH")
        modeled_count = sum(1 for row in inventory_rows if bool(row.get("ODI_MAPPING_EXISTS")))
        total_count = len(inventory_rows)
        missing_count = total_count - modeled_count
        rows = [
            {
                "SOURCE_TABLE": row.get("SOURCE_TABLE"),
                "EXPECTED_TARGET_TABLE": row.get("EXPECTED_TARGET_TABLE"),
                "ODI_MAPPING_EXISTS": bool(row.get("ODI_MAPPING_EXISTS")),
                "MAPPING_NAME": row.get("MAPPING_NAME") or "",
                "STATUS": row.get("STATUS"),
            }
            for row in inventory_rows
        ]
        return {
            "aciklama": (
                f"{modeled_count} of {total_count} source tables in {source_schema} appear to have "
                f"a DWH ODS flow modeled in ODI. Missing: {missing_count}."
            ),
            "summary": {
                "source_schema": source_schema,
                "source_table_count": total_count,
                "modeled_count": modeled_count,
                "missing_count": missing_count,
            },
            "queries": [{
                "action": "select",
                "label": f"{source_schema} ODI Modeled Table Coverage",
                "sql": "-- Read-only metadata result from source tables, DWH table metadata, and ODI repository mapping references.",
                "params": {},
                "result": {
                    "columns": [
                        "SOURCE_TABLE",
                        "EXPECTED_TARGET_TABLE",
                        "ODI_MAPPING_EXISTS",
                        "MAPPING_NAME",
                        "STATUS",
                    ],
                    "rows": rows,
                },
                "auto_executed": True,
            }],
            "raw": "",
        }

    raw_data = odi_get_mapping_lineage(limit=100)
    items = json.loads(raw_data)
    if not isinstance(items, list):
        items = []
    rows = []
    for m in _group_odi_mapping_lineage_items(items):
        target = "?"
        if m.get("TGT_TABLE") not in (None, "?"):
            target = f"{m.get('TGT_OWNER', 'DWH')}.{m.get('TGT_TABLE')}"
        rows.append({
            "MAPPING_NAME": m.get("MAPPING_NAME"),
            "SOURCE_TABLES": display_db_table(m.get("SRC_TABLE")),
            "TARGET_TABLE": display_db_table(target),
            "LAYER": m.get("LAYER"),
            "DESCRIPTION": m.get("DESC"),
        })
    return {
        "aciklama": "Tables modeled in the current ODI mapping inventory are listed below.",
        "queries": [{
            "action": "select",
            "label": "Modeled Tables",
            "sql": "-- Read-only result from the current ODI mapping inventory.",
            "params": {},
            "result": {
                "columns": ["MAPPING_NAME", "SOURCE_TABLES", "TARGET_TABLE", "LAYER", "DESCRIPTION"],
                "rows": rows,
            },
            "auto_executed": True,
        }],
        "raw": "",
    }


def _normalize_chat_text(value: str) -> str:
    text = (value or "").lower()
    replacements = {
        "\u0131": "i", "\u0130": "i", "\u011f": "g", "\u00fc": "u",
        "\u015f": "s", "\u00f6": "o", "\u00e7": "c",
        "ÃƒÆ’Ã¢â‚¬ÂÃƒâ€šÃ‚Â±": "i", "ÃƒÆ’Ã¢â‚¬ÂÃƒâ€šÃ‚Â°": "i", "Ãƒâ€Ã‚Â±": "i", "Ãƒâ€Ã‚Â°": "i",
        "ÃƒÆ’Ã¢â‚¬ÂÃƒâ€¦Ã‚Â¸": "g", "Ãƒâ€Ã…Â¸": "g",
        "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¼": "u", "ÃƒÆ’Ã‚Â¼": "u",
        "ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€¦Ã‚Â¸": "s", "Ãƒâ€¦Ã…Â¸": "s",
        "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¶": "o", "ÃƒÆ’Ã‚Â¶": "o",
        "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â§": "c", "ÃƒÆ’Ã‚Â§": "c",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _is_truncate_mapping_intent(message: str) -> bool:
    msg = _normalize_chat_text(message)
    return any(phrase in msg for phrase in [
        "truncate ekle",
        "target truncate",
        "truncate olsun",
        "truncate ederek",
        "append yapma",
        "append olmasin",
        "append istemiyorum",
    ])


def _extract_mapping_name_from_text(message: str) -> str | None:
    match = re.search(r"\bMAP_[A-Z0-9_]+\b", (message or "").upper())
    return match.group(0) if match else None


def _truncate_mapping_intent_answer(message: str) -> dict:
    mapping_name = _extract_mapping_name_from_text(message)
    if not mapping_name:
        return {
            "aciklama": (
                "Which mapping should be updated? Provide the mapping name, for example "
                "`MAP_HR_EMPLOYEES_TO_ODS truncate ekle` (Turkish) or `MAP_HR_EMPLOYEES_TO_ODS target truncate` (English). "
                "No ODI metadata update or scenario run was started."
            ),
            "queries": [],
            "requires_mapping": True,
            "requires_confirmation": False,
            "raw": "",
        }

    return {
        "aciklama": (
            f"`{mapping_name}` proposes `TRUNCATE_TARGET_TABLE=true`. "
            "This will update ODI mapping KM metadata only after explicit confirmation. "
            "It will not run the scenario or truncate/load data from this chat response."
        ),
        "queries": [],
        "requires_confirmation": True,
        "action_request": {
            "type": "mapping_km_option_update",
            "mapping_name": mapping_name,
            "proposed_changes": {"truncate_target_table": True},
            "requires_approval": True,
            "will_run_scenario": False,
        },
        "raw": "",
    }


def _extract_source_schema_for_flow_inventory(message: str) -> str:
    msg = _normalize_chat_text(message)
    candidates = config.SOURCE_ORACLE_SCHEMAS or ["HR", "SH"]
    for schema in candidates:
        clean = str(schema or "").strip().upper()
        if clean and re.search(rf"\b{re.escape(clean.lower())}\b", msg):
            return clean
    return "HR"


def _is_unmapped_odi_flow_question(message: str) -> bool:
    msg = _normalize_chat_text(message)
    has_source_schema = any(
        re.search(rf"\b{re.escape(str(schema).lower())}\b", msg)
        for schema in (config.SOURCE_ORACLE_SCHEMAS or ["HR", "SH"])
    )
    has_target = "dwh" in msg or "ods" in msg
    has_table = any(token in msg for token in ["tablo", "table", "tables"])
    has_flow = any(token in msg for token in ["odi", "mapping", "akis", "flow", "aktarim"])
    has_missing = any(token in msg for token in [
        "yapilmamis", "yapilmayan", "olmayan", "eksik", "yok",
        "unmapped", "missing", "without", "not mapped", "not modeled",
    ]) or ("yap" in msg and "mam" in msg)
    has_create_intent = any(token in msg for token in ["olustur", "create mapping", "mapping yap", "kur", "uret"]) or bool(
        _CREATE_MAPPING_EN_RE.search(msg)
    )
    return has_source_schema and has_target and has_table and has_flow and has_missing and not has_create_intent


def _unmapped_odi_flow_answer(message: str):
    import flow_inventory

    source_schema = _extract_source_schema_for_flow_inventory(message)
    rows = flow_inventory.get_ods_flow_inventory(source_schema=source_schema, target_owner="DWH")
    missing_rows = [row for row in rows if not bool(row.get("ODI_MAPPING_EXISTS"))]
    _remember_unmapped_odi_flows(missing_rows)
    columns = [
        "SOURCE_TABLE",
        "EXPECTED_TARGET_TABLE",
        "TARGET_TABLE_EXISTS",
        "SOURCE_DATASTORE_EXISTS",
        "TARGET_DATASTORE_EXISTS",
        "ODI_MAPPING_EXISTS",
        "MAPPING_NAME",
        "STATUS",
    ]
    return {
        "aciklama": (
            f"The tables in the {source_schema} schema were checked using read-only metadata. "
            f"{len(missing_rows)} of {len(rows)} source tables are missing an ODI flow "
            "to the DWH ODS layer (no ODI flow to DWH). The existence of a DWH target table alone does not "
            "mean an ODI mapping exists; ODI repository references were checked as well."
        ),
        "queries": [{
            "action": "select",
            "label": f"{source_schema} Tables Without ODI Flow to DWH",
            "sql": "-- Read-only metadata result from source tables, DWH table metadata, and ODI repository mapping references.",
            "params": {},
            "result": {
                "columns": columns,
                "rows": missing_rows,
            },
            "auto_executed": True,
        }],
        "raw": "",
    }


def _mapping_name_for_ods_flow(src_table_ref: str) -> str:
    owner, table = src_table_ref.upper().split(".", 1)
    return f"MAP_{owner}_{table}_TO_ODS"


def _flow_row_to_mapping_memory(row: dict) -> dict | None:
    src = str(row.get("SOURCE_TABLE") or "").upper()
    target_ref = str(row.get("EXPECTED_TARGET_TABLE") or "").upper()
    if not re.match(r"^[A-Z][A-Z0-9_$]*\.[A-Z][A-Z0-9_$]*$", src):
        return None
    if "." in target_ref:
        tgt_owner, tgt_table = target_ref.split(".", 1)
    else:
        tgt_owner, tgt_table = "DWH", target_ref
    if not re.match(r"^[A-Z][A-Z0-9_$]*$", tgt_owner) or not re.match(r"^[A-Z][A-Z0-9_$]*$", tgt_table):
        return None
    return {
        "src_table": src,
        "src_schema": src.split(".", 1)[0],
        "tgt_owner": tgt_owner,
        "tgt_table": tgt_table,
        "mapping_name": _mapping_name_for_ods_flow(src),
        "status": str(row.get("STATUS") or ""),
    }


def _remember_unmapped_odi_flows(rows: list[dict]) -> None:
    if not has_request_context():
        return
    remembered = []
    for row in rows:
        item = _flow_row_to_mapping_memory(row)
        if item:
            remembered.append(item)
    if remembered:
        session["last_unmapped_odi_flows"] = remembered
    else:
        session.pop("last_unmapped_odi_flows", None)


def _has_recent_bulk_model_intent(message: str, history: list) -> bool:
    texts = [message or ""]
    for item in reversed(history or []):
        if str(item.get("role") or "") in {"user", "assistant"}:
            texts.append(str(item.get("content") or ""))
        if len(texts) >= 4:
            break
    joined = _normalize_chat_text("\n".join(texts))
    has_missing_ref = any(token in joined for token in ["eksik", "missing", "akisi olmayan", "flow olmayan", "yapilmamis", "yap?lmam"])
    has_bulk_ref = any(token in joined for token in ["5 tablo", "bes tablo", "bu 5", "olan 5", "hepsini"])
    has_model_intent = any(token in joined for token in [
        "modelle", "modelleyelim", "ods katmanina", "mapping olustur",
        "mapping yap", "create mapping", "olustur", "aktarim olustur",
    ])
    return has_missing_ref and (has_bulk_ref or bool(_TABLE_WORD_RE.search(joined))) and has_model_intent


def _is_pronoun_bulk_model_intent(message: str) -> bool:
    msg = _normalize_chat_text(message)
    has_pronoun = any(token in msg for token in ["bunlari", "bunlarÃ„Â±", "bunlar", "bu tablolar", "hepsini"])
    has_model_intent = any(token in msg for token in [
        "modelle", "modelleyelim", "ods katmanina", "mapping olustur",
        "mapping yap", "create mapping", "olustur", "aktarim olustur",
    ])
    return has_pronoun and has_model_intent


def _recent_chat_text(message: str, history: list) -> str:
    texts = [message or ""]
    for item in reversed(history or []):
        if str(item.get("role") or "") in {"user", "assistant"}:
            texts.append(str(item.get("content") or ""))
        if len(texts) >= 6:
            break
    return "\n".join(texts)


def _load_unmapped_odi_flow_memory(message: str, history: list) -> list[dict]:
    should_refresh = _has_recent_bulk_model_intent(message, history) or _is_pronoun_bulk_model_intent(message)
    if should_refresh:
        import flow_inventory

        source_schema = _extract_source_schema_for_flow_inventory(_recent_chat_text(message, history))
        rows = flow_inventory.get_ods_flow_inventory(source_schema=source_schema, target_owner="DWH")
        missing_rows = [row for row in rows if not bool(row.get("ODI_MAPPING_EXISTS"))]
        _remember_unmapped_odi_flows(missing_rows)
        return session.get("last_unmapped_odi_flows", []) if has_request_context() else [
            item for row in missing_rows if (item := _flow_row_to_mapping_memory(row))
        ]

    remembered = session.get("last_unmapped_odi_flows") if has_request_context() else []
    if remembered:
        return list(remembered)

    return []


def _is_remembered_unmapped_flow_mapping_request(message: str, history: list) -> bool:
    if not has_request_context():
        return False
    if _is_unmapped_odi_flow_question(message):
        return False
    msg = _normalize_chat_text(message)
    if _has_recent_bulk_model_intent(message, history):
        _load_unmapped_odi_flow_memory(message, history)
        return True
    if _is_pronoun_bulk_model_intent(message):
        _load_unmapped_odi_flow_memory(message, history)
        return True
    if not session.get("last_unmapped_odi_flows"):
        return False
    return (
        any(token in msg for token in ["eksik olan", "missing", "bu 5", "5 tablo"])
        and any(token in msg for token in ["tablo", "table", "flow", "akis", "mapping"])
    )


def _mapping_request_from_memory(item: dict) -> dict:
    src_table = str(item.get("src_table") or "").upper()
    src_schema = str(item.get("src_schema") or src_table.split(".", 1)[0]).upper()
    tgt_table = str(item.get("tgt_table") or "").upper()
    tgt_owner = str(item.get("tgt_owner") or "DWH").upper()
    return {
        "operation": "CREATE",
        "mapping_name": str(item.get("mapping_name") or _mapping_name_for_ods_flow(src_table)).upper(),
        "src_model": "ORACLE_SOURCE",
        "src_schema": src_schema,
        "src_table": src_table,
        "tgt_model": "ORACLE_ODS",
        "tgt_owner": tgt_owner,
        "tgt_table": tgt_table,
        "needs_skey": False,
        "skey_natural_key": None,
        "description": f"Create ODS mapping for {src_table}.",
    }


def _remembered_unmapped_flow_mapping_answer(message: str = "", history: list | None = None):
    remembered = _load_unmapped_odi_flow_memory(message, history or [])
    mapping_requests = [_mapping_request_from_memory(item) for item in remembered or []]
    return {
        "aciklama": (
            f"Mapping requests were prepared for {len(mapping_requests)} tables with a missing ODI flow. "
            "This response is only a preparation step; to start any persistent ODI/DB operation, you must approve each mapping card individually."
        ),
        "queries": [],
        "mapping_requests": mapping_requests,
        "raw": "",
    }


def _is_table_data_request(message: str) -> bool:
    msg = _normalize_chat_text(message)
    return any(token in msg for token in [
        "verisini getir", "veriyi getir", "verilerini getir",
        "verisini goster", "veriyi goster", "verileri goster",
        "data getir", "get data", "show data", "select *",
        "listele", "rows", "satir",
    ])


def _extract_table_ref_from_text(text: str) -> str | None:
    match = re.search(r"\b([A-Za-z][A-Za-z0-9_$]*\.[A-Za-z][A-Za-z0-9_$]*)\b", text or "")
    if not match:
        return None
    table_ref = match.group(1).upper()
    if table_ref.startswith("WH."):
        table_ref = "D" + table_ref
    return table_ref


def _extract_context_target_table(history: list) -> str | None:
    for item in reversed(history or []):
        content = str(item.get("content") or "")
        match = re.search(r"\bTARGET_TABLE\s*[=:]\s*([A-Za-z][A-Za-z0-9_$]*\.[A-Za-z][A-Za-z0-9_$]*)\b", content)
        if match:
            return match.group(1).upper()
        match = re.search(r"\b(DWH\.[A-Za-z][A-Za-z0-9_$]*|ODS\.[A-Za-z][A-Za-z0-9_$]*)\b", content, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    if has_request_context():
        remembered = session.get("last_modeled_target_table")
        if remembered:
            return str(remembered).upper()
    return None


def _remember_modeled_target(answer: dict) -> None:
    if not has_request_context():
        return
    rows = answer.get("queries", [{}])[0].get("result", {}).get("rows", [])
    for row in rows:
        target = str(row.get("TARGET_TABLE") or "").upper()
        if re.match(r"^[A-Z][A-Z0-9_$]*\.[A-Z][A-Z0-9_$]*$", target):
            session["last_modeled_target_table"] = target
            session["last_modeled_source_tables"] = str(row.get("SOURCE_TABLES") or "")
            session["last_modeled_mapping_name"] = str(row.get("MAPPING_NAME") or "")
            return


def _table_data_clarification_answer():
    remembered = session.get("last_modeled_target_table") if has_request_context() else None
    if remembered:
        return _table_data_answer(f"{remembered} verisini getir", [])
    return {
        "aciklama": "Which table's data should I fetch? Example: `DWH.ODS_HR_EMPLOYEES verisini getir` (Turkish) or `get data DWH.ODS_HR_EMPLOYEES` (English).",
        "queries": [],
        "raw": "",
    }


def _build_table_preview_sql(table_ref: str, limit: int = 3) -> str:
    owner, table = table_ref.upper().split(".", 1)
    if not re.match(r"^[A-Z][A-Z0-9_$]*$", owner) or not re.match(r"^[A-Z][A-Z0-9_$]*$", table):
        raise ValueError("Invalid table name")
    cols_df = execute_select(f"""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER = '{owner}' AND TABLE_NAME = '{table}'
        ORDER BY COLUMN_ID
    """)
    select_parts = []
    for _, row in cols_df.iterrows():
        col = str(row["COLUMN_NAME"]).upper()
        data_type = str(row["DATA_TYPE"]).upper()
        if not re.match(r"^[A-Z][A-Z0-9_$]*$", col):
            continue
        quoted = f'"{col}"'
        if "WITH TIME ZONE" in data_type:
            select_parts.append(f'CAST({quoted} AS TIMESTAMP) AS {quoted}')
        else:
            select_parts.append(quoted)
    if not select_parts:
        return f"SELECT * FROM {owner}.{table} WHERE ROWNUM <= {int(limit)}"
    return f"SELECT {', '.join(select_parts)} FROM {owner}.{table} WHERE ROWNUM <= {int(limit)}"


def _modeled_target_lookup() -> dict:
    answer = _modeled_tables_answer()
    rows = answer.get("queries", [{}])[0].get("result", {}).get("rows", [])
    lookup = {}
    for row in rows:
        target = str(row.get("TARGET_TABLE") or "").upper()
        if target and target != "?":
            lookup[target] = row
    return lookup


def _table_availability_answer(table_ref: str, modeled_row: dict | None, error_msg: str):
    source_tables = modeled_row.get("SOURCE_TABLES", "?") if modeled_row else "?"
    mapping_name = modeled_row.get("MAPPING_NAME", "?") if modeled_row else "?"
    rows = [{
        "REQUESTED_TABLE": table_ref,
        "STATUS": "NOT_VISIBLE_OR_NOT_CREATED",
        "MAPPING_NAME": mapping_name,
        "SOURCE_TABLES": source_tables,
        "NEXT_STEP": "Create/grant/load the target table before querying target rows.",
    }]
    explanation = (
        f"ODI model/mapping information exists for {table_ref}, but the physical target table is not "
        "visible to the current connection user or has not been created and loaded yet. "
        f"Source side: {source_tables}. No DDL/grant/load operation was run automatically."
    )
    return {
        "aciklama": explanation,
        "queries": [{
            "action": "select",
            "label": "Modeled Target Availability",
            "sql": "-- Read-only availability check; no DDL or data load was executed.",
            "params": {},
            "result": {
                "columns": ["REQUESTED_TABLE", "STATUS", "MAPPING_NAME", "SOURCE_TABLES", "NEXT_STEP"],
                "rows": rows,
            },
            "auto_executed": True,
            "error": error_msg,
        }],
        "raw": "",
    }


def _table_data_answer(message: str, history: list):
    table_ref = _extract_table_ref_from_text(message) or _extract_context_target_table(history)
    if not table_ref:
        return None
    if not re.match(r"^[A-Z][A-Z0-9_$]*\.[A-Z][A-Z0-9_$]*$", table_ref):
        return None

    modeled_lookup = _modeled_target_lookup()
    modeled_row = modeled_lookup.get(table_ref)
    sql = _build_table_preview_sql(table_ref, 3)
    try:
        df = execute_select(sql)
        return {
            "aciklama": f"Fetched the first 3 rows for {table_ref}.",
            "queries": [{
                "action": "select",
                "label": f"{table_ref} Preview",
                "sql": sql,
                "params": {},
                "result": {
                    "columns": list(df.columns),
                    "rows": df_to_jsonable(df),
                },
                "auto_executed": True,
                "preview_limited": True,
                "preview_rows": 3,
            }],
            "raw": "",
        }
    except Exception as e:
        error_msg = str(e)
        if "ORA-00942" in error_msg and modeled_row:
            return _table_availability_answer(table_ref, modeled_row, error_msg)
        return None


def _is_llm_status_question(message: str) -> bool:
    msg = _normalize_chat_text(message)
    return (
        any(token in msg for token in ["hangi model", "hangi llm", "aktif model", "aktif llm", "llm aktif", "model aktif"]) or
        ("provider" in msg and any(token in msg for token in ["aktif", "active", "current"]))
    )


def _local_openai_model_ids() -> list[str]:
    try:
        base_url, headers = local_openai_settings(config.LOCAL_OPENAI_BASE_URL)
        res = requests.get(f"{base_url}/models", headers=headers, timeout=2)
        res.raise_for_status()
        data = res.json()
        return [str(item.get("id")) for item in data.get("data", []) if item.get("id")]
    except Exception:
        return []


def _ollama_model_names() -> list[str]:
    try:
        res = requests.get(config.OLLAMA_TAGS_URL, timeout=2)
        res.raise_for_status()
        data = res.json()
        return [str(item.get("name")) for item in data.get("models", []) if item.get("name")]
    except Exception:
        return []


def _llm_status_answer():
    from ai_agent import normalize_provider
    provider = normalize_provider(os.getenv("LLM_PROVIDER", "hybrid"))
    checks = {
        "LOCAL_OPENAI": _chk_local_openai(),
        "OLLAMA": _chk_llm(),
        "CLAUDE": _chk_claude(),
    }
    local_model = os.getenv("LOCAL_OPENAI_MODEL") or getattr(config, "LOCAL_OPENAI_MODEL", "local-model")
    ollama_model = os.getenv("OLLAMA_MODEL") or getattr(config, "OLLAMA_MODEL", "qwen2.5-coder:14b")
    local_models = _local_openai_model_ids()
    ollama_models = _ollama_model_names()

    rows = [
        {
            "PROVIDER": "local_openai",
            "SELECTED": "YES" if provider == "local_openai" else ("FIRST" if provider == "hybrid" else "NO"),
            "STATUS": "UP" if checks["LOCAL_OPENAI"] else "DOWN",
            "CONFIGURED_MODEL": local_model,
            "AVAILABLE_MODELS": ", ".join(local_models[:5]) if local_models else "-",
        },
        {
            "PROVIDER": "ollama",
            "SELECTED": "YES" if provider == "ollama" else ("FALLBACK" if provider == "hybrid" else "NO"),
            "STATUS": "UP" if checks["OLLAMA"] else "DOWN",
            "CONFIGURED_MODEL": ollama_model,
            "AVAILABLE_MODELS": ", ".join(ollama_models[:5]) if ollama_models else "-",
        },
        {
            "PROVIDER": "claude",
            "SELECTED": "YES" if provider == "claude" else ("FALLBACK" if provider == "hybrid" else "NO"),
            "STATUS": "CONFIGURED" if checks["CLAUDE"] else "NOT_CONFIGURED",
            "CONFIGURED_MODEL": "claude-sonnet-4-6",
            "AVAILABLE_MODELS": "-",
        },
    ]
    active_row = next((row for row in rows if row["PROVIDER"] == provider), rows[0])
    note = ""
    if provider in ("local_openai", "hybrid") and checks["LOCAL_OPENAI"] and local_models and local_model not in local_models:
        note = f" The selected Local OpenAI model id `{local_model}` is not in the endpoint model list; this may cause a 400 error in this chat request."
    explanation = (
        f"The currently selected LLM provider is `{provider}`. "
        f"Active row status: {active_row['STATUS']}, model setting: `{active_row['CONFIGURED_MODEL']}`."
        f"{note}"
    )
    return {
        "aciklama": explanation,
        "queries": [{
            "action": "select",
            "label": "LLM Runtime Status",
            "sql": "-- Runtime status assembled from local health checks; no secrets are returned.",
            "params": {},
            "result": {
                "columns": ["PROVIDER", "SELECTED", "STATUS", "CONFIGURED_MODEL", "AVAILABLE_MODELS"],
                "rows": rows,
            },
            "auto_executed": True,
        }],
        "raw": "",
    }

