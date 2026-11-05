"""Fixed post-stock backup tests with real synthetic set comparisons, no Oracle."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

import dim_product_scd2_stock_checkpoint as checkpoint
from dim_product_scd2_stock_migration import EXPECTED_COLUMNS, expected_column_metadata_rows


class World:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        names = [column["name"] for column in EXPECTED_COLUMNS]
        self.db.execute('CREATE TABLE DIM_PRODUCT (' + ','.join(f'"{name}"' for name in names) + ')')
        for number in range(72):
            values = []
            for column in EXPECTED_COLUMNS:
                name = column["name"]
                values.append(number if name in {"PRODUCT_SK", "PROD_ID"} else
                              "1" if name == "CURRENT_FLAG" else
                              "2026-01-01" if name == "EFFECTIVE_FROM" else
                              "2400-01-01" if name == "EFFECTIVE_TO" else
                              "X" if column["data_type"].startswith("VARCHAR") else 1)
            self.db.execute('INSERT INTO DIM_PRODUCT VALUES (' + ','.join('?' for _ in names) + ')', values)
        self.db.execute('CREATE TABLE ODS_SH_PRODUCTS (N)')
        self.db.executemany('INSERT INTO ODS_SH_PRODUCTS VALUES (?)', [(1,)] * 72)
        self.db.execute('CREATE TABLE ODI68_ODS_PRODUCTS_BKP (' + ','.join(f'C{i}' for i in range(24)) + ')')
        self.db.commit()
        self.log = []
        self.connections = []
        self.fail = None
        self.drift = False
        self.write_after_create = False

    def exists(self, table):
        return self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone()[0]

    def factory(self):
        connection = Connection(self)
        self.connections.append(connection)
        return connection


class Connection:
    autocommit = False
    transaction_in_progress = False

    def __init__(self, world):
        self.world = world
        self.events = []

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        self.events.append("rollback")
        if self.world.fail == "rollback":
            raise RuntimeError("PRIVATE_ROLLBACK")
        self.world.db.rollback()

    def close(self):
        self.events.append("connection_close")
        if self.world.fail == "connection_close":
            raise RuntimeError("PRIVATE_CLOSE")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=None):
        world = self.connection.world
        world.log.append(sql)
        assert not re.search(r'\b(DROP|DELETE|TRUNCATE|ALTER|INSERT|UPDATE)\b', sql)
        if sql == "SET TRANSACTION READ ONLY":
            return
        if "SYS_CONTEXT" in sql:
            self.rows = [(0 if world.fail == "owner" else 1,)]
        elif "FROM ALL_OBJECTS" in sql or "FROM ALL_TABLES" in sql:
            self.rows = [(world.exists(params["table"]),)]
        elif "FROM ALL_CONSTRAINTS" in sql:
            self.rows = [(0 if world.fail == "pk" else 1,)]
        elif "FROM ALL_TAB_COLUMNS" in sql:
            table = params["table"]
            if sql.startswith("SELECT COUNT(*)"):
                self.rows = [(24 if table == "ODI68_ODS_PRODUCTS_BKP" else 26,)]
            else:
                self.rows = []
                for ordinal, row in enumerate(expected_column_metadata_rows(), 1):
                    name, kind, length, precision, scale, nullable = row
                    used, chars = ("B", length) if kind in {"VARCHAR2", "CHAR"} else (None, 0)
                    self.rows.append((ordinal, name, kind, None, length, precision, scale, used, chars, nullable))
                if world.drift and table == checkpoint.BACKUP_TABLE:
                    self.rows[0] = (*self.rows[0][:4], 99, *self.rows[0][5:])
        else:
            actual = sql.replace('"DWH".', '').replace("MINUS", "EXCEPT")
            actual = actual.replace("DATE '2400-01-01'", "'2400-01-01'")
            if actual.startswith("CREATE TABLE"):
                assert checkpoint.BACKUP_TABLE in sql and '"DIM_PRODUCT"' in sql
                world.db.execute(actual)
                world.db.commit()  # Model Oracle DDL implicit commit, including later ack loss.
                if world.write_after_create:
                    world.db.execute("UPDATE DIM_PRODUCT SET UPDATE_DATE=9 WHERE PRODUCT_SK=0")
                    world.db.commit()
                if world.fail == "create_ack":
                    raise RuntimeError("PRIVATE_DDL_ACK")
                self.rows = []
            else:
                assert sql.startswith("SELECT COUNT(*)")
                if world.fail == "read":
                    raise RuntimeError("PRIVATE_READ")
                self.rows = world.db.execute(actual).fetchall()

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.connection.events.append("cursor_close")
        if self.connection.world.fail == "cursor_close":
            raise RuntimeError("PRIVATE_CURSOR")


@pytest.fixture
def world():
    state = World()
    yield state
    state.db.close()


def create(world, **override):
    args = dict(approved=True, approval_scope=checkpoint.CREATE_SCOPE, review_status="approved")
    return checkpoint.create_stock_checkpoint(world.factory, **{**args, **override})


@pytest.mark.parametrize("override", [{"approved": "yes"}, {"approved": 1}, {"approved": False},
                                     {"approval_scope": "odi68_dim_product_scd2_initial_create"}, {"review_status": "pending"}])
def test_authority_checks_precede_all_connections(world, override):
    assert create(world, **override)["status"] == "blocked"
    assert world.connections == [] and world.log == []


def test_create_then_retained_verify_full_snapshot(world):
    result = create(world)
    assert result["status"] == "passed" and result["created_absent_only"] is True
    assert result["side_effects"] is True and result["full_snapshot_equal"] is True
    world.log.clear()
    report = checkpoint.verify_stock_checkpoint(world.factory)
    assert world.log[0] == "SET TRANSACTION READ ONLY"
    assert report["status"] == "passed" and report["side_effects"] is False
    assert report["created_absent_only"] is False and report["full_snapshot_equal"] is True
    assert report["counts"]["backup_rows"] == 72
    assert all(connection.events == ["rollback", "cursor_close", "connection_close"] for connection in world.connections)


def test_creator_does_not_replace_an_existing_backup(world):
    assert create(world)["status"] == "passed"
    world.log.clear()
    assert create(world)["status"] == "blocked"
    assert not any(sql.startswith("CREATE") for sql in world.log)


@pytest.mark.parametrize("change", ["business", "audit", "duplicate", "metadata", "legacy", "missing_ods_backup"])
def test_retained_count_equality_is_not_enough(world, change):
    assert create(world)["status"] == "passed"
    if change == "metadata":
        world.drift = True
    elif change == "business":
        world.db.execute('UPDATE DIM_PRODUCT SET PROD_NAME=\'Y\' WHERE PRODUCT_SK=0')
    elif change == "audit":
        world.db.execute('UPDATE DIM_PRODUCT SET UPDATE_DATE=9 WHERE PRODUCT_SK=0')
    elif change == "duplicate":
        world.db.execute('UPDATE DIM_PRODUCT SET PRODUCT_SK=1 WHERE PRODUCT_SK=0')
    elif change == "legacy":
        world.db.execute("UPDATE DIM_PRODUCT SET CURRENT_FLAG='Y',EFFECTIVE_TO='9999-12-31'")
    else:
        world.db.execute('DROP TABLE ODI68_ODS_PRODUCTS_BKP')  # Synthetic test setup only.
    world.db.commit()
    report = checkpoint.verify_stock_checkpoint(world.factory)
    assert report["status"] != "passed" and report["full_snapshot_equal"] is False


@pytest.mark.parametrize("failure", ["read", "owner", "rollback", "cursor_close", "connection_close", "create_ack"])
def test_failures_never_return_a_success_proof_or_exception_text(world, failure):
    world.fail = failure
    report = create(world)
    assert report["status"] != "passed" and report["full_snapshot_equal"] is False
    assert "PRIVATE" not in json.dumps(report)
    if failure == "create_ack":
        assert world.exists(checkpoint.BACKUP_TABLE)
        assert report["side_effects"] is None and report["backup_may_exist"] is True
        assert report["outcome_unknown"] is True
    if failure.endswith("close") or failure == "rollback":
        assert world.connections[-1].events == ["rollback", "cursor_close", "connection_close"]


def test_concurrent_visible_change_after_ddl_does_not_produce_a_checkpoint(world):
    world.write_after_create = True
    report = create(world)
    assert report["status"] != "passed" and report["full_snapshot_equal"] is False
    assert report["side_effects"] is True and report["backup_may_exist"] is True
    assert world.exists(checkpoint.BACKUP_TABLE)


@pytest.mark.parametrize("change", ["duplicate", "null", "legacy", "pk"])
def test_invalid_precreate_surrogate_or_stock_state_precedes_ddl(world, change):
    if change == "pk":
        world.fail = "pk"
    else:
        update = {"duplicate": "PRODUCT_SK=1 WHERE PRODUCT_SK=0", "null": "PRODUCT_SK=NULL WHERE PRODUCT_SK=0",
                  "legacy": "CURRENT_FLAG='Y',EFFECTIVE_TO='9999-12-31'"}[change]
        world.db.execute("UPDATE DIM_PRODUCT SET " + update)
        world.db.commit()
    assert create(world)["status"] != "passed"
    assert not any(sql.startswith("CREATE") for sql in world.log)


@pytest.mark.parametrize("point", ["rollback", "cursor_close", "connection_close"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_cleanup_interrupt_still_attempts_all_releases(world, monkeypatch, point, interrupt):
    cls, method = (Cursor, "close") if point == "cursor_close" else (Connection, "rollback" if point == "rollback" else "close")
    original = getattr(cls, method)

    def fail(resource):
        original(resource)
        raise interrupt()

    monkeypatch.setattr(cls, method, fail)
    with pytest.raises(interrupt):
        create(world)
    assert world.connections[-1].events == ["rollback", "cursor_close", "connection_close"]


def test_autocommit_is_rejected_before_any_statement(world, monkeypatch):
    monkeypatch.setattr(Connection, "autocommit", True)
    report = create(world)
    assert report["status"] != "passed" and world.log == []
    assert world.connections[-1].events == ["rollback", "connection_close"]


def test_a_namespace_collision_is_not_overwritten(world):
    world.db.execute(f'CREATE VIEW "{checkpoint.BACKUP_TABLE}" AS SELECT * FROM DIM_PRODUCT')
    world.db.commit()
    assert create(world)["status"] == "blocked"
    assert not any(sql.startswith("CREATE") for sql in world.log)
