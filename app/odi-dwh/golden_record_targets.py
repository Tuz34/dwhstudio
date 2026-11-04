"""Controlled dev/TST target setup for golden record checks.

The routines here do not print secrets or raw rows. They prepare narrow target
tables needed by golden_record_regression.py smoke checks.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any


HR_EMPLOYEES_TARGET = "DWH.ODS_HR_EMPLOYEES"
SH_SALES_TARGET = "DWH.ODS_SH_SALES"


def hr_employees_target_ddl() -> str:
    return """
        CREATE TABLE DWH.ODS_HR_EMPLOYEES AS
        SELECT
            EMPLOYEE_ID,
            FIRST_NAME,
            LAST_NAME,
            EMAIL,
            PHONE_NUMBER,
            HIRE_DATE,
            JOB_ID,
            SALARY,
            COMMISSION_PCT,
            MANAGER_ID,
            DEPARTMENT_ID,
            SYSTIMESTAMP AS ODS_CREATED_AT,
            SYSTIMESTAMP AS ODS_UPDATED_AT,
            'HR' AS SOURCE_SYSTEM
        FROM HR.EMPLOYEES
    """


def sh_sales_target_ddl() -> str:
    return """
        CREATE TABLE DWH.ODS_SH_SALES AS
        SELECT
            PROD_ID,
            CUST_ID,
            TIME_ID,
            CHANNEL_ID,
            PROMO_ID,
            QUANTITY_SOLD,
            AMOUNT_SOLD,
            SYSTIMESTAMP AS ODS_CREATED_AT,
            SYSTIMESTAMP AS ODS_UPDATED_AT,
            'SH' AS SOURCE_SYSTEM
        FROM SH.SALES
    """


def _safe_execute(cursor: Any, sql: str, label: str, operations: list[dict[str, Any]]) -> None:
    try:
        cursor.execute(sql)
        operations.append({"label": label, "status": "executed"})
    except Exception as exc:
        text = str(exc)
        if "ORA-00942" in text and label.startswith("DROP TABLE"):
            operations.append({"label": label, "status": "already_absent"})
            return
        operations.append({"label": label, "status": "failed", "error": text.splitlines()[0][:250]})
        raise


def initialize_hr_employees_target(*, recreate: bool = True) -> dict[str, Any]:
    return _initialize_target(
        source_table="HR.EMPLOYEES",
        target_table=HR_EMPLOYEES_TARGET,
        grant_sql="GRANT SELECT ON HR.EMPLOYEES TO DWH",
        drop_sql="DROP TABLE DWH.ODS_HR_EMPLOYEES PURGE",
        create_sql=hr_employees_target_ddl(),
        create_label="CREATE TABLE DWH.ODS_HR_EMPLOYEES AS SELECT HR.EMPLOYEES",
        recreate=recreate,
    )


def initialize_sh_sales_target(*, recreate: bool = True) -> dict[str, Any]:
    return _initialize_target(
        source_table="SH.SALES",
        target_table=SH_SALES_TARGET,
        grant_sql="GRANT SELECT ON SH.SALES TO DWH",
        drop_sql="DROP TABLE DWH.ODS_SH_SALES PURGE",
        create_sql=sh_sales_target_ddl(),
        create_label="CREATE TABLE DWH.ODS_SH_SALES AS SELECT SH.SALES",
        recreate=recreate,
    )


def _initialize_target(
    *,
    source_table: str,
    target_table: str,
    grant_sql: str,
    drop_sql: str,
    create_sql: str,
    create_label: str,
    recreate: bool,
) -> dict[str, Any]:
    import oracledb
    import config

    operations: list[dict[str, Any]] = []

    sys_user = os.getenv("ORACLE_SYS_USER") or os.getenv("SETUP_ORACLE_SYS_USER") or "SYS"
    sys_password = os.getenv("ORACLE_SYS_PASSWORD") or os.getenv("SETUP_ORACLE_SYS_PASSWORD") or config.ORACLE_PASSWORD
    sys_conn = oracledb.connect(
        user=sys_user,
        password=sys_password,
        dsn=config.ORACLE_DSN,
        mode=oracledb.AUTH_MODE_SYSDBA,
    )
    try:
        cur = sys_conn.cursor()
        _safe_execute(cur, grant_sql, grant_sql, operations)
        sys_conn.commit()
    finally:
        sys_conn.close()

    target_conn = oracledb.connect(
        user=config.TARGET_ORACLE_USER,
        password=config.TARGET_ORACLE_PASSWORD,
        dsn=config.TARGET_ORACLE_DSN,
    )
    try:
        cur = target_conn.cursor()
        if recreate:
            _safe_execute(cur, drop_sql, f"DROP TABLE {target_table}", operations)
        _safe_execute(cur, create_sql, create_label, operations)
        cur.execute(f"SELECT COUNT(*) FROM {target_table}")
        row_count = int(cur.fetchone()[0])
        target_conn.commit()
    finally:
        target_conn.close()

    return {
        "status": "ready",
        "source_table": source_table,
        "target_table": target_table,
        "row_count": row_count,
        "secret_printed": False,
        "raw_rows_included": False,
        "operations": operations,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare dev/TST golden record target tables.")
    parser.add_argument("--target", choices=["HR.EMPLOYEES", "SH.SALES"], default="HR.EMPLOYEES")
    parser.add_argument("--no-recreate", action="store_true")
    args = parser.parse_args()
    if args.target == "HR.EMPLOYEES":
        result = initialize_hr_employees_target(recreate=not args.no_recreate)
    elif args.target == "SH.SALES":
        result = initialize_sh_sales_target(recreate=not args.no_recreate)
    else:
        raise ValueError(args.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
