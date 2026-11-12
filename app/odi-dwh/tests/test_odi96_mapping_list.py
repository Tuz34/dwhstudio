"""ODI-96: mapping-level lineage limit, complete source/target rows, truncation flag."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

MAPPINGS_LIST_CAP = 500
_FETCH = "FETCH FIRST :lmt ROWS ONLY"
_OUTER_SELECT = "m.NAME as MAPPING_NAME"
_ROW_LIMIT = re.compile(
    r"FETCH\s+FIRST\s+(?P<count>\d+|:(?P<bind>[A-Za-z_]\w*))\s+ROWS\s+ONLY"
    r"|ROWNUM\s*<=\s*(?P<rcount>\d+|:(?P<rbind>[A-Za-z_]\w*))",
    re.IGNORECASE,
)


def _pair_rows(prefix: str, count: int, extra_join: bool = False) -> list[dict]:
    rows = []
    for index in range(count):
        name = f"{prefix}_{index:03d}"
        mapping_id = index + 1
        rows.append({
            "MAPPING_ID": mapping_id,
            "MAPPING_NAME": name,
            "PROJECT_CODE": "ORA_DWH",
            "PROJECT_NAME": "Oracle DWH",
            "FOLDER_NAME": "FLD_CORE_MAPPINGS",
            "COMP_NAME": f"SRC_{index:03d}",
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": f"HR.SRC_{index:03d}",
            "IS_TARGET": "N",
        })
        if extra_join:
            rows.append({
                "MAPPING_ID": mapping_id,
                "MAPPING_NAME": name,
                "PROJECT_CODE": "ORA_DWH",
                "PROJECT_NAME": "Oracle DWH",
                "FOLDER_NAME": "FLD_CORE_MAPPINGS",
                "COMP_NAME": f"J_{index:03d}",
                "COMP_TYPE": "JOIN",
                "TABLE_QUALIFIED_NAME": None,
                "IS_TARGET": "N",
            })
        rows.append({
            "MAPPING_ID": mapping_id,
            "MAPPING_NAME": name,
            "PROJECT_CODE": "ORA_DWH",
            "PROJECT_NAME": "Oracle DWH",
            "FOLDER_NAME": "FLD_CORE_MAPPINGS",
            "COMP_NAME": f"ODS_TGT_{index:03d}",
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": f"DWH.ODS_TGT_{index:03d}",
            "IS_TARGET": "Y",
        })
    return rows


def _component_rows_for_mapping_limit(rows: list[dict], limit: int) -> list[dict]:
    seen: set[str] = set()
    keep: list[str] = []
    for row in rows:
        name = row["MAPPING_NAME"]
        if name in seen:
            continue
        if len(seen) >= limit:
            break
        seen.add(name)
        keep.append(name)
    chosen = set(keep)
    return [row for row in rows if row["MAPPING_NAME"] in chosen]


def _lineage_function_body() -> str:
    source = Path(__file__).resolve().parents[1].joinpath("odi_mcp.py").read_text(encoding="utf-8")
    start = source.index("def odi_get_mapping_lineage")
    return source[start:source.index("\ndef odi_get_mapping_details")]


def _cte_and_outer(sql: str) -> tuple[str, str]:
    """Split at the outer SELECT. The mapping cap stays in the CTE before it."""
    select_at = sql.rfind("SELECT", 0, sql.index(_OUTER_SELECT))
    return sql[:select_at], sql[select_at:]


def _row_limit(sql: str, params: dict) -> int | None:
    match = _ROW_LIMIT.search(sql)
    if match is not None:
        token = match.group("count") or match.group("rcount")
        if token.startswith(":"):
            bind = match.group("bind") or match.group("rbind")
            return int(params[bind])
        return int(token)
    if re.search(r"FETCH\s+FIRST|ROWNUM", sql, re.IGNORECASE):
        return int(params["lmt"])
    return None


def _apply_lineage_row_limits(sql: str, params: dict, catalog: list[list[dict]]) -> list[dict]:
    """Apply the statement's own FETCH FIRST / ROWNUM caps.

    The CTE cap keeps whole mappings. An outer cap keeps component rows, which
    drops a mapping's remaining components once the row budget is spent.
    """
    cte, outer = _cte_and_outer(sql)
    mapping_limit = _row_limit(cte, params)
    ordered = sorted(catalog, key=lambda comps: (comps[0]["_name"], comps[0]["_mapping_id"]))
    if mapping_limit is not None:
        ordered = ordered[:mapping_limit]
    rows: list[dict] = []
    for comps in ordered:
        rows.extend(sorted(comps, key=lambda row: (row["COMP_TYPE"], row["_comp_id"])))
    outer_limit = _row_limit(outer, params)
    if outer_limit is not None:
        rows = rows[:outer_limit]
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def _component_catalog(mapping_count: int, components_per_mapping: int = 3) -> list[list[dict]]:
    catalog = []
    for index in range(mapping_count):
        name = f"MAP_{index:03d}"
        mapping_id = 1000 + index
        comps = []
        for comp_index in range(components_per_mapping):
            is_target = comp_index == components_per_mapping - 1
            comps.append({
                "MAPPING_NAME": name,
                "PROJECT_CODE": "ORA_DWH",
                "PROJECT_NAME": "Oracle DWH",
                "FOLDER_NAME": "FLD",
                "COMP_NAME": f"C_{index:03d}_{comp_index}",
                "COMP_TYPE": "DATASTORE",
                "TABLE_QUALIFIED_NAME": f"HR.T_{index:03d}_{comp_index}",
                "IS_TARGET": "Y" if is_target else "N",
                "_name": name,
                "_mapping_id": mapping_id,
                "_comp_id": comp_index,
            })
        catalog.append(comps)
    return catalog


def test_mapping_lineage_sql_is_mapping_capped_with_binds_and_stable_is_target():
    body = _lineage_function_body()
    case_start = body.index("CASE\n")
    case_end = body.index("END AS IS_TARGET") + len("END AS IS_TARGET")
    expected_case = (
        "CASE\n"
        "                WHEN UPPER(c.TYPE_NAME) IN ('DATASTORE', 'FILE')\n"
        "                 AND EXISTS (\n"
        "                    SELECT 1\n"
        "                    FROM {ODI_REPO_SCHEMA}.SNP_MAP_CP cp\n"
        "                    JOIN {ODI_REPO_SCHEMA}.SNP_MAP_ATTR a\n"
        "                      ON a.I_OWNER_MAP_CP = cp.I_MAP_CP\n"
        "                    LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e\n"
        "                      ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR\n"
        "                    WHERE cp.I_OWNER_MAP_COMP = c.I_MAP_COMP\n"
        "                      AND cp.DIRECTION = 'O'\n"
        "                      AND TRIM(e.TXT) IS NOT NULL\n"
        "                 )\n"
        "                THEN 'Y'\n"
        "                ELSE 'N'\n"
        "            END AS IS_TARGET"
    )
    assert body[case_start:case_end] == expected_case
    cte, outer = _cte_and_outer(body)
    assert "ORDER BY m2.NAME, m2.I_MAPPING" in cte
    assert _FETCH in cte
    assert body.index(_FETCH) < body.index(_OUTER_SELECT)
    assert body.count("FETCH FIRST") == 1
    assert "FETCH FIRST" not in outer
    assert "ROWNUM" not in outer.upper()
    assert "ORDER BY m.NAME, c.TYPE_NAME, c.I_MAP_COMP" in outer
    assert "FETCH FIRST {limit}" not in body
    assert ":lmt" in body
    sql_assign = body[body.index("sql ="):body.index("try:")]
    assert "INSERT" not in sql_assign
    assert "UPDATE" not in sql_assign
    assert "DELETE" not in sql_assign
    assert "MERGE" not in sql_assign
    assert "DROP" not in sql_assign


def test_mapping_lineage_binds_n_and_does_not_interpolate_the_limit(monkeypatch):
    import odi_mcp

    captured = {}

    def fake_select(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame([{
            "MAPPING_NAME": "MAP_A",
            "PROJECT_CODE": "ORA_DWH",
            "PROJECT_NAME": "Oracle DWH",
            "FOLDER_NAME": "FLD",
            "COMP_NAME": "SRC",
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": "HR.SRC",
            "IS_TARGET": "N",
        }])

    monkeypatch.setattr(odi_mcp, "execute_odi_select", fake_select)
    payload = json.loads(odi_mcp.odi_get_mapping_lineage(limit=7))
    assert captured["params"] == {"lmt": 7}
    assert "FETCH FIRST :lmt ROWS ONLY" in captured["sql"]
    assert "FETCH FIRST 7" not in captured["sql"]
    assert payload[0]["MAPPING_NAME"] == "MAP_A"


def test_fake_cursor_applies_outer_row_limit_and_keeps_every_component(monkeypatch):
    """Component rows outnumber the mappings. An outer FETCH FIRST / ROWNUM cuts them."""
    import odi_mcp

    limit = 5
    components_per_mapping = 3
    catalog = _component_catalog(8, components_per_mapping)
    captured = {}

    def fake_select(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame(_apply_lineage_row_limits(sql, params, catalog))

    monkeypatch.setattr(odi_mcp, "execute_odi_select", fake_select)
    payload = json.loads(odi_mcp.odi_get_mapping_lineage(limit=limit))
    assert captured["params"] == {"lmt": limit}
    assert len(payload) == limit * components_per_mapping
    assert len(payload) > limit
    counts: dict[str, int] = {}
    flags: dict[str, set[str]] = {}
    for row in payload:
        name = row["MAPPING_NAME"]
        counts[name] = counts.get(name, 0) + 1
        flags.setdefault(name, set()).add(row["IS_TARGET"])
    assert counts == {f"MAP_{index:03d}": components_per_mapping for index in range(limit)}
    assert all(flag == {"N", "Y"} for flag in flags.values())


def test_mappings_route_keeps_source_and_target_past_100_component_rows(app_client, monkeypatch):
    import app as app_module

    rows = _pair_rows("MAP_ODI96", 60, extra_join=True)
    assert len(rows) > 100
    assert len({row["MAPPING_NAME"] for row in rows}) > 50
    calls = []

    def fake_lineage(limit=50):
        calls.append(limit)
        return json.dumps(_component_rows_for_mapping_limit(rows, limit))

    monkeypatch.setattr(app_module, "odi_get_mapping_lineage", fake_lineage)
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    assert calls == [MAPPINGS_LIST_CAP + 1]
    found = response.get_json()
    assert isinstance(found, list)
    assert len(found) == 60
    for item in found:
        assert item["COMP_COUNT"] == 3
        assert item["JOIN_COUNT"] == 1
        assert item["SRC_TABLE"] != "?"
        assert item["TGT_TABLE"] != "?"
        assert item["TGT_TABLE"].startswith("ODS_TGT_")
        assert "HR.SRC_" in item["SRC_TABLE"]


@pytest.mark.parametrize("mapping_count,truncated", [(499, False), (500, False), (501, True)])
def test_mappings_route_truncates_only_past_the_cap(app_client, monkeypatch, mapping_count, truncated):
    import app as app_module

    rows = _pair_rows("MAP_CAP", mapping_count, extra_join=True)
    calls = []

    def fake_lineage(limit=50):
        calls.append(limit)
        return json.dumps(_component_rows_for_mapping_limit(rows, limit))

    monkeypatch.setattr(app_module, "odi_get_mapping_lineage", fake_lineage)
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    assert calls == [MAPPINGS_LIST_CAP + 1]
    found = response.get_json()
    expected = min(mapping_count, MAPPINGS_LIST_CAP)
    assert [item["MAPPING_NAME"] for item in found] == [
        f"MAP_CAP_{index:03d}" for index in range(expected)
    ]
    for item in found:
        assert item["COMP_COUNT"] == 3
        assert item["JOIN_COUNT"] == 1
        assert item["SRC_TABLE"] != "?"
        assert item["TGT_TABLE"].startswith("ODS_TGT_")
        assert "HR.SRC_" in item["SRC_TABLE"]
    if truncated:
        assert response.headers.get("X-ODI-Mappings-Truncated") == "1"
        assert response.headers.get("X-ODI-Mappings-Limit") == str(MAPPINGS_LIST_CAP)
        assert f"MAP_CAP_{MAPPINGS_LIST_CAP:03d}" not in {item["MAPPING_NAME"] for item in found}
    else:
        assert response.headers.get("X-ODI-Mappings-Truncated") in (None, "")
        assert response.headers.get("X-ODI-Mappings-Limit") in (None, "")


def test_mappings_route_omits_truncation_headers_below_the_cap(app_client, monkeypatch):
    import app as app_module

    rows = _pair_rows("MAP_UNDER", 3)
    calls = []

    def fake_lineage(limit=50):
        calls.append(limit)
        return json.dumps(_component_rows_for_mapping_limit(rows, limit))

    monkeypatch.setattr(app_module, "odi_get_mapping_lineage", fake_lineage)
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    assert calls == [MAPPINGS_LIST_CAP + 1]
    assert len(response.get_json()) == 3
    assert response.headers.get("X-ODI-Mappings-Truncated") in (None, "")
    assert response.headers.get("X-ODI-Mappings-Limit") in (None, "")


@pytest.mark.parametrize("mapping_count,truncated", [(99, False), (100, False), (101, True)])
def test_modeled_tables_answer_mentions_the_cap_only_past_it(monkeypatch, mapping_count, truncated):
    import ai_helpers

    rows = _pair_rows("MAP_AI", mapping_count, extra_join=True)
    calls = []

    def fake_lineage(limit=100):
        calls.append(limit)
        return json.dumps(_component_rows_for_mapping_limit(rows, limit))

    monkeypatch.setattr(ai_helpers, "odi_get_mapping_lineage", fake_lineage)
    body = ai_helpers._modeled_tables_answer()
    assert calls == [101]
    listed = body["queries"][0]["result"]["rows"]
    expected = min(mapping_count, 100)
    assert [row["MAPPING_NAME"] for row in listed] == [f"MAP_AI_{index:03d}" for index in range(expected)]
    for row in listed:
        assert "HR.SRC_" in row["SOURCE_TABLES"]
        assert "ODS_TGT_" in row["TARGET_TABLE"]
        assert "?" not in row["SOURCE_TABLES"]
        assert "?" not in row["TARGET_TABLE"]
    base = "Tables modeled in the current ODI mapping inventory are listed below."
    if truncated:
        assert body["aciklama"] == f"{base} This listing summarises the first 100 mappings."
        assert f"MAP_AI_{100:03d}" not in {row["MAPPING_NAME"] for row in listed}
    else:
        assert body["aciklama"] == base


def test_odi_lineage_route_passes_its_mapping_limit_explicitly():
    source = Path(__file__).resolve().parents[1].joinpath("routes", "ops_read.py").read_text(encoding="utf-8")
    start = source.index("def api_odi_lineage")
    body = source[start:source.index("@bp.route", start)]
    assert "odi_get_mapping_lineage(limit=50)" in body
    assert "odi_get_mapping_lineage()" not in body


def _folder_pair(
    name: str,
    mapping_id: int,
    folder: str,
    source: str,
    target: str,
    comp_base: int,
) -> list[dict]:
    shared = {
        "MAPPING_ID": mapping_id,
        "MAPPING_NAME": name,
        "PROJECT_CODE": "ORA_DWH",
        "PROJECT_NAME": "Oracle DWH",
        "FOLDER_NAME": folder,
    }
    return [
        {
            **shared,
            "COMP_NAME": source.split(".")[-1],
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": source,
            "IS_TARGET": "N",
            "_comp_id": comp_base,
        },
        {
            **shared,
            "COMP_NAME": target.split(".")[-1],
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": target,
            "IS_TARGET": "Y",
            "_comp_id": comp_base + 2,
        },
    ]


def _rows_past_cap_with_shared_name(cap: int) -> list[dict]:
    """`cap` mappings plus one later id that reuses the last name in folder F2."""
    rows: list[dict] = []
    for index in range(cap):
        rows.extend(_folder_pair(
            f"MAP_CAP_{index:03d}",
            index + 1,
            "F1",
            f"HR.SRC_{index:03d}",
            f"DWH.ODS_TGT_{index:03d}",
            index * 10,
        ))
    last = cap - 1
    # Source comp id sits between the kept mapping's source and target.
    rows.extend(_folder_pair(
        f"MAP_CAP_{last:03d}",
        cap + 1,
        "F2",
        "HR.EXTRA_SOURCE",
        "DWH.ODS_EXTRA",
        last * 10 + 1,
    ))
    return rows


def _rows_inside_cap_with_shared_name() -> list[dict]:
    """Two ids share a name. Both are inside either cap. A third name does not."""
    return [
        *_folder_pair("MAP_SAME", 1, "F1", "HR.SRC_INSIDE", "DWH.ODS_INSIDE", 10),
        *_folder_pair("MAP_SAME", 2, "F2", "HR.EXTRA_SOURCE", "DWH.ODS_EXTRA", 11),
        *_folder_pair("MAP_OTHER", 3, "F1", "HR.SRC_OTHER", "DWH.ODS_OTHER", 30),
    ]


def _sql_mapping_page(rows: list[dict], limit: int) -> list[dict]:
    """CTE keeps the first `limit` ids by name then id; outer orders component rows."""
    seen: set[int] = set()
    order: list[int] = []
    for row in sorted(rows, key=lambda item: (item["MAPPING_NAME"], item["MAPPING_ID"], item["_comp_id"])):
        mapping_id = row["MAPPING_ID"]
        if mapping_id in seen:
            continue
        seen.add(mapping_id)
        order.append(mapping_id)
    keep = set(order[:limit])
    kept = [row for row in rows if row["MAPPING_ID"] in keep]
    kept.sort(key=lambda item: (item["MAPPING_NAME"], item["COMP_TYPE"], item["_comp_id"]))
    return [{key: value for key, value in row.items() if not str(key).startswith("_")} for row in kept]


def test_lineage_select_adds_mapping_id_and_leaves_the_rest_unchanged():
    body = _lineage_function_body()
    cte, outer = _cte_and_outer(body)
    assert "m2.I_MAPPING" in cte
    assert "m.I_MAPPING AS MAPPING_ID" in outer
    assert outer.count("m.I_MAPPING AS MAPPING_ID") == 1
    for column in (
        "m.NAME as MAPPING_NAME",
        "p.PROJECT_CODE as PROJECT_CODE",
        "p.PROJECT_NAME as PROJECT_NAME",
        "f.FOLDER_NAME as FOLDER_NAME",
        "c.NAME as COMP_NAME",
        "c.TYPE_NAME as COMP_TYPE",
        "r.QUALIFIED_NAME as TABLE_QUALIFIED_NAME",
    ):
        assert column in outer


def _install_lineage(monkeypatch, module, rows: list[dict]):
    calls = []

    def fake_lineage(limit=50):
        calls.append(limit)
        return json.dumps(_sql_mapping_page(rows, limit))

    monkeypatch.setattr(module, "odi_get_mapping_lineage", fake_lineage)
    return calls


@pytest.mark.parametrize("cap", [MAPPINGS_LIST_CAP, 100])
def test_same_named_mapping_past_the_cap_stays_out(cap, app_client, monkeypatch):
    """Folder F2 reuses the last kept name. Its components must not enter the page."""
    rows = _rows_past_cap_with_shared_name(cap)
    last = cap - 1
    kept_name = f"MAP_CAP_{last:03d}"
    if cap == MAPPINGS_LIST_CAP:
        import app as app_module

        calls = _install_lineage(monkeypatch, app_module, rows)
        response = app_client.get("/api/odi/mappings")
        assert response.status_code == 200
        assert calls == [cap + 1]
        found = response.get_json()
        assert [item["MAPPING_NAME"] for item in found] == [f"MAP_CAP_{index:03d}" for index in range(cap)]
        assert all(item["COMP_COUNT"] == 2 for item in found)
        assert all(item["FOLDER_NAME"] == "F1" for item in found)
        last_item = found[-1]
        assert last_item["MAPPING_NAME"] == kept_name
        assert last_item["TGT_TABLE"] == f"ODS_TGT_{last:03d}"
        assert last_item["SRC_TABLE"] == f"HR.SRC_{last:03d}"
        blob = json.dumps(found)
        assert "HR.EXTRA_SOURCE" not in blob
        assert "ODS_EXTRA" not in blob
        assert "F2" not in blob
        assert response.headers.get("X-ODI-Mappings-Truncated") == "1"
        assert response.headers.get("X-ODI-Mappings-Limit") == str(cap)
        return

    import ai_helpers

    calls = _install_lineage(monkeypatch, ai_helpers, rows)
    body = ai_helpers._modeled_tables_answer()
    assert calls == [cap + 1]
    listed = body["queries"][0]["result"]["rows"]
    assert [row["MAPPING_NAME"] for row in listed] == [f"MAP_CAP_{index:03d}" for index in range(cap)]
    assert all(row["TARGET_TABLE"] == f"DWH.ODS_TGT_{index:03d}" for index, row in enumerate(listed))
    assert all(row["SOURCE_TABLES"] == f"HR.SRC_{index:03d}" for index, row in enumerate(listed))
    blob = json.dumps(body)
    assert "HR.EXTRA_SOURCE" not in blob
    assert "ODS_EXTRA" not in blob
    assert "F2" not in blob
    base = "Tables modeled in the current ODI mapping inventory are listed below."
    assert body["aciklama"] == f"{base} This listing summarises the first {cap} mappings."


@pytest.mark.parametrize("surface", ["route", "ai"])
def test_same_named_mappings_inside_the_cap_merge_as_on_0ac6fa2(surface, app_client, monkeypatch):
    """Name grouping from 0ac6fa2 still merges two in-cap ids. The cap does not split them."""
    from app_helpers import _group_odi_mapping_lineage_items

    rows = _rows_inside_cap_with_shared_name()
    page = _sql_mapping_page(rows, limit=10_000)
    expected = _group_odi_mapping_lineage_items(page)
    # SQL order is name, then component. MAP_OTHER is the first group on 0ac6fa2.
    assert [item["MAPPING_NAME"] for item in expected] == ["MAP_OTHER", "MAP_SAME"]
    merged = expected[1]
    assert merged["COMP_COUNT"] == 4
    assert merged["TGT_TABLE"] == "?"
    assert merged["SRC_TABLE"] == "HR.EXTRA_SOURCE, HR.SRC_INSIDE"
    assert merged["FOLDER_NAME"] == "F1"
    assert expected[0]["COMP_COUNT"] == 2
    assert expected[0]["TGT_TABLE"] == "ODS_OTHER"
    assert expected[0]["SRC_TABLE"] == "HR.SRC_OTHER"

    if surface == "route":
        import app as app_module

        calls = _install_lineage(monkeypatch, app_module, rows)
        response = app_client.get("/api/odi/mappings")
        assert response.status_code == 200
        assert calls == [MAPPINGS_LIST_CAP + 1]
        assert response.get_json() == expected
        assert response.headers.get("X-ODI-Mappings-Truncated") in (None, "")
        assert response.headers.get("X-ODI-Mappings-Limit") in (None, "")
        return

    import ai_helpers

    calls = _install_lineage(monkeypatch, ai_helpers, rows)
    body = ai_helpers._modeled_tables_answer()
    assert calls == [101]
    assert body["aciklama"] == "Tables modeled in the current ODI mapping inventory are listed below."
    listed = body["queries"][0]["result"]["rows"]
    assert [row["MAPPING_NAME"] for row in listed] == ["MAP_OTHER", "MAP_SAME"]
    assert listed[0]["SOURCE_TABLES"] == "HR.SRC_OTHER"
    assert listed[0]["TARGET_TABLE"] == "DWH.ODS_OTHER"
    assert listed[1]["SOURCE_TABLES"] == "HR.EXTRA_SOURCE, HR.SRC_INSIDE"
    assert listed[1]["TARGET_TABLE"] == "?"
