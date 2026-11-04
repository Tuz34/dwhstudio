"""
Data Quality Testleri
━━━━━━━━━━━━━━━━━━━━━

DataQualityManager sinifinin unit testleri.
Oracle baglantisi gerektirir (integration test).
"""

import pytest
from data_quality import (
    QualityRule,
    ValidationResult,
    default_post_load_dq_specs,
    run_post_load_dq_gate,
)


class FakeDQCursor:
    def __init__(self, connection):
        self.connection = connection
        self._row = None
        self._rows = []

    def execute(self, sql, params=None):
        sql_upper = " ".join(sql.upper().split())
        params = params or {}
        if "FROM ALL_TAB_COLUMNS" in sql_upper:
            table_key = f"{params['owner'].upper()}.{params['tname'].upper()}"
            columns = self.connection.columns.get(table_key, [])
            self._rows = [(column,) for column in columns]
            return

        table_key = self.connection.table_from_sql(sql)
        rows = self.connection.tables.get(table_key)
        if rows is None:
            raise RuntimeError("ORA-00942: table or view does not exist")

        if "GROUP BY" in sql_upper and "HAVING COUNT(*) > 1" in sql_upper:
            group_text = sql_upper.split("GROUP BY", 1)[1].split("HAVING", 1)[0]
            key_columns = [item.strip().strip('"') for item in group_text.split(",")]
            seen = {}
            for row in rows:
                key = tuple(row.get(column) for column in key_columns)
                seen[key] = seen.get(key, 0) + 1
            self._row = (sum(1 for count in seen.values() if count > 1),)
            return

        if " IS NULL" in sql_upper:
            column = sql_upper.split("WHERE", 1)[1].split("IS NULL", 1)[0].strip().strip('"')
            self._row = (sum(1 for row in rows if row.get(column) is None),)
            return

        if sql_upper.startswith("SELECT COUNT(*)"):
            self._row = (len(rows),)
            return

        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeDQConnection:
    def __init__(self, tables, columns):
        self.tables = tables
        self.columns = columns

    def cursor(self):
        return FakeDQCursor(self)

    @staticmethod
    def table_from_sql(sql):
        marker = 'FROM "'
        after = sql.split(marker, 1)[1]
        owner = after.split('"', 1)[0]
        table = after.split('."', 1)[1].split('"', 1)[0]
        return f"{owner}.{table}"


class TestQualityRule:
    """QualityRule model testleri."""

    def test_rule_creation(self):
        rule = QualityRule("TEST_RULE", "COL1", "not_null")
        assert rule.rule_name == "TEST_RULE"
        assert rule.column == "COL1"
        assert rule.rule_type == "not_null"
        assert rule.severity == "ERROR"
        assert rule.params == {}

    def test_rule_with_params(self):
        rule = QualityRule("RANGE_RULE", "AGE", "range", {"min": 0, "max": 150}, "WARNING")
        assert rule.params["min"] == 0
        assert rule.severity == "WARNING"


class TestValidationResult:
    """ValidationResult model testleri."""

    def test_new_result_is_passed(self):
        result = ValidationResult("DWH", "SALES")
        assert result.passed is True
        assert result.violations == []

    def test_add_error_violation(self):
        result = ValidationResult("DWH", "SALES")
        result.total_rows = 100
        rule = QualityRule("NULL_CHECK", "ID", "not_null", severity="ERROR")
        result.add_violation(rule, 5)
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0]["violation_count"] == 5

    def test_add_warning_keeps_passed(self):
        result = ValidationResult("DWH", "SALES")
        result.total_rows = 100
        rule = QualityRule("DATE_CHECK", "CREATED", "date_range", severity="WARNING")
        result.add_violation(rule, 3)
        assert result.passed is True  # WARNING passed'i bozmaz

    def test_summary(self):
        result = ValidationResult("DWH", "SALES")
        result.total_rows = 1000
        result.finish()
        summary = result.summary()
        assert summary["table"] == "DWH.SALES"
        assert summary["total_rows"] == 1000
        assert summary["passed"] is True
        assert summary["errors"] == 0
        assert summary["duration_ms"] is not None


def test_post_load_dq_gate_passes_without_raw_rows():
    spec = default_post_load_dq_specs()["HR_EMPLOYEES_ODS"]
    conn = FakeDQConnection(
        tables={
            "DWH.ODS_HR_EMPLOYEES": [
                {"EMPLOYEE_ID": 100, "FIRST_NAME": "Steven", "LAST_NAME": "King", "EMAIL": "SKING", "HIRE_DATE": "2026-01-01", "JOB_ID": "AD_PRES"},
                {"EMPLOYEE_ID": 101, "FIRST_NAME": "Neena", "LAST_NAME": "Kochhar", "EMAIL": "NKOCHHAR", "HIRE_DATE": "2026-01-01", "JOB_ID": "AD_VP"},
            ]
        },
        columns={"DWH.ODS_HR_EMPLOYEES": ["EMPLOYEE_ID", "FIRST_NAME", "LAST_NAME", "EMAIL", "HIRE_DATE", "JOB_ID"]},
    )

    report = run_post_load_dq_gate([spec], conn=conn)
    result = report["results"][0]

    assert report["tool"] == "odi.post_load_dq_gate"
    assert report["status"] == "passed"
    assert report["raw_rows_included"] is False
    assert result["row_count"] == 2
    assert all(check["status"] == "passed" for check in result["checks"])
    assert "rows" not in result
    assert "sample_values" not in str(report)


def test_post_load_dq_gate_fails_on_null_and_duplicate_key_without_samples():
    spec = default_post_load_dq_specs()["HR_EMPLOYEES_ODS"]
    conn = FakeDQConnection(
        tables={
            "DWH.ODS_HR_EMPLOYEES": [
                {"EMPLOYEE_ID": 100, "FIRST_NAME": "Steven"},
                {"EMPLOYEE_ID": 100, "FIRST_NAME": "Duplicate"},
                {"EMPLOYEE_ID": None, "FIRST_NAME": "Missing key"},
            ]
        },
        columns={"DWH.ODS_HR_EMPLOYEES": ["EMPLOYEE_ID", "FIRST_NAME"]},
    )

    report = run_post_load_dq_gate([spec], conn=conn)
    checks = {check["name"]: check for check in report["results"][0]["checks"]}

    assert report["status"] == "failed"
    assert checks["not_null:EMPLOYEE_ID"]["status"] == "failed"
    assert checks["not_null:EMPLOYEE_ID"]["violation_count"] == 1
    assert checks["duplicate_key"]["status"] == "failed"
    assert checks["duplicate_key"]["violation_count"] == 1
    assert "Duplicate" not in str(report)


def test_post_load_dq_gate_reports_missing_target_without_raw_rows():
    spec = default_post_load_dq_specs()["HR_EMPLOYEES_ODS"]
    conn = FakeDQConnection(tables={}, columns={})

    report = run_post_load_dq_gate([spec], conn=conn)
    result = report["results"][0]

    assert report["status"] == "failed"
    assert result["status"] == "missing_target"
    assert result["raw_rows_included"] is False
    assert result["checks"][0]["name"] == "target_exists"
    assert result["checks"][0]["status"] == "failed"
