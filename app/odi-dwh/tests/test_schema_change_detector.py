import schema_change_detector as detector


def test_get_odi_column_definitions_uses_current_snp_col_type_columns(monkeypatch):
    captured = {}

    class FakeCursor:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            if "c.DATA_TYPE" in sql or "c.SCALE," in sql or "c.PRECISION" in sql:
                raise AssertionError("ODI SNP_COL type query must use SOURCE_DT, LONGC, and SCALEC")

        def fetchall(self):
            return [("DWH", "ODS_HR_EMPLOYEES", "FIRST_NAME", "VARCHAR2", 20, None, "MAP_HR_EMPLOYEES_TO_ODS")]

        def close(self):
            pass

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(detector, "ODI_REPO_SCHEMA", "ODI_REPO")
    monkeypatch.setattr(detector.oracledb, "connect", lambda **_kwargs: FakeConnection())

    rows = detector.get_odi_column_definitions("DWH")

    assert "c.SOURCE_DT" in captured["sql"]
    assert "c.LONGC" in captured["sql"]
    assert "c.SCALEC" in captured["sql"]
    assert captured["params"] == {"owner": "DWH"}
    assert rows == [
        {
            "owner": "DWH",
            "table_name": "ODS_HR_EMPLOYEES",
            "column_name": "FIRST_NAME",
            "data_type": "VARCHAR2",
            "data_length": 20,
            "data_precision": None,
            "data_scale": None,
            "full_data_type": "VARCHAR2(20)",
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        }
    ]


def test_get_odi_column_definitions_treats_longc_as_number_precision(monkeypatch):
    class FakeCursor:
        def execute(self, _sql, _params):
            pass

        def fetchall(self):
            return [("DWH", "ODS_HR_EMPLOYEES", "SALARY", "NUMBER", 8, 2, None)]

        def close(self):
            pass

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(detector, "ODI_REPO_SCHEMA", "ODI_REPO")
    monkeypatch.setattr(detector.oracledb, "connect", lambda **_kwargs: FakeConnection())

    [row] = detector.get_odi_column_definitions("DWH")

    assert row["data_length"] is None
    assert row["data_precision"] == 8
    assert row["data_scale"] == 2
    assert row["full_data_type"] == "NUMBER(8,2)"


def test_detect_db_odi_type_drift_reports_length_mismatch(monkeypatch):
    monkeypatch.setattr(
        detector,
        "get_db_column_definitions",
        lambda owner: [
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "FIRST_NAME",
                "data_type": "VARCHAR2",
                "data_length": 30,
                "data_precision": None,
                "data_scale": None,
                "full_data_type": "VARCHAR2(30)",
            }
        ],
    )
    monkeypatch.setattr(
        detector,
        "get_odi_column_definitions",
        lambda owner: [
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "FIRST_NAME",
                "data_type": "VARCHAR2",
                "data_length": 20,
                "data_precision": None,
                "data_scale": None,
                "full_data_type": "VARCHAR2(20)",
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            }
        ],
    )

    [drift] = detector.detect_db_odi_type_drift("DWH")

    assert drift == {
        "owner": "DWH",
        "table": "ODS_HR_EMPLOYEES",
        "column": "FIRST_NAME",
        "db_type": "VARCHAR2(30)",
        "odi_type": "VARCHAR2(20)",
        "db_length": 30,
        "odi_length": 20,
        "db_precision": None,
        "odi_precision": None,
        "db_scale": None,
        "odi_scale": None,
        "in_sync": False,
        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
    }


def test_detect_db_odi_type_drift_ignores_matching_columns(monkeypatch):
    same = {
        "owner": "DWH",
        "table_name": "ODS_HR_EMPLOYEES",
        "column_name": "FIRST_NAME",
        "data_type": "VARCHAR2",
        "data_length": 30,
        "data_precision": None,
        "data_scale": None,
        "full_data_type": "VARCHAR2(30)",
    }
    monkeypatch.setattr(detector, "get_db_column_definitions", lambda owner: [same])
    monkeypatch.setattr(detector, "get_odi_column_definitions", lambda owner: [same])

    assert detector.detect_db_odi_type_drift("DWH") == []


