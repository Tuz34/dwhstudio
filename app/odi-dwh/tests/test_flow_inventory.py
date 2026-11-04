import pytest


def test_build_ods_flow_inventory_does_not_treat_target_table_as_odi_flow():
    from flow_inventory import build_ods_flow_inventory

    rows = build_ods_flow_inventory(
        source_tables=["HR.COUNTRIES", "HR.EMPLOYEES", "HR.JOBS"],
        target_table_names={"ODS_HR_COUNTRIES", "ODS_HR_EMPLOYEES"},
        datastore_rows=[
            {"model_code": "SRC_HR", "model_name": "MDL_SRC_ORACLE_HR", "logical_schema": "HR", "datastore_name": "COUNTRIES", "resource_name": "COUNTRIES"},
            {"model_code": "SRC_HR", "model_name": "MDL_SRC_ORACLE_HR", "logical_schema": "HR", "datastore_name": "EMPLOYEES", "resource_name": "EMPLOYEES"},
            {"model_code": "SRC_HR", "model_name": "MDL_SRC_ORACLE_HR", "logical_schema": "HR", "datastore_name": "JOBS", "resource_name": "JOBS"},
            {"model_code": "TGT_DWH", "model_name": "MDL_TGT_ORACLE_DWH", "logical_schema": "DWH", "datastore_name": "ODS_HR_COUNTRIES", "resource_name": "ODS_HR_COUNTRIES"},
            {"model_code": "TGT_DWH", "model_name": "MDL_TGT_ORACLE_DWH", "logical_schema": "DWH", "datastore_name": "ODS_HR_EMPLOYEES", "resource_name": "ODS_HR_EMPLOYEES"},
        ],
        mapping_ref_rows=[
            {"mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "comp_name": "EMPLOYEES", "comp_type": "DATASTORE", "table_qualified_name": "SRC_HR.EMPLOYEES", "is_target": "N"},
            {"mapping_name": "MAP_HR_EMPLOYEES_TO_ODS", "comp_name": "ODS_HR_EMPLOYEES", "comp_type": "DATASTORE", "table_qualified_name": "TGT_DWH.ODS_HR_EMPLOYEES", "is_target": "Y"},
        ],
        source_schema="HR",
        target_owner="DWH",
    )

    by_source = {row["SOURCE_TABLE"]: row for row in rows}
    assert by_source["HR.EMPLOYEES"]["ODI_MAPPING_EXISTS"] is True
    assert by_source["HR.COUNTRIES"]["TARGET_TABLE_EXISTS"] is True
    assert by_source["HR.COUNTRIES"]["ODI_MAPPING_EXISTS"] is False
    assert by_source["HR.COUNTRIES"]["STATUS"] == "MAPPING_MISSING"
    assert by_source["HR.JOBS"]["STATUS"] == "TARGET_TABLE_MISSING"


def test_unmapped_odi_flow_answer_only_returns_missing_rows(monkeypatch):
    import flow_inventory

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {"SOURCE_TABLE": "HR.EMPLOYEES", "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_EMPLOYEES", "ODI_MAPPING_EXISTS": True, "STATUS": "FLOW_OK"},
            {"SOURCE_TABLE": "HR.COUNTRIES", "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_COUNTRIES", "ODI_MAPPING_EXISTS": False, "STATUS": "MAPPING_MISSING"},
        ],
    )

    from app import _unmapped_odi_flow_answer

    answer = _unmapped_odi_flow_answer("HR semasinda olup DWH semasina ODI akisi yapilmamis tablo listesi")
    result = answer["queries"][0]["result"]

    assert result["columns"] == [
        "SOURCE_TABLE",
        "EXPECTED_TARGET_TABLE",
        "TARGET_TABLE_EXISTS",
        "SOURCE_DATASTORE_EXISTS",
        "TARGET_DATASTORE_EXISTS",
        "ODI_MAPPING_EXISTS",
        "MAPPING_NAME",
        "STATUS",
    ]
    assert result["rows"] == [
        {"SOURCE_TABLE": "HR.COUNTRIES", "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_COUNTRIES", "ODI_MAPPING_EXISTS": False, "STATUS": "MAPPING_MISSING"}
    ]


