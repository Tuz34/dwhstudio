import pytest

from lineage_manager import LineageManager


class _OdiCursor:
    def __init__(self):
        self.query_index = 0
        self.closed = False

    def execute(self, _sql):
        self.query_index += 1

    def fetchall(self):
        if self.query_index == 1:
            return [("MAP_A", "TARGET_A")]
        return [("MAP_A", "SOURCE_A")]

    def close(self):
        self.closed = True


class _AuditCursor:
    def __init__(self, *, fail_insert):
        self.fail_insert = fail_insert
        self.executed = []
        self.closed = False

    def execute(self, sql, _params=None):
        self.executed.append(sql)

    def executemany(self, sql, rows):
        self.executed.append(sql)
        assert rows == [{"mname": "MAP_A", "stbl": "SOURCE_A", "ttbl": "TARGET_A"}]
        if self.fail_insert:
            raise RuntimeError("synthetic insert failure")

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _manager_with_connections(monkeypatch, *, fail_insert):
    manager = LineageManager()
    odi_connection = _Connection(_OdiCursor())
    audit_connection = _Connection(_AuditCursor(fail_insert=fail_insert))
    monkeypatch.setattr(manager, "_get_odi_conn", lambda: odi_connection)
    monkeypatch.setattr(manager, "_get_conn", lambda: audit_connection)
    return manager, audit_connection


def test_lineage_refresh_rolls_back_delete_when_replacement_insert_fails(monkeypatch):
    manager, audit_connection = _manager_with_connections(monkeypatch, fail_insert=True)

    with pytest.raises(RuntimeError, match="synthetic insert failure"):
        manager.populate_from_odi()

    assert audit_connection.commits == 0
    assert audit_connection.rollbacks == 1


def test_lineage_refresh_commits_delete_and_insert_once(monkeypatch):
    manager, audit_connection = _manager_with_connections(monkeypatch, fail_insert=False)

    result = manager.populate_from_odi()

    assert result == {"inserted": 1, "mappings_processed": 1}
    assert audit_connection.commits == 1
    assert audit_connection.rollbacks == 0

