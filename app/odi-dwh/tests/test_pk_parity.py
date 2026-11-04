import pandas as pd

import pk_parity


def test_pk_parity_reports_missing_and_extra_counts_without_raw_pk_values(monkeypatch):
    monkeypatch.setattr(
        pk_parity,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.EMPLOYEES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_EMPLOYEES",
                "ODI_MAPPING_EXISTS": True,
                "TARGET_TABLE_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_EMPLOYEES_TO_ODS",
            }
        ],
    )
    monkeypatch.setattr(pk_parity, "get_source_pk_columns", lambda table_ref: ["EMPLOYEE_ID"])
    monkeypatch.setattr(
        pk_parity,
        "get_table_constraints",
        lambda table_ref: pd.DataFrame(
            [{"CONSTRAINT_TYPE": "P", "COLUMN_NAME": "EMPLOYEE_ID", "STATUS": "ENABLED"}]
        ),
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": "EMPLOYEE_ID"}]),
    )

    source_rows = [{"EMPLOYEE_ID": "SRC_PK_ALPHA"}, {"EMPLOYEE_ID": "SRC_PK_BETA"}]
    target_rows = [{"EMPLOYEE_ID": "SRC_PK_ALPHA"}, {"EMPLOYEE_ID": "TGT_PK_EXTRA"}]

    def source_select(sql, params=None):
        if "COUNT(*)" in sql.upper():
            return pd.DataFrame([{"COUNT": len(source_rows)}])
        return pd.DataFrame(source_rows)

    def target_select(sql, params=None):
        if "COUNT(*)" in sql.upper():
            return pd.DataFrame([{"COUNT": len(target_rows)}])
        return pd.DataFrame(target_rows)

    monkeypatch.setattr(pk_parity, "execute_source_select", source_select)
    monkeypatch.setattr(pk_parity, "execute_select", target_select)

    report = pk_parity.build_pk_parity_report(source_schema="HR", target_owner="DWH")

    assert report["read_only"] is True
    assert report["raw_rows_included"] is False
    assert report["status"] == "attention_required"
    assert report["result_count"] == 1
    result = report["results"][0]
    assert result["status"] == "failed"
    assert result["source_row_count"] == 2
    assert result["target_row_count"] == 2
    assert result["missing_key_count"] == 1
    assert result["extra_key_count"] == 1
    assert result["pk_columns"] == ["EMPLOYEE_ID"]
    assert result["mapping_name"] == "MAP_HR_EMPLOYEES_TO_ODS"
    assert "SRC_PK_ALPHA" not in str(report)
    assert "SRC_PK_BETA" not in str(report)
    assert "TGT_PK_EXTRA" not in str(report)


def test_pk_parity_blocks_when_pk_columns_are_unavailable(monkeypatch):
    monkeypatch.setattr(
        pk_parity,
        "get_ods_flow_inventory",
        lambda source_schema="HR", target_owner="DWH": [
            {
                "SOURCE_TABLE": "HR.JOBS",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_HR_JOBS",
                "ODI_MAPPING_EXISTS": True,
                "TARGET_TABLE_EXISTS": True,
                "MAPPING_NAME": "MAP_HR_JOBS_TO_ODS",
            }
        ],
    )
    monkeypatch.setattr(pk_parity, "get_source_pk_columns", lambda table_ref: [])
    monkeypatch.setattr(
        pk_parity,
        "get_table_constraints",
        lambda table_ref: pd.DataFrame(columns=["CONSTRAINT_TYPE", "COLUMN_NAME", "STATUS"]),
    )

    report = pk_parity.build_pk_parity_report(source_schema="HR", target_owner="DWH")

    assert report["read_only"] is True
    assert report["status"] == "attention_required"
    assert report["results"][0]["status"] == "blocked"
    assert report["results"][0]["blocker"] == "source_pk_missing"
    assert report["raw_rows_included"] is False


