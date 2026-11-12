"""Screen-edge rulings for skipped steps, mapping layers, and preflight text."""

import json
from pathlib import Path

import pytest

from app_helpers import _group_odi_mapping_lineage_items
from mapping_generator import MappingGenerator
from routes.error_responses import redacted_operation_result
from skip_sentences import EXPECTED_SKIP_SENTENCES, STEP_SKIPPED


_AUDIT_SKIP = "Audit preservation does not apply to this load."
_SKEY_SKIP = "SKEY not requested"
_SOURCE_DATASTORE_SKIP = "Source datastore already exists."
_TARGET_DATASTORE_SKIP = "Target datastore already exists."
_SKEY_KEY_SKIP = "skey_natural_key was not specified"
_NO_SKEY_CHANGE = "No SKEY change"
_TARGET_UNCHANGED = "Target table did not change"
_NO_ROLLBACK = "No rollback record found."

_ACCEPTANCE = (
    ("MAP_ODI81N_F1", "ODI81N_SRC_F1", "ODS_ODI81N_F1", "ODS"),
    ("MAP_ODI81N_F1_UI", "ODI81N_SRC_F1", "ODS_ODI81N_F1_UI", "ODS"),
    ("MAP_ODI81N_F1_V2", "ODI81N_SRC_F1", "ODS_ODI81N_F1_V2", "ODS"),
    ("MAP_ODI81N_F2_UI", "ODI81N_SRC_F2", "ODS_ODI81N_F2_UI", "ODS"),
    ("MAP_ODI81N_F3_UI", "ODI81N_SRC_F3", "EDW_ODI81N_F3_UI", "EDW"),
    ("MAP_ODI81N_F3_V2", "ODI81N_SRC_F3", "EDW_ODI81N_F3_V2", "EDW"),
    ("MAP_ODI81N_F4_UI", "ODS_ODI81N_F4", "DIM_ODI81N_F4_UI", "EDW"),
    ("MAP_ODI81N_F4_SCR", "ODS_ODI81N_F4", "DIM_ODI81N_F4_SCR", "EDW"),
    ("MAP_ODI81N_F5_UI", "DWH_ODI81N_F5", "DM_DIM_ODI81N_F5", "DM"),
    ("MAP_ODI81N_F5_SCR", "DWH_ODI81N_F5", "DM_DIM_ODI81N_F5_SCR", "DM"),
    ("MAP_ODI81N_F5B_UI", "DWH_ODI81N_F5", "DM_DIM_ODI81N_F5B", "DM"),
)


def _component(mapping, name, qualified, **extra):
    row = {
        "MAPPING_NAME": mapping,
        "COMP_NAME": name,
        "COMP_TYPE": "DATASTORE",
        "TABLE_QUALIFIED_NAME": f"DWH.{qualified}",
        "PROJECT_CODE": "ORA_DWH",
        "PROJECT_NAME": "Oracle DWH",
        "FOLDER_NAME": "FLD_CORE_MAPPINGS",
    }
    row.update(extra)
    return row


def _truncate_generator():
    """A truncate load logs audit preservation as an expected skip. No database."""
    generator = MappingGenerator()
    params = {
        "integration_type": "CONTROL_APPEND",
        "src_owner": "DWH",
        "tgt_owner": "DWH",
        "src_model": "ORACLE_DWH",
        "tgt_model": "ORACLE_ODS",
        "tgt_table": "ODS_ODI81N_F2_UI",
        "src_table": "ODI81N_SRC_F2",
        "update_key_columns": [],
    }
    plan = {
        "src_columns": [{"COLUMN_NAME": "ENTITY_ID"}],
        "desired_col_defs": [("ENTITY_ID", "NUMBER(10)")],
    }
    profile = generator._prepare_audit_preservation(params, plan)
    assert profile["qualified"] is False
    generator._add_log("create_skey", "SKIPPED", _SKEY_SKIP)
    generator._add_log("register_datastores", "SKIPPED", _SOURCE_DATASTORE_SKIP)
    generator._add_log("populate_lineage", "SKIPPED", "ORA-00942: secret table missing")
    return generator


