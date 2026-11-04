import pytest
import threading
import time

import db
from exceptions import DatabaseQueryError


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("CREATE TABLE DWH.T1 (ID NUMBER)", True),
        ("  /* reviewed setup */ ALTER TABLE DWH.T1 ADD C1 NUMBER", True),
        ("-- reviewed setup\nGRANT SELECT ON DWH.T1 TO APP_USER", True),
        ("UPDATE DWH.T1 SET NOTE = 'CREATE TABLE text only'", False),
        ("/* CREATE TABLE DWH.BAD */ UPDATE DWH.T1 SET ID = ID", False),
        ("-- ALTER TABLE DWH.BAD\nDELETE FROM DWH.T1 WHERE 1 = 0", False),
        ("BEGIN EXECUTE IMMEDIATE 'CREATE TABLE DWH.T2 (ID NUMBER)'; END;", False),
    ],
)
def test_ddl_connection_requires_leading_ddl_statement(sql, expected):
    assert db._requires_ddl_connection(sql) is expected


def test_connection_pool_initialization_is_serialized(monkeypatch):
    created = []
    start = threading.Barrier(8)

    class FakePool:
        pass

    def create_pool(**_kwargs):
        time.sleep(0.01)
        pool = FakePool()
        created.append(pool)
        return pool

    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "ORACLE_USER", "APP_USER")
    monkeypatch.setattr(db, "ORACLE_PASSWORD", "configured-outside-test")
    monkeypatch.setattr(db.oracledb, "create_pool", create_pool)
    results = []

    def get_pool():
        start.wait()
        results.append(db._get_pool())

    threads = [threading.Thread(target=get_pool) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(created) == 1
    assert all(pool is created[0] for pool in results)


def test_execute_dml_closes_connection_when_cursor_creation_fails(monkeypatch):
    class BrokenConnection:
        def __init__(self):
            self.closed = False
            self.rolled_back = False

        def cursor(self):
            raise RuntimeError("synthetic cursor failure")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(db, "get_connection", lambda **_kwargs: connection)

    with pytest.raises(DatabaseQueryError, match="Database write failed") as captured:
        db.execute_dml("UPDATE DWH.T1 SET ID = ID")

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert connection.rolled_back is True
    assert connection.closed is True
