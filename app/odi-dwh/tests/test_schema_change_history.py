from __future__ import annotations

import pytest


def test_recent_change_read_failure_is_not_reported_as_an_empty_history(monkeypatch):
    import schema_change_detector

    class BrokenCursor:
        description = []

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("internal credential detail")

        def close(self):
            return None

    class FakeConnection:
        def cursor(self):
            return BrokenCursor()

        def close(self):
            return None

    monkeypatch.setattr(schema_change_detector, "get_connection", FakeConnection)

    with pytest.raises(RuntimeError, match="internal credential detail"):
        schema_change_detector.get_recent_changes(limit=20)