def _classic_success(generator):
    return {
        "batch_id": generator._batch_id,
        "overall_status": "SUCCESS",
        "params": {"integration_type": "CONTROL_APPEND", "tgt_table": "ODS_ODI81N_F2_UI"},
        "steps": {
            "create_skey": {"skipped": True},
            "populate_lineage": {"skipped": True, "reason": "ORA-00942: secret table missing"},
        },
        "log": list(generator._log_entries),
    }


def test_expected_skip_sentences_are_the_closed_generator_set():
    assert EXPECTED_SKIP_SENTENCES == {
        _AUDIT_SKIP,
        _SKEY_SKIP,
        _SOURCE_DATASTORE_SKIP,
        _TARGET_DATASTORE_SKIP,
        _SKEY_KEY_SKIP,
        _NO_SKEY_CHANGE,
        _TARGET_UNCHANGED,
        _NO_ROLLBACK,
    }
    assert STEP_SKIPPED == "Step skipped."
    assert all("failed" not in sentence.lower() for sentence in EXPECTED_SKIP_SENTENCES)


def test_redactor_keeps_expected_skips_and_neutralizes_exception_skips():
    generator = _truncate_generator()
    audit = next(entry for entry in generator._log_entries if entry["step"] == "audit_preservation")
    assert audit["status"] == "SKIPPED"
    assert audit["detail"] == _AUDIT_SKIP

    cleaned = redacted_operation_result(_classic_success(generator), "mapping generation")
    details = {entry["step"]: entry["detail"] for entry in cleaned["log"]}
    assert details["audit_preservation"] == _AUDIT_SKIP
    assert details["create_skey"] == _SKEY_SKIP
    assert details["register_datastores"] == _SOURCE_DATASTORE_SKIP
    assert details["populate_lineage"] == STEP_SKIPPED
    assert cleaned["steps"]["populate_lineage"]["reason"] == STEP_SKIPPED
    rendered = json.dumps(cleaned)
    assert "secret table" not in rendered
    assert "mapping generation failed." not in rendered
    assert cleaned["overall_status"] == "SUCCESS"


def test_redactor_still_replaces_a_genuine_failure():
    secret = "ORA-00942: secret table missing"
    cleaned = redacted_operation_result(
        {
            "overall_status": "FAILED",
            "error": secret,
            "error_code": "target_changed_during_generation",
            "steps": {"create_mapping": {"status": "FAILED", "detail": secret}},
            "log": [
                {"step": "pipeline", "status": "FAILED", "detail": secret},
                {"step": "create_skey", "status": "SKIPPED", "detail": _SKEY_SKIP},
            ],
        },
        "mapping generation",
    )
    assert cleaned["error"] == "mapping generation failed."
    assert cleaned["error_type"] == "OperationError"
    assert cleaned["steps"]["create_mapping"]["detail"] == "mapping generation failed."
    assert cleaned["log"][0]["detail"] == "mapping generation failed."
    assert cleaned["log"][1]["detail"] == _SKEY_SKIP
    assert secret not in json.dumps(cleaned)
    assert cleaned["error_code"] == "target_changed_during_generation"


@pytest.mark.parametrize("mapping, source, target, layer", _ACCEPTANCE)
def test_acceptance_mapping_layer_comes_from_the_target_name(mapping, source, target, layer):
    grouped = {
        item["MAPPING_NAME"]: item
        for item in _group_odi_mapping_lineage_items([
            _component(mapping, source, source, IS_TARGET="N"),
            _component(mapping, target, target, IS_TARGET="Y"),
        ])
    }
    found = grouped[mapping]
    assert found["TGT_TABLE"] == target
    assert found["TGT_OWNER"] == "DWH"
    assert found["LAYER"] == layer
    assert source in found["SRC_TABLE"]
    assert target not in found["SRC_TABLE"]


def test_mapping_without_a_recognisable_target_has_an_empty_layer():
    [found] = _group_odi_mapping_lineage_items([
        _component("MAP_PLAIN", "EMPLOYEES", "EMPLOYEES"),
        {
            "MAPPING_NAME": "MAP_PLAIN",
            "COMP_NAME": "CUSTOMERS",
            "COMP_TYPE": "DATASTORE",
            "TABLE_QUALIFIED_NAME": "DWH.CUSTOMERS",
        },
    ])
    assert found["LAYER"] == ""
    assert found["LAYER"] != "STG"
    assert found["TGT_TABLE"] == "?"


