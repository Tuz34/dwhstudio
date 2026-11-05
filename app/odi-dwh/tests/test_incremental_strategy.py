import pytest
from datetime import datetime

import watermark_manager


class FakeIncrementalCursor:
    def __init__(self, connection):
        self.connection = connection
        self._row = None
        self._rows = []

    def execute(self, sql, params=None):
        sql_upper = " ".join(sql.upper().split())
        params = params or {}

        if "FROM ALL_TAB_COLUMNS" in sql_upper:
            table_key = f"{params['owner'].upper()}.{params['tname'].upper()}"
            self._rows = [(column,) for column in self.connection.columns.get(table_key, [])]
            return

        if "FROM ALL_CONSTRAINTS" in sql_upper:
            table_key = f"{params['owner'].upper()}.{params['tname'].upper()}"
            self._rows = [(column,) for column in self.connection.primary_keys.get(table_key, [])]
            return

        table_key = self.connection.table_from_sql(sql)
        rows = self.connection.tables.get(table_key)
        if rows is None:
            raise RuntimeError("ORA-00942: table or view does not exist")

        if sql_upper.startswith("SELECT COUNT(*)") and "WHERE" in sql_upper:
            column = sql_upper.split("WHERE", 1)[1].split(">", 1)[0].strip().strip('"')
            watermark = params["last_watermark"]
            self._row = (sum(1 for row in rows if row.get(column) is not None and row.get(column) > watermark),)
            return

        if sql_upper.startswith("SELECT COUNT(*)"):
            self._row = (len(rows),)
            return

        if sql_upper.startswith("SELECT MAX("):
            column = sql_upper.split("SELECT MAX(", 1)[1].split(")", 1)[0].strip().strip('"')
            values = [row.get(column) for row in rows if row.get(column) is not None]
            self._row = (max(values) if values else None,)
            return

        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeIncrementalConnection:
    def __init__(self, *, tables, columns, primary_keys=None):
        self.tables = tables
        self.columns = columns
        self.primary_keys = primary_keys or {}

    def cursor(self):
        return FakeIncrementalCursor(self)

    @staticmethod
    def table_from_sql(sql):
        marker = 'FROM "'
        after = sql.split(marker, 1)[1]
        owner = after.split('"', 1)[0]
        table = after.split('."', 1)[1].split('"', 1)[0]
        return f"{owner}.{table}"


class FakeAppendCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = connection.rowcount

    def execute(self, sql, params=None):
        sql_upper = " ".join(sql.upper().split())
        if "FOR UPDATE" in sql_upper:
            self._row = (1,)
            self.connection.locks.append("row")
            return
        if sql_upper.startswith("LOCK TABLE"):
            self.connection.locks.append("table")
            return
        if sql_upper.startswith("SELECT MAX("):
            self._row = (self.connection.target_max,)
            return
        self.connection.executed.append((sql, params or {}))

    def fetchone(self):
        return self._row

    def close(self):
        self.connection.closed_cursors += 1


class FakeAppendConnection:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.target_max = "2026-06-19"
        self.locks = []
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0

    def cursor(self):
        return FakeAppendCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeWatermarkWriter:
    def __init__(self):
        self.calls = []

    def update_watermark(self, owner, table_name, extract_date=None, extract_value=None, row_count=None,
                         conn=None, commit=True):
        self.calls.append(
            {
                "owner": owner,
                "table_name": table_name,
                "extract_date": extract_date,
                "extract_value": extract_value,
                "row_count": row_count,
            }
        )
        self.last_conn = conn
        self.last_commit = commit


class FailingWatermarkWriter:
    def update_watermark(self, owner, table_name, extract_date=None, extract_value=None, row_count=None,
                         conn=None, commit=True):
        raise RuntimeError("simulated audit write failure with internal details")


def _patch_gates(monkeypatch, *, golden="passed", dq="passed"):
    monkeypatch.setattr(
        watermark_manager,
        "run_live_regressions",
        lambda specs, **_kw: {"status": golden, "results": [{"key_differences": ["secret"]}]},
    )
    monkeypatch.setattr(
        watermark_manager,
        "run_post_load_dq_gate",
        lambda specs, **_kw: {"status": dq, "results": []},
    )


