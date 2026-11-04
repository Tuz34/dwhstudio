from types import SimpleNamespace
from datetime import date

import pytest


def test_check_and_alert_skips_existing_unacknowledged_freshness_alert(monkeypatch):
    import sys

    import alert_manager

    monkeypatch.setitem(
        sys.modules,
        "schema_change_detector",
        SimpleNamespace(get_recent_changes=lambda limit=10: []),
    )

    class FakeCursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return [("DWH", "ODS_STALE_TARGET", 3.0)]

        def fetchone(self):
            if "ALERT_LOG" in self.sql:
                return (1,)
            return (0,)

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    manager = alert_manager.AlertManager()
    manager._get_conn = lambda: FakeConn()
    manager.create_alert = lambda **_kwargs: pytest.fail("duplicate FRESHNESS alert must not be created")

    assert manager.check_and_alert(freshness_days=2) == []


def test_check_and_alert_reads_schema_history_metadata_contract(monkeypatch):
    import sys

    import alert_manager

    monkeypatch.setitem(
        sys.modules,
        "schema_change_detector",
        SimpleNamespace(
            get_recent_changes=lambda limit=10: [
                {
                    "CHECK_DATE": date.today(),
                    "TABLE_OWNER": "DWH",
                    "TABLE_NAME": "ODS_STATUS",
                }
            ]
        ),
    )

    class EmptyCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return []

        def close(self):
            return None

    manager = alert_manager.AlertManager()
    manager._get_conn = lambda: SimpleNamespace(cursor=lambda: EmptyCursor())
    manager.has_unacknowledged = lambda *_args: False
    created = []

    def fake_create_alert(**kwargs):
        created.append(kwargs)
        return 7

    manager.create_alert = fake_create_alert

    result = manager.check_and_alert(freshness_days=2)

    assert result == [{"alert_id": 7, "type": "SCHEMA_CHANGE", "source": "DWH.ODS_STATUS"}]
    assert created[0]["table_owner"] == "DWH"
    assert created[0]["table_name"] == "ODS_STATUS"
    assert created[0]["message"] == "Schema change detected."


def test_check_and_alert_fails_closed_without_logging_backend_detail(monkeypatch, caplog):
    import sys

    import alert_manager

    monkeypatch.setitem(
        sys.modules,
        "schema_change_detector",
        SimpleNamespace(get_recent_changes=lambda limit=10: []),
    )
    closed = []

    class BrokenCursor:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("internal credential detail")

        def close(self):
            closed.append(True)

    manager = alert_manager.AlertManager()
    manager._get_conn = lambda: SimpleNamespace(cursor=lambda: BrokenCursor())

    with pytest.raises(alert_manager.AlertCheckError, match="One or more alert checks failed") as exc:
        manager.check_and_alert(freshness_days=2)

    assert closed == [True]
    assert "internal credential detail" not in str(exc.value)
    assert "internal credential detail" not in caplog.text