def test_build_schema_sync_findings_reports_type_and_name_mismatches_without_raw_values(monkeypatch):
    monkeypatch.setattr(
        detector,
        "get_db_column_definitions",
        lambda owner: [
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "FIRST_NAME",
                "data_type": "VARCHAR2",
                "data_length": 30,
                "data_precision": None,
                "data_scale": None,
                "full_data_type": "VARCHAR2(30)",
            },
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "EMAIL",
                "data_type": "VARCHAR2",
                "data_length": 25,
                "data_precision": None,
                "data_scale": None,
                "full_data_type": "VARCHAR2(25)",
            },
        ],
    )
    monkeypatch.setattr(
        detector,
        "get_odi_column_definitions",
        lambda owner: [
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "FIRST_NAME",
                "data_type": "VARCHAR2",
                "data_length": 20,
                "data_precision": None,
                "data_scale": None,
                "full_data_type": "VARCHAR2(20)",
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            },
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "LEGACY_EMAIL",
                "data_type": "VARCHAR2",
                "data_length": 25,
                "data_precision": None,
                "data_scale": None,
                "full_data_type": "VARCHAR2(25)",
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            },
        ],
    )

    report = detector.build_schema_sync_findings("DWH")

    assert report["read_only"] is True
    assert report["owner"] == "DWH"
    assert report["finding_count"] == 3
    assert [item["kind"] for item in report["findings"]] == [
        "column_type_size_mismatch",
        "target_column_missing_in_odi",
        "odi_column_missing_in_db",
    ]
    assert report["findings"][0]["summary"] == "DWH.ODS_HR_EMPLOYEES.FIRST_NAME DB VARCHAR2(30) != ODI VARCHAR2(20)"
    assert report["findings"][1]["column"] == "EMAIL"
    assert report["findings"][2]["column"] == "LEGACY_EMAIL"
    assert "sample" not in str(report).lower()
    assert "value" not in str(report).lower()


def test_build_source_target_metadata_findings_reports_direct_metadata_gaps_without_raw_values(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(detector, "get_source_tables", lambda: ["HR.EMPLOYEES"])
    monkeypatch.setattr(
        detector,
        "get_source_table_schema",
        lambda table_name: pd.DataFrame(
            [
                {
                    "COLUMN_NAME": "EMPLOYEE_ID",
                    "DATA_TYPE": "NUMBER",
                    "DATA_LENGTH": None,
                    "DATA_PRECISION": 6,
                    "DATA_SCALE": 0,
                    "NULLABLE": "N",
                    "FULL_DATA_TYPE": "NUMBER(6,0)",
                },
                {
                    "COLUMN_NAME": "FIRST_NAME",
                    "DATA_TYPE": "VARCHAR2",
                    "DATA_LENGTH": 20,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "Y",
                    "FULL_DATA_TYPE": "VARCHAR2(20)",
                },
                {
                    "COLUMN_NAME": "EMAIL",
                    "DATA_TYPE": "VARCHAR2",
                    "DATA_LENGTH": 25,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "N",
                    "FULL_DATA_TYPE": "VARCHAR2(25)",
                },
                {
                    "COLUMN_NAME": "HIRE_DATE",
                    "DATA_TYPE": "DATE",
                    "DATA_LENGTH": None,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "N",
                    "FULL_DATA_TYPE": "DATE",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        detector,
        "get_db_column_definitions",
        lambda owner: [
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "EMPLOYEE_ID",
                "data_type": "NUMBER",
                "data_length": None,
                "data_precision": 6,
                "data_scale": 0,
                "nullable": "N",
                "full_data_type": "NUMBER(6,0)",
            },
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "FIRST_NAME",
                "data_type": "VARCHAR2",
                "data_length": 30,
                "data_precision": None,
                "data_scale": None,
                "nullable": "Y",
                "full_data_type": "VARCHAR2(30)",
            },
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "EMAIL",
                "data_type": "VARCHAR2",
                "data_length": 25,
                "data_precision": None,
                "data_scale": None,
                "nullable": "Y",
                "full_data_type": "VARCHAR2(25)",
            },
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "LEGACY_COL",
                "data_type": "VARCHAR2",
                "data_length": 10,
                "data_precision": None,
                "data_scale": None,
                "nullable": "Y",
                "full_data_type": "VARCHAR2(10)",
            },
        ],
    )
    monkeypatch.setattr(
        detector,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [],
    )

    report = detector.build_source_target_metadata_findings(source_schema="HR", target_owner="DWH")

    assert report["read_only"] is True
    assert report["source_schema"] == "HR"
    assert report["target_owner"] == "DWH"
    assert report["mapping_link_aware"] is False
    assert report["finding_count"] == 4
    assert [item["kind"] for item in report["findings"]] == [
        "source_target_type_size_mismatch",
        "source_target_nullability_mismatch",
        "source_column_missing_in_target",
        "target_column_missing_in_source",
    ]
    assert report["findings"][0]["source_table"] == "HR.EMPLOYEES"
    assert report["findings"][0]["target_table"] == "DWH.ODS_HR_EMPLOYEES"
    assert report["findings"][0]["column"] == "FIRST_NAME"
    assert report["findings"][1]["column"] == "EMAIL"
    assert report["findings"][2]["column"] == "HIRE_DATE"
    assert report["findings"][3]["column"] == "LEGACY_COL"
    assert "sample" not in str(report).lower()
    assert "password" not in str(report).lower()
    assert "jdbc:" not in str(report).lower()