def test_row_expression_does_not_choose_the_target():
    [found] = _group_odi_mapping_lineage_items([
        _component("MAP_STRUCT", "ODS_SOURCE", "ODS_SOURCE"),
        _component("MAP_STRUCT", "CUSTOMERS", "CUSTOMERS", EXPRESSION="ODS_SOURCE.ENTITY_ID"),
    ])
    assert found["TGT_TABLE"] == "?"
    assert found["TGT_OWNER"] == "DWH"
    assert found["LAYER"] == ""
    assert found["SRC_TABLE"] == "DWH.CUSTOMERS"


def test_mappings_route_uses_the_same_layer_rules(app_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "odi_get_mapping_lineage",
        lambda limit=100: json.dumps([
            _component("MAP_ODI81N_F3_UI", "ODI81N_SRC_F3", "ODI81N_SRC_F3", IS_TARGET="N"),
            _component("MAP_ODI81N_F3_UI", "EDW_ODI81N_F3_UI", "EDW_ODI81N_F3_UI", IS_TARGET="Y"),
        ]),
    )
    response = app_client.get("/api/odi/mappings")
    assert response.status_code == 200
    [mapping] = response.get_json()
    assert mapping["TGT_TABLE"] == "EDW_ODI81N_F3_UI"
    assert mapping["LAYER"] == "EDW"
    assert mapping["folder_name"] == "FLD_CORE_MAPPINGS"
    assert mapping["project_code"] == "ORA_DWH"


def test_external_preflight_repository_failure_keeps_class_name_only(monkeypatch):
    from tests.test_e2e_repository_preflight import _f1_generator, _quiet_database

    _quiet_database(monkeypatch)
    generator, params = _f1_generator()
    params = dict(params, src_model="ORACLE_SOURCE")

    def explode(_params):
        raise RuntimeError("password=secret\nhost=10.1.2.3")

    monkeypatch.setattr(generator, "_resolve_odi_repository_bindings", explode)
    failed = generator.target_table_preflight(params)
    repository = failed["repository"]
    assert repository["status"] == "unavailable"
    assert repository["error"] == "Repository identity check failed."
    assert repository["error_type"] == "RuntimeError"
    rendered = json.dumps(failed)
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "10.1.2.3" not in rendered
    assert "RuntimeError" in rendered


_MARKER = "synthetic-exception-marker"
_ROW_PROBE_MARKER = "synthetic-row-probe-marker"
_RULED_FIELDS = ("LAYER", "TGT_TABLE", "TGT_OWNER", "SRC_TABLE")
_AUDIT_KEEP = "Audit preservation does not apply to this load."


def _lineage_row(mapping, comp, qualified, comp_type="DATASTORE", **extra):
    row = {
        "MAPPING_NAME": mapping,
        "COMP_NAME": comp,
        "COMP_TYPE": comp_type,
        "TABLE_QUALIFIED_NAME": qualified,
        "PROJECT_CODE": "ORA_DWH",
        "PROJECT_NAME": "Oracle DWH",
        "FOLDER_NAME": "FLD_CORE_MAPPINGS",
    }
    row.update(extra)
    return row