def test_pk_parity_uses_in_db_anti_join_for_large_tables_without_fetching_pk_values(monkeypatch):
    pk_columns = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"]
    anti_join_sqls = []

    monkeypatch.setattr(
        pk_parity,
        "get_ods_flow_inventory",
        lambda source_schema="SH", target_owner="DWH": [
            {
                "SOURCE_TABLE": "SH.SALES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_SH_SALES",
                "ODI_MAPPING_EXISTS": True,
                "TARGET_TABLE_EXISTS": True,
                "MAPPING_NAME": "MAP_SH_SALES_TO_ODS",
            }
        ],
    )
    monkeypatch.setattr(pk_parity, "get_source_pk_columns", lambda table_ref: pk_columns)
    monkeypatch.setattr(
        pk_parity,
        "get_table_constraints",
        lambda table_ref: pd.DataFrame(
            [
                {"CONSTRAINT_TYPE": "P", "COLUMN_NAME": column, "STATUS": "ENABLED"}
                for column in pk_columns
            ]
        ),
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": column} for column in pk_columns]),
    )

    def source_select(sql, params=None):
        upper_sql = sql.upper()
        assert "FETCH FIRST" not in upper_sql
        if "COUNT(*)" in upper_sql:
            return pd.DataFrame([{"ROW_COUNT": 918843}])
        raise AssertionError("large-table parity must not fetch source PK values into app memory")

    def target_select(sql, params=None):
        upper_sql = sql.upper()
        if "COUNT(*) AS ROW_COUNT" in upper_sql:
            return pd.DataFrame([{"ROW_COUNT": 918843}])
        if "MINUS" in upper_sql:
            anti_join_sqls.append(upper_sql)
            assert "FETCH FIRST" not in upper_sql
            assert '"SH"."SALES"' in upper_sql
            assert '"DWH"."ODS_SH_SALES"' in upper_sql
            first_source_pos = upper_sql.find('"SH"."SALES"')
            first_target_pos = upper_sql.find('"DWH"."ODS_SH_SALES"')
            if first_source_pos < first_target_pos:
                return pd.DataFrame([{"KEY_COUNT": 2}])
            return pd.DataFrame([{"KEY_COUNT": 0}])
        raise AssertionError(f"unexpected target SQL: {sql}")

    monkeypatch.setattr(pk_parity, "execute_source_select", source_select)
    monkeypatch.setattr(pk_parity, "execute_select", target_select)

    report = pk_parity.build_pk_parity_report(source_schema="SH", target_owner="DWH", max_keys=100000)

    assert len(anti_join_sqls) == 2
    assert report["read_only"] is True
    assert report["raw_rows_included"] is False
    assert report["pk_values_included"] is False
    result = report["results"][0]
    assert result["status"] == "failed"
    assert result["count_strategy"] == "in_db_anti_join"
    assert result["source_row_count"] == 918843
    assert result["target_row_count"] == 918843
    assert result["missing_key_count"] == 2
    assert result["extra_key_count"] == 0
    assert "SRC_PK_ALPHA" not in str(report)


def test_pk_parity_blocks_large_table_when_shared_read_connection_is_unavailable(monkeypatch):
    pk_columns = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"]

    monkeypatch.setattr(
        pk_parity,
        "get_ods_flow_inventory",
        lambda source_schema="SH", target_owner="DWH": [
            {
                "SOURCE_TABLE": "SH.SALES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_SH_SALES",
                "ODI_MAPPING_EXISTS": True,
                "TARGET_TABLE_EXISTS": True,
                "MAPPING_NAME": "MAP_SH_SALES_TO_ODS",
            }
        ],
    )
    monkeypatch.setattr(pk_parity, "get_source_pk_columns", lambda table_ref: pk_columns)
    monkeypatch.setattr(
        pk_parity,
        "get_table_constraints",
        lambda table_ref: pd.DataFrame(
            [
                {"CONSTRAINT_TYPE": "P", "COLUMN_NAME": column, "STATUS": "ENABLED"}
                for column in pk_columns
            ]
        ),
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": column} for column in pk_columns]),
    )

    def source_select(sql, params=None):
        upper_sql = sql.upper()
        assert "FETCH FIRST" not in upper_sql
        if "COUNT(*)" in upper_sql:
            return pd.DataFrame([{"ROW_COUNT": 918843}])
        raise AssertionError("large-table fallback must not fetch source PK values")

    def target_select(sql, params=None):
        upper_sql = sql.upper()
        if "COUNT(*) AS ROW_COUNT" in upper_sql:
            return pd.DataFrame([{"ROW_COUNT": 918843}])
        if "MINUS" in upper_sql:
            raise RuntimeError("ORA-00942: table or view does not exist")
        raise AssertionError(f"unexpected target SQL: {sql}")

    monkeypatch.setattr(pk_parity, "execute_source_select", source_select)
    monkeypatch.setattr(pk_parity, "execute_select", target_select)

    report = pk_parity.build_pk_parity_report(source_schema="SH", target_owner="DWH", max_keys=100000)

    result = report["results"][0]
    assert result["status"] == "blocked"
    assert result["blocker"] == "shared_read_connection_unavailable"
    assert result["count_strategy"] == "in_db_anti_join"
    assert result["missing_key_count"] is None
    assert result["extra_key_count"] is None
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False


