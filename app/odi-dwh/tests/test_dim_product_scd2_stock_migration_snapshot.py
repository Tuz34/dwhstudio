"""Row-level migration tests.

Counts are computed from stored rows on each statement. The work transaction
sees its own uncommitted update. Another session sees committed rows only, and
LOCK TABLE ... NOWAIT rejects that session while the lock is held. This is a
behavioral stand-in for those rules, not an Oracle server.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import oracledb
import pytest

from dim_product_scd2_candidate_preflight import DIM_PRODUCT_TRACKED_COLUMNS
from dim_product_scd2_stock_contract import EXPECTED_COLUMN_COUNT, EXPECTED_ROW_COUNT
from dim_product_scd2_stock_migration import (
    EXPECTED_NOT_NULL_COUNT,
    MIGRATION_APPROVAL_SCOPE,
    PASSING_GUARD_COUNTS,
    RESTORE_APPROVAL_SCOPE,
    classify_dim_product_stock_migration,
    collect_dim_product_stock_migration_evidence,
    execute_dim_product_stock_migration,
    execute_dim_product_stock_migration_restore,
    expected_column_metadata_rows,
)

METADATA = expected_column_metadata_rows()
COLUMN_NAMES = tuple(str(row[0]) for row in METADATA)
PRESERVED = (
    "PROD_ID",
    "EFFECTIVE_FROM",
    *DIM_PRODUCT_TRACKED_COLUMNS,
    "INSERT_DATE",
    "UPDATE_DATE",
    "SOURCE_SYSTEM",
    "ETL_BATCH_ID",
)
OPEN_9999 = datetime(9999, 12, 31)
OPEN_2400 = datetime(2400, 1, 1)
START = datetime(2019, 1, 1)
CLOSED = datetime(2020, 6, 1, 15, 30)
FUTURE_CLOSED = datetime(2500, 1, 1)
SENTINEL_WITH_TIME = datetime(9999, 12, 31, 10, 0)
FUTURE_START = datetime(2450, 1, 1)
AUDIT = datetime(2024, 5, 1, 8, 0)
PAYLOAD = "seed"
_MARKER = re.compile(r"/\* odi87:([a-z0-9_]+) \*/")
MIGRATION_SOURCE = Path(inspect.getfile(execute_dim_product_stock_migration)).read_text(encoding="utf-8")


def make_row(
    sk: int,
    *,
    flag: str = "Y",
    start: datetime = START,
    end: datetime = OPEN_9999,
    payload: str = PAYLOAD,
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for meta in METADATA:
        name = str(meta[0])
        if name == "PRODUCT_SK":
            row[name] = sk
        elif name == "PROD_ID":
            row[name] = sk
        elif name == "CURRENT_FLAG":
            row[name] = flag
        elif name == "EFFECTIVE_FROM":
            row[name] = start
        elif name == "EFFECTIVE_TO":
            row[name] = end
        elif name == "PROD_NAME":
            row[name] = payload
        elif name == "SOURCE_SYSTEM":
            row[name] = "SH"
        elif name == "ETL_BATCH_ID":
            row[name] = 7
        elif name in {"INSERT_DATE", "UPDATE_DATE"}:
            row[name] = AUDIT
        elif meta[1] in {"DATE", "TIMESTAMP(6)"}:
            row[name] = AUDIT
        elif meta[1] == "NUMBER":
            row[name] = 1
        else:
            row[name] = "v"
    return row


def plain_rows() -> list[dict[str, Any]]:
    return [make_row(sk) for sk in range(1, EXPECTED_ROW_COUNT + 1)]


def legacy_with_closed() -> list[dict[str, Any]]:
    rows = plain_rows()
    rows[-1]["CURRENT_FLAG"] = "N"
    rows[-1]["EFFECTIVE_TO"] = CLOSED
    rows[-1]["PROD_NAME"] = "closed-row"
    return rows


def as_stock(row: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(row)
    if migrated["CURRENT_FLAG"] == "Y":
        migrated["CURRENT_FLAG"] = "1"
    elif migrated["CURRENT_FLAG"] == "N":
        migrated["CURRENT_FLAG"] = "0"
    if migrated["EFFECTIVE_TO"] == OPEN_9999:
        migrated["EFFECTIVE_TO"] = OPEN_2400
    return migrated


def clone_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deepcopy(row) for row in rows]


def stock_with_legacy_backup() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    legacy = plain_rows()
    return [as_stock(row) for row in legacy], clone_rows(legacy)


def _signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[name] for name in COLUMN_NAMES)


def _minus(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> int:
    right_set = {_signature(row) for row in right}
    return len({_signature(row) for row in left} - right_set)


def _duplicate_groups(values: list[Any]) -> int:
    return sum(1 for count in Counter(values).values() if count > 1)


def _join_count(target: list[dict[str, Any]], backup: list[dict[str, Any]], predicate) -> int:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in backup:
        grouped.setdefault(row["PRODUCT_SK"], []).append(row)
    return sum(
        1
        for row in target
        for other in grouped.get(row["PRODUCT_SK"], [])
        if predicate(row, other)
    )


def _metrics(target: list[dict[str, Any]], backup: list[dict[str, Any]] | None, world: World) -> dict[str, int]:
    def n(predicate) -> int:
        return sum(1 for row in target if predicate(row))

    values = {
        "target_row_count": len(target),
        "legacy_current_flag_count": n(lambda row: row["CURRENT_FLAG"] == "Y"),
        "legacy_historical_flag_count": n(lambda row: row["CURRENT_FLAG"] == "N"),
        "stock_current_flag_count": n(lambda row: row["CURRENT_FLAG"] == "1"),
        "stock_historical_flag_count": n(lambda row: row["CURRENT_FLAG"] == "0"),
        "null_flag_count": n(lambda row: row["CURRENT_FLAG"] is None),
        "other_flag_count": n(
            lambda row: row["CURRENT_FLAG"] is not None and row["CURRENT_FLAG"] not in {"Y", "N", "1", "0"}
        ),
        "legacy_open_ended_count": n(lambda row: row["EFFECTIVE_TO"] == OPEN_9999),
        "stock_open_ended_count": n(lambda row: row["EFFECTIVE_TO"] == OPEN_2400),
        "current_legacy_open_mismatch_count": n(
            lambda row: row["CURRENT_FLAG"] == "Y" and row["EFFECTIVE_TO"] != OPEN_9999
        ),
        "current_stock_open_mismatch_count": n(
            lambda row: row["CURRENT_FLAG"] == "1" and row["EFFECTIVE_TO"] != OPEN_2400
        ),
        "historical_open_sentinel_count": n(
            lambda row: row["CURRENT_FLAG"] in {"N", "0"}
            and row["EFFECTIVE_TO"] in {OPEN_9999, OPEN_2400}
        ),
        "historical_effective_to_on_or_after_stock_open_count": n(
            lambda row: row["CURRENT_FLAG"] in {"N", "0"} and row["EFFECTIVE_TO"] >= OPEN_2400
        ),
        "current_effective_from_after_stock_open_count": n(
            lambda row: row["CURRENT_FLAG"] in {"Y", "1"} and row["EFFECTIVE_FROM"] > OPEN_2400
        ),
        "duplicate_product_sk_groups": _duplicate_groups([row["PRODUCT_SK"] for row in target]),
        "duplicate_legacy_current_groups": _duplicate_groups(
            [row["PROD_ID"] for row in target if row["CURRENT_FLAG"] == "Y"]
        ),
        "duplicate_stock_current_groups": _duplicate_groups(
            [row["PROD_ID"] for row in target if row["CURRENT_FLAG"] == "1"]
        ),
        "invalid_date_order_count": n(lambda row: row["EFFECTIVE_FROM"] > row["EFFECTIVE_TO"]),
        "null_surrogate_key_count": n(lambda row: row["PRODUCT_SK"] is None),
        "null_natural_key_count": n(lambda row: row["PROD_ID"] is None),
        "null_effective_from_count": n(lambda row: row["EFFECTIVE_FROM"] is None),
        "sequence_exists": world.sequence_exists,
        "ods_row_count": world.ods_row_count,
    }
    if backup is None:
        values["backup_exists"] = 0
        for name in (
            "backup_column_count",
            "backup_row_count",
            "target_minus_backup_count",
            "backup_minus_target_count",
            "backup_legacy_current_flag_count",
            "backup_legacy_historical_flag_count",
            "backup_legacy_open_ended_count",
            "backup_stock_flag_count",
            "preserved_payload_mismatch_count",
            "flag_mapping_mismatch_count",
            "date_mapping_mismatch_count",
            "sk_only_in_target_count",
            "sk_only_in_backup_count",
        ):
            values[name] = 0
        return values
    values.update(
        {
            "backup_exists": 1,
            "backup_column_count": EXPECTED_COLUMN_COUNT,
            "backup_row_count": len(backup),
            "target_minus_backup_count": _minus(target, backup),
            "backup_minus_target_count": _minus(backup, target),
            "backup_legacy_current_flag_count": sum(row["CURRENT_FLAG"] == "Y" for row in backup),
            "backup_legacy_historical_flag_count": sum(row["CURRENT_FLAG"] == "N" for row in backup),
            "backup_legacy_open_ended_count": sum(row["EFFECTIVE_TO"] == OPEN_9999 for row in backup),
            "backup_stock_flag_count": sum(row["CURRENT_FLAG"] in {"1", "0"} for row in backup),
            "preserved_payload_mismatch_count": _join_count(
                target,
                backup,
                lambda row, other: any(row[column] != other[column] for column in PRESERVED),
            ),
            "flag_mapping_mismatch_count": _join_count(
                target,
                backup,
                lambda row, other: (
                    (other["CURRENT_FLAG"] == "Y" and row["CURRENT_FLAG"] != "1")
                    or (other["CURRENT_FLAG"] == "N" and row["CURRENT_FLAG"] != "0")
                    or other["CURRENT_FLAG"] not in {"Y", "N"}
                    or row["CURRENT_FLAG"] not in {"1", "0"}
                ),
            ),
            "date_mapping_mismatch_count": _join_count(
                target,
                backup,
                lambda row, other: (
                    (other["EFFECTIVE_TO"] == OPEN_9999 and row["EFFECTIVE_TO"] != OPEN_2400)
                    or (other["EFFECTIVE_TO"] != OPEN_9999 and row["EFFECTIVE_TO"] != other["EFFECTIVE_TO"])
                ),
            ),
            "sk_only_in_target_count": len(
                {row["PRODUCT_SK"] for row in target} - {row["PRODUCT_SK"] for row in backup}
            ),
            "sk_only_in_backup_count": len(
                {row["PRODUCT_SK"] for row in backup} - {row["PRODUCT_SK"] for row in target}
            ),
        }
    )
    return values


class World:
    def __init__(self, target: list[dict[str, Any]], backup: list[dict[str, Any]] | None = None) -> None:
        self.target = target
        self.backup = backup
        self.guards = dict(PASSING_GUARD_COUNTS)
        self.sequence_exists = 1
        self.ods_row_count = EXPECTED_ROW_COUNT
        self.locks = {"target": None, "backup": None}
        self.ctas_mode = "ok"
        self.commit_mode = "ok"
        self.fail_next_lock: str | None = None
        self.work_rollback_raises = False
        self.before: list[Callable[[str, Conn], None]] = []
        self.after: list[Callable[[str, Conn], None]] = []
        self.conns: list[Conn] = []
        self.attempts: dict[str, str] = {}
        self.next_id = 1

    def once(self, phase: str, predicate, action) -> None:
        done = False

        def hook(sql: str, conn: Conn) -> None:
            nonlocal done
            if done or not predicate(sql, conn):
                return
            done = True
            action(self, conn)

        (self.before if phase == "before" else self.after).append(hook)

    def fire(self, phase: str, sql: str, conn: Conn) -> None:
        for hook in tuple(self.before if phase == "before" else self.after):
            hook(sql, conn)

    def visible_target(self, conn: Conn | None) -> list[dict[str, Any]]:
        if conn is not None and conn.dirty_target is not None:
            return conn.dirty_target
        return self.target

    def count(self, name: str, conn: Conn) -> int:
        values = _metrics(self.visible_target(conn), self.backup, self)
        if name not in values:
            raise AssertionError(name)
        return int(values[name])

    def side_insert(self, row: dict[str, Any]) -> None:
        if self.locks.get("target") is not None:
            raise oracledb.DatabaseError("SIDE_LOCK_TOKEN")
        self.target.append(row)

    def side_update_payload(self, sk: int, payload: str) -> None:
        if self.locks.get("target") is not None:
            raise oracledb.DatabaseError("SIDE_LOCK_TOKEN")
        for row in self.target:
            if row["PRODUCT_SK"] == sk:
                row["PROD_NAME"] = payload
                return
        raise AssertionError("missing sk")

    def side_touch_backup(self, payload: str = "mutated-backup") -> None:
        if self.backup is None or self.locks.get("backup") is not None:
            raise oracledb.DatabaseError("SIDE_LOCK_TOKEN")
        self.backup[0]["PROD_NAME"] = payload

    def side_attempt(self, label: str, operation) -> None:
        try:
            operation()
        except oracledb.DatabaseError:
            self.attempts[label] = "rejected"
            return
        self.attempts[label] = "accepted"


class Conn:
    def __init__(self, world: World, *, role: str, autocommit: bool = False, hooks: bool = True) -> None:
        self.world = world
        self.role = role
        self.autocommit = autocommit
        self.hooks = hooks
        self.id = world.next_id
        world.next_id += 1
        self.statements: list[str] = []
        self.rollback_calls = 0
        self.commits = 0
        self.closed = False
        self.held: set[str] = set()
        self.dirty_target: list[dict[str, Any]] | None = None
        self.set_txn_count = 0
        self._cursor: Cursor | None = None

    def cursor(self) -> Cursor:
        if self._cursor is None:
            self._cursor = Cursor(self)
        return self._cursor

    def lock(self, table: str) -> None:
        if self.world.fail_next_lock == table:
            self.world.fail_next_lock = None
            raise oracledb.DatabaseError("LOCK_CONTENTION_TOKEN")
        holder = self.world.locks.get(table)
        if holder not in (None, self.id):
            raise oracledb.DatabaseError("LOCK_CONTENTION_TOKEN")
        self.world.locks[table] = self.id
        self.held.add(table)

    def release_locks(self) -> None:
        for table in list(self.held):
            if self.world.locks.get(table) == self.id:
                self.world.locks[table] = None
        self.held.clear()

    def _dirty_target(self) -> list[dict[str, Any]]:
        if self.dirty_target is None:
            self.dirty_target = clone_rows(self.world.target)
        return self.dirty_target

    def ctas(self) -> None:
        if self.world.ctas_mode == "refuse":
            raise oracledb.DatabaseError("CTAS_REFUSED_TOKEN")
        self.world.backup = clone_rows(self.world.visible_target(self))
        self.dirty_target = None
        self.release_locks()
        if self.world.ctas_mode == "ack_lost":
            raise oracledb.DatabaseError("CTAS_ACK_LOST_TOKEN")

    def migrate(self) -> int:
        changed = 0
        for row in self._dirty_target():
            if row["CURRENT_FLAG"] in {"Y", "N"} or row["EFFECTIVE_TO"] == OPEN_9999:
                changed += 1
                if row["CURRENT_FLAG"] == "Y":
                    row["CURRENT_FLAG"] = "1"
                elif row["CURRENT_FLAG"] == "N":
                    row["CURRENT_FLAG"] = "0"
                if row["EFFECTIVE_TO"] == OPEN_9999:
                    row["EFFECTIVE_TO"] = OPEN_2400
        return changed

    def restore(self) -> int:
        backup_by_sk = {row["PRODUCT_SK"]: row for row in (self.world.backup or [])}
        changed = 0
        for row in self._dirty_target():
            source = backup_by_sk.get(row["PRODUCT_SK"])
            if source is None:
                continue
            row["CURRENT_FLAG"] = source["CURRENT_FLAG"]
            row["EFFECTIVE_TO"] = source["EFFECTIVE_TO"]
            changed += 1
        return changed

    def _publish(self) -> None:
        if self.dirty_target is not None:
            self.world.target = self.dirty_target
            self.dirty_target = None
        self.release_locks()

    def commit(self) -> None:
        if self.role == "work" and self.world.commit_mode == "raise_before":
            raise oracledb.DatabaseError("COMMIT_BEFORE_TOKEN")
        self._publish()
        self.commits += 1
        if self.role == "work" and self.world.commit_mode == "apply_then_raise":
            raise oracledb.DatabaseError("COMMIT_AFTER_TOKEN")

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.role == "work" and self.world.work_rollback_raises and self.rollback_calls > 1:
            raise oracledb.DatabaseError("ROLLBACK_TOKEN")
        self.dirty_target = None
        self.release_locks()

    def close(self) -> None:
        self.closed = True


class Cursor:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self.rowcount = -1
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params=None) -> None:
        self.conn.statements.append(sql)
        if sql.startswith("SET TRANSACTION") and self.conn.role == "work":
            self.conn.set_txn_count += 1
        if self.conn.hooks:
            self.conn.world.fire("before", sql, self.conn)
        marker = _MARKER.search(sql)
        name = marker.group(1) if marker else None
        if sql.startswith("SET TRANSACTION"):
            self._rows = []
        elif name == "lock_target":
            self.conn.lock("target")
            self._rows = []
        elif name == "lock_backup":
            self.conn.lock("backup")
            self._rows = []
        elif name == "create_backup":
            self.conn.ctas()
            self._rows = []
        elif name == "migrate_update":
            self.rowcount = self.conn.migrate()
            self._rows = []
        elif name == "restore_update":
            self.rowcount = self.conn.restore()
            self._rows = []
        elif name in PASSING_GUARD_COUNTS:
            self._rows = [(int(self.conn.world.guards[name]),)]
        elif name == "target_metadata":
            self._rows = list(METADATA)
        elif name == "backup_metadata":
            self._rows = [] if self.conn.world.backup is None else list(METADATA)
        else:
            if name is None:
                raise AssertionError(sql)
            self._rows = [(self.conn.world.count(name, self.conn),)]
        if self.conn.hooks:
            self.conn.world.fire("after", sql, self.conn)

    def fetchone(self) -> tuple[Any, ...]:
        return self._rows[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class Factory:
    def __init__(self, world: World, *, autocommit: bool = False, fail_calls: set[int] | None = None) -> None:
        self.world = world
        self.autocommit = autocommit
        self.fail_calls = fail_calls or set()
        self.calls = 0

    def __call__(self) -> Conn:
        self.calls += 1
        if self.calls in self.fail_calls:
            raise oracledb.DatabaseError("FACTORY_TOKEN")
        conn = Conn(
            self.world,
            role="work" if self.calls == 1 else "proof",
            autocommit=self.autocommit if self.calls == 1 else False,
        )
        self.world.conns.append(conn)
        return conn


def _collect(world: World) -> dict[str, int]:
    return collect_dim_product_stock_migration_evidence(Conn(world, role="collect", hooks=False).cursor())


def run_executor(world: World, mode: str, prepare=None, **factory_kwargs):
    reviewed = _collect(world)
    if prepare is not None:
        prepare(world)
    factory = Factory(world, **factory_kwargs)
    if mode == "migrate":
        result = execute_dim_product_stock_migration(
            reviewed,
            independent_review_status="approved",
            approved=True,
            approval_scope=MIGRATION_APPROVAL_SCOPE,
            connection_factory=factory,
        )
    else:
        result = execute_dim_product_stock_migration_restore(
            reviewed,
            independent_review_status="approved",
            approved=True,
            approval_scope=RESTORE_APPROVAL_SCOPE,
            connection_factory=factory,
        )
    return result, factory, reviewed


def work(world: World) -> Conn:
    return next(conn for conn in world.conns if conn.role == "work")


def sql_text(world: World, role: str = "work") -> str:
    return "\n".join(statement for conn in world.conns if conn.role == role for statement in conn.statements)


def assert_hidden(result: dict[str, Any], token: str) -> None:
    assert token not in str(result)
    assert result["secret_printed"] is False


def test_clean_legacy_migration_locks_after_backup_and_keeps_closed_dates():
    world = World(legacy_with_closed())
    result, factory, _reviewed = run_executor(world, "migrate")
    text = sql_text(world)
    closed = next(row for row in world.target if row["PROD_NAME"] == "closed-row")
    backup_closed = next(row for row in world.backup or [] if row["PROD_NAME"] == "closed-row")
    current = [row for row in world.target if row["PROD_NAME"] != "closed-row"]

    assert result["status"] == "passed"
    assert result["committed"] is True
    assert result["rolled_back"] is False
    assert result["post_commit_verification"] == "verified"
    assert result["backup_create_attempted"] is True
    assert result["backup_may_exist"] is True
    assert result["backup_verified"] is True
    assert result["migration_backup_retained"] is True
    assert result["automatic_drop_performed"] is False
    assert factory.calls == 2
    assert work(world).commits == 1
    assert len(world.target) == EXPECTED_ROW_COUNT
    assert len(world.backup or []) == EXPECTED_ROW_COUNT
    assert len(current) == EXPECTED_ROW_COUNT - 1
    assert {row["CURRENT_FLAG"] for row in current} == {"1"}
    assert {row["EFFECTIVE_TO"] for row in current} == {OPEN_2400}
    assert {row["PROD_NAME"] for row in current} == {PAYLOAD}
    assert {row["INSERT_DATE"] for row in current} == {AUDIT}
    assert closed["CURRENT_FLAG"] == "0"
    assert closed["EFFECTIVE_TO"] == CLOSED
    assert closed["EFFECTIVE_FROM"] == START
    assert closed["INSERT_DATE"] == AUDIT
    assert closed["UPDATE_DATE"] == AUDIT
    assert closed["SOURCE_SYSTEM"] == "SH"
    assert closed["ETL_BATCH_ID"] == 7
    assert backup_closed["CURRENT_FLAG"] == "N"
    assert backup_closed["EFFECTIVE_TO"] == CLOSED
    assert backup_closed["PROD_NAME"] == "closed-row"
    assert "SERIALIZABLE" not in text.upper()
    assert "READ COMMITTED" in text.upper()
    assert "SEARCH_CONDITION" not in text
    assert "DROP" not in text.upper()
    assert text.index("odi87:enabled_target_trigger_count") < text.index("odi87:create_backup")
    assert text.index("odi87:create_backup") < text.index("odi87:lock_target")
    assert text.index("odi87:lock_target") < text.index("odi87:lock_backup")
    assert text.index("odi87:lock_backup") < text.index("odi87:migrate_update")


def test_restore_of_legacy_preimage_rewrites_only_flag_and_end_date():
    target, backup = stock_with_legacy_backup()
    world = World(target, backup)
    result, factory, _reviewed = run_executor(world, "restore")
    text = sql_text(world).upper()

    assert result["status"] == "passed"
    assert result["committed"] is True
    assert result["post_commit_verification"] == "verified"
    assert result["migration_backup_retained"] is True
    assert factory.calls == 2
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert {row["EFFECTIVE_TO"] for row in world.target} == {OPEN_9999}
    assert {row["PROD_NAME"] for row in world.target} == {PAYLOAD}
    assert {row["INSERT_DATE"] for row in world.target} == {AUDIT}
    assert {row["CURRENT_FLAG"] for row in world.backup or []} == {"Y"}
    assert "DELETE" not in text
    assert "odi87:restore_update".upper() in text
    assert "CREATE TABLE" not in text


def test_insert_before_lock_is_visible_and_blocks_migration():
    world = World(plain_rows())

    def prepare(store: World) -> None:
        store.once(
            "before",
            lambda sql, conn: sql.startswith("SET TRANSACTION") and conn.role == "work" and conn.set_txn_count == 2,
            lambda store, _conn: store.side_insert(make_row(EXPECTED_ROW_COUNT + 1)),
        )

    result, factory, _reviewed = run_executor(world, "migrate", prepare=prepare)
    text = sql_text(world)

    assert result["status"] == "blocked_stale_state"
    assert result["status"] != "passed"
    assert "odi87:migrate_update" not in text
    assert "odi87:create_backup" in text
    assert factory.calls == 1
    assert len(world.target) == EXPECTED_ROW_COUNT + 1
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert len(world.backup or []) == EXPECTED_ROW_COUNT
    assert result["backup_may_exist"] is True
    assert result["migration_backup_retained"] is True
    assert result["backup_verified"] is False


def test_insert_and_update_while_locked_are_rejected_and_migration_proceeds():
    world = World(plain_rows())

    def prepare(store: World) -> None:
        def action(store: World, _conn: Conn) -> None:
            store.side_attempt("insert", lambda: store.side_insert(make_row(EXPECTED_ROW_COUNT + 1)))
            store.side_attempt("update", lambda: store.side_update_payload(1, "raced"))

        store.once(
            "after",
            lambda sql, conn: "odi87:lock_backup" in sql and conn.role == "work",
            action,
        )

    result, _factory, _reviewed = run_executor(world, "migrate", prepare=prepare)

    assert world.attempts == {"insert": "rejected", "update": "rejected"}
    assert result["status"] == "passed"
    assert len(world.target) == EXPECTED_ROW_COUNT
    assert {row["CURRENT_FLAG"] for row in world.target} == {"1"}
    assert world.target[0]["PROD_NAME"] == PAYLOAD
    assert "SIDE_LOCK_TOKEN" not in str(result)


def test_backup_mutation_before_lock_blocks_without_update():
    world = World(plain_rows())

    def prepare(store: World) -> None:
        store.once(
            "before",
            lambda sql, conn: sql.startswith("SET TRANSACTION") and conn.role == "work" and conn.set_txn_count == 2,
            lambda store, _conn: store.side_touch_backup(),
        )

    result, _factory, _reviewed = run_executor(world, "migrate", prepare=prepare)

    assert result["status"] == "blocked_backup_mismatch"
    assert "odi87:migrate_update" not in sql_text(world)
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert (world.backup or [])[0]["PROD_NAME"] == "mutated-backup"
    assert result["migration_backup_retained"] is True
    assert result["backup_verified"] is False


def test_restore_insert_before_lock_keeps_the_extra_row():
    target, backup = stock_with_legacy_backup()
    world = World(target, backup)

    def prepare(store: World) -> None:
        store.once(
            "before",
            lambda sql, conn: sql.startswith("SET TRANSACTION") and conn.role == "work" and conn.set_txn_count == 2,
            lambda store, _conn: store.side_insert(make_row(EXPECTED_ROW_COUNT + 1, flag="1", end=OPEN_2400)),
        )

    result, _factory, _reviewed = run_executor(world, "restore", prepare=prepare)

    assert result["status"] == "blocked_stale_state"
    assert "odi87:restore_update" not in sql_text(world)
    assert len(world.target) == EXPECTED_ROW_COUNT + 1
    assert world.target[-1]["PRODUCT_SK"] == EXPECTED_ROW_COUNT + 1
    assert world.target[-1]["CURRENT_FLAG"] == "1"
    assert {row["CURRENT_FLAG"] for row in world.target[:-1]} == {"1"}


@pytest.mark.parametrize("rollback_raises", [False, True])
def test_migration_commit_ack_lost_after_apply_is_unknown(rollback_raises: bool):
    world = World(plain_rows())
    world.commit_mode = "apply_then_raise"
    world.work_rollback_raises = rollback_raises
    result, _factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "outcome_unknown"
    assert result["committed"] is None
    assert result["rolled_back"] is None
    assert result["post_commit_verification"] == "proven_post_state"
    assert result["manual_recovery_review_required"] is True
    assert work(world).commits == 1
    assert work(world).rollback_calls == 1
    assert {row["CURRENT_FLAG"] for row in world.target} == {"1"}
    assert_hidden(result, "COMMIT_AFTER_TOKEN")
    assert "ROLLBACK_TOKEN" not in str(result)


def test_restore_commit_ack_lost_after_apply_is_unknown():
    target, backup = stock_with_legacy_backup()
    world = World(target, backup)
    world.commit_mode = "apply_then_raise"
    world.work_rollback_raises = True
    result, _factory, _reviewed = run_executor(world, "restore")

    assert result["status"] == "outcome_unknown"
    assert result["committed"] is None
    assert result["rolled_back"] is None
    assert result["post_commit_verification"] == "proven_post_state"
    assert result["manual_recovery_review_required"] is True
    assert work(world).rollback_calls == 1
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert {row["EFFECTIVE_TO"] for row in world.target} == {OPEN_9999}
    assert_hidden(result, "COMMIT_AFTER_TOKEN")


def test_commit_ack_lost_before_publish_stays_unknown_and_unverified():
    world = World(plain_rows())
    world.commit_mode = "raise_before"
    result, factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "outcome_unknown"
    assert result["committed"] is None
    assert result["rolled_back"] is None
    assert result["post_commit_verification"] == "unavailable"
    assert result["manual_recovery_review_required"] is True
    assert result["backup_may_exist"] is True
    assert result["backup_verified"] is False
    assert result["migration_backup_retained"] is False
    assert factory.calls == 2
    assert work(world).commits == 0
    assert work(world).rollback_calls == 1
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert_hidden(result, "COMMIT_BEFORE_TOKEN")


def test_ctas_refused_does_not_claim_a_backup():
    world = World(plain_rows())
    world.ctas_mode = "refuse"
    result, _factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "failed"
    assert result["rolled_back"] is False
    assert result["manual_recovery_review_required"] is True
    assert result["backup_create_attempted"] is True
    assert result["backup_may_exist"] is False
    assert result["backup_verified"] is False
    assert result["migration_backup_retained"] is False
    assert world.backup is None
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert "odi87:migrate_update" not in sql_text(world)
    assert_hidden(result, "CTAS_REFUSED_TOKEN")


def test_ctas_ack_lost_verifies_the_before_image():
    world = World(plain_rows())
    world.ctas_mode = "ack_lost"
    result, _factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "failed"
    assert result["rolled_back"] is False
    assert result["manual_recovery_review_required"] is True
    assert result["backup_create_attempted"] is True
    assert result["backup_may_exist"] is True
    assert result["backup_verified"] is True
    assert result["migration_backup_retained"] is True
    assert len(world.backup or []) == EXPECTED_ROW_COUNT
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert {row["CURRENT_FLAG"] for row in world.backup or []} == {"Y"}
    assert "odi87:migrate_update" not in sql_text(world)
    assert_hidden(result, "CTAS_ACK_LOST_TOKEN")


def test_ctas_ack_lost_with_unreadable_proof_does_not_claim_retention():
    world = World(plain_rows())
    world.ctas_mode = "ack_lost"
    result, _factory, _reviewed = run_executor(world, "migrate", fail_calls={2})

    assert result["status"] == "failed"
    assert result["backup_create_attempted"] is True
    assert result["backup_may_exist"] is True
    assert result["backup_verified"] is False
    assert result["migration_backup_retained"] is False
    assert result["manual_recovery_review_required"] is True
    assert world.backup is not None
    assert_hidden(result, "FACTORY_TOKEN")


def test_factory_failure_ignores_caller_backup_evidence():
    rows = plain_rows()
    world = World(rows, clone_rows(rows))

    def prepare(store: World) -> None:
        store.backup = None

    result, factory, reviewed = run_executor(world, "migrate", prepare=prepare, fail_calls={1})

    assert reviewed["backup_exists"] == 1
    assert factory.calls == 1
    assert result["status"] == "failed"
    assert result["connection_attempted"] is True
    assert result["backup_create_attempted"] is False
    assert result["backup_may_exist"] is None
    assert result["backup_verified"] is False
    assert result["migration_backup_retained"] is False
    assert result["error_type"] == "DatabaseError"
    assert_hidden(result, "FACTORY_TOKEN")


def test_autocommit_is_refused_before_any_statement():
    rows = plain_rows()
    world = World(clone_rows(rows))
    result, factory, _reviewed = run_executor(world, "migrate", autocommit=True)

    assert result["status"] == "blocked_autocommit"
    assert result["connection_attempted"] is True
    assert factory.calls == 1
    assert work(world).autocommit is True
    assert work(world).statements == []
    assert work(world).commits == 0
    assert [row["CURRENT_FLAG"] for row in world.target] == [row["CURRENT_FLAG"] for row in rows]


def test_missing_or_raising_autocommit_fails_closed_without_a_statement():
    reviewed = _collect(World(plain_rows()))

    class Bare:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self):
            raise AssertionError("cursor opened")

        def close(self) -> None:
            self.closed = True

    class Raising:
        def __init__(self) -> None:
            self.closed = False

        @property
        def autocommit(self):
            raise oracledb.DatabaseError("AUTOCOMMIT_TOKEN")

        def cursor(self):
            raise AssertionError("cursor opened")

        def close(self) -> None:
            self.closed = True

    bare = Bare()
    missing = execute_dim_product_stock_migration(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=MIGRATION_APPROVAL_SCOPE,
        connection_factory=lambda: bare,
    )
    raising = Raising()
    raised = execute_dim_product_stock_migration(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=MIGRATION_APPROVAL_SCOPE,
        connection_factory=lambda: raising,
    )

    assert missing["status"] == "blocked_autocommit"
    assert missing["connection_attempted"] is True
    assert bare.closed is True
    assert raised["status"] == "blocked_autocommit"
    assert raised["connection_attempted"] is True
    assert raising.closed is True
    assert_hidden(raised, "AUTOCOMMIT_TOKEN")


def test_autocommit_off_rolls_back_a_preservation_failure():
    world = World(plain_rows())

    def prepare(store: World) -> None:
        store.once(
            "after",
            lambda sql, conn: "odi87:migrate_update" in sql and conn.role == "work",
            lambda _store, conn: conn.dirty_target.__setitem__(
                0, {**conn.dirty_target[0], "PROD_NAME": "changed-after-update"}
            ),
        )

    result, _factory, _reviewed = run_executor(world, "migrate", prepare=prepare)

    assert result["status"] == "failed"
    assert result["committed"] is False
    assert result["rolled_back"] is True
    assert work(world).commits == 0
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert all(row["PROD_NAME"] == PAYLOAD for row in world.target)


def test_enabled_trigger_blocks_before_backup_create():
    world = World(plain_rows())
    world.guards["enabled_target_trigger_count"] = 1
    result, factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "blocked_enabled_triggers"
    assert factory.calls == 1
    assert world.backup is None
    assert "odi87:create_backup" not in sql_text(world)
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}


def test_trigger_enabled_after_the_unlocked_check_blocks_dml():
    world = World(plain_rows())

    def prepare(store: World) -> None:
        store.once(
            "before",
            lambda sql, _conn: "odi87:create_backup" in sql,
            lambda store, _conn: store.guards.__setitem__("enabled_target_trigger_count", 1),
        )

    result, _factory, _reviewed = run_executor(world, "migrate", prepare=prepare)

    assert result["status"] == "blocked_enabled_triggers"
    assert "odi87:create_backup" in sql_text(world)
    assert "odi87:migrate_update" not in sql_text(world)
    assert world.backup is not None
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert result["manual_recovery_review_required"] is True


def test_extra_check_constraint_blocks_before_backup_create():
    world = World(plain_rows())
    world.guards["check_constraint_count"] = EXPECTED_NOT_NULL_COUNT + 1
    result, _factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "blocked_unreviewed_constraints"
    assert world.backup is None
    assert "odi87:create_backup" not in sql_text(world)
    assert "SEARCH_CONDITION" not in sql_text(world)
    assert "SEARCH_CONDITION" not in MIGRATION_SOURCE


@pytest.mark.parametrize(
    ("flag", "end", "start"),
    [
        ("N", FUTURE_CLOSED, START),
        ("N", SENTINEL_WITH_TIME, START),
        ("Y", OPEN_9999, FUTURE_START),
    ],
)
def test_out_of_range_dates_block_before_the_factory(flag: str, end: datetime, start: datetime):
    rows = plain_rows()
    rows[-1]["CURRENT_FLAG"] = flag
    rows[-1]["EFFECTIVE_TO"] = end
    rows[-1]["EFFECTIVE_FROM"] = start
    world = World(rows)
    result, factory, reviewed = run_executor(world, "migrate")

    assert classify_dim_product_stock_migration(reviewed) == "blocked_invalid_dates"
    assert result["status"] == "blocked_invalid_dates"
    assert factory.calls == 0
    assert result["connection_attempted"] is False
    assert world.backup is None
    assert rows[-1]["EFFECTIVE_TO"] == end
    assert rows[-1]["EFFECTIVE_FROM"] == start


def test_future_history_blocks_restore_before_dml():
    rows = [as_stock(make_row(sk)) for sk in range(1, EXPECTED_ROW_COUNT + 1)]
    rows[-1]["CURRENT_FLAG"] = "0"
    rows[-1]["EFFECTIVE_TO"] = FUTURE_CLOSED
    world = World(rows)
    result, factory, reviewed = run_executor(world, "restore")

    assert classify_dim_product_stock_migration(reviewed) == "blocked_invalid_dates"
    assert result["status"] == "blocked_invalid_dates"
    assert factory.calls == 0
    assert rows[-1]["EFFECTIVE_TO"] == FUTURE_CLOSED
    assert rows[-1]["CURRENT_FLAG"] == "0"


def test_fresh_proof_failure_reports_committed_unverified():
    world = World(plain_rows())
    result, _factory, _reviewed = run_executor(world, "migrate", fail_calls={2})

    assert result["status"] == "committed_unverified"
    assert result["committed"] is True
    assert result["rolled_back"] is False
    assert result["post_commit_verification"] == "unavailable"
    assert result["manual_recovery_review_required"] is True
    assert result["backup_may_exist"] is True
    assert result["backup_verified"] is False
    assert result["migration_backup_retained"] is False
    assert work(world).commits == 1
    assert work(world).rollback_calls == 1
    assert {row["CURRENT_FLAG"] for row in world.target} == {"1"}
    assert_hidden(result, "FACTORY_TOKEN")


def test_lock_contention_blocks_without_the_flag_update():
    world = World(plain_rows())
    world.fail_next_lock = "target"
    result, _factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "blocked_lock_contention"
    assert result["committed"] is False
    assert result["rolled_back"] is False
    assert result["error_type"] == "DatabaseError"
    assert result["backup_may_exist"] is True
    assert result["backup_verified"] is True
    assert result["migration_backup_retained"] is True
    assert "odi87:migrate_update" not in sql_text(world)
    assert "odi87:create_backup" in sql_text(world)
    assert {row["CURRENT_FLAG"] for row in world.target} == {"Y"}
    assert_hidden(result, "LOCK_CONTENTION_TOKEN")


def test_payload_change_after_review_is_stale_and_skips_ctas():
    rows = plain_rows()
    world = World(rows, clone_rows(rows))

    def prepare(store: World) -> None:
        store.once(
            "before",
            lambda sql, conn: sql.startswith("SET TRANSACTION") and conn.role == "work" and conn.set_txn_count == 1,
            lambda store, _conn: store.side_update_payload(1, "stale-payload"),
        )

    result, _factory, _reviewed = run_executor(world, "migrate", prepare=prepare)

    assert result["status"] == "blocked_stale_state"
    assert "odi87:create_backup" not in sql_text(world)
    assert "odi87:migrate_update" not in sql_text(world)
    assert world.target[0]["PROD_NAME"] == "stale-payload"
    assert world.target[0]["CURRENT_FLAG"] == "Y"
    assert (world.backup or [])[0]["PROD_NAME"] == PAYLOAD


def test_already_stock_without_backup_is_a_noop():
    world = World([as_stock(make_row(sk)) for sk in range(1, EXPECTED_ROW_COUNT + 1)])
    result, factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "rerun_noop"
    assert result["migration_backup_retained"] is False
    assert factory.calls == 1
    assert work(world).commits == 0
    assert "odi87:create_backup" not in sql_text(world)
    assert "odi87:migrate_update" not in sql_text(world)


def test_already_stock_with_legacy_preimage_retains_backup():
    target, backup = stock_with_legacy_backup()
    world = World(target, backup)
    result, factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "rerun_noop"
    assert result["migration_backup_retained"] is True
    assert result["backup_verified"] is True
    assert result["backup_create_attempted"] is False
    assert factory.calls == 1
    assert work(world).commits == 0
    assert {row["CURRENT_FLAG"] for row in world.target} == {"1"}


def test_stock_backup_that_is_not_the_preimage_blocks_before_the_factory():
    stock = [as_stock(make_row(sk)) for sk in range(1, EXPECTED_ROW_COUNT + 1)]
    world = World(stock, clone_rows(stock))
    result, factory, _reviewed = run_executor(world, "migrate")

    assert result["status"] == "blocked_backup_mismatch"
    assert factory.calls == 0
    assert result["connection_attempted"] is False
    assert {row["CURRENT_FLAG"] for row in world.target} == {"1"}
