"""Finding #8: append and watermark advance share one transaction."""

import threading
from datetime import datetime

import pytest

import watermark_manager
from tests.test_incremental_strategy import FakeIncrementalConnection, FakeIncrementalCursor

COLUMNS = ["PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD"]
SOURCE_ROWS = [{"PROD_ID": 1, "TIME_ID": "2026-06-19"}, {"PROD_ID": 2, "TIME_ID": "2026-06-20"}]


class TxnCursor(FakeIncrementalCursor):
    rowcount = 0

    def execute(self, sql, params=None):
        upper = " ".join(sql.upper().split())
        if "FOR UPDATE" in upper or upper.startswith("LOCK TABLE"):
            self.connection.acquire_lock(table=upper.startswith("LOCK TABLE"))
            self._row = (1,) if self.connection.watermark_row_exists and "FOR UPDATE" in upper else None
            return
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
        self.lock = threading.Lock()
        self.holds_lock = False
        self.watermark_row_exists = True
        self.lock_kinds = []

    def share_with(self, other):
        """Make `other` a second session on the same database (shared committed state and lock)."""
        other.tables = self.tables
        other.lock = self.lock
        other.watermark_row_exists = self.watermark_row_exists
        return other

    def acquire_lock(self, table=False):
        self.lock_kinds.append("table" if table else "row")
        if not self.holds_lock:
            self.lock.acquire()
            self.holds_lock = True

    def release_lock(self):
        if self.holds_lock:
            self.holds_lock = False
            self.lock.release()

    def cursor(self):
        return TxnCursor(self)

    def commit(self):
        self.tables["DWH.ODS_SH_SALES"].extend(self.pending_rows)
        self.pending_rows = []
        if self.pending_watermark is not None:
            self.committed_watermark = self.pending_watermark
            self.pending_watermark = None
        self.commits += 1
        self.release_lock()

    def rollback(self):
        self.pending_rows = []
        self.pending_watermark = None
        self.rollbacks += 1
        self.release_lock()


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


@pytest.mark.parametrize("row_exists", [True, False])
def test_concurrent_requests_serialize_and_second_is_blocked(row_exists):
    first = TxnTargetConnection()
    first.watermark_row_exists = row_exists
    second = first.share_with(TxnTargetConnection())
    # Both requests passed preflight against the same pre-append state.
    stale_preflight_1 = _preflight(first)
    stale_preflight_2 = _preflight(second)
    results = {}
    started = threading.Event()

    class SlowWriter(TxnWatermarkWriter):
        def update_watermark(self, *a, **k):
            started.set()
            # Give the second request time to try (and block on) the lock.
            threading.Event().wait(0.3)
            super().update_watermark(*a, **k)

    def run_first():
        results["first"] = _run(first, SlowWriter(), preflight=stale_preflight_1)

    def run_second():
        started.wait(5)
        results["second"] = _run(second, TxnWatermarkWriter(), preflight=stale_preflight_2)

    threads = [threading.Thread(target=run_first), threading.Thread(target=run_second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert results["first"]["status"] == "executed"
    assert results["second"]["status"] == "blocked_by_rerun_guard"
    assert results["second"]["write_operations_attempted"] == []
    assert len(first.tables["DWH.ODS_SH_SALES"]) == 2  # window appended exactly once
    assert second.commits == 0 and second.pending_rows == []
    expected_kinds = ["row"] if row_exists else ["row", "table"]
    assert first.lock_kinds == expected_kinds
    assert second.lock_kinds == expected_kinds