def _base_route_group(items: list) -> list:
    """Oracle copied from the base /api/odi/mappings grouping (c9febe9)."""
    grouped = {}
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
                "LAYER": "STG",
                "DESC": "",
                "COMP_COUNT": 0,
                "JOIN_COUNT": 0,
                "SRC_LIST": set(),
            }
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
        qualified_name = str(item.get("TABLE_QUALIFIED_NAME") or "").strip()
        comp_type = str(item.get("COMP_TYPE") or "").upper()
        comp_name = str(item.get("COMP_NAME") or "").upper()
        if comp_type == "JOIN" or comp_type == "LOOKUP":
            grouped[key]["JOIN_COUNT"] += 1
        expression = item.get("EXPRESSION")
        if expression:
            expression_text = str(expression).strip()
            if any(char in expression_text for char in ["'", "(", " ", "+", "||"]) or "." not in expression_text:
                grouped[key]["HAS_EXP"] = True
        if comp_type == "FILTER":
            grouped[key]["HAS_FILTER"] = True

        mapping_name_upper = str(name).upper()
        qualified_short = qualified_name.split(".")[-1].upper() if qualified_name else ""
        is_target = (
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
        if is_target and qualified_name:
            parts = qualified_name.split(".")
            target_name = parts[1] if len(parts) > 1 else qualified_name
            grouped[key]["TGT_OWNER"] = parts[0] if len(parts) > 1 else "DWH"
            grouped[key]["TGT_TABLE"] = target_name
            target_name_upper = target_name.upper()
            if target_name_upper.startswith("ODS_"):
                grouped[key]["LAYER"] = "ODS"
            elif target_name_upper.startswith("SKEY_"):
                grouped[key]["LAYER"] = "SKEY"
            elif target_name_upper.startswith("EDW_"):
                grouped[key]["LAYER"] = "EDW"
            elif target_name_upper.startswith("DM_"):
                grouped[key]["LAYER"] = "DM"
            else:
                grouped[key]["LAYER"] = "STG"
        elif qualified_name or comp_name:
            source_name = qualified_name if qualified_name else comp_name
            if comp_type in ["FILE", "XML", "DATASTORE", "XSD"]:
                grouped[key]["SRC_LIST"].add(source_name)
            if grouped[key]["SRC_TABLE"] == "?" or comp_type in ["FILE", "XML"]:
                grouped[key]["SRC_TABLE"] = source_name

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

    for key in grouped:
        if grouped.get(key) and "SRC_LIST" in grouped[key]:
            grouped[key]["SRC_TABLE"] = ", ".join(sorted(grouped[key]["SRC_LIST"]))
            del grouped[key]["SRC_LIST"]
    return list(grouped.values())


def _reviewer_rows():
    rows = []
    for mapping, source, target, _layer in _ACCEPTANCE:
        rows.append(_lineage_row(mapping, source, f"DWH.{source}", IS_TARGET="N"))
        rows.append(_lineage_row(mapping, target, f"DWH.{target}", IS_TARGET="Y"))
    rows.extend([
        _lineage_row("MAP_HR_EMPLOYEES_TO_ODS", "EMPLOYEES", "HR.EMPLOYEES", IS_TARGET="N"),
        _lineage_row("MAP_HR_EMPLOYEES_TO_ODS", "J_ORDERS", None, "JOIN"),
        _lineage_row("MAP_HR_EMPLOYEES_TO_ODS", "LKP_CUST", None, "LOOKUP"),
        _lineage_row(
            "MAP_HR_EMPLOYEES_TO_ODS", "FLT_EMP", None, "FILTER",
            EXPRESSION="EMPLOYEES.EMPLOYEE_ID > 0",
        ),
        _lineage_row(
            "MAP_HR_EMPLOYEES_TO_ODS", "ODS_HR_EMPLOYEES", "DWH.ODS_HR_EMPLOYEES",
            IS_TARGET="Y", EXPRESSION="EMPLOYEES.EMPLOYEE_ID",
        ),
        _lineage_row("MAP_MULTI", "B_SRC", "SH.B_TABLE", IS_TARGET="N"),
        _lineage_row("MAP_MULTI", "A_SRC", "HR.A_TABLE", IS_TARGET="N"),
        _lineage_row("MAP_MULTI", "ODS_T", "DWH.ODS_T", IS_TARGET="Y"),
        _lineage_row("MAP_LOWER_Q", "orders", "hr.orders", IS_TARGET="N"),
        _lineage_row("MAP_LOWER_Q", "dim_customer", "dwh.dim_customer", IS_TARGET="Y"),
        _lineage_row("MAP_PLAIN", "EMPLOYEES", "DWH.EMPLOYEES"),
        _lineage_row("MAP_PLAIN", "CUSTOMERS", "DWH.CUSTOMERS"),
        _lineage_row(
            "MAP_ORDERS_SKEY_V1", "ODS_ORDERS", "DWH.ODS_ORDERS",
            IS_TARGET="N", EXPRESSION="ODS_ORDERS.ID + 1",
        ),
        _lineage_row("MAP_ORDERS_SKEY_V1", "SKEY_ORDERS", "DWH.SKEY_ORDERS", IS_TARGET="Y"),
        _lineage_row("MAP_CASE", "employees", None),
        _lineage_row("MAP_CASE", "Customers", ""),
        _lineage_row("MAP_3PART", "ORDERS", "HR.ORDERS", IS_TARGET="N"),
        _lineage_row("MAP_3PART", "DIM_CUSTOMER", "ACME.EDW.DIM_CUSTOMER", IS_TARGET="Y"),
        _lineage_row(
            "MAP_DM_TO_DIM", "DM_X", "DWH.DM_X",
            IS_TARGET="N", EXPRESSION="DM_X.ID + 1",
        ),
        _lineage_row("MAP_DM_TO_DIM", "DIM_Y", "DWH.DIM_Y", IS_TARGET="Y"),
        _lineage_row(
            "MAP_ODS_TO_EDW", "ODS_X", "DWH.ODS_X",
            IS_TARGET="N", EXPRESSION="ODS_X.ID + 1",
        ),
        _lineage_row("MAP_ODS_TO_EDW", "EDW_Y", "DWH.EDW_Y", IS_TARGET="Y"),
        _lineage_row(
            "MAP_ODS_TO_STG", "ODS_X", "DWH.ODS_X",
            IS_TARGET="N", EXPRESSION="ODS_X.ID + 1",
        ),
        _lineage_row("MAP_ODS_TO_STG", "STG_Y", "DWH.STG_Y", IS_TARGET="Y"),
        _lineage_row(
            "MAP_ODS_TO_PLAIN", "ODS_X", "DWH.ODS_X",
            IS_TARGET="N", EXPRESSION="ODS_X.ID + 1",
        ),
        _lineage_row("MAP_ODS_TO_PLAIN", "CUSTOMERS", "DWH.CUSTOMERS", IS_TARGET="Y"),
        _lineage_row("MAP_TWO", "ODS_A", "DWH.ODS_A", IS_TARGET="Y"),
        _lineage_row("MAP_TWO", "EDW_B", "DWH.EDW_B", IS_TARGET="Y"),
        _lineage_row("MAP_NOFLAG", "ORDERS", "HR.ORDERS"),
        _lineage_row("MAP_NOFLAG", "ODS_ORDERS", "DWH.ODS_ORDERS"),
        _lineage_row(
            "MAP_PAD", "ORDERS", "HR.ORDERS",
            FOLDER_NAME=" FLD ", PROJECT_CODE=" P ", PROJECT_NAME=" Name ",
        ),
    ])
    return rows


_EXPLICIT = {
    "MAP_DM_TO_DIM": ("EDW", "DIM_Y", "DWH", "DWH.DM_X"),
    "MAP_ODS_TO_EDW": ("EDW", "EDW_Y", "DWH", "DWH.ODS_X"),
    "MAP_ODS_TO_STG": ("STG", "STG_Y", "DWH", "DWH.ODS_X"),
    "MAP_ODS_TO_PLAIN": ("", "CUSTOMERS", "DWH", "DWH.ODS_X"),
    "MAP_ORDERS_SKEY_V1": ("SKEY", "SKEY_ORDERS", "DWH", "DWH.ODS_ORDERS"),
    "MAP_TWO": ("", "?", "DWH", "DWH.EDW_B"),
    "MAP_NOFLAG": ("", "?", "DWH", "HR.ORDERS"),
    "MAP_3PART": ("EDW", "DIM_CUSTOMER", "EDW", "HR.ORDERS"),
    "MAP_LOWER_Q": ("EDW", "dim_customer", "dwh", "hr.orders"),
    "MAP_CASE": ("", "?", "DWH", "CUSTOMERS, EMPLOYEES"),
    "MAP_PLAIN": ("", "?", "DWH", "DWH.CUSTOMERS, DWH.EMPLOYEES"),
    "MAP_HR_EMPLOYEES_TO_ODS": ("ODS", "ODS_HR_EMPLOYEES", "DWH", "HR.EMPLOYEES"),
    "MAP_MULTI": ("ODS", "ODS_T", "DWH", "HR.A_TABLE, SH.B_TABLE"),
    "MAP_PAD": ("", "?", "DWH", "HR.ORDERS"),
}
for _mapping, _source, _target, _layer in _ACCEPTANCE:
    _EXPLICIT[_mapping] = (_layer, _target, "DWH", f"DWH.{_source}")


def test_lineage_grouping_matches_the_base_route_except_ruled_fields():
    rows = _reviewer_rows()
    found = {item["MAPPING_NAME"]: item for item in _group_odi_mapping_lineage_items(rows)}
    oracle = {item["MAPPING_NAME"]: item for item in _base_route_group(rows)}
    assert set(found) == set(oracle)
    for name, expected in found.items():
        base = oracle[name]
        assert set(expected) == set(base), name
        for key, value in expected.items():
            if key in _RULED_FIELDS:
                continue
            assert value == base[key], name
            if isinstance(value, str):
                assert value.encode() == base[key].encode(), name
        layer, table, owner, source = _EXPLICIT[name]
        assert expected["LAYER"] == layer, name
        assert expected["TGT_TABLE"] == table, name
        assert expected["TGT_OWNER"] == owner, name
        assert expected["SRC_TABLE"] == source, name
        assert expected["SRC_TABLE"].encode() == source.encode(), name
        if table == "?":
            assert expected["SRC_TABLE"] == base["SRC_TABLE"], name
            assert expected["SRC_TABLE"].encode() == base["SRC_TABLE"].encode(), name


def test_lineage_query_adds_is_target_from_the_details_expression_joins():
    """Text contract only. This session cannot run the repository SELECT."""
    source = Path(__file__).resolve().parents[1].joinpath("odi_mcp.py").read_text(encoding="utf-8")
    start = source.index("def odi_get_mapping_lineage")
    body = source[start:source.index("\ndef odi_get_mapping_details")]
    assert "CASE" in body
    assert "END AS IS_TARGET" in body
    assert "THEN 'Y'" in body
    assert "ELSE 'N'" in body
    assert "UPPER(c.TYPE_NAME) IN ('DATASTORE', 'FILE')" in body
    assert "EXISTS (" in body
    joined = " ".join(body.split())
    assert "FROM {ODI_REPO_SCHEMA}.SNP_MAP_CP cp" in joined
    assert "cp.I_OWNER_MAP_COMP = c.I_MAP_COMP" in joined
    assert "cp.DIRECTION = 'O'" in joined
    assert "JOIN {ODI_REPO_SCHEMA}.SNP_MAP_ATTR a ON a.I_OWNER_MAP_CP = cp.I_MAP_CP" in joined
    assert "LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR" in joined
    assert "TRIM(e.TXT) IS NOT NULL" in joined
    for column in (
        "m.NAME as MAPPING_NAME",
        "p.PROJECT_CODE as PROJECT_CODE",
        "p.PROJECT_NAME as PROJECT_NAME",
        "f.FOLDER_NAME as FOLDER_NAME",
        "c.NAME as COMP_NAME",
        "c.TYPE_NAME as COMP_TYPE",
        "r.QUALIFIED_NAME as TABLE_QUALIFIED_NAME",
    ):
        assert column in body
    assert "ORDER BY m.NAME, c.TYPE_NAME" in body
    assert "FETCH FIRST {limit} ROWS ONLY" in body
    assert "FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m" in body
    assert "LEFT JOIN {ODI_REPO_SCHEMA}.SNP_PROJECT p ON p.I_PROJECT = m.I_PROJECT" in body
    assert "LEFT JOIN {ODI_REPO_SCHEMA}.SNP_FOLDER f ON f.I_FOLDER = m.I_FOLDER" in body
    assert "JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP c ON m.I_MAPPING = c.I_OWNER_MAPPING" in body
    assert "LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_REF r ON c.I_MAP_REF = r.I_MAP_REF" in body
    for forbidden in ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP"):
        assert forbidden not in body


def test_row_probe_http_200_hides_the_exception_text(app_client, monkeypatch):
    import db
    import mapping_generator as mg
    import odi81_app_acceptance as driver
    from tests.test_e2e_repository_preflight import _PREFLIGHT_COLUMNS

    class Cursor:
        def execute(self, sql, *_args, **_kwargs):
            if "ROWNUM" in str(sql):
                raise RuntimeError(f"{_ROW_PROBE_MARKER}\npassword=secret")

        def fetchone(self):
            return (1,)

        def fetchall(self):
            return [("ENTITY_ID",)]

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(db, "get_connection", lambda use_ddl=True: Connection())
    monkeypatch.setattr(
        mg.MappingGenerator,
        "_source_columns_for",
        lambda self, _params: [dict(column) for column in _PREFLIGHT_COLUMNS],
    )
    response = app_client.post(
        "/api/mapping/target-preflight",
        json={"params": driver.classic_params("F1")},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["exists"] is True
    assert body["row_state"] == "unknown"
    assert body["row_probe_error"] == "Row count check failed."
    assert body["row_probe_error_type"] == "RuntimeError"
    rendered = response.get_data(as_text=True)
    assert _ROW_PROBE_MARKER not in rendered
    assert "password" not in rendered
    assert "secret" not in rendered


def test_skipped_overall_status_redacts_message():
    cleaned = redacted_operation_result(
        {"overall_status": "SKIPPED", "message": _MARKER},
        "mapping generation",
    )
    assert cleaned["message"] == STEP_SKIPPED
    assert cleaned["overall_status"] == "SKIPPED"
    assert _MARKER not in json.dumps(cleaned)


def test_skipped_nested_message_and_detail_are_neutral():
    cleaned = redacted_operation_result(
        {
            "status": "SKIPPED",
            "message": [_MARKER, {"note": _MARKER}],
            "detail": {"kept": _AUDIT_KEEP, "leak": _MARKER, "n": 3},
            "reason": _MARKER,
        },
        "mapping generation",
    )
    assert cleaned["message"] == [STEP_SKIPPED, {"note": STEP_SKIPPED}]
    assert cleaned["detail"] == {"kept": _AUDIT_KEEP, "leak": STEP_SKIPPED, "n": 3}
    assert cleaned["reason"] == STEP_SKIPPED
    assert _MARKER not in json.dumps(cleaned)


def test_success_plus_skipped_overall_is_a_skip():
    cleaned = redacted_operation_result(
        {
            "status": "SUCCESS",
            "overall_status": "SKIPPED",
            "message": _MARKER,
            "detail": _MARKER,
        },
        "mapping generation",
    )
    assert cleaned["status"] == "SUCCESS"
    assert cleaned["overall_status"] == "SKIPPED"
    assert cleaned["message"] == STEP_SKIPPED
    assert cleaned["detail"] == STEP_SKIPPED
    assert _MARKER not in json.dumps(cleaned)


def test_mixed_case_skip_redacts_nested_strings():
    cleaned = redacted_operation_result(
        {
            "status": "Skipped",
            "overall_status": "ok",
            "detail": [f"  {_MARKER}  ", _SKEY_SKIP],
            "message": {"inner": {"text": _MARKER}},
        },
        "mapping update",
    )
    assert cleaned["detail"] == [STEP_SKIPPED, _SKEY_SKIP]
    assert cleaned["message"] == {"inner": {"text": STEP_SKIPPED}}
    other = redacted_operation_result(
        {
            "status": "success",
            "overall_status": "Skipped",
            "message": [_MARKER],
            "reason": {"why": _AUDIT_KEEP},
        },
        "mapping rollback",
    )
    assert other["message"] == [STEP_SKIPPED]
    assert other["reason"] == {"why": _AUDIT_KEEP}
    assert _MARKER not in json.dumps(cleaned)
    assert _MARKER not in json.dumps(other)


def test_failure_in_either_status_keeps_failure_treatment():
    cleaned = redacted_operation_result(
        {
            "status": "skipped",
            "overall_status": "FAILED",
            "detail": _MARKER,
            "message": _MARKER,
            "reason": _AUDIT_KEEP,
        },
        "mapping generation",
    )
    assert cleaned["detail"] == "mapping generation failed."
    assert cleaned["reason"] == "mapping generation failed."
    assert cleaned["message"] == _MARKER
    other = redacted_operation_result(
        {"status": "SUCCESS", "overall_status": "INVALID", "detail": _MARKER},
        "mapping delete",
    )
    assert other["detail"] == "mapping delete failed."
    assert _MARKER not in other["detail"]


def test_success_dict_leaves_message_and_detail_unchanged():
    cleaned = redacted_operation_result(
        {
            "status": "SUCCESS",
            "overall_status": "SUCCESS",
            "message": _MARKER,
            "detail": _MARKER,
            "reason": _MARKER,
        },
        "mapping generation",
    )
    assert cleaned["message"] == _MARKER
    assert cleaned["detail"] == _MARKER
    assert cleaned["reason"] == _MARKER
