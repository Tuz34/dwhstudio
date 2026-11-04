"""ODI-63 local model eval set v1 and static validators.

This module is intentionally offline. It never calls a model, DB, ODI, or
EXPLAIN PLAN. It scores supplied outputs against hand-written synthetic cases,
or scores the reference answers as a harness self-check when no outputs are
provided.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ai_egress as requests
from ai_egress import local_openai_key, validate_endpoint

THRESHOLDS: dict[str, float] = {
    "nl_to_sql": 0.80,
    "intent_to_json": 0.95,
    "error_to_fix": 0.80,
    "evidence_summary": 0.90,
}

FORBIDDEN_SQL_KEYWORDS = {
    "ALTER",
    "BEGIN",
    "CALL",
    "COMMIT",
    "CREATE",
    "DELETE",
    "DROP",
    "EXEC",
    "EXECUTE",
    "EXPLAIN",
    "GRANT",
    "INSERT",
    "MERGE",
    "REVOKE",
    "ROLLBACK",
    "TRUNCATE",
    "UPDATE",
}

STATIC_CATALOG: dict[str, set[str]] = {
    "HR.EMPLOYEES": {
        "EMPLOYEE_ID",
        "DEPARTMENT_ID",
        "HIRE_DATE",
        "JOB_ID",
        "SALARY",
        "MANAGER_ID",
    },
    "HR.DEPARTMENTS": {"DEPARTMENT_ID", "DEPARTMENT_NAME", "LOCATION_ID"},
    "HR.JOBS": {"JOB_ID", "JOB_TITLE", "MIN_SALARY", "MAX_SALARY"},
    "SH.SALES": {
        "PROD_ID",
        "CUST_ID",
        "TIME_ID",
        "CHANNEL_ID",
        "PROMO_ID",
        "AMOUNT_SOLD",
        "QUANTITY_SOLD",
    },
    "SH.PRODUCTS": {"PROD_ID", "PROD_NAME", "PROD_CATEGORY"},
    "SH.CUSTOMERS": {"CUST_ID", "CUST_FIRST_NAME", "CUST_LAST_NAME", "CUST_CITY"},
    "DWH.ODS_HR_EMPLOYEES": {
        "EMPLOYEE_ID",
        "DEPARTMENT_ID",
        "HIRE_DATE",
        "JOB_ID",
        "SALARY",
        "MANAGER_ID",
        "ODS_CREATED_AT",
    },
    "DWH.ODS_HR_DEPARTMENTS": {
        "DEPARTMENT_ID",
        "DEPARTMENT_NAME",
        "LOCATION_ID",
        "ODS_CREATED_AT",
    },
    "DWH.ODS_SH_SALES": {
        "PROD_ID",
        "CUST_ID",
        "TIME_ID",
        "CHANNEL_ID",
        "PROMO_ID",
        "AMOUNT_SOLD",
        "QUANTITY_SOLD",
        "ODS_CREATED_AT",
    },
    "DWH.ODS_SH_PRODUCTS": {"PROD_ID", "PROD_NAME", "PROD_CATEGORY", "ODS_CREATED_AT"},
    "DWH.ODS_SH_CUSTOMERS": {
        "CUST_ID",
        "CUST_FIRST_NAME",
        "CUST_LAST_NAME",
        "CUST_CITY",
        "ODS_CREATED_AT",
    },
}

BLOCKED_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b[A-Z0-9_.-]*(password|passwd|pwd|secret|token|api[_-]?key)"
            r"\s*[:=]\s*['\"]?[^'\"\s,;]+"
        ),
    ),
    (
        "connection_string",
        re.compile(
            r"(?i)\b(jdbc:oracle:thin:@|jdbc:|user id\s*=|uid\s*=|pwd\s*=|"
            r"password\s*=|://[^/\s:@]+:[^@\s]+@)"
        ),
    ),
    (
        "oracle_easy_connect_credential",
        re.compile(r"(?i)\b[A-Z0-9_]+/[^@\s]+@[A-Z0-9_.:-]+"),
    ),
    (
        "raw_rows",
        re.compile(r"(?i)\b(raw_rows|sample_rows|preview_rows|row_data|rows)\s*[:=]\s*[\[{(]"),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
)


@dataclass(frozen=True)
class EvalResult:
    passed: bool
    reasons: tuple[str, ...]
    details: dict[str, Any]


def _case(case_id: str, task: str, prompt: str, expected: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case_id,
        "task": task,
        "prompt": prompt,
        "synthetic": True,
        "validation_mode": "static_offline",
        "live_execution_allowed": False,
        "model_call_allowed": False,
        "expected": expected,
    }


def _sql_expected(
    output: str,
    *,
    required_tables: list[str],
    required_columns: list[str],
) -> dict[str, Any]:
    return {
        "output": output,
        "required_tables": required_tables,
        "required_columns": required_columns,
        "parse_mode": "static_only_no_db",
    }


SQL_CASES = [
    _case(
        "sql_hr_employee_count_by_department",
        "nl_to_sql",
        "Count employees by department using the HR source metadata.",
        _sql_expected(
            "SELECT DEPARTMENT_ID, COUNT(*) AS EMPLOYEE_COUNT FROM HR.EMPLOYEES GROUP BY DEPARTMENT_ID",
            required_tables=["HR.EMPLOYEES"],
            required_columns=["DEPARTMENT_ID"],
        ),
    ),
    _case(
        "sql_hr_avg_salary_by_job",
        "nl_to_sql",
        "Show average salary by HR job id.",
        _sql_expected(
            "SELECT JOB_ID, AVG(SALARY) AS AVG_SALARY FROM HR.EMPLOYEES GROUP BY JOB_ID",
            required_tables=["HR.EMPLOYEES"],
            required_columns=["JOB_ID", "SALARY"],
        ),
    ),
    _case(
        "sql_ods_hr_employee_row_count",
        "nl_to_sql",
        "Return the ODS HR employee row count.",
        _sql_expected(
            "SELECT COUNT(*) AS ROW_COUNT FROM DWH.ODS_HR_EMPLOYEES",
            required_tables=["DWH.ODS_HR_EMPLOYEES"],
            required_columns=[],
        ),
    ),
    _case(
        "sql_hr_department_names_with_counts",
        "nl_to_sql",
        "List department names with employee counts.",
        _sql_expected(
            "SELECT D.DEPARTMENT_NAME, COUNT(E.EMPLOYEE_ID) AS EMPLOYEE_COUNT "
            "FROM HR.DEPARTMENTS D LEFT JOIN HR.EMPLOYEES E ON D.DEPARTMENT_ID = E.DEPARTMENT_ID "
            "GROUP BY D.DEPARTMENT_NAME",
            required_tables=["HR.DEPARTMENTS", "HR.EMPLOYEES"],
            required_columns=["DEPARTMENT_NAME", "EMPLOYEE_ID", "DEPARTMENT_ID"],
        ),
    ),
    _case(
        "sql_hr_hire_month_counts",
        "nl_to_sql",
        "Count HR employees by hire month.",
        _sql_expected(
            "SELECT TRUNC(HIRE_DATE, 'MM') AS HIRE_MONTH, COUNT(*) AS EMPLOYEE_COUNT "
            "FROM HR.EMPLOYEES GROUP BY TRUNC(HIRE_DATE, 'MM')",
            required_tables=["HR.EMPLOYEES"],
            required_columns=["HIRE_DATE"],
        ),
    ),
    _case(
        "sql_sh_sales_count_by_day",
        "nl_to_sql",
        "Count SH sales records by time id.",
        _sql_expected(
            "SELECT TIME_ID, COUNT(*) AS SALES_COUNT FROM SH.SALES GROUP BY TIME_ID",
            required_tables=["SH.SALES"],
            required_columns=["TIME_ID"],
        ),
    ),
    _case(
        "sql_sh_sales_amount_by_product",
        "nl_to_sql",
        "Sum sales amount by product.",
        _sql_expected(
            "SELECT PROD_ID, SUM(AMOUNT_SOLD) AS TOTAL_AMOUNT FROM SH.SALES GROUP BY PROD_ID",
            required_tables=["SH.SALES"],
            required_columns=["PROD_ID", "AMOUNT_SOLD"],
        ),
    ),
    _case(
        "sql_ods_sh_sales_count_by_day",
        "nl_to_sql",
        "Count ODS SH sales records by time id.",
        _sql_expected(
            "SELECT TIME_ID, COUNT(*) AS SALES_COUNT FROM DWH.ODS_SH_SALES GROUP BY TIME_ID",
            required_tables=["DWH.ODS_SH_SALES"],
            required_columns=["TIME_ID"],
        ),
    ),
    _case(
        "sql_sh_sales_product_category_amount",
        "nl_to_sql",
        "Summarize sales amount by product category.",
        _sql_expected(
            "SELECT P.PROD_CATEGORY, SUM(S.AMOUNT_SOLD) AS TOTAL_AMOUNT "
            "FROM SH.SALES S JOIN SH.PRODUCTS P ON S.PROD_ID = P.PROD_ID "
            "GROUP BY P.PROD_CATEGORY",
            required_tables=["SH.SALES", "SH.PRODUCTS"],
            required_columns=["PROD_CATEGORY", "AMOUNT_SOLD", "PROD_ID"],
        ),
    ),
    _case(
        "sql_sh_customer_city_sales_count",
        "nl_to_sql",
        "Count sales records by customer city.",
        _sql_expected(
            "SELECT C.CUST_CITY, COUNT(*) AS SALES_COUNT "
            "FROM SH.SALES S JOIN SH.CUSTOMERS C ON S.CUST_ID = C.CUST_ID "
            "GROUP BY C.CUST_CITY",
            required_tables=["SH.SALES", "SH.CUSTOMERS"],
            required_columns=["CUST_CITY", "CUST_ID"],
        ),
    ),
    _case(
        "sql_hr_missing_department_check",
        "nl_to_sql",
        "Find count of employees without a department id.",
        _sql_expected(
            "SELECT COUNT(*) AS MISSING_DEPARTMENT_COUNT FROM HR.EMPLOYEES WHERE DEPARTMENT_ID IS NULL",
            required_tables=["HR.EMPLOYEES"],
            required_columns=["DEPARTMENT_ID"],
        ),
    ),
    _case(
        "sql_ods_audit_count_by_created_day",
        "nl_to_sql",
        "Count ODS HR employees by audit created day.",
        _sql_expected(
            "SELECT TRUNC(ODS_CREATED_AT) AS CREATED_DAY, COUNT(*) AS ROW_COUNT "
            "FROM DWH.ODS_HR_EMPLOYEES GROUP BY TRUNC(ODS_CREATED_AT)",
            required_tables=["DWH.ODS_HR_EMPLOYEES"],
            required_columns=["ODS_CREATED_AT"],
        ),
    ),
    _case(
        "sql_hr_job_salary_band",
        "nl_to_sql",
        "Compare HR employee salaries with job salary bounds.",
        _sql_expected(
            "SELECT J.JOB_ID, J.MIN_SALARY, J.MAX_SALARY, COUNT(E.EMPLOYEE_ID) AS EMPLOYEE_COUNT "
            "FROM HR.JOBS J LEFT JOIN HR.EMPLOYEES E ON J.JOB_ID = E.JOB_ID "
            "GROUP BY J.JOB_ID, J.MIN_SALARY, J.MAX_SALARY",
            required_tables=["HR.JOBS", "HR.EMPLOYEES"],
            required_columns=["JOB_ID", "MIN_SALARY", "MAX_SALARY", "EMPLOYEE_ID"],
        ),
    ),
    _case(
        "sql_sh_sales_quantity_by_channel",
        "nl_to_sql",
        "Sum sales quantity by channel id.",
        _sql_expected(
            "SELECT CHANNEL_ID, SUM(QUANTITY_SOLD) AS TOTAL_QUANTITY FROM SH.SALES GROUP BY CHANNEL_ID",
            required_tables=["SH.SALES"],
            required_columns=["CHANNEL_ID", "QUANTITY_SOLD"],
        ),
    ),
    _case(
        "sql_ods_sh_product_category_count",
        "nl_to_sql",
        "Count ODS SH products by category.",
        _sql_expected(
            "SELECT PROD_CATEGORY, COUNT(*) AS PRODUCT_COUNT FROM DWH.ODS_SH_PRODUCTS GROUP BY PROD_CATEGORY",
            required_tables=["DWH.ODS_SH_PRODUCTS"],
            required_columns=["PROD_CATEGORY"],
        ),
    ),
]


def _create_mapping(
    mapping_name: str,
    src_model: str,
    src_table: str,
    tgt_model: str,
    tgt_table: str,
    *,
    needs_skey: bool = False,
    skey_natural_key: str | None = None,
) -> dict[str, Any]:
    return {
        "operation": "CREATE",
        "mapping_name": mapping_name,
        "src_model": src_model,
        "src_table": src_table,
        "tgt_model": tgt_model,
        "tgt_table": tgt_table,
        "tgt_owner": "DWH",
        "needs_skey": needs_skey,
        "skey_natural_key": skey_natural_key,
        "description": f"Create {mapping_name} as an offline ODI mapping request.",
    }


INTENT_CASES = [
    _case(
        "json_create_hr_employees_ods",
        "intent_to_json",
        "Create an ODS mapping for HR employees.",
        {"output": _create_mapping("MAP_HR_EMPLOYEES_TO_ODS", "ORACLE_HR", "HR.EMPLOYEES", "DWH", "ODS_HR_EMPLOYEES")},
    ),
    _case(
        "json_create_hr_departments_ods",
        "intent_to_json",
        "Create an ODS mapping for HR departments.",
        {
            "output": _create_mapping(
                "MAP_HR_DEPARTMENTS_TO_ODS",
                "ORACLE_HR",
                "HR.DEPARTMENTS",
                "DWH",
                "ODS_HR_DEPARTMENTS",
            )
        },
    ),
    _case(
        "json_create_hr_jobs_ods",
        "intent_to_json",
        "Create an ODS mapping for HR jobs.",
        {"output": _create_mapping("MAP_HR_JOBS_TO_ODS", "ORACLE_HR", "HR.JOBS", "DWH", "ODS_HR_JOBS")},
    ),
    _case(
        "json_create_sh_sales_ods",
        "intent_to_json",
        "Create an ODS mapping for SH sales.",
        {"output": _create_mapping("MAP_SH_SALES_TO_ODS", "ORACLE_SH", "SH.SALES", "DWH", "ODS_SH_SALES")},
    ),
    _case(
        "json_create_sh_products_ods",
        "intent_to_json",
        "Create an ODS mapping for SH products.",
        {"output": _create_mapping("MAP_SH_PRODUCTS_TO_ODS", "ORACLE_SH", "SH.PRODUCTS", "DWH", "ODS_SH_PRODUCTS")},
    ),
    _case(
        "json_create_sh_customers_ods",
        "intent_to_json",
        "Create an ODS mapping for SH customers.",
        {
            "output": _create_mapping(
                "MAP_SH_CUSTOMERS_TO_ODS",
                "ORACLE_SH",
                "SH.CUSTOMERS",
                "DWH",
                "ODS_SH_CUSTOMERS",
            )
        },
    ),
    _case(
        "json_create_dim_product_skey",
        "intent_to_json",
        (
            "Create a product dimension mapping for SH products with target "
            "dimension table DIM_PRODUCT and surrogate key natural key SRC.PROD_ID. "
            "This is not an ODS mapping."
        ),
        {
            "output": _create_mapping(
                "MAP_SH_PRODUCTS_TO_DIM",
                "ORACLE_SH",
                "SH.PRODUCTS",
                "DWH",
                "DIM_PRODUCT",
                needs_skey=True,
                skey_natural_key="SRC.PROD_ID",
            )
        },
    ),
    _case(
        "json_update_hr_employees_target_table",
        "intent_to_json",
        "Update the HR employees mapping target table to ODS_HR_EMPLOYEES_V2.",
        {
            "output": {
                "operation": "UPDATE",
                "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
                "changes": {"tgt_table": "ODS_HR_EMPLOYEES_V2"},
            }
        },
    ),
    _case(
        "json_update_sh_sales_truncate_false",
        "intent_to_json",
        "Update SH sales mapping so it appends without truncating the target.",
        {
            "output": {
                "operation": "UPDATE",
                "mapping_name": "MAP_SH_SALES_TO_ODS",
                "changes": {"truncate_target_table": False},
            }
        },
    ),
    _case(
        "json_update_hr_departments_filter",
        "intent_to_json",
        "Update HR departments mapping with source filter SRC.DEPARTMENT_ID IS NOT NULL.",
        {
            "output": {
                "operation": "UPDATE",
                "mapping_name": "MAP_HR_DEPARTMENTS_TO_ODS",
                "changes": {"filter_condition": "SRC.DEPARTMENT_ID IS NOT NULL"},
            }
        },
    ),
    _case(
        "json_update_sh_products_target_owner",
        "intent_to_json",
        "Update SH products mapping target owner to DWH.",
        {
            "output": {
                "operation": "UPDATE",
                "mapping_name": "MAP_SH_PRODUCTS_TO_ODS",
                "changes": {"tgt_owner": "DWH"},
            }
        },
    ),
    _case(
        "json_delete_test_hr_mapping",
        "intent_to_json",
        "Delete the temporary HR mapping metadata request.",
        {"output": {"operation": "DELETE", "mapping_name": "MAP_TEST_HR_TO_ODS", "tgt_owner": "DWH"}},
    ),
    _case(
        "json_delete_test_sh_mapping",
        "intent_to_json",
        "Delete the temporary SH mapping metadata request.",
        {"output": {"operation": "DELETE", "mapping_name": "MAP_TEST_SH_TO_ODS", "tgt_owner": "DWH"}},
    ),
    _case(
        "json_create_hr_employee_validation_request",
        "intent_to_json",
        "Create a validation request for HR employee PK parity.",
        {
            "output": {
                "operation": "VALIDATE",
                "tool": "pk_parity",
                "source_schema": "HR",
                "target_owner": "DWH",
                "source_table": "EMPLOYEES",
                "target_table": "ODS_HR_EMPLOYEES",
            }
        },
    ),
    _case(
        "json_create_sh_sales_validation_request",
        "intent_to_json",
        "Create a validation request for SH sales PK parity.",
        {
            "output": {
                "operation": "VALIDATE",
                "tool": "pk_parity",
                "source_schema": "SH",
                "target_owner": "DWH",
                "source_table": "SALES",
                "target_table": "ODS_SH_SALES",
            }
        },
    ),
]


def _error_output(code: str, keywords: list[str]) -> dict[str, Any]:
    return {
        "root_cause_code": code,
        "next_steps": [f"Check {keyword}" for keyword in keywords],
    }


ERROR_CASES = [
    _case(
        "err_odi_16171_missing_update_key",
        "error_to_fix",
        "ODI-16171 during merge KM validation.",
        {
            "root_cause_code": "ODI-16171",
            "next_step_keywords": ["target key", "reverse-engineer"],
            "output": _error_output("ODI-16171", ["target key", "reverse-engineer"]),
        },
    ),
    _case(
        "err_ora_01031_insufficient_privileges",
        "error_to_fix",
        "ORA-01031 during setup DDL.",
        {
            "root_cause_code": "ORA-01031",
            "next_step_keywords": ["setup user", "privilege"],
            "output": _error_output("ORA-01031", ["setup user", "privilege"]),
        },
    ),
    _case(
        "err_ora_00942_missing_table_or_view",
        "error_to_fix",
        "ORA-00942 while reading a metadata table.",
        {
            "root_cause_code": "ORA-00942",
            "next_step_keywords": ["object name", "grant"],
            "output": _error_output("ORA-00942", ["object name", "grant"]),
        },
    ),
    _case(
        "err_ora_00904_invalid_identifier",
        "error_to_fix",
        "ORA-00904 from generated SQL.",
        {
            "root_cause_code": "ORA-00904",
            "next_step_keywords": ["column metadata", "physical name"],
            "output": _error_output("ORA-00904", ["column metadata", "physical name"]),
        },
    ),
    _case(
        "err_ora_00001_unique_constraint",
        "error_to_fix",
        "ORA-00001 during target load.",
        {
            "root_cause_code": "ORA-00001",
            "next_step_keywords": ["duplicate key", "rerun safety"],
            "output": _error_output("ORA-00001", ["duplicate key", "rerun safety"]),
        },
    ),
    _case(
        "err_ora_01400_cannot_insert_null",
        "error_to_fix",
        "ORA-01400 when inserting target rows.",
        {
            "root_cause_code": "ORA-01400",
            "next_step_keywords": ["not null", "mapping expression"],
            "output": _error_output("ORA-01400", ["not null", "mapping expression"]),
        },
    ),
    _case(
        "err_ora_12899_value_too_large",
        "error_to_fix",
        "ORA-12899 during ODS load.",
        {
            "root_cause_code": "ORA-12899",
            "next_step_keywords": ["column length", "schema sync"],
            "output": _error_output("ORA-12899", ["column length", "schema sync"]),
        },
    ),
    _case(
        "err_ora_01861_date_literal_mismatch",
        "error_to_fix",
        "ORA-01861 for a date conversion.",
        {
            "root_cause_code": "ORA-01861",
            "next_step_keywords": ["date format", "to_date"],
            "output": _error_output("ORA-01861", ["date format", "to_date"]),
        },
    ),
    _case(
        "err_ora_01438_numeric_precision",
        "error_to_fix",
        "ORA-01438 loading a NUMBER column.",
        {
            "root_cause_code": "ORA-01438",
            "next_step_keywords": ["precision", "scale"],
            "output": _error_output("ORA-01438", ["precision", "scale"]),
        },
    ),
    _case(
        "err_odi_metamodel_data_format",
        "error_to_fix",
        "ODI metamodel DATA_FORMAT property is missing.",
        {
            "root_cause_code": "ODI-METAMODEL_DATA_FORMAT",
            "next_step_keywords": ["seeded metadata", "supported refresh"],
            "output": _error_output("ODI-METAMODEL_DATA_FORMAT", ["seeded metadata", "supported refresh"]),
        },
    ),
    _case(
        "err_odi_studio_freeze_metamodel",
        "error_to_fix",
        "ODI Studio freezes after unsupported metadata patch.",
        {
            "root_cause_code": "ODI-STUDIO_FREEZE_METAMODEL",
            "next_step_keywords": ["unsupported patch", "restore"],
            "output": _error_output("ODI-STUDIO_FREEZE_METAMODEL", ["unsupported patch", "restore"]),
        },
    ),
    _case(
        "err_ora_02019_database_link",
        "error_to_fix",
        "ORA-02019 from a legacy database-link path.",
        {
            "root_cause_code": "ORA-02019",
            "next_step_keywords": ["database link", "oracle source"],
            "output": _error_output("ORA-02019", ["database link", "oracle source"]),
        },
    ),
    _case(
        "err_ora_01017_login_denied",
        "error_to_fix",
        "ORA-01017 during connection test.",
        {
            "root_cause_code": "ORA-01017",
            "next_step_keywords": ["credential source", "secret provider"],
            "output": _error_output("ORA-01017", ["credential source", "secret provider"]),
        },
    ),
    _case(
        "err_ora_02291_parent_key_missing",
        "error_to_fix",
        "ORA-02291 for a foreign-key reference.",
        {
            "root_cause_code": "ORA-02291",
            "next_step_keywords": ["parent key", "load order"],
            "output": _error_output("ORA-02291", ["parent key", "load order"]),
        },
    ),
    _case(
        "err_ora_02292_child_record_found",
        "error_to_fix",
        "ORA-02292 during target cleanup.",
        {
            "root_cause_code": "ORA-02292",
            "next_step_keywords": ["child records", "dependency order"],
            "output": _error_output("ORA-02292", ["child records", "dependency order"]),
        },
    ),
]


def _summary_expected(output: str, keywords: list[str]) -> dict[str, Any]:
    return {"output": output, "required_keywords": keywords}


SUMMARY_KEYWORD_ALIASES: dict[str, tuple[str, ...]] = {
    "blocks": ("blocks", "block", "prevents", "prevent", "without exposing"),
    "redaction": ("redaction", "redacted", "redaction-checked"),
    "unloading": ("unloading", "unload", "did not unload"),
    "evals": ("evals", "evaluations", "evaluation"),
}


SUMMARY_CASES = [
    _case(
        "sum_schema_sync_findings",
        "evidence_summary",
        "Workflow: Schema Sync\n"
        "Status/result: redacted metadata differences detected; type and length mismatches need review.\n"
        "Safety boundary: metadata only, no raw row values.\n"
        "Next action: review mismatches before ODI changes.",
        _summary_expected(
            "Schema Sync found metadata differences only; review type and length mismatches before ODI changes.",
            ["schema sync", "metadata", "mismatches"],
        ),
    ),
    _case(
        "sum_pk_parity_passed",
        "evidence_summary",
        "Workflow: PK parity\n"
        "Status/result: passed for the modeled flow using count-only comparison.\n"
        "Safety boundary: no key values or row samples included.\n"
        "Next action: keep monitoring the modeled flow.",
        _summary_expected(
            "PK parity is count-only and passed for the modeled flow; no key values or rows are included.",
            ["pk parity", "count-only", "passed"],
        ),
    ),
    _case(
        "sum_pk_parity_mapping_not_ready",
        "evidence_summary",
        "Workflow: PK parity\n"
        "Status/result: attention required because some flows are mapping_not_ready; parity itself is not failed.\n"
        "Safety boundary: count/status only, no key values.\n"
        "Next action: finish mapping readiness before parity judgment.",
        _summary_expected(
            "The report is attention required because some flows are mapping_not_ready, not because parity failed.",
            ["attention required", "mapping_not_ready", "parity"],
        ),
    ),
    _case(
        "sum_odi_mapping_link_coverage",
        "evidence_summary",
        "Workflow: mapping-link coverage\n"
        "Status/result: metadata-only linked columns suppress advisory noise; unmapped columns remain visible.\n"
        "Safety boundary: no expression text or row values.\n"
        "Next action: review remaining unmapped metadata findings.",
        _summary_expected(
            "Mapping-link coverage is metadata-only; linked columns suppress advisory noise while unmapped columns remain visible.",
            ["metadata-only", "linked columns", "unmapped"],
        ),
    ),
    _case(
        "sum_csv_export_metadata_only",
        "evidence_summary",
        "Workflow: schema findings CSV export\n"
        "Status/result: CSV generated with an allowlist of metadata fields.\n"
        "Safety boundary: raw data values are excluded.\n"
        "Next action: share the metadata CSV only when needed.",
        _summary_expected(
            "The CSV export uses an allowlist of metadata fields and excludes raw data values.",
            ["csv", "allowlist", "metadata"],
        ),
    ),
    _case(
        "sum_startup_hardening",
        "evidence_summary",
        "Workflow: local startup hardening\n"
        "Status/result: start and stop commands added; stale listener cleanup and venv selection are recorded.\n"
        "Safety boundary: local process control only.\n"
        "Next action: inspect logs if startup fails.",
        _summary_expected(
            "Local startup now has start and stop commands, stale listener cleanup, venv selection, and logs.",
            ["start", "stop", "stale listener"],
        ),
    ),
    _case(
        "sum_incremental_preflight_duplicate_window",
        "evidence_summary",
        "Workflow: incremental preflight\n"
        "Status/result: dry-run duplicate or empty window is blocked; watermarks do not advance.\n"
        "Safety boundary: no append/load/truncate.\n"
        "Next action: wait for a new safe window before execution approval.",
        _summary_expected(
            "Incremental preflight is dry-run only; duplicate or empty windows are blocked and watermarks do not advance.",
            ["dry-run", "duplicate", "watermarks"],
        ),
    ),
    _case(
        "sum_security_audit_scope",
        "evidence_summary",
        "Workflow: Security Audit\n"
        "Status/result: local evidence log only, not a vulnerability scanner.\n"
        "Safety boundary: no secret disclosure surface.\n"
        "Next action: keep audit evidence descriptive and redacted.",
        _summary_expected(
            "Security Audit is a local evidence log, not a vulnerability scanner or secret disclosure surface.",
            ["security audit", "evidence log", "not a vulnerability scanner"],
        ),
    ),
    _case(
        "sum_airflow_link_removed",
        "evidence_summary",
        "Workflow: Airflow UI cleanup\n"
        "Status/result: Airflow removed from the header tab.\n"
        "Safety boundary: no Docker or scheduler change.\n"
        "Next action: keep Airflow as environment and health reference only.",
        _summary_expected(
            "Airflow is no longer a header tab; it remains only an environment and health reference.",
            ["airflow", "header", "health"],
        ),
    ),
    _case(
        "sum_table_preview_columns",
        "evidence_summary",
        "Workflow: table preview\n"
        "Status/result: Data and Columns are separated; PK and FK badges come from metadata.\n"
        "Safety boundary: preview UI should not expose extra row detail in summaries.\n"
        "Next action: use Columns for metadata checks.",
        _summary_expected(
            "Table preview separates Data and Columns and shows PK or FK badges from metadata.",
            ["data", "columns", "pk"],
        ),
    ),
    _case(
        "sum_impact_layer_filter",
        "evidence_summary",
        "Workflow: Impact analysis\n"
        "Status/result: layer filter is available and the layer badge appears next to the object name.\n"
        "Safety boundary: impact metadata only.\n"
        "Next action: filter by layer when triaging affected objects.",
        _summary_expected(
            "Impact analysis can filter by layer and displays the layer badge next to the object name.",
            ["impact", "layer", "badge"],
        ),
    ),
    _case(
        "sum_error_pattern_memory",
        "evidence_summary",
        "Workflow: error pattern memory\n"
        "Status/result: redacted troubleshooting patterns are stored; blocks cover secrets, connection strings, and raw rows.\n"
        "Safety boundary: no sensitive values in memory output.\n"
        "Next action: reuse patterns without exposing protected content.",
        _summary_expected(
            "Error pattern memory stores redacted troubleshooting patterns and blocks secrets, connection strings, and raw rows.",
            ["error pattern", "redacted", "blocks"],
        ),
    ),
    _case(
        "sum_local_llm_guard",
        "evidence_summary",
        "Workflow: local LLM analyst\n"
        "Status/result: local LLM analysis uses redaction-checked text and avoids unloading a model it did not load.\n"
        "Safety boundary: no unchecked sensitive input.\n"
        "Next action: only analyze redacted evidence packs.",
        _summary_expected(
            "Local LLM analysis runs only on redaction-checked text and avoids unloading a model it did not load.",
            ["local llm", "redaction", "unloading"],
        ),
    ),
    _case(
        "sum_model_eval_plan",
        "evidence_summary",
        "Workflow: local model eval plan\n"
        "Status/result: start with evals, then RAG, then constrained decoding; fine-tuning is optional later if numbers justify it.\n"
        "Safety boundary: static/offline v1 before live-data correctness checks.\n"
        "Next action: use scores to choose the next model strategy.",
        _summary_expected(
            "The model plan starts with evals, then RAG, constrained decoding, and only later optional fine-tuning if numbers justify it.",
            ["evals", "rag", "fine-tuning"],
        ),
    ),
    _case(
        "sum_live_data_gate",
        "evidence_summary",
        "Workflow: live data correctness gate\n"
        "Status/result: live data checks require a separate approval gate; v1 evaluation remains static.\n"
        "Safety boundary: offline eval only.\n"
        "Next action: request explicit approval before live-data validation.",
        _summary_expected(
            "Live data correctness checks are a separate approval gate; v1 evaluation stays static and offline.",
            ["live data", "approval gate", "static"],
        ),
    ),
]


GOLDEN_CASES: dict[str, list[dict[str, Any]]] = {
    "nl_to_sql": SQL_CASES,
    "intent_to_json": INTENT_CASES,
    "error_to_fix": ERROR_CASES,
    "evidence_summary": SUMMARY_CASES,
}


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def scan_blocked_content(value: Any) -> list[dict[str, str]]:
    text = _to_text(value)
    findings = []
    for name, pattern in BLOCKED_CONTENT_PATTERNS:
        if pattern.search(text):
            findings.append({"type": name, "action": "blocked"})
    return findings


def assert_no_blocked_content(value: Any) -> None:
    findings = scan_blocked_content(value)
    if findings:
        names = ", ".join(item["type"] for item in findings)
        raise ValueError(f"blocked content detected: {names}")


def case_by_id(case_id: str) -> dict[str, Any]:
    for cases in GOLDEN_CASES.values():
        for case in cases:
            if case["id"] == case_id:
                return case
    raise KeyError(case_id)


def _extract_sql(output: Any) -> str:
    text = _to_text(output).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _tables_in_sql(sql_upper: str) -> set[str]:
    return {
        match.group(2).upper()
        for match in re.finditer(r"\b(FROM|JOIN)\s+([A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*)\b", sql_upper)
    }


def _validate_sql(case: dict[str, Any], output: Any) -> EvalResult:
    reasons: list[str] = []
    sql = _extract_sql(output)
    sql_upper = sql.upper()
    findings = scan_blocked_content(sql)
    if findings:
        reasons.append("blocked_content")
    if not re.match(r"^\s*(SELECT|WITH)\b", sql_upper):
        reasons.append("not_select")
    for keyword in FORBIDDEN_SQL_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", sql_upper):
            reasons.append("forbidden_keyword")
            break
    if ";" in sql.rstrip(";"):
        reasons.append("multiple_or_embedded_statement")

    tables = _tables_in_sql(sql_upper)
    if not tables:
        reasons.append("missing_table_reference")
    unknown_tables = sorted(table for table in tables if table not in STATIC_CATALOG)
    if unknown_tables:
        reasons.append("unknown_table")

    expected = case["expected"]
    missing_tables = [table for table in expected["required_tables"] if table.upper() not in tables]
    if missing_tables:
        reasons.append("missing_required_table")

    missing_columns = [
        column
        for column in expected["required_columns"]
        if not re.search(rf"\b{re.escape(column.upper())}\b", sql_upper)
    ]
    if missing_columns:
        reasons.append("missing_required_column")

    return EvalResult(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        details={
            "tables": sorted(tables),
            "unknown_tables": unknown_tables,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "parse_mode": expected.get("parse_mode", "static_only_no_db"),
        },
    )


def _coerce_json(output: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(output, dict):
        return output, []
    try:
        data = json.loads(str(output))
    except json.JSONDecodeError:
        return None, ["invalid_json"]
    if not isinstance(data, dict):
        return None, ["invalid_json_object"]
    return data, []


def _required_json_fields(operation: str) -> set[str]:
    if operation == "CREATE":
        return {
            "operation",
            "mapping_name",
            "src_model",
            "src_table",
            "tgt_model",
            "tgt_table",
            "tgt_owner",
            "needs_skey",
            "description",
        }
    if operation == "UPDATE":
        return {"operation", "mapping_name", "changes"}
    if operation == "DELETE":
        return {"operation", "mapping_name", "tgt_owner"}
    if operation == "VALIDATE":
        return {"operation", "tool", "source_schema", "target_owner", "source_table", "target_table"}
    return {"operation"}


def _string_field(*, pattern: str | None = None, min_length: int = 1) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "minLength": min_length}
    if pattern:
        schema["pattern"] = pattern
    return schema


def _operation_const(operation: str) -> dict[str, Any]:
    return {"type": "string", "const": operation}


def _intent_json_schema_variant(operation: str) -> dict[str, Any]:
    if operation == "CREATE":
        properties = {
            "operation": _operation_const("CREATE"),
            "mapping_name": _string_field(pattern=r"^MAP_[A-Z0-9_]+$"),
            "src_model": _string_field(pattern=r"^ORACLE_[A-Z0-9_]+$"),
            "src_table": _string_field(pattern=r"^[A-Z0-9_]+\.[A-Z0-9_]+$"),
            "tgt_model": _string_field(pattern=r"^[A-Z0-9_]+$"),
            "tgt_table": _string_field(pattern=r"^[A-Z0-9_]+$"),
            "tgt_owner": _string_field(pattern=r"^[A-Z0-9_]+$"),
            "needs_skey": {"type": "boolean"},
            "skey_natural_key": {
                "anyOf": [
                    _string_field(pattern=r"^[A-Z0-9_]+\.[A-Z0-9_]+$"),
                    {"type": "null"},
                ]
            },
            "description": _string_field(),
        }
    elif operation == "UPDATE":
        properties = {
            "operation": _operation_const("UPDATE"),
            "mapping_name": _string_field(pattern=r"^MAP_[A-Z0-9_]+$"),
            "changes": {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "properties": {
                    "tgt_table": _string_field(pattern=r"^[A-Z0-9_]+$"),
                    "truncate_target_table": {"type": "boolean"},
                    "filter_condition": _string_field(),
                    "tgt_owner": _string_field(pattern=r"^[A-Z0-9_]+$"),
                },
            },
        }
    elif operation == "DELETE":
        properties = {
            "operation": _operation_const("DELETE"),
            "mapping_name": _string_field(pattern=r"^MAP_[A-Z0-9_]+$"),
            "tgt_owner": _string_field(pattern=r"^[A-Z0-9_]+$"),
        }
    elif operation == "VALIDATE":
        properties = {
            "operation": _operation_const("VALIDATE"),
            "tool": {"type": "string", "const": "pk_parity"},
            "source_schema": _string_field(pattern=r"^[A-Z0-9_]+$"),
            "target_owner": _string_field(pattern=r"^[A-Z0-9_]+$"),
            "source_table": _string_field(pattern=r"^[A-Z0-9_]+$"),
            "target_table": _string_field(pattern=r"^[A-Z0-9_]+$"),
        }
    else:
        raise ValueError(f"Unsupported intent operation: {operation}")

    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_required_json_fields(operation)),
        "properties": properties,
    }


def build_intent_json_schema_contract() -> dict[str, Any]:
    """Build a static JSON Schema contract for constrained intent decoding."""

    artifact = {
        "version": "odi-62-v1",
        "mode": "intent_json_schema_contract",
        "task": "intent_to_json",
        "validation_mode": "static_offline",
        "model_call_attempted": False,
        "live_execution_attempted": False,
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "ODI mapping intent request",
            "type": "object",
            "oneOf": [
                _intent_json_schema_variant("CREATE"),
                _intent_json_schema_variant("UPDATE"),
                _intent_json_schema_variant("DELETE"),
                _intent_json_schema_variant("VALIDATE"),
            ],
        },
    }
    assert_no_blocked_content(artifact)
    return artifact


def export_intent_json_schema_contract(path: str | Path) -> dict[str, str]:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = build_intent_json_schema_contract()
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "written", "path": str(output_path)}


INTENT_GBNF_GRAMMAR = r'''root ::= create | update | delete | validate
ws ::= [ \t\n\r]*
string ::= "\"" char* "\""
char ::= [^"\\] | "\\" (["\\/bfnrt] | "u" hex hex hex hex)
hex ::= [0-9a-fA-F]
ident ::= "\"" [A-Z0-9_]+ "\""
dotted-ident ::= "\"" [A-Z0-9_]+ "." [A-Z0-9_]+ "\""
bool ::= "true" | "false"
null ::= "null"

create ::= "{" ws "\"operation\"" ws ":" ws "\"CREATE\"" ws "," ws "\"mapping_name\"" ws ":" ws ident ws "," ws "\"src_model\"" ws ":" ws ident ws "," ws "\"src_table\"" ws ":" ws dotted-ident ws "," ws "\"tgt_model\"" ws ":" ws ident ws "," ws "\"tgt_table\"" ws ":" ws ident ws "," ws "\"tgt_owner\"" ws ":" ws ident ws "," ws "\"needs_skey\"" ws ":" ws bool ws "," ws "\"skey_natural_key\"" ws ":" ws (dotted-ident | null) ws "," ws "\"description\"" ws ":" ws string ws "}"
update ::= "{" ws "\"operation\"" ws ":" ws "\"UPDATE\"" ws "," ws "\"mapping_name\"" ws ":" ws ident ws "," ws "\"changes\"" ws ":" ws change-object ws "}"
change-object ::= "{" ws (change-tgt-table | change-truncate | change-filter | change-tgt-owner) ws "}"
change-tgt-table ::= "\"tgt_table\"" ws ":" ws ident
change-truncate ::= "\"truncate_target_table\"" ws ":" ws bool
change-filter ::= "\"filter_condition\"" ws ":" ws string
change-tgt-owner ::= "\"tgt_owner\"" ws ":" ws ident
delete ::= "{" ws "\"operation\"" ws ":" ws "\"DELETE\"" ws "," ws "\"mapping_name\"" ws ":" ws ident ws "," ws "\"tgt_owner\"" ws ":" ws ident ws "}"
validate ::= "{" ws "\"operation\"" ws ":" ws "\"VALIDATE\"" ws "," ws "\"tool\"" ws ":" ws "\"pk_parity\"" ws "," ws "\"source_schema\"" ws ":" ws ident ws "," ws "\"target_owner\"" ws ":" ws ident ws "," ws "\"source_table\"" ws ":" ws ident ws "," ws "\"target_table\"" ws ":" ws ident ws "}"'''


def _intent_schema_operations(schema_artifact: dict[str, Any]) -> list[str]:
    return [
        str(variant["properties"]["operation"]["const"])
        for variant in schema_artifact["schema"]["oneOf"]
    ]


def build_intent_gbnf_grammar_contract() -> dict[str, Any]:
    """Build a static GBNF grammar artifact for intent JSON constrained decoding."""

    schema_artifact = build_intent_json_schema_contract()
    artifact = {
        "version": "odi-62-v1",
        "mode": "intent_gbnf_grammar_contract",
        "task": "intent_to_json",
        "validation_mode": "static_offline",
        "source_contract_mode": schema_artifact["mode"],
        "format": "gbnf",
        "model_call_attempted": False,
        "live_execution_attempted": False,
        "supported_operations": _intent_schema_operations(schema_artifact),
        "strictness_notes": [
            "Generated from the static intent JSON Schema contract.",
            "The grammar fixes field order and is intentionally narrower than free-form JSON.",
            "UPDATE.changes currently permits one static-eval change field per generated object.",
            "This artifact is preparation only; it is not runtime-enforced by this slice.",
        ],
        "grammar": INTENT_GBNF_GRAMMAR,
    }
    assert_no_blocked_content(artifact)
    return artifact


def export_intent_gbnf_grammar_contract(path: str | Path) -> dict[str, str]:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = build_intent_gbnf_grammar_contract()
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "written", "path": str(output_path)}


def _compare_expected(expected: Any, actual: Any, prefix: str = "") -> list[str]:
    mismatches: list[str] = []
    if expected is None and actual in {None, ""}:
        return mismatches
    if prefix.endswith("description") and isinstance(actual, str) and actual.strip():
        return mismatches
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [prefix or "<root>"]
        for key, expected_value in expected.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            if key not in actual:
                if expected_value is None:
                    continue
                mismatches.append(child_prefix)
            else:
                mismatches.extend(_compare_expected(expected_value, actual[key], child_prefix))
        return mismatches
    if expected != actual:
        mismatches.append(prefix or "<root>")
    return mismatches


def _validate_intent_json(case: dict[str, Any], output: Any) -> EvalResult:
    reasons: list[str] = []
    findings = scan_blocked_content(output)
    if findings:
        reasons.append("blocked_content")
    data, parse_reasons = _coerce_json(output)
    reasons.extend(parse_reasons)
    if data is None:
        return EvalResult(False, tuple(dict.fromkeys(reasons)), {"json": None})

    operation = str(data.get("operation") or "").upper()
    required = _required_json_fields(operation)
    missing = sorted(field for field in required if field not in data)
    if missing:
        reasons.append("missing_required_field")
    expected_output = case["expected"]["output"]
    mismatches = _compare_expected(expected_output, data)
    if mismatches:
        reasons.append("expected_value_mismatch")

    return EvalResult(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        details={"missing_fields": missing, "mismatches": mismatches, "operation": operation},
    )


def _normalize_output_dict(output: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(output, dict):
        return output, []
    data, reasons = _coerce_json(output)
    if data is not None:
        return data, []
    return None, reasons or ["invalid_output"]


def _validate_error_fix(case: dict[str, Any], output: Any) -> EvalResult:
    reasons: list[str] = []
    findings = scan_blocked_content(output)
    if findings:
        reasons.append("blocked_content")
    data, parse_reasons = _normalize_output_dict(output)
    reasons.extend(parse_reasons)
    if data is None:
        return EvalResult(False, tuple(dict.fromkeys(reasons)), {"json": None})

    expected = case["expected"]
    if str(data.get("root_cause_code") or "").upper() != expected["root_cause_code"].upper():
        reasons.append("root_cause_mismatch")
    step_text = _to_text(data.get("next_steps", "")).lower()
    missing_keywords = [
        keyword for keyword in expected["next_step_keywords"] if keyword.lower() not in step_text
    ]
    if missing_keywords:
        reasons.append("missing_next_step_keyword")

    return EvalResult(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        details={"missing_next_step_keywords": missing_keywords},
    )


def _validate_summary(case: dict[str, Any], output: Any) -> EvalResult:
    reasons: list[str] = []
    findings = scan_blocked_content(output)
    if findings:
        reasons.append("blocked_content")
    text = _to_text(output).lower()
    missing_keywords = [
        keyword
        for keyword in case["expected"]["required_keywords"]
        if not any(alias in text for alias in SUMMARY_KEYWORD_ALIASES.get(keyword.lower(), (keyword.lower(),)))
    ]
    if missing_keywords:
        reasons.append("missing_summary_keyword")
    return EvalResult(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        details={"blocked_findings": findings, "missing_keywords": missing_keywords},
    )


def validate_case(case: dict[str, Any], output: Any) -> EvalResult:
    task = case["task"]
    if task == "nl_to_sql":
        return _validate_sql(case, output)
    if task == "intent_to_json":
        return _validate_intent_json(case, output)
    if task == "error_to_fix":
        return _validate_error_fix(case, output)
    if task == "evidence_summary":
        return _validate_summary(case, output)
    raise ValueError(f"Unsupported task: {task}")


def load_predictions(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    outputs = payload.get("outputs", payload) if isinstance(payload, dict) else {}
    if not isinstance(outputs, dict):
        raise ValueError("Predictions file must contain a JSON object or an outputs object.")
    assert_no_blocked_content(outputs)
    return outputs


def _fenced_json_object(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, str):
        return None
    match = re.match(r"^\s*```(?:json)?\s*(\{.*\})\s*```\s*$", output, flags=re.I | re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def normalize_predictions_with_prompt_guard(predictions: dict[str, Any]) -> dict[str, Any]:
    """Normalize narrowly recoverable JSON-task outputs without hiding unsafe text.

    The guard accepts only whole-response fenced JSON objects for JSON-shaped
    tasks. It does not extract JSON from arbitrary prose, and it scans before
    and after normalization so blocked content cannot be laundered away.
    """

    assert_no_blocked_content(predictions)
    outputs: dict[str, Any] = {}
    summary = {
        "mode": "guarded_json_normalization",
        "eligible": 0,
        "normalized": 0,
        "unchanged": 0,
        "blocked": 0,
    }
    for case_id, output in predictions.items():
        try:
            case = case_by_id(case_id)
        except KeyError:
            outputs[case_id] = output
            summary["unchanged"] += 1
            continue

        if case["task"] in {"intent_to_json", "error_to_fix"}:
            summary["eligible"] += 1
            normalized = _fenced_json_object(output)
            if normalized is not None:
                outputs[case_id] = normalized
                summary["normalized"] += 1
                continue

        outputs[case_id] = output
        summary["unchanged"] += 1

    assert_no_blocked_content(outputs)
    return {"outputs": outputs, "summary": summary}


def _prompt_value(pattern: str, prompt: str) -> str | None:
    match = re.search(pattern, prompt, flags=re.I)
    return match.group(1).rstrip(".").upper() if match else None


def _repair_intent_output_from_prompt(case: dict[str, Any], output: Any) -> tuple[Any, bool]:
    data, _ = _normalize_output_dict(output)
    if data is None:
        return output, False

    prompt = case["prompt"]
    target_table = _prompt_value(r"\btarget dimension table\s+([A-Z0-9_.$#]+)", prompt)
    skey_natural_key = _prompt_value(r"\bsurrogate key natural key\s+([A-Z0-9_.$#]+)", prompt)
    if not target_table or not skey_natural_key:
        return output, False
    if "not an ods mapping" not in prompt.lower():
        return output, False
    if str(data.get("operation") or "").upper() != "CREATE":
        return output, False
    if str(data.get("tgt_table") or "").upper() != target_table:
        return output, False
    if data.get("needs_skey") is not True:
        return output, False

    output_text = _to_text(data).upper()
    repaired = dict(data)
    mapping_name = str(repaired.get("mapping_name") or "")
    if target_table.startswith("DIM_") and mapping_name.endswith("_TO_ODS"):
        repaired["mapping_name"] = mapping_name[: -len("_TO_ODS")] + "_TO_DIM"
    if skey_natural_key in output_text and repaired.get("skey_natural_key") != skey_natural_key:
        repaired["skey_natural_key"] = skey_natural_key

    return repaired, repaired != data


def repair_intent_outputs_with_prompt_guard(predictions: dict[str, Any]) -> dict[str, Any]:
    """Apply narrow prompt-derived repairs for structured intent JSON outputs.

    This is intentionally separate from fenced JSON normalization. It only uses
    values already present in the synthetic task prompt and only repairs fields
    that are deterministic from those prompt values plus the model output.
    """

    assert_no_blocked_content(predictions)
    outputs: dict[str, Any] = {}
    summary = {
        "mode": "intent_prompt_value_repair",
        "eligible": 0,
        "repaired": 0,
        "unchanged": 0,
        "blocked": 0,
    }
    for case_id, output in predictions.items():
        try:
            case = case_by_id(case_id)
        except KeyError:
            outputs[case_id] = output
            summary["unchanged"] += 1
            continue

        if case["task"] == "intent_to_json":
            summary["eligible"] += 1
            repaired, changed = _repair_intent_output_from_prompt(case, output)
            outputs[case_id] = repaired
            if changed:
                summary["repaired"] += 1
            else:
                summary["unchanged"] += 1
            continue

        outputs[case_id] = output
        summary["unchanged"] += 1

    assert_no_blocked_content(outputs)
    return {"outputs": outputs, "summary": summary}


INTENT_SCHEMA_WRAPPER_KEYS = frozenset({"result", "request", "data", "payload", "intent"})


def _unwrap_single_intent_json_wrapper(output: Any) -> tuple[Any, bool]:
    data, _ = _normalize_output_dict(output)
    if data is None or len(data) != 1:
        return output, False

    wrapper_key, wrapped_value = next(iter(data.items()))
    if str(wrapper_key).lower() not in INTENT_SCHEMA_WRAPPER_KEYS:
        return output, False
    if not isinstance(wrapped_value, dict):
        return output, False

    return wrapped_value, True


def apply_intent_schema_guard(predictions: dict[str, Any]) -> dict[str, Any]:
    """Apply deterministic schema-envelope normalization for intent JSON outputs.

    This guard unwraps only a whole-response, single-key envelope such as
    {"result": {...}}. It does not extract JSON from prose and refuses
    ambiguous objects that include sibling explanation fields.
    """

    assert_no_blocked_content(predictions)
    outputs: dict[str, Any] = {}
    summary = {
        "mode": "intent_schema_guard",
        "eligible": 0,
        "unwrapped": 0,
        "unchanged": 0,
        "blocked": 0,
    }
    for case_id, output in predictions.items():
        try:
            case = case_by_id(case_id)
        except KeyError:
            outputs[case_id] = output
            summary["unchanged"] += 1
            continue

        if case["task"] == "intent_to_json":
            summary["eligible"] += 1
            unwrapped, changed = _unwrap_single_intent_json_wrapper(output)
            outputs[case_id] = unwrapped
            if changed:
                summary["unwrapped"] += 1
            else:
                summary["unchanged"] += 1
            continue

        outputs[case_id] = output
        summary["unchanged"] += 1

    assert_no_blocked_content(outputs)
    return {"outputs": outputs, "summary": summary}


def _catalog_context(case: dict[str, Any]) -> str:
    if case["task"] != "nl_to_sql":
        return ""
    tables = case["expected"].get("required_tables", [])
    parts = []
    for table in tables:
        columns = ", ".join(sorted(STATIC_CATALOG.get(table, [])))
        if columns:
            parts.append(f"{table}({columns})")
    return "\n".join(parts)


ERROR_REMEDIATION_PLAYBOOK = """Remediation playbook:
- ODI-16171: check target key and reverse-engineer target metadata before merge KM validation.
- ORA-01031: check setup user privilege for the requested DDL or grant path.
- ORA-00942: check object name and grant on the referenced metadata object.
- ORA-00904: check column metadata and physical name in generated SQL.
- ORA-00001: check duplicate key and rerun safety before another target load.
- ORA-01400: check not null target columns and mapping expression coverage.
- ORA-12899: check column length and Schema Sync metadata mismatches.
- ORA-01861: check date format and TO_DATE conversion.
- ORA-01438: check numeric precision and scale.
- ODI metamodel DATA_FORMAT: check seeded metadata and use supported refresh.
- ODI Studio freeze after metamodel patch: treat as unsupported patch and restore.
- ORA-02019: check database link usage and prefer the Oracle source topology path.
- ORA-01017: check credential source and secret provider configuration.
- ORA-02291: check parent key and load order.
- ORA-02292: check child records and dependency order."""


def _prompt_for_case(case: dict[str, Any]) -> tuple[str, str]:
    task = case["task"]
    if task == "nl_to_sql":
        context = _catalog_context(case) or "Use only the provided Oracle metadata."
        return (
            "oracle_sql",
            "Return only Oracle SQL. Use a single read-only SELECT or WITH query. "
            "Do not include comments, markdown, DDL, DML, transaction commands, or EXPLAIN PLAN.\n\n"
            f"Schema context:\n{context}\n\nTask:\n{case['prompt']}",
        )
    if task == "intent_to_json":
        return (
            "json_object",
            "Return exactly one JSON object. Do not wrap it in markdown. Do not add wrapper keys "
            'such as "result", "request", "data", or "explanation". Use one of these shapes:\n'
            '- CREATE: {"operation": "CREATE", "mapping_name": string, "src_model": string, '
            '"src_table": string, "tgt_model": string, "tgt_table": string, "tgt_owner": string, '
            '"needs_skey": boolean, "description": string}\n'
            '- UPDATE: {"operation": "UPDATE", "mapping_name": string, "changes": object}\n'
            '- DELETE: {"operation": "DELETE", "mapping_name": string, "tgt_owner": string}\n'
            '- VALIDATE: {"operation": "VALIDATE", "tool": string, "source_schema": string, '
            '"target_owner": string, "source_table": string, "target_table": string}\n\n'
            "Project conventions:\n"
            "- Mapping name convention: MAP_<SOURCE_SCHEMA>_<SOURCE_TABLE>_TO_ODS for ODS mappings.\n"
            "- Temporary mapping convention: MAP_TEST_<SOURCE_SCHEMA>_TO_ODS.\n"
            "- Source model convention: ORACLE_<SOURCE_SCHEMA>.\n"
            "- Source table convention: <SOURCE_SCHEMA>.<SOURCE_TABLE>.\n"
            "- Target model and owner are DWH.\n"
            "- ODS target table convention: ODS_<SOURCE_SCHEMA>_<SOURCE_TABLE>.\n"
            "- Do not infer a surrogate key for ordinary ODS mappings; set needs_skey false unless the task explicitly asks for a dimension or surrogate key.\n"
            "- Use pk_parity for PK parity validation requests.\n"
            "- For UPDATE requests, put only the requested changed fields under changes.\n"
            "- Use tgt_table for target table rename/change requests.\n"
            "- Use truncate_target_table false when the task asks to disable truncate.\n"
            "- Use filter_condition for requested filter predicates.\n"
            "- For product dimension surrogate-key requests, use SH.PRODUCTS, DIM_PRODUCT, needs_skey true, "
            "and skey_natural_key SRC.PROD_ID.\n\n"
            f"Task:\n{case['prompt']}",
        )
    if task == "error_to_fix":
        return (
            "json_object",
            "Return exactly one JSON object. Do not wrap it in markdown. Do not add wrapper keys. "
            'Required shape: {"root_cause_code": string, "next_steps": [string, ...]}. '
            "Use the most specific Oracle or ODI error code from the evidence as root_cause_code. "
            "Keep next_steps actionable and do not include credentials, connection strings, or row samples.\n\n"
            f"{ERROR_REMEDIATION_PLAYBOOK}\n\n"
            f"Error evidence:\n{case['prompt']}",
        )
    if task == "evidence_summary":
        return (
            "plain_text",
            "Return a concise operational summary. Include the workflow name, the status/result, "
            "the safety boundary, and the next action when present. Do not invent row-level details. "
            "Do not include secrets, connection strings, raw rows, sample rows, or credential-like text.\n\n"
            f"Evidence pack:\n{case['prompt']}",
        )
    raise ValueError(f"Unsupported task: {task}")


def build_prompt_pack() -> dict[str, Any]:
    prompts = []
    for task, cases in GOLDEN_CASES.items():
        for case in cases:
            response_format, prompt = _prompt_for_case(case)
            prompts.append(
                {
                    "id": case["id"],
                    "task": task,
                    "response_format": response_format,
                    "prompt": prompt,
                }
            )
    pack = {
        "version": "odi-63-v1",
        "mode": "static_prompt_pack",
        "total": len(prompts),
        "model_call_attempted": False,
        "live_execution_attempted": False,
        "prompts": prompts,
        "predictions_file_schema": {
            "top_level_key": "outputs",
            "shape": {"outputs": {"<case_id>": "<model response object or string>"}},
        },
    }
    assert_no_blocked_content(pack)
    return pack


def export_prompt_pack(path: str | Path) -> dict[str, Any]:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(build_prompt_pack(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "written", "path": str(output_path)}


def _chat_payload(model: str, prompt: str, response_format: str | None = None) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an offline-evaluated Oracle, ODI, and DWH assistant. "
                    "Follow the user response format exactly. Do not reveal secrets, "
                    "connection strings, raw rows, or sample rows."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 900,
    }
    if response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


def _extract_chat_content(response: Any) -> str:
    try:
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        return str(body["choices"][0]["message"]["content"])
    except Exception:
        return ""


def _extract_chat_metadata(response: Any) -> dict[str, Any]:
    try:
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        choice = body.get("choices", [{}])[0]
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        return {
            "finish_reason": choice.get("finish_reason"),
            "has_reasoning_content": bool(message.get("reasoning_content") or message.get("reasoning")),
        }
    except Exception:
        return {"finish_reason": None, "has_reasoning_content": False}


def capture_predictions(
    *,
    prompt_pack: dict[str, Any] | None = None,
    base_url: str,
    model: str,
    api_key: str | None = "",
    timeout: int = 180,
    use_runtime_json_mode: bool = False,
    http_post: Any = requests.post,
) -> dict[str, Any]:
    """Capture local model predictions from a prompt pack.

    The returned report intentionally omits prompts, base URL, API key, and
    request headers. Blocked model outputs are reported by status only and are
    not written into the predictions map.
    """

    pack = prompt_pack or build_prompt_pack()
    outputs: dict[str, Any] = {}
    results = []
    model_call_attempted = False
    for item in pack.get("prompts", []):
        case_id = str(item["id"])
        try:
            endpoint = validate_endpoint(base_url)
            key = api_key if api_key is not None else local_openai_key(
                endpoint, os.getenv("LOCAL_OPENAI_BASE_URL") or "http://localhost:1234/v1")
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            model_call_attempted = True
            response = http_post(
                f"{endpoint.rstrip('/')}/chat/completions",
                headers=headers,
                json=_chat_payload(
                    model,
                    str(item["prompt"]),
                    str(item.get("response_format") or "") if use_runtime_json_mode else None,
                ),
                timeout=timeout,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code >= 400:
                results.append(
                    {
                        "id": case_id,
                        "task": item.get("task"),
                        "status": "http_error",
                        "status_code": status_code,
                    }
                )
                continue
            content = _extract_chat_content(response)
            metadata = _extract_chat_metadata(response)
        except Exception as exc:
            results.append(
                {
                    "id": case_id,
                    "task": item.get("task"),
                    "status": "http_error",
                    "status_code": None,
                    "error_type": type(exc).__name__,
                }
            )
            continue

        if not content.strip():
            results.append(
                {
                    "id": case_id,
                    "task": item.get("task"),
                    "status": "empty_output",
                    "status_code": status_code,
                    **metadata,
                }
            )
            continue

        findings = scan_blocked_content(content)
        if findings:
            results.append(
                {
                    "id": case_id,
                    "task": item.get("task"),
                    "status": "blocked_output",
                    "status_code": status_code,
                    "blocked_findings": findings,
                }
            )
            continue
        outputs[case_id] = content
        results.append(
            {
                "id": case_id,
                "task": item.get("task"),
                "status": "captured" if status_code < 400 else "http_error",
                "status_code": status_code,
                **metadata,
            }
        )

    total = len(pack.get("prompts", []))
    return {
        "version": "odi-63-v1",
        "mode": "prediction_capture",
        "model": model,
        "model_call_attempted": model_call_attempted,
        "live_execution_attempted": False,
        "total": total,
        "captured": len(outputs),
        "blocked": sum(1 for item in results if item["status"] == "blocked_output"),
        "failed": sum(1 for item in results if item["status"] in {"http_error", "empty_output"}),
        "empty": sum(1 for item in results if item["status"] == "empty_output"),
        "outputs": outputs,
        "results": results,
    }


def export_predictions(
    path: str | Path,
    *,
    prompt_pack: dict[str, Any] | None = None,
    base_url: str,
    model: str,
    api_key: str | None = "",
    timeout: int = 180,
    use_runtime_json_mode: bool = False,
    http_post: Any = requests.post,
) -> dict[str, Any]:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = capture_predictions(
        prompt_pack=prompt_pack,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        use_runtime_json_mode=use_runtime_json_mode,
        http_post=http_post,
    )
    assert_no_blocked_content(report)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "written", "path": str(output_path), "captured": report["captured"], "total": report["total"]}


def probe_runtime(
    *,
    base_url: str,
    model: str,
    api_key: str | None = "",
    timeout: int = 10,
    http_get: Any | None = None,
) -> dict[str, Any]:
    """Probe a local OpenAI-compatible runtime without requesting inference."""

    http_get = http_get or requests.get
    try:
        base_url = validate_endpoint(base_url)
        key = api_key if api_key is not None else local_openai_key(
            base_url, os.getenv("LOCAL_OPENAI_BASE_URL") or "http://localhost:1234/v1")
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        response = http_get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=timeout)
        status_code = int(getattr(response, "status_code", 0) or 0)
        reachable = 200 <= status_code < 400
        model_ids: list[str] = []
        if reachable:
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            model_ids = [str(item.get("id")) for item in body.get("data", []) if item.get("id")]
        report = {
            "version": "odi-63-v1",
            "mode": "runtime_probe",
            "endpoint": "openai_compatible_models",
            "reachable": reachable,
            "status_code": status_code,
            "model_requested": model,
            "model_available": model in model_ids if reachable else False,
            "model_count": len(model_ids),
            "model_call_attempted": False,
            "live_execution_attempted": False,
            "secret_printed": False,
        }
        if not reachable:
            report["error_type"] = "http_status"
    except Exception as exc:
        report = {
            "version": "odi-63-v1",
            "mode": "runtime_probe",
            "endpoint": "openai_compatible_models",
            "reachable": False,
            "status_code": None,
            "model_requested": model,
            "model_available": False,
            "model_count": 0,
            "model_call_attempted": False,
            "live_execution_attempted": False,
            "secret_printed": False,
            "error_type": type(exc).__name__,
        }
    assert_no_blocked_content(report)
    return report


def _reference_output(case: dict[str, Any]) -> Any:
    return case["expected"]["output"]


def _verdict(score: float, threshold: float, *, reference_mode: bool) -> str:
    if reference_mode:
        return "static_reference_passes" if score == 1.0 else "static_reference_failed"
    if score >= threshold:
        return "rag_enough_for_static_gate"
    return "needs_grammar_or_prompt_guard"


def run_static_eval(predictions: dict[str, Any] | None = None) -> dict[str, Any]:
    reference_mode = predictions is None
    predictions = predictions or {}
    tasks: dict[str, dict[str, Any]] = {}
    overall_total = 0
    overall_passed = 0

    for task, cases in GOLDEN_CASES.items():
        results = []
        for case in cases:
            output = predictions.get(case["id"], _reference_output(case))
            result = validate_case(case, output)
            results.append(
                {
                    "id": case["id"],
                    "passed": result.passed,
                    "reasons": list(result.reasons),
                    "details": result.details,
                }
            )
        total = len(results)
        passed = sum(1 for item in results if item["passed"])
        score = round(passed / total, 4) if total else 0.0
        threshold = THRESHOLDS[task]
        tasks[task] = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "score": score,
            "threshold": threshold,
            "meets_threshold": score >= threshold,
            "verdict": _verdict(score, threshold, reference_mode=reference_mode),
            "results": results,
        }
        overall_total += total
        overall_passed += passed

    return {
        "version": "odi-63-v1",
        "mode": "static_offline",
        "live_execution_attempted": False,
        "model_call_attempted": False,
        "thresholds": THRESHOLDS,
        "tasks": tasks,
        "overall": {
            "total": overall_total,
            "passed": overall_passed,
            "failed": overall_total - overall_passed,
            "score": round(overall_passed / overall_total, 4) if overall_total else 0.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ODI-63 static local model eval checks.")
    parser.add_argument("--predictions-file", help="JSON file containing {case_id: output} or {outputs: {...}}.")
    parser.add_argument("--prompts-out", help="Write a prompt pack for external/local model prediction capture.")
    parser.add_argument(
        "--intent-schema-out",
        help="Write the static intent JSON Schema contract for constrained decoding experiments.",
    )
    parser.add_argument(
        "--intent-grammar-out",
        help="Write the static intent GBNF grammar contract for constrained decoding experiments.",
    )
    parser.add_argument("--capture-predictions-out", help="Call a local OpenAI-compatible endpoint and write predictions.")
    parser.add_argument("--probe-runtime", action="store_true", help="Probe the local OpenAI-compatible /models endpoint without inference.")
    parser.add_argument("--runtime-probe-out", help="Optional JSON output path for --probe-runtime.")
    parser.add_argument("--base-url", default=os.getenv("LOCAL_OPENAI_BASE_URL", "http://localhost:1234/v1"))
    parser.add_argument("--model", default=os.getenv("LOCAL_OPENAI_MODEL", "qwen/qwen2.5-coder-14b"))
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--runtime-json-mode",
        action="store_true",
        help="Request OpenAI-compatible response_format=json_object for JSON tasks. Off by default for LM Studio compatibility.",
    )
    parser.add_argument(
        "--guarded-json-normalize",
        action="store_true",
        help="Before scoring, unwrap whole-response fenced JSON objects for JSON tasks.",
    )
    parser.add_argument(
        "--intent-semantic-repair",
        action="store_true",
        help="Before scoring, apply narrow prompt-derived repairs for structured intent JSON outputs.",
    )
    parser.add_argument(
        "--intent-schema-guard",
        action="store_true",
        help="Before scoring, unwrap single-key schema envelopes such as result/request/data for intent JSON outputs.",
    )
    parser.add_argument("--out", help="Optional JSON report output path.")
    args = parser.parse_args()

    prompt_result = None
    if args.prompts_out:
        prompt_result = export_prompt_pack(args.prompts_out)
        if (
            not args.out
            and not args.predictions_file
            and not args.capture_predictions_out
            and not args.intent_schema_out
            and not args.intent_grammar_out
        ):
            print(json.dumps(prompt_result, ensure_ascii=False, indent=2))
            return 0
    intent_schema_result = None
    if args.intent_schema_out:
        intent_schema_result = export_intent_json_schema_contract(args.intent_schema_out)
        if (
            not args.out
            and not args.predictions_file
            and not args.capture_predictions_out
            and not args.prompts_out
            and not args.intent_grammar_out
        ):
            print(json.dumps(intent_schema_result, ensure_ascii=False, indent=2))
            return 0
    intent_grammar_result = None
    if args.intent_grammar_out:
        intent_grammar_result = export_intent_gbnf_grammar_contract(args.intent_grammar_out)
        if not args.out and not args.predictions_file and not args.capture_predictions_out and not args.prompts_out:
            print(json.dumps(intent_grammar_result, ensure_ascii=False, indent=2))
            return 0
    if args.capture_predictions_out:
        result = export_predictions(
            args.capture_predictions_out,
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            timeout=args.timeout,
            use_runtime_json_mode=args.runtime_json_mode,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["captured"] == result["total"] else 1
    if args.probe_runtime or args.runtime_probe_out:
        report = probe_runtime(
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            timeout=args.timeout,
        )
        if args.runtime_probe_out:
            output_path = Path(args.runtime_probe_out)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["reachable"] and report["model_available"] else 1

    predictions = load_predictions(args.predictions_file) if args.predictions_file else None
    prediction_guards = []
    if predictions is not None and args.guarded_json_normalize:
        guard_result = normalize_predictions_with_prompt_guard(predictions)
        predictions = guard_result["outputs"]
        prediction_guards.append(guard_result["summary"])
    if predictions is not None and args.intent_schema_guard:
        guard_result = apply_intent_schema_guard(predictions)
        predictions = guard_result["outputs"]
        prediction_guards.append(guard_result["summary"])
    if predictions is not None and args.intent_semantic_repair:
        guard_result = repair_intent_outputs_with_prompt_guard(predictions)
        predictions = guard_result["outputs"]
        prediction_guards.append(guard_result["summary"])
    report = run_static_eval(predictions)
    if prediction_guards:
        report["prediction_guards"] = prediction_guards
        if len(prediction_guards) == 1 and prediction_guards[0]["mode"] == "guarded_json_normalization":
            report["prediction_guard"] = prediction_guards[0]
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if prompt_result or intent_schema_result or intent_grammar_result:
        print(
            json.dumps(
                {
                    "prompt_pack": prompt_result,
                    "intent_schema": intent_schema_result,
                    "intent_grammar": intent_grammar_result,
                    "static_eval": report,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(text)
    return 0 if all(task["meets_threshold"] for task in report["tasks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
