"""Offline SQLite stand-in for the ODI-81 SCD2 acceptance tests."""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal

import odi81_scd2_fixtures as fixtures

OPEN_ENDED = "2400-01-01"
_TIMESTAMP = "2024-06-01T00:00:00"
_SOURCE_SEED = ((101, "FIXTURE_A", 10.0), (102, "FIXTURE_B", 20.0), (103, "FIXTURE_C", 30.0))
_SOURCE_CHANGED = (
    (101, "FIXTURE_A_UPDATED", 10.0),
    (102, "FIXTURE_B", 20.0),
    (103, "FIXTURE_C", 30.0),
    (104, "FIXTURE_D", 40.0),
)


def _audit(flag: str, start: str, end: str) -> tuple[str, str, str, str, str, str, int]:
    return (start, end, flag, _TIMESTAMP, _TIMESTAMP, "ODI_SCD2", 1)


def scd_rows(flow: str, state: str) -> tuple[tuple, ...]:
    """Synthetic SCD2 versions. Values stay in the test harness, not in reports."""
    if state == "empty":
        return ()
    if state == "seed":
        return tuple(
            (index, key, text, amount, *_audit("1", "2020-01-01", OPEN_ENDED))
            for index, (key, text, amount) in enumerate(_SOURCE_SEED, start=1)
        )
    if state != "changed":
        raise ValueError(state)
    historical = (1, 101, "FIXTURE_A", 10.0, *_audit("0", "2020-01-01", "2024-06-01"))
    current = (
        (2, 102, "FIXTURE_B", 20.0, *_audit("1", "2020-01-01", OPEN_ENDED)),
        (3, 103, "FIXTURE_C", 30.0, *_audit("1", "2020-01-01", OPEN_ENDED)),
        (4, 101, "FIXTURE_A_UPDATED", 10.0, *_audit("1", "2024-06-01", OPEN_ENDED)),
        (5, 104, "FIXTURE_D", 40.0, *_audit("1", "2024-06-01", OPEN_ENDED)),
    )
    return (historical, *current)


