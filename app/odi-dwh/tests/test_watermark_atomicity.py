"""Finding #8: append and watermark advance share one transaction."""

from datetime import datetime

import pytest

import watermark_manager
from tests.test_incremental_strategy import FakeIncrementalConnection, FakeIncrementalCursor

COLUMNS = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]
SOURCE_ROWS = [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}, {"PROD_ID": 2, "TIME_ID": "2026-06-20"}]


class TxnCursor(FakeIncrementalCursor):
    rowcount = 0

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("INSERT INTO"):
            if self.connection.fail_on_insert:
                raise RuntimeError("simulated insert failure")
            last = params["last_watermark"].strftime("%Y-%m-%d")
            new = [dict(r) for r in SOURCE_ROWS if r["TIME_ID"] > last]
            self.connection.pending_rows.extend(new)
            self.rowcount = len(new)
            return
        super().execute(sql, params)


class TxnTargetConnection(FakeIncrementalConnection):
    """Target with committed state (`tables`) and an uncommitted overlay."""

    def __init__(self):
        super().__init__(
            tables={"DWH.ODS_SH_SALES": [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}]},
            columns={"DWH.ODS_SH_SALES": COLUMNS},
        )
        self.pending_rows = []
        self.pending_watermark = None
        self.committed_watermark = None
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_insert = False

    def cursor(self):
        return TxnCursor(self)

    def commit(self):
        self.tables["DWH.ODS_SH_SALES"].extend(self.pending_rows)
        self.pending_rows = []
        if self.pending_watermark is not None:
            self.committed_watermark = self.pending_watermark
            self.pending_watermark = None
        self.commits += 1

    def rollback(self):
        self.pending_rows = []
        self.pending_watermark = None
        self.rollbacks += 1


class TxnWatermarkWriter:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def update_watermark(self, owner, table_name, extract_date=None, extract_value=None, row_count=None,
                         conn=None, commit=True):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated watermark failure")
        assert conn is not None and commit is False, "watermark must join the append transaction"
        conn.pending_watermark = (owner, table_name, extract_value, row_count)


def _preflight(target):
    source = FakeIncrementalConnection(tables={"SH.SALES": SOURCE_ROWS}, columns={"SH.SALES": COLUMNS})
    specs = [watermark_manager.default_incremental_strategy_specs()["SH_SALES_ODS"]]
    return watermark_manager.run_incremental_strategy_preflight(specs, source_conn=source, target_conn=target)["results"][0]


@pytest.fixture(autouse=True)
def _gates_pass(monkeypatch):
    monkeypatch.setattr(watermark_manager, "run_live_regressions", lambda specs, **_k: {"status": "passed"})
    monkeypatch.setattr(watermark_manager, "run_post_load_dq_gate", lambda specs, **_k: {"status": "passed"})


def _run(target, writer, preflight=None):
    return watermark_manager.execute_incremental_append(
        preflight or _preflight(target),
        approved=True,
        target_conn=target,
        source_conn=object(),
        watermark_writer=writer,
    )


def test_failure_after_append_before_watermark_rolls_back_both():
    target = TxnTargetConnection()
    writer = TxnWatermarkWriter(fail=True)

    result = _run(target, writer)

    assert result["status"] == "rolled_back_watermark_update_failed"
    assert target.commits == 0
    assert target.rollbacks == 1
    assert len(target.tables["DWH.ODS_SH_SALES"]) == 1
    assert target.pending_rows == []
    assert target.committed_watermark is None and target.pending_watermark is None


def test_failure_during_append_rolls_back_and_raises():
    target = TxnTargetConnection()
    target.fail_on_insert = True
    writer = TxnWatermarkWriter()

    with pytest.raises(RuntimeError):
        _run(target, writer)

    assert target.commits == 0 and target.rollbacks == 1
    assert writer.calls == 0


def test_success_commits_exactly_once_with_append_and_watermark_together():
    target = TxnTargetConnection()
    writer = TxnWatermarkWriter()

    result = _run(target, writer)

    assert result["status"] == "executed"
    assert result["rows_appended"] == 1
    assert target.commits == 1
    assert target.rollbacks == 0
    assert len(target.tables["DWH.ODS_SH_SALES"]) == 2
    assert target.committed_watermark == ("INCREMENTAL_STRATEGY", "SH_SALES_ODS", "2026-06-20", 1)


def test_rerun_after_success_appends_nothing_and_keeps_watermark():
    target = TxnTargetConnection()
    writer = TxnWatermarkWriter()
    assert _run(target, writer)["status"] == "executed"
    watermark_after_first = target.committed_watermark
    rows_after_first = len(target.tables["DWH.ODS_SH_SALES"])

    second_preflight = _preflight(target)
    result = _run(target, writer, preflight=second_preflight)

    assert result["status"] == "blocked_by_rerun_guard"
    assert result["write_operations_attempted"] == []
    assert len(target.tables["DWH.ODS_SH_SALES"]) == rows_after_first
    assert target.committed_watermark == watermark_after_first
    assert target.commits == 1
    assert writer.calls == 1


def test_update_watermark_with_caller_connection_does_not_commit():
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append(("execute", params["owner"]))

        def close(self):
            calls.append(("close",))

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            calls.append(("commit",))

    manager = watermark_manager.WatermarkManager()
    manager.update_watermark("OWN", "TBL", extract_date=datetime(2026, 6, 20), extract_value="x", row_count=1,
                             conn=Conn(), commit=False)
    assert ("commit",) not in calls
    assert manager.conn is None

    manager.update_watermark("OWN", "TBL", conn=Conn(), commit=True)
    assert ("commit",) in calls