def test_build_source_target_metadata_findings_uses_mapping_links_for_renamed_columns(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(detector, "get_source_tables", lambda: ["HR.EMPLOYEES"])
    monkeypatch.setattr(
        detector,
        "get_source_table_schema",
        lambda table_name: pd.DataFrame(
            [
                {
                    "COLUMN_NAME": "EMPLOYEE_ID",
                    "DATA_TYPE": "NUMBER",
                    "DATA_LENGTH": None,
                    "DATA_PRECISION": 6,
                    "DATA_SCALE": 0,
                    "NULLABLE": "N",
                    "FULL_DATA_TYPE": "NUMBER(6,0)",
                },
                {
                    "COLUMN_NAME": "HIRE_DATE",
                    "DATA_TYPE": "DATE",
                    "DATA_LENGTH": None,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "N",
                    "FULL_DATA_TYPE": "DATE",
                },
                {
                    "COLUMN_NAME": "LEGACY_CODE",
                    "DATA_TYPE": "VARCHAR2",
                    "DATA_LENGTH": 12,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "Y",
                    "FULL_DATA_TYPE": "VARCHAR2(12)",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        detector,
        "get_db_column_definitions",
        lambda owner: [
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "EMPLOYEE_ID",
                "data_type": "NUMBER",
                "data_length": None,
                "data_precision": 6,
                "data_scale": 0,
                "nullable": "N",
                "full_data_type": "NUMBER(6,0)",
            },
            {
                "owner": "DWH",
                "table_name": "ODS_HR_EMPLOYEES",
                "column_name": "HIRE_DT",
                "data_type": "DATE",
                "data_length": None,
                "data_precision": None,
                "data_scale": None,
                "nullable": "N",
                "full_data_type": "DATE",
            },
        ],
    )
    monkeypatch.setattr(
        detector,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.EMPLOYEES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_EMPLOYEES",
                "ODI_MAPPING_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
            }
        ],
        raising=False,
    )
    monkeypatch.setattr(
        detector,
        "get_odi_mapping_column_links",
        lambda mapping_names, source_schema="HR", target_owner="DWH": [
            {
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "source_table": "HR.EMPLOYEES",
                "source_column": "HIRE_DATE",
                "target_table": "DWH.ODS_HR_EMPLOYEES",
                "target_column": "HIRE_DT",
                "link_type": "direct",
            }
        ],
        raising=False,
    )

    report = detector.build_source_target_metadata_findings(source_schema="HR", target_owner="DWH")

    assert report["read_only"] is True
    assert report["mapping_link_aware"] is True
    assert report["mapping_link_count"] == 1
    assert report["finding_count"] == 1
    [finding] = report["findings"]
    assert finding["kind"] == "source_column_missing_in_target"
    assert finding["column"] == "LEGACY_CODE"
    assert finding["mapping_name"] == "MAP_HR_EMPLOYEES_TO_ODS"
    assert finding["mapping_link_aware"] is True
    assert finding["mapping_status"] == "unmapped_source_column"
    assert "HIRE_DATE" not in str(report)
    assert "HIRE_DT" not in str(report)
    assert "EMPLOYEES.HIRE_DATE" not in str(report)
    assert "sample" not in str(report).lower()
    assert "password" not in str(report).lower()


def test_build_odi_mapping_column_links_extracts_links_without_leaking_expressions():
    rows = [
        {
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "comp_name": "EMPLOYEES",
            "comp_type": "DATASTORE",
            "table_qualified_name": "HR.EMPLOYEES",
            "attr_name": "HIRE_DATE",
            "expression": None,
        },
        {
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "comp_name": "ODS_HR_EMPLOYEES",
            "comp_type": "DATASTORE",
            "table_qualified_name": "DWH.ODS_HR_EMPLOYEES",
            "attr_name": "HIRE_DT",
            "expression": "EMPLOYEES.HIRE_DATE",
        },
        {
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "comp_name": "ODS_HR_EMPLOYEES",
            "comp_type": "DATASTORE",
            "table_qualified_name": "DWH.ODS_HR_EMPLOYEES",
            "attr_name": "ODS_CREATED_AT",
            "expression": "SYSDATE",
        },
    ]

    links = detector.build_odi_mapping_column_links(rows, source_schema="HR", target_owner="DWH")

    assert links == [
        {
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "source_table": "HR.EMPLOYEES",
            "source_column": "HIRE_DATE",
            "target_table": "DWH.ODS_HR_EMPLOYEES",
            "target_column": "HIRE_DT",
            "link_type": "direct",
        },
        {
            "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
            "source_table": None,
            "source_column": None,
            "target_table": "DWH.ODS_HR_EMPLOYEES",
            "target_column": "ODS_CREATED_AT",
            "link_type": "derived_expression",
        },
    ]
    assert "EMPLOYEES.HIRE_DATE" not in str(links)
    assert "SYSDATE" not in str(links)