class Database:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("CREATE TABLE DUAL (X INTEGER)")
        self.db.execute("INSERT INTO DUAL VALUES (1)")
        self.db.execute("CREATE TABLE ALL_OBJECTS (OWNER TEXT, OBJECT_NAME TEXT, OBJECT_TYPE TEXT)")
        self.db.execute("CREATE TABLE ALL_TABLES (OWNER TEXT, TABLE_NAME TEXT, TEMPORARY TEXT)")
        self.db.execute(
            "CREATE TABLE ALL_TAB_COLUMNS (OWNER TEXT, TABLE_NAME TEXT, COLUMN_ID INTEGER, "
            "COLUMN_NAME TEXT, DATA_TYPE TEXT, DATA_LENGTH INTEGER, DATA_PRECISION INTEGER, "
            "DATA_SCALE INTEGER, CHAR_USED TEXT, CHAR_LENGTH INTEGER, NULLABLE TEXT)"
        )
        self.db.execute(
            "CREATE TABLE ALL_CONSTRAINTS (OWNER TEXT, TABLE_NAME TEXT, CONSTRAINT_NAME TEXT, "
            "CONSTRAINT_TYPE TEXT, STATUS TEXT, VALIDATED TEXT)"
        )
        self.db.execute(
            "CREATE TABLE ALL_CONS_COLUMNS (OWNER TEXT, CONSTRAINT_NAME TEXT, COLUMN_NAME TEXT, POSITION INTEGER)"
        )
        self.db.execute("CREATE TABLE ALL_TRIGGERS (TABLE_OWNER TEXT, TABLE_NAME TEXT, STATUS TEXT)")
        self.events: list[tuple[str, object]] = []
        self.connections: list[Connection] = []
        self.select_any = True
        self.reader_allowed = True
        self.fail_sql: str | None = None
        self.fail_commit = False
        self.fail_cleanup: str | None = None
        self.fail_cleanup_role: str | None = None
        self.interrupt_role = "writer"
        self.bad_count: object = None

    def factory(self, role: str, session_user: str | None = None):
        def open_connection() -> Connection:
            self.events.append((role, "connect"))
            user = session_user or {"dm": "DM", "dm_reader": "DM", "source": "APP"}.get(role, "DWH")
            connection = Connection(self, role, user)
            self.connections.append(connection)
            return connection
        return open_connection

    def register_table(self, owner: str, name: str, columns: tuple[str, ...], pk_column: str | None) -> None:
        quoted = ",".join(f'"{column}"' for column in columns)
        self.db.execute(f'CREATE TABLE "{name}" ({quoted})')
        self.db.execute(
            "INSERT INTO ALL_OBJECTS (OWNER, OBJECT_NAME, OBJECT_TYPE) VALUES (?,?,?)",
            (owner, name, "TABLE"),
        )
        self.db.execute(
            "INSERT INTO ALL_TABLES (OWNER, TABLE_NAME, TEMPORARY) VALUES (?,?,?)",
            (owner, name, "N"),
        )
        for row in fixtures.catalog_rows(name):
            self.db.execute(
                "INSERT INTO ALL_TAB_COLUMNS (OWNER, TABLE_NAME, COLUMN_ID, COLUMN_NAME, DATA_TYPE, "
                "DATA_LENGTH, DATA_PRECISION, DATA_SCALE, CHAR_USED, CHAR_LENGTH, NULLABLE) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner, name, *row),
            )
        if pk_column is not None:
            constraint = f"PK_{name}"[:30]
            self.db.execute(
                "INSERT INTO ALL_CONSTRAINTS VALUES (?,?,?,?,?,?)",
                (owner, name, constraint, "P", "ENABLED", "VALIDATED"),
            )
            self.db.execute(
                "INSERT INTO ALL_CONS_COLUMNS VALUES (?,?,?,?)",
                (owner, constraint, pk_column, 1),
            )

    def insert_rows(self, name: str, columns: tuple[str, ...], rows: tuple[tuple, ...]) -> None:
        if not rows:
            return
        quoted = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        self.db.executemany(f'INSERT INTO "{name}" ({quoted}) VALUES ({placeholders})', rows)

    def install_source(self, flow: str, state: str) -> None:
        spec = fixtures.FLOWS[flow]
        columns = fixtures.BUSINESS
        self.register_table(spec["source_owner"], spec["source_table"], columns, "ENTITY_ID")
        rows = {"seed": _SOURCE_SEED, "changed": _SOURCE_CHANGED, "empty": ()}[state]
        self.insert_rows(spec["source_table"], columns, rows)

    def install_target(self, flow: str, state: str) -> None:
        spec = fixtures.FLOWS[flow]
        name = spec["target_table"]
        columns = fixtures.sqlite_columns(name)
        if self.table_exists(name):
            self.db.execute(f'DELETE FROM "{name}"')
        else:
            self.register_table(spec["target_owner"], name, columns, fixtures.SK[flow])
        self.insert_rows(name, columns, scd_rows(flow, state))

    def install_backup(self, flow: str, kind: str, run: int, state: str) -> None:
        spec = fixtures.FLOWS[flow]
        owner = spec["source_owner"] if kind == "source" else spec["target_owner"]
        name = fixtures.backup_name(flow, kind, run)
        columns = fixtures.sqlite_columns(name)
        self.register_table(owner, name, columns, None)
        if kind == "source":
            rows = {"seed": _SOURCE_SEED, "changed": _SOURCE_CHANGED, "empty": ()}[state]
        else:
            rows = scd_rows(flow, state)
        self.insert_rows(name, columns, rows)

    def table_exists(self, name: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]

    def close(self) -> None:
        self.db.close()