def test_incremental_preflight_reports_hr_blocker_without_raw_rows():
    specs = [watermark_manager.default_incremental_strategy_specs()["HR_EMPLOYEES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={"HR.EMPLOYEES": [{"EMPLOYEE_ID": 100}, {"EMPLOYEE_ID": 101}]},
        columns={"HR.EMPLOYEES": ["EMPLOYEE_ID", "HIRE_DATE", "FIRST_NAME"]},
        primary_keys={"HR.EMPLOYEES": ["EMPLOYEE_ID"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_HR_EMPLOYEES": [{"EMPLOYEE_ID": 100}, {"EMPLOYEE_ID": 101}]},
        columns={"DWH.ODS_HR_EMPLOYEES": ["EMPLOYEE_ID", "HIRE_DATE", "FIRST_NAME"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    result = report["results"][0]

    assert report["tool"] == "odi.incremental_strategy_preflight"
    assert report["status"] == "attention_required"
    assert report["will_run_incremental"] is False
    assert report["raw_rows_included"] is False
    assert result["strategy"] == "full_load_then_merge_by_pk"
    assert result["can_run_incremental"] is False
    assert "no_reliable_update_watermark" in result["blockers"]
    assert result["watermark_column"] is None
    assert result["source"]["row_count"] == 2
    assert result["target"]["row_count"] == 2
    assert "HIRE_DATE" in result["business_date_columns_not_used_as_watermark"]
    assert "Steven" not in str(report)
    assert "King" not in str(report)


def test_incremental_preflight_accepts_sh_time_id_append_watermark_without_raw_rows():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-18"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-19"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-18"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    result = report["results"][0]

    assert report["status"] == "passed"
    assert result["strategy"] == "incremental_append_with_watermark"
    assert result["can_run_incremental"] is True
    assert result["watermark_column"] == "TIME_ID"
    assert result["source"]["max_watermark"] == "2026-06-19"
    assert result["target"]["max_watermark"] == "2026-06-18"
    assert result["dry_run_delta"]["estimated_new_rows"] == 1
    assert result["dry_run_delta"]["predicate_template"] == '"TIME_ID" > :last_watermark'
    assert result["append_columns"] == ["AMOUNT_SOLD", "CHANNEL_ID", "CUST_ID", "PROD_ID", "PROMO_ID", "TIME_ID"]
    assert result["raw_rows_included"] is False
    assert "2026-06-18" in str(report)
    assert "PROD_ID" not in result["source"]


def test_incremental_preflight_reports_sh_append_rerun_guard_without_raw_rows():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-18"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-19"},
                {"PROD_ID": 3, "TIME_ID": "2026-06-20"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    result = report["results"][0]

    assert report["will_run_incremental"] is False
    assert result["strategy"] == "incremental_append_with_watermark"
    assert result["target_action"] == "append_new_window"
    assert result["dry_run_delta"]["window_start_exclusive"] == "2026-06-19"
    assert result["dry_run_delta"]["window_end_inclusive"] == "2026-06-20"
    assert result["dry_run_delta"]["estimated_new_rows"] == 1
    assert result["rerun_guard"] == {
        "state": "new_window_ready",
        "can_append_without_duplicate": True,
        "watermark_column": "TIME_ID",
        "protected_by": "high_water_window",
        "reason": "source high-water is greater than target high-water",
    }
    assert result["watermark_advance"] == {
        "will_advance": False,
        "candidate_value": "2026-06-20",
        "advance_after": ["load_success", "post_load_golden_record_passed", "post_load_dq_passed"],
        "advance_on_failure": False,
    }
    assert result["execution_plan"] == {
        "mode": "proposal_only",
        "status": "awaiting_explicit_approval",
        "will_execute": False,
        "requires_user_approval": True,
        "write_operations": ["append_delta_rows_to_target", "advance_watermark_after_post_load_gates"],
        "watermark_store": {
            "owner": "AUDIT_USER",
            "table": "ETL_WATERMARK",
            "key": "SH_SALES_ODS",
            "column": "TIME_ID",
            "candidate_value": "2026-06-20",
            "advance_on_failure": False,
        },
        "source_window": {
            "predicate_template": '"TIME_ID" > :last_watermark',
            "window_start_exclusive": "2026-06-19",
            "window_end_inclusive": "2026-06-20",
            "estimated_new_rows": 1,
        },
        "required_gates": [
            "explicit_user_approval",
            "rerun_guard_can_append",
            "load_success",
            "post_load_golden_record_passed",
            "post_load_dq_passed",
        ],
        "failure_policy": {
            "advance_watermark": False,
            "operator_action": "review_failure_before_retry",
        },
        "raw_rows_included": False,
    }
    assert result["raw_rows_included"] is False
    assert "PROD_ID" not in result["source"]


def test_incremental_preflight_blocks_duplicate_sh_append_window():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    rows = [
        {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
        {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
    ]
    source_conn = FakeIncrementalConnection(
        tables={"SH.SALES": rows},
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": rows},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    result = report["results"][0]

    assert result["target_action"] == "no_append_duplicate_or_empty_window"
    assert result["dry_run_delta"]["window_start_exclusive"] == "2026-06-20"
    assert result["dry_run_delta"]["window_end_inclusive"] == "2026-06-20"
    assert result["dry_run_delta"]["estimated_new_rows"] == 0
    assert result["rerun_guard"]["state"] == "duplicate_or_empty_window_blocked"
    assert result["rerun_guard"]["can_append_without_duplicate"] is False
    assert result["watermark_advance"]["will_advance"] is False
    assert result["watermark_advance"]["advance_on_failure"] is False
    assert result["execution_plan"]["status"] == "blocked_by_rerun_guard"
    assert result["execution_plan"]["will_execute"] is False
    assert result["execution_plan"]["requires_user_approval"] is True
    assert result["execution_plan"]["write_operations"] == []
    assert result["execution_plan"]["failure_policy"]["advance_watermark"] is False


def test_incremental_execution_request_blocks_without_explicit_approval():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    request = watermark_manager.build_incremental_execution_request(report["results"][0], approved=False)

    assert request == {
        "mode": "approval_gate",
        "status": "blocked_until_explicit_approval",
        "approved": False,
        "will_execute": False,
        "write_operations_attempted": [],
        "watermark_update_attempted": False,
        "requires_user_approval": True,
        "reason": "explicit user approval is required before any incremental append or watermark write",
        "raw_rows_included": False,
        "secret_printed": False,
    }


def test_incremental_execution_request_blocks_duplicate_window_even_if_approved():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    rows = [
        {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
        {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
    ]
    source_conn = FakeIncrementalConnection(
        tables={"SH.SALES": rows},
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": rows},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    request = watermark_manager.build_incremental_execution_request(report["results"][0], approved=True)

    assert request["status"] == "blocked_by_rerun_guard"
    assert request["approved"] is True
    assert request["will_execute"] is False
    assert request["write_operations_attempted"] == []
    assert request["watermark_update_attempted"] is False
    assert request["reason"] == "rerun guard is not in a new_window_ready state"


def test_incremental_execution_request_allows_executor_after_approval_and_new_window():
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )

    report = watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source_conn, target_conn=target_conn)
    request = watermark_manager.build_incremental_execution_request(report["results"][0], approved=True)

    assert request["status"] == "ready_for_executor"
    assert request["approved"] is True
    assert request["will_execute"] is False
    assert request["write_operations_attempted"] == []
    assert request["watermark_update_attempted"] is False
    assert request["reason"] == "approved request passed rerun guard and is ready for the live executor"


def test_execute_incremental_append_writes_server_side_and_advances_watermark_after_gates(monkeypatch):
    _patch_gates(monkeypatch)
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_preflight_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_write_conn = FakeAppendConnection(rowcount=1)
    watermark_writer = FakeWatermarkWriter()

    report = watermark_manager.run_incremental_strategy_preflight(
        specs,
        source_conn=source_conn,
        target_conn=target_preflight_conn,
    )
    result = watermark_manager.execute_incremental_append(
        report["results"][0],
        approved=True,
        target_conn=target_write_conn,
        source_conn=object(),
        watermark_writer=watermark_writer,
    )

    assert result["status"] == "executed"
    assert result["will_execute"] is True
    assert result["rows_appended"] == 1
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["write_operations_attempted"] == ["append_delta_rows_to_target", "advance_watermark_after_post_load_gates"]
    sql, params = target_write_conn.executed[0]
    assert 'INSERT INTO "DWH"."ODS_SH_SALES"' in sql
    assert 'SELECT "AMOUNT_SOLD", "CHANNEL_ID", "CUST_ID", "PROD_ID", "PROMO_ID", "TIME_ID" FROM "SH"."SALES"' in sql
    assert 'WHERE "TIME_ID" > :last_watermark' in sql
    assert 'AND "TIME_ID" <= :window_end' in sql
    assert params == {"last_watermark": datetime(2026, 6, 19), "window_end": datetime(2026, 6, 20)}
    assert target_write_conn.commits == 1
    assert target_write_conn.rollbacks == 0
    assert watermark_writer.last_conn is target_write_conn
    assert watermark_writer.last_commit is False
    assert watermark_writer.calls == [
        {
            "owner": "INCREMENTAL_STRATEGY",
            "table_name": "SH_SALES_ODS",
            "extract_date": datetime(2026, 6, 20),
            "extract_value": "2026-06-20",
            "row_count": 1,
        }
    ]


def test_execute_incremental_append_rolls_back_and_skips_watermark_when_post_gate_fails(monkeypatch):
    _patch_gates(monkeypatch, golden="attention_required")
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_preflight_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_write_conn = FakeAppendConnection(rowcount=1)
    watermark_writer = FakeWatermarkWriter()

    report = watermark_manager.run_incremental_strategy_preflight(
        specs,
        source_conn=source_conn,
        target_conn=target_preflight_conn,
    )
    result = watermark_manager.execute_incremental_append(
        report["results"][0],
        approved=True,
        target_conn=target_write_conn,
        source_conn=object(),
        watermark_writer=watermark_writer,
    )

    assert result["status"] == "rolled_back_post_load_gate_failed"
    assert result["will_execute"] is False
    assert result["rows_appended"] == 0
    assert result["failed_gates"] == ["post_load_golden_record_passed"]
    assert target_write_conn.commits == 0
    assert target_write_conn.rollbacks == 1
    assert watermark_writer.calls == []


def test_execute_incremental_append_rolls_back_append_when_watermark_update_fails(monkeypatch):
    _patch_gates(monkeypatch)
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    source_conn = FakeIncrementalConnection(
        tables={
            "SH.SALES": [
                {"PROD_ID": 1, "TIME_ID": "2026-06-19"},
                {"PROD_ID": 2, "TIME_ID": "2026-06-20"},
            ]
        },
        columns={"SH.SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_preflight_conn = FakeIncrementalConnection(
        tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
        columns={"DWH.ODS_SH_SALES": ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]},
    )
    target_write_conn = FakeAppendConnection(rowcount=1)

    report = watermark_manager.run_incremental_strategy_preflight(
        specs,
        source_conn=source_conn,
        target_conn=target_preflight_conn,
    )
    result = watermark_manager.execute_incremental_append(
        report["results"][0],
        approved=True,
        target_conn=target_write_conn,
        source_conn=object(),
        watermark_writer=FailingWatermarkWriter(),
    )

    assert result["status"] == "rolled_back_watermark_update_failed"
    assert result["will_execute"] is False
    assert result["rows_appended"] == 0
    assert result["watermark_update_attempted"] is True
    assert result["watermark_update_status"] == "failed"
    assert result["watermark_error_type"] == "RuntimeError"
    assert result["safe_to_retry"] is True
    assert "simulated audit write failure" not in str(result)
    assert target_write_conn.commits == 0
    assert target_write_conn.rollbacks == 1
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False


def test_incremental_strategy_rejects_unsafe_identifiers():
    spec = watermark_manager.IncrementalStrategySpec(
        name="BAD",
        source_table="HR.EMPLOYEES;DROP",
        target_table="DWH.ODS_HR_EMPLOYEES",
        candidate_watermark_columns=("TIME_ID",),
    )
    conn = FakeIncrementalConnection(tables={}, columns={})

    with pytest.raises(ValueError):
        watermark_manager.run_incremental_strategy_preflight([spec], source_conn=conn, target_conn=conn)


def test_delta_query_quotes_identifiers_and_rejects_unsafe_input():
    wm = watermark_manager.WatermarkManager()
    wm.get_watermark = lambda _owner, _table: {"last_extract_date": datetime(2026, 6, 19, 12, 30, 0)}

    sql = wm.get_delta_query("SH", "SALES", "TIME_ID")

    assert 'FROM "SH"."SALES"' in sql
    assert 'WHERE "TIME_ID" > TO_TIMESTAMP' in sql
    assert 'ORDER BY "TIME_ID"' in sql

    with pytest.raises(ValueError):
        wm.get_delta_query("SH", "SALES;DROP", "TIME_ID")