def test_api_ask_unmapped_odi_flow_shortcut_does_not_call_llm(app_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_unmapped_odi_flow_answer",
        lambda message: {"aciklama": "shortcut", "queries": [], "raw": ""},
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ask_ai should not be called for unmapped ODI flow inventory questions")

    monkeypatch.setattr(app_module, "ask_ai", fail_if_called)

    resp = app_client.post(
        "/api/ask",
        json={"message": "HR semasinda olup DWH semasina ODI akisi yapilmamis tablo listesi", "history": []},
    )

    assert resp.status_code == 200
    assert resp.get_json()["aciklama"] == "shortcut"


def test_modeled_tables_answer_counts_schema_flows(monkeypatch):
    import flow_inventory

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.COUNTRIES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_COUNTRIES",
                "ODI_MAPPING_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_COUNTRIES_TO_ODS",
                "STATUS": "FLOW_OK",
            },
            {
                "SOURCE_TABLE": "HR.EMPLOYEES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_EMPLOYEES",
                "ODI_MAPPING_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                "STATUS": "FLOW_OK",
            },
            {
                "SOURCE_TABLE": "HR.REGIONS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_REGIONS",
                "ODI_MAPPING_EXISTS": False,
                "MAPPING_NAME": "",
                "STATUS": "MAPPING_MISSING",
            },
        ],
    )

    from app import _modeled_tables_answer

    answer = _modeled_tables_answer("HR semasindaki tablolarin kaci ODI'da modelli?")
    result = answer["queries"][0]["result"]

    assert answer["summary"] == {
        "source_schema": "HR",
        "source_table_count": 3,
        "modeled_count": 2,
        "missing_count": 1,
    }
    assert "2 of 3 source tables" in answer["aciklama"]
    assert result["columns"] == [
        "SOURCE_TABLE",
        "EXPECTED_TARGET_TABLE",
        "ODI_MAPPING_EXISTS",
        "MAPPING_NAME",
        "STATUS",
    ]
    assert [row["SOURCE_TABLE"] for row in result["rows"]] == [
        "HR.COUNTRIES",
        "HR.EMPLOYEES",
        "HR.REGIONS",
    ]


def test_unmapped_odi_flow_intent_accepts_turkish_characters():
    from app import _is_unmapped_odi_flow_question

    assert _is_unmapped_odi_flow_question("HR şemasında olup DWH şemasına ODI akışı yapılmamış tablo listesi")


def test_unmapped_odi_flow_intent_tolerates_question_mark_encoding_loss():
    from app import _is_unmapped_odi_flow_question

    assert _is_unmapped_odi_flow_question("HR ?emas?nda olup DWH ?emas?na ODI ak??? yap?lmam?? tablo listesi")


def test_unmapped_odi_flow_answer_remembers_missing_rows(monkeypatch):
    import flow_inventory

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.DEPARTMENTS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_DEPARTMENTS",
                "TARGET_TABLE_EXISTS": False,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": False,
                "ODI_MAPPING_EXISTS": False,
                "MAPPING_NAME": "",
                "STATUS": "TARGET_TABLE_MISSING",
            }
        ],
    )

    from app import app, _unmapped_odi_flow_answer

    app.config["TESTING"] = True
    with app.test_request_context("/api/ask"):
        _unmapped_odi_flow_answer("HR semasinda DWH ODI akisi yapilmamis tablo listesi")
        assert app.session_interface is not None
        from flask import session

        assert session["last_unmapped_odi_flows"][0]["src_table"] == "HR.DEPARTMENTS"
        assert session["last_unmapped_odi_flows"][0]["tgt_table"] == "ODS_HR_DEPARTMENTS"


