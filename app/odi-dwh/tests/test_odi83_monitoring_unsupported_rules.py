"""ODI-83: a DQ score must not look healthy when rules could not be evaluated."""
import json
import sys
import types

import pytest

import monitoring_mcp


def _install_fake_manager(monkeypatch, summary_by_table):
    class FakeResult:
        def __init__(self, table):
            self._table = table

        def summary(self):
            return summary_by_table[self._table]

    class FakeManager:
        closed = False

        def validate_table(self, _owner, table):
            return FakeResult(table)

        def close(self):
            FakeManager.closed = True

    fake_module = types.ModuleType("data_quality")
    fake_module.DataQualityManager = FakeManager
    monkeypatch.setitem(sys.modules, "data_quality", fake_module)
    return FakeManager


def _summary(*, total=100, violation_count=0, unsupported=()):
    return {
        "total_rows": total,
        "errors": 1 if violation_count else 0,
        "warnings": 0,
        "violations": [{"violation_count": violation_count}] if violation_count else [],
        "unsupported_rules": list(unsupported),
    }


UNSUPPORTED = {
    "rule_name": "R1",
    "column": "AMOUNT",
    "rule_type": "made_up",
    "reason": "secret-marker free-form reason",
}


def _score(monkeypatch, summary):
    manager = _install_fake_manager(monkeypatch, {"T": summary})
    rows = json.loads(monitoring_mcp.mon_dq_score("DWH", "T"))
    assert manager.closed is True
    assert len(rows) == 1
    return rows[0]


def test_clean_table_with_unsupported_rule_is_not_green(monkeypatch):
    row = _score(monkeypatch, _summary(unsupported=[UNSUPPORTED]))
    assert row["status"] == "UNKNOWN"
    assert row["score"] is None
    assert row["unsupported_rule_count"] == 1
    assert row["unsupported_rules"] == [
        {"rule_name": "R1", "column": "AMOUNT", "rule_type": "made_up"}
    ]
    assert "secret-marker" not in json.dumps(row)


def test_yellow_table_with_unsupported_rule_is_not_presented_as_measured(monkeypatch):
    row = _score(monkeypatch, _summary(violation_count=10, unsupported=[UNSUPPORTED]))
    assert row["status"] == "UNKNOWN"
    assert row["score"] is None
    assert row["violations"] == 10


def test_red_table_with_unsupported_rule_keeps_measured_failure(monkeypatch):
    row = _score(monkeypatch, _summary(violation_count=50, unsupported=[UNSUPPORTED]))
    assert row["status"] == "RED"
    assert row["score"] == 50.0
    assert row["unsupported_rule_count"] == 1


@pytest.mark.parametrize(
    ("violation_count", "score", "status"),
    [(0, 100.0, "GREEN"), (10, 90.0, "YELLOW"), (50, 50.0, "RED")],
)
def test_fully_evaluated_table_keeps_existing_score(monkeypatch, violation_count, score, status):
    row = _score(monkeypatch, _summary(violation_count=violation_count))
    assert (row["score"], row["status"]) == (score, status)
    assert row["unsupported_rule_count"] == 0
    assert row["unsupported_rules"] == []


def test_empty_table_with_unsupported_rule_is_not_green(monkeypatch):
    row = _score(monkeypatch, _summary(total=0, unsupported=[UNSUPPORTED]))
    assert row["status"] == "UNKNOWN"
    assert row["score"] is None
