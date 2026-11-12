"""ODI-96: mapping-level lineage limit, complete source/target rows, truncation flag."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

MAPPINGS_LIST_CAP = 500


def _pair_rows(prefix: str, count: int, extra_join: bool = False) -> list[dict]:
    rows = []
    for index in range(count):
        name = f"{prefix}_{index:03d}"
        rows.append({
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


def test_mapping_lineage_sql_is_mapping_capped_with_binds_and_stable_is_target():
    source = Path(__file__).resolve().parents[1].joinpath("odi_mcp.py").read_text(encoding="utf-8")
    start = source.index("def odi_get_mapping_lineage")
    body = source[start:source.index("\ndef odi_get_mapping_details")]
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
    assert "ORDER BY m2.NAME, m2.I_MAPPING" in body
    assert "ORDER BY m.NAME, c.TYPE_NAME, c.I_MAP_COMP" in body
    assert "FETCH FIRST :lmt ROWS ONLY" in body
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


def test_mappings_route_keeps_source_and_target_past_100_component_rows(app_client, monkeypatch):
    import app as app_module

    rows = _pair_rows("MAP_ODI96", 60, extra_join=True)
    assert len(rows) > 100
    assert len({row["MAPPING_NAME"] for row in rows}) > 50

    def fake_lineage(limit=50):
        return json.dumps(rows[:limit])

    monkeypatch.setattr(app_module, "odi_get_mapping_lineage", fake_lineage)
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    found = response.get_json()
    assert isinstance(found, list)
    assert len(found) == 60
    for item in found:
        assert item["SRC_TABLE"] != "?"
        assert item["TGT_TABLE"] != "?"
        assert item["TGT_TABLE"].startswith("ODS_TGT_")
        assert "HR.SRC_" in item["SRC_TABLE"]


def test_mappings_route_sets_truncation_headers_at_the_cap(app_client, monkeypatch):
    import app as app_module

    rows = _pair_rows("MAP_CAP", MAPPINGS_LIST_CAP)

    def fake_lineage(limit=50):
        names = []
        seen = set()
        for row in rows:
            name = row["MAPPING_NAME"]
            if name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) >= limit:
                break
        keep = set(names[:limit])
        return json.dumps([row for row in rows if row["MAPPING_NAME"] in keep])

    monkeypatch.setattr(app_module, "odi_get_mapping_lineage", fake_lineage)
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    found = response.get_json()
    assert isinstance(found, list)
    assert len(found) == MAPPINGS_LIST_CAP
    assert response.headers.get("X-ODI-Mappings-Truncated") == "1"
    assert response.headers.get("X-ODI-Mappings-Limit") == str(MAPPINGS_LIST_CAP)


def test_mappings_route_omits_truncation_headers_below_the_cap(app_client, monkeypatch):
    import app as app_module

    rows = _pair_rows("MAP_UNDER", 3)
    monkeypatch.setattr(app_module, "odi_get_mapping_lineage", lambda limit=50: json.dumps(rows))
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    assert len(response.get_json()) == 3
    assert response.headers.get("X-ODI-Mappings-Truncated") in (None, "")
    assert response.headers.get("X-ODI-Mappings-Limit") in (None, "")


def test_modeled_tables_answer_summarises_the_first_n_when_truncated(monkeypatch):
    import ai_helpers

    rows = _pair_rows("MAP_AI", 100)
    monkeypatch.setattr(ai_helpers, "odi_get_mapping_lineage", lambda limit=100: json.dumps(rows))
    body = ai_helpers._modeled_tables_answer()
    assert "summarises the first 100 mappings" in body["aciklama"]
    assert len(body["queries"][0]["result"]["rows"]) == 100
