from decimal import Decimal

from golden_record_regression import (
    build_post_odi_gate_summary,
    build_snapshot,
    compare_snapshots,
    default_specs,
    run_live_regression,
    run_live_regressions,
)


class FakeCursor:
    def __init__(self, tables):
        self.tables = tables
        self.description = []
        self._rows = []
        self.sql = ""

    def execute(self, sql):
        self.sql = sql
        table_name = sql.split(" FROM ", 1)[1].split(" FETCH ", 1)[0].strip()
        if table_name not in self.tables:
            raise RuntimeError("ORA-00942: table or view does not exist")
        columns = [item.strip() for item in sql.split("SELECT ", 1)[1].split(" FROM ", 1)[0].split(",")]
        rows = self.tables[table_name]
        self.description = [(column,) for column in columns]
        self._rows = [tuple(row.get(column) for column in columns) for row in rows]

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, tables):
        self.tables = tables
        self.last_cursor = None

    def cursor(self):
        self.last_cursor = FakeCursor(self.tables)
        return self.last_cursor


def test_hr_employees_snapshot_matches_golden_record():
    spec = default_specs()["HR.EMPLOYEES"]
    rows = [
        {"employee_id": 101, "first_name": "Neena", "salary": Decimal("17000")},
        {"employee_id": 100, "first_name": "Steven", "salary": Decimal("24000")},
    ]

    expected = build_snapshot(rows, spec)
    actual = build_snapshot(reversed(rows), spec)
    report = compare_snapshots(expected, actual, spec)

    assert expected["row_count"] == 2
    assert expected["aggregates"]["SALARY"] == "41000"
    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])


def test_sh_sales_snapshot_allows_configured_amount_tolerance():
    spec = default_specs()["SH.SALES"]
    expected_rows = [
        {
            "prod_id": 13,
            "cust_id": 987,
            "time_id": "2026-06-01",
            "channel_id": 3,
            "promo_id": 999,
            "amount_sold": Decimal("10.00"),
            "quantity_sold": Decimal("1"),
        },
    ]
    actual_rows = [
        {
            "prod_id": 13,
            "cust_id": 987,
            "time_id": "2026-06-01",
            "channel_id": 3,
            "promo_id": 999,
            "amount_sold": Decimal("10.01"),
            "quantity_sold": Decimal("1"),
        },
    ]

    report = compare_snapshots(
        build_snapshot(expected_rows, spec),
        build_snapshot(actual_rows, spec),
        spec,
    )
    checks = {item["check"]: item for item in report["checks"]}

    assert checks["aggregate:AMOUNT_SOLD"]["passed"] is True
    assert checks["row_count"]["passed"] is True
    assert checks["checksum"]["passed"] is False
    assert checks["row_checksums"]["passed"] is False
    assert report["row_diff"]["changed"] == ["13|987|2026-06-01|3|999"]
    assert report["passed"] is False


def test_snapshot_report_identifies_missing_extra_and_changed_rows():
    spec = default_specs()["HR.EMPLOYEES"]
    expected = build_snapshot(
        [
            {"employee_id": 100, "first_name": "Steven", "salary": 24000},
            {"employee_id": 101, "first_name": "Neena", "salary": 17000},
        ],
        spec,
    )
    actual = build_snapshot(
        [
            {"employee_id": 100, "first_name": "Steven", "salary": 25000},
            {"employee_id": 102, "first_name": "Lex", "salary": 17000},
        ],
        spec,
    )

    report = compare_snapshots(expected, actual, spec)

    assert report["passed"] is False
    assert report["row_diff"] == {
        "missing": ["101"],
        "extra": ["102"],
        "changed": ["100"],
    }


