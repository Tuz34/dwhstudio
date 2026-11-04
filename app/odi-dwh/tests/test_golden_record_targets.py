from golden_record_targets import (
    HR_EMPLOYEES_TARGET,
    SH_SALES_TARGET,
    hr_employees_target_ddl,
    sh_sales_target_ddl,
)


def test_hr_employees_target_ddl_is_scoped_and_has_audit_columns():
    ddl = hr_employees_target_ddl()

    assert HR_EMPLOYEES_TARGET == "DWH.ODS_HR_EMPLOYEES"
    assert "CREATE TABLE DWH.ODS_HR_EMPLOYEES AS" in ddl
    assert "FROM HR.EMPLOYEES" in ddl
    assert "ODS_CREATED_AT" in ddl
    assert "ODS_UPDATED_AT" in ddl
    assert "SOURCE_SYSTEM" in ddl
    assert "*" not in ddl


def test_sh_sales_target_ddl_is_scoped_and_has_golden_columns():
    ddl = sh_sales_target_ddl()

    assert SH_SALES_TARGET == "DWH.ODS_SH_SALES"
    assert "CREATE TABLE DWH.ODS_SH_SALES AS" in ddl
    assert "FROM SH.SALES" in ddl
    for column in ("PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "QUANTITY_SOLD", "AMOUNT_SOLD"):
        assert column in ddl
    assert "ODS_CREATED_AT" in ddl
    assert "SOURCE_SYSTEM" in ddl
    assert "*" not in ddl