class Connection:
    def __init__(self, database: Database, role: str, session_user: str) -> None:
        self.database = database
        self.role = role
        self.session_user = session_user
        self.autocommit = False
        self.snapshot = database.db.serialize()
        self.dirty = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self) -> None:
        self.database.events.append((self.role, "commit"))
        self.commits += 1
        if self.database.fail_commit:
            raise RuntimeError("LEAK password=hidden")
        self.snapshot = self.database.db.serialize()
        self.dirty = False

    def rollback(self) -> None:
        database = self.database
        database.events.append((self.role, "rollback"))
        self.rollbacks += 1
        if database.fail_cleanup == "interrupt" and self.role == database.interrupt_role:
            raise KeyboardInterrupt()
        if database.fail_cleanup == "rollback" and self._cleanup_role():
            raise RuntimeError("LEAK")
        if self.dirty:
            database.db.deserialize(self.snapshot)
            self.dirty = False

    def close(self) -> None:
        self.database.events.append((self.role, "close"))
        self.closed = True
        if self.database.fail_cleanup == "connection" and self._cleanup_role():
            raise RuntimeError("LEAK")

    def _cleanup_role(self) -> bool:
        role = self.database.fail_cleanup_role
        return role is None or role == self.role


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.result: tuple | None = None
        self.rowcount = -1

    def execute(self, sql: str, params: dict | None = None) -> None:
        database = self.connection.database
        role = self.connection.role
        params = dict(params or {})
        database.events.append((role, sql))
        if database.fail_sql and database.fail_sql in sql:
            raise RuntimeError("LEAK password=hidden FIXTURE_A 101 SELECT")
        if any(token in sql for token in ("DROP ", "TRUNCATE", "ALTER ", "ODI68")):
            raise AssertionError(sql)
        if sql == "SET TRANSACTION READ ONLY" or sql.startswith("LOCK TABLE"):
            self.result = None
            self.rowcount = -1
            return
        if "SYS_CONTEXT" in sql or "SESSION_PRIVS" in sql:
            self.result = (self._identity(sql, params),)
            return
        if sql.startswith("CREATE TABLE"):
            self._create(sql)
            return
        translated = sql.replace('"DWH".', "").replace('"DM".', "").replace(" MINUS ", " EXCEPT ")
        translated = translated.replace("DATE '2400-01-01'", "'2400-01-01'")
        values = {key: float(value) if isinstance(value, Decimal) else value for key, value in params.items()}
        if role == "source" and not database.reader_allowed and "ALL_" not in sql and "DUAL" not in sql:
            raise RuntimeError("LEAK ORA access denied")
        cursor = database.db.execute(translated, values)
        self.result = cursor.fetchone() if cursor.description else None
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            self.rowcount = database.db.execute("SELECT changes()").fetchone()[0]
            self.connection.dirty = True
        else:
            self.rowcount = cursor.rowcount
        if database.bad_count is not None and sql.startswith("SELECT COUNT"):
            self.result = (database.bad_count,)

    def _identity(self, sql: str, params: dict) -> int:
        if "owner" in params:
            expected = params["owner"]
        elif "='DM'" in sql:
            expected = "DM"
        else:
            expected = "DWH"
        user_ok = self.connection.session_user == expected
        if "SESSION_PRIVS" in sql:
            return int(user_ok or self.connection.database.select_any)
        return int(user_ok)

    def _create(self, sql: str) -> None:
        match = re.search(r'CREATE TABLE "([A-Z0-9_]+)"\."([A-Z0-9_]+)"', sql)
        if match is None:
            raise AssertionError("unparsed ddl")
        owner, name = match.group(1), match.group(2)
        pk_match = re.search(r'PRIMARY KEY\s*\(([A-Z0-9_]+)\)', sql)
        database = self.connection.database
        database.register_table(
            owner, name, fixtures.sqlite_columns(name), pk_match.group(1) if pk_match else None,
        )
        self.connection.snapshot = database.db.serialize()
        self.connection.dirty = False
        self.result = None
        self.rowcount = -1

    def fetchone(self) -> tuple | None:
        return self.result

    def close(self) -> None:
        database = self.connection.database
        database.events.append((self.connection.role, "cursor_close"))
        if database.fail_cleanup == "cursor" and self.connection._cleanup_role():
            raise RuntimeError("LEAK")