def test_live_regression_uses_projection_and_redacts_rows():
    spec = default_specs()["HR.EMPLOYEES"]
    rows = [
        {"EMPLOYEE_ID": 100, "SALARY": Decimal("24000")},
        {"EMPLOYEE_ID": 101, "SALARY": Decimal("17000")},
    ]
    source_conn = FakeConnection({"HR.EMPLOYEES": rows})
    target_conn = FakeConnection({"DWH.ODS_HR_EMPLOYEES": list(reversed(rows))})

    report = run_live_regression(spec, source_conn=source_conn, target_conn=target_conn, row_limit=10)

    assert report["status"] == "passed"
    assert report["raw_rows_included"] is False
    assert "row_checksums" not in report["source_snapshot"]
    assert report["source_snapshot"]["row_count"] == 2
    assert source_conn.last_cursor.sql == "SELECT EMPLOYEE_ID, SALARY FROM HR.EMPLOYEES FETCH FIRST 10 ROWS ONLY"


def test_live_regression_reports_missing_target_without_raw_rows():
    spec = default_specs()["HR.EMPLOYEES"]
    source_conn = FakeConnection({"HR.EMPLOYEES": [{"EMPLOYEE_ID": 100, "SALARY": Decimal("24000")}]})
    target_conn = FakeConnection({})

    report = run_live_regression(spec, source_conn=source_conn, target_conn=target_conn)

    assert report["status"] == "missing_target"
    assert report["raw_rows_included"] is False
    assert report["secret_printed"] is False
    assert "ORA-00942" in report["error"]


def test_post_odi_gate_summary_reports_pass_without_raw_rows():
    spec = default_specs()["HR.EMPLOYEES"]
    rows = [
        {"EMPLOYEE_ID": 100, "SALARY": Decimal("24000")},
        {"EMPLOYEE_ID": 101, "SALARY": Decimal("17000")},
    ]
    report = run_live_regressions(
        [spec],
        source_conn=FakeConnection({"HR.EMPLOYEES": rows}),
        target_conn=FakeConnection({"DWH.ODS_HR_EMPLOYEES": list(reversed(rows))}),
    )

    summary = build_post_odi_gate_summary(report)

    assert summary["tool"] == "odi.post_run_golden_record_gate"
    assert summary["status"] == "passed"
    assert summary["raw_rows_included"] is False
    assert summary["results"][0]["row_counts"] == {"source": 2, "target": 2, "matched": True}
    assert summary["results"][0]["checksum_status"] == "passed"
    assert summary["results"][0]["aggregate_checks"][0]["status"] == "passed"


def test_post_odi_gate_summary_reports_failed_diff_without_row_keys():
    spec = default_specs()["HR.EMPLOYEES"]
    report = run_live_regressions(
        [spec],
        source_conn=FakeConnection({"HR.EMPLOYEES": [{"EMPLOYEE_ID": 100, "SALARY": Decimal("24000")}]}),
        target_conn=FakeConnection({"DWH.ODS_HR_EMPLOYEES": [{"EMPLOYEE_ID": 100, "SALARY": Decimal("25000")}]}),
    )

    summary = build_post_odi_gate_summary(report)
    result = summary["results"][0]

    assert summary["status"] == "failed"
    assert result["checksum_status"] == "failed"
    assert result["row_diff_counts"] == {"missing": 0, "extra": 0, "changed": 1}
    assert result["row_diff_sample_included"] is False
    assert "row_diff" not in result


def test_post_odi_gate_summary_reports_missing_target():
    spec = default_specs()["HR.EMPLOYEES"]
    report = run_live_regressions(
        [spec],
        source_conn=FakeConnection({"HR.EMPLOYEES": [{"EMPLOYEE_ID": 100, "SALARY": Decimal("24000")}]}),
        target_conn=FakeConnection({}),
    )

    summary = build_post_odi_gate_summary(report)
    result = summary["results"][0]

    assert summary["status"] == "failed"
    assert result["status"] == "missing_target"
    assert result["row_counts"] == {"source": None, "target": None, "matched": False}
    assert result["checksum_status"] == "not_available"
    assert result["raw_rows_included"] is False