def test_pk_parity_uses_configured_fact_grain_when_declared_pk_is_missing(monkeypatch):
    grain_columns = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"]

    monkeypatch.setattr(
        pk_parity,
        "get_ods_flow_inventory",
        lambda source_schema="SH", target_owner="DWH": [
            {
                "SOURCE_TABLE": "SH.SALES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_SH_SALES",
                "ODI_MAPPING_EXISTS": True,
                "TARGET_TABLE_EXISTS": True,
                "MAPPING_NAME": "MAP_SH_SALES_TO_ODS",
            }
        ],
    )
    monkeypatch.setattr(pk_parity, "get_source_pk_columns", lambda table_ref: [])
    monkeypatch.setattr(
        pk_parity,
        "get_source_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": column} for column in grain_columns]),
        raising=False,
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_constraints",
        lambda table_ref: pd.DataFrame(columns=["CONSTRAINT_TYPE", "COLUMN_NAME", "STATUS"]),
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": column} for column in grain_columns]),
    )

    def source_select(sql, params=None):
        if "COUNT(*)" in sql.upper():
            return pd.DataFrame([{"ROW_COUNT": 918843}])
        raise AssertionError("configured-grain large parity must not fetch source PK values")

    def target_select(sql, params=None):
        upper_sql = sql.upper()
        if "COUNT(*) AS ROW_COUNT" in upper_sql:
            return pd.DataFrame([{"ROW_COUNT": 918843}])
        if "MINUS" in upper_sql:
            return pd.DataFrame([{"KEY_COUNT": 0}])
        raise AssertionError(f"unexpected target SQL: {sql}")

    monkeypatch.setattr(pk_parity, "execute_source_select", source_select)
    monkeypatch.setattr(pk_parity, "execute_select", target_select)

    report = pk_parity.build_pk_parity_report(source_schema="SH", target_owner="DWH", max_keys=100000)

    result = report["results"][0]
    assert result["status"] == "passed"
    assert result["pk_columns"] == grain_columns
    assert result["count_strategy"] == "in_db_anti_join"
    assert result["missing_key_count"] == 0
    assert result["extra_key_count"] == 0
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False


def test_pk_parity_blocks_configured_grain_when_source_columns_are_missing(monkeypatch):
    grain_columns = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"]

    monkeypatch.setattr(
        pk_parity,
        "get_ods_flow_inventory",
        lambda source_schema="SH", target_owner="DWH": [
            {
                "SOURCE_TABLE": "SH.SALES",
                "EXPECTED_TARGET_TABLE": "DWH.ODS_SH_SALES",
                "ODI_MAPPING_EXISTS": True,
                "TARGET_TABLE_EXISTS": True,
                "MAPPING_NAME": "MAP_SH_SALES_TO_ODS",
            }
        ],
    )
    monkeypatch.setattr(pk_parity, "get_source_pk_columns", lambda table_ref: [])
    monkeypatch.setattr(
        pk_parity,
        "get_source_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": "PROD_ID"}]),
        raising=False,
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_constraints",
        lambda table_ref: pd.DataFrame(columns=["CONSTRAINT_TYPE", "COLUMN_NAME", "STATUS"]),
    )
    monkeypatch.setattr(
        pk_parity,
        "get_table_schema",
        lambda table_ref: pd.DataFrame([{"COLUMN_NAME": column} for column in grain_columns]),
    )

    report = pk_parity.build_pk_parity_report(source_schema="SH", target_owner="DWH", max_keys=100000)

    result = report["results"][0]
    assert result["status"] == "blocked"
    assert result["blocker"] == "configured_key_columns_missing_in_source"
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