def test_api_ask_creates_bulk_mapping_requests_from_last_missing_flows(app_client, monkeypatch):
    import app as app_module
    import flow_inventory

    with app_client.session_transaction() as sess:
        sess["last_unmapped_odi_flows"] = [
            {
                "src_table": "HR.DEPARTMENTS",
                "tgt_table": "ODS_HR_DEPARTMENTS",
                "tgt_owner": "DWH",
                "mapping_name": "MAP_HR_DEPARTMENTS_TO_ODS",
            },
            {
                "src_table": "HR.JOBS",
                "tgt_table": "ODS_HR_JOBS",
                "tgt_owner": "DWH",
                "mapping_name": "MAP_HR_JOBS_TO_ODS",
            },
        ]

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.DEPARTMENTS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_DEPARTMENTS",
                "TARGET_TABLE_EXISTS": False,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": False,
                "ODI_MAPPING_EXISTS": False,
                "MAPPING_NAME": "",
                "STATUS": "TARGET_TABLE_MISSING",
            },
            {
                "SOURCE_TABLE": "HR.JOBS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_JOBS",
                "TARGET_TABLE_EXISTS": False,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": False,
                "ODI_MAPPING_EXISTS": False,
                "MAPPING_NAME": "",
                "STATUS": "TARGET_TABLE_MISSING",
            },
        ],
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ask_ai should not be called for remembered missing-flow mapping requests")

    monkeypatch.setattr(app_module, "ask_ai", fail_if_called)

    resp = app_client.post(
        "/api/ask",
        json={"message": "eksik olan 5 tablo", "history": [{"role": "user", "content": "bu 5 tabloyu ODS katmanina modelleyelim"}]},
    )

    data = resp.get_json()
    assert resp.status_code == 200
    assert "mapping_request" not in data
    assert len(data["mapping_requests"]) == 2
    assert data["mapping_requests"][0]["src_table"] == "HR.DEPARTMENTS"
    assert data["mapping_requests"][0]["tgt_table"] == "ODS_HR_DEPARTMENTS"
    assert data["mapping_requests"][0]["operation"] == "CREATE"


def test_api_ask_rebuilds_bulk_mapping_requests_when_session_memory_is_missing(app_client, monkeypatch):
    import app as app_module
    import flow_inventory

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.DEPARTMENTS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_DEPARTMENTS",
                "TARGET_TABLE_EXISTS": False,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": False,
                "ODI_MAPPING_EXISTS": False,
                "MAPPING_NAME": "",
                "STATUS": "TARGET_TABLE_MISSING",
            },
            {
                "SOURCE_TABLE": "HR.EMPLOYEES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_EMPLOYEES",
                "TARGET_TABLE_EXISTS": True,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": True,
                "ODI_MAPPING_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
                "STATUS": "FLOW_OK",
            },
        ],
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ask_ai should not be called for history-based missing-flow mapping requests")

    monkeypatch.setattr(app_module, "ask_ai", fail_if_called)

    history = [{
        "role": "assistant",
        "content": (
            "The tables in the HR schema were checked using read-only metadata. "
            "Of 2 source tables, 1 are missing an ODI flow to the DWH ODS layer (no ODI flow to DWH).\n\n"
            "[HR Tables Without ODI Flow to DWH]\n"
            "Columns: SOURCE_TABLE, EXPECTED_TARGET_TABLE, TARGET_TABLE_EXISTS, SOURCE_DATASTORE_EXISTS, TARGET_DATASTORE_EXISTS, ODI_MAPPING_EXISTS, MAPPING_NAME, STATUS\n"
            "Rows:\n"
            "SOURCE_TABLE=HR.DEPARTMENTS, EXPECTED_TARGET_TABLE=DWH.ODS_HR_DEPARTMENTS, STATUS=TARGET_TABLE_MISSING"
        ),
    }]

    resp = app_client.post(
        "/api/ask",
        json={"message": "bunları modelleyelim", "history": history},
    )

    data = resp.get_json()
    assert resp.status_code == 200
    assert len(data["mapping_requests"]) == 1
    assert data["mapping_requests"][0]["src_table"] == "HR.DEPARTMENTS"


def test_api_ask_rebuilds_bulk_mapping_requests_for_pronoun_followup_without_history(app_client, monkeypatch):
    import app as app_module
    import flow_inventory

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.DEPARTMENTS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_DEPARTMENTS",
                "TARGET_TABLE_EXISTS": False,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": False,
                "ODI_MAPPING_EXISTS": False,
                "MAPPING_NAME": "",
                "STATUS": "TARGET_TABLE_MISSING",
            }
        ],
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ask_ai should not be called for pronoun missing-flow mapping requests")

    monkeypatch.setattr(app_module, "ask_ai", fail_if_called)

    resp = app_client.post(
        "/api/ask",
        json={"message": "bunları modelleyelim", "history": []},
    )

    data = resp.get_json()
    assert resp.status_code == 200
    assert len(data["mapping_requests"]) == 1
    assert data["mapping_requests"][0]["mapping_name"] == "MAP_HR_DEPARTMENTS_TO_ODS"


def test_api_ask_does_not_use_stale_missing_flow_session_after_mappings_exist(app_client, monkeypatch):
    import app as app_module
    import flow_inventory

    with app_client.session_transaction() as sess:
        sess["last_unmapped_odi_flows"] = [
            {
                "src_table": "HR.DEPARTMENTS",
                "tgt_table": "ODS_HR_DEPARTMENTS",
                "tgt_owner": "DWH",
                "mapping_name": "MAP_HR_DEPARTMENTS_TO_ODS",
            }
        ]

    monkeypatch.setattr(
        flow_inventory,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.DEPARTMENTS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_DEPARTMENTS",
                "TARGET_TABLE_EXISTS": True,
                "SOURCE_DATASTORE_EXISTS": True,
                "TARGET_DATASTORE_EXISTS": True,
                "ODI_MAPPING_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_DEPARTMENTS_TO_ODS",
                "STATUS": "FLOW_OK",
            }
        ],
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ask_ai should not be called for stale missing-flow follow-up")

    monkeypatch.setattr(app_module, "ask_ai", fail_if_called)

    resp = app_client.post(
        "/api/ask",
        json={"message": "bunları modelleyelim", "history": []},
    )

    data = resp.get_json()
    assert resp.status_code == 200
    assert data["mapping_requests"] == []
    assert "for 0 tables" in data["aciklama"]


def test_mapping_inventory_includes_repository_folder_metadata():
    from flow_inventory import build_ods_flow_inventory

    rows = build_ods_flow_inventory(
        source_tables=["HR.DEPARTMENTS"],
        target_table_names={"ODS_HR_DEPARTMENTS"},
        datastore_rows=[
            {"model_code": "SRC_HR", "model_name": "MDL_SRC_ORACLE_HR", "logical_schema": "HR", "datastore_name": "DEPARTMENTS", "resource_name": "DEPARTMENTS"},
            {"model_code": "TGT_DWH", "model_name": "MDL_TGT_ORACLE_DWH", "logical_schema": "DWH", "datastore_name": "ODS_HR_DEPARTMENTS", "resource_name": "ODS_HR_DEPARTMENTS"},
        ],
        mapping_ref_rows=[
            {
                "mapping_name": "MAP_HR_DEPARTMENTS_TO_ODS",
                "project_code": "PRJ_ORACLE_DWH_AUTOMATION",
                "folder_name": "FLD_CORE_MAPPINGS",
                "comp_name": "DEPARTMENTS",
                "comp_type": "DATASTORE",
                "table_qualified_name": "SRC_HR.DEPARTMENTS",
                "is_target": "N",
            },
            {
                "mapping_name": "MAP_HR_DEPARTMENTS_TO_ODS",
                "project_code": "PRJ_ORACLE_DWH_AUTOMATION",
                "folder_name": "FLD_CORE_MAPPINGS",
                "comp_name": "ODS_HR_DEPARTMENTS",
                "comp_type": "DATASTORE",
                "table_qualified_name": "TGT_DWH.ODS_HR_DEPARTMENTS",
                "is_target": "Y",
            },
        ],
        source_schema="HR",
        target_owner="DWH",
    )

    assert rows[0]["ODI_PROJECT"] == "PRJ_ORACLE_DWH_AUTOMATION"
    assert rows[0]["ODI_FOLDER"] == "FLD_CORE_MAPPINGS"
    assert rows[0]["ODI_PATH"] == "PRJ_ORACLE_DWH_AUTOMATION / FLD_CORE_MAPPINGS"
