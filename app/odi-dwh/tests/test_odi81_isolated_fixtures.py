"""Offline SQLite business comparisons plus strict synthetic Oracle metadata fakes."""

from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal

import pytest

import odi81_isolated_fixtures as fixture


class Database:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("CREATE TABLE DUAL (X INTEGER)")
        self.db.execute("INSERT INTO DUAL VALUES (1)")
        self.events = []
        self.connections = []
        self.reader_allowed = True
        self.reader_capable = True
        self.writer_owner = True
        self.fail_sql = None
        self.fail_commit = False
        self.commit_then_fail = False
        self.fail_cleanup = None
        self.bad_count = None
        self.bad_metadata = False
        self.bad_pk = False
        self.bad_rowcount = None
        self.autocommit = False
        self.views = set()
        self.enabled_triggers = set()

    def factory(self, role):
        def open_connection():
            self.events.append((role, "connect"))
            connection = Connection(self, role)
            self.connections.append(connection)
            return connection
        return open_connection

    def table_exists(self, name):
        return self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]

    def provision(self, flow, state="seed", *, target=True, backups=True, run=2):
        source, target_name = fixture._FLOWS[flow]
        self.create(source, fixture._BUSINESS, fixture._SEED if state == "seed" else fixture._CHANGED)
        if target:
            extra = fixture._EDW_EXTRA if flow == "F3" else fixture._ODS_EXTRA
            before = {1: (), 2: fixture._SEED, 3: fixture._CHANGED}[run]
            self.create(target_name, fixture._BUSINESS + extra, before)
            if backups:
                self.copy(target_name, f"ODI81_{flow}_T_R{run}_BKP")
        if backups:
            self.create(f"ODI81_{flow}_S_R2_BKP", fixture._BUSINESS, fixture._SEED)

    def create(self, name, columns, rows, *, types=None):
        column_types = {"ENTITY_ID": "INTEGER", "ATTR_TEXT": "TEXT", "VALUE_AMT": "NUMERIC"}
        if types:
            column_types.update(types)
        definitions = ",".join(f'"{col}" {column_types.get(col, "TEXT")}' for col in columns)
        self.db.execute(f'CREATE TABLE "{name}" ({definitions})')
        for row in rows:
            values = tuple(float(v) if isinstance(v, Decimal) else v for v in row) + (None,) * (len(columns) - 3)
            self.db.execute(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in columns)})', values)

    def copy(self, source, target):
        self.db.execute(f'CREATE TABLE "{target}" AS SELECT * FROM "{source}"')

    def close(self):
        self.db.close()


class Connection:
    def __init__(self, db, role):
        self.database, self.role = db, role
        self.autocommit = db.autocommit
        self.snapshot = db.db.serialize()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.database.events.append((self.role, "commit"))
        self.commits += 1
        if self.database.fail_commit and not self.database.commit_then_fail:
            raise RuntimeError("LEAK password=hidden")
        self.snapshot = self.database.db.serialize()
        if self.database.fail_commit:
            raise RuntimeError("LEAK password=hidden")

    def rollback(self):
        self.database.events.append((self.role, "rollback"))
        self.rollbacks += 1
        if self.database.fail_cleanup == "rollback":
            raise RuntimeError("LEAK")
        self.database.db.deserialize(self.snapshot)

    def close(self):
        self.database.events.append((self.role, "close"))
        self.closed = True
        if self.database.fail_cleanup == "connection":
            raise RuntimeError("LEAK")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.rowcount = -1

    def execute(self, sql, params=None):
        db, role = self.connection.database, self.connection.role
        params = params or {}
        db.events.append((role, sql))
        if db.fail_sql and db.fail_sql in sql:
            raise RuntimeError("LEAK password=hidden")
        if sql == "SET TRANSACTION READ ONLY" or sql.startswith("LOCK TABLE"):
            return
        if "SESSION_PRIVS" in sql:
            self.result = (int(db.reader_capable),)
        elif "SYS_CONTEXT" in sql:
            self.result = (int(db.writer_owner),)
        elif "FROM ALL_OBJECTS" in sql:
            self.result = (db.table_exists(params["name"]),)
        elif "FROM ALL_TABLES" in sql:
            self.result = (int(db.table_exists(params["name"]) and params["name"] not in db.views),)
        elif "FROM ALL_CONSTRAINTS" in sql:
            self.result = (0 if db.bad_pk else 1,)
        elif "FROM ALL_TRIGGERS" in sql:
            actual = db.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                                   (params["name"],)).fetchone()[0]
            self.result = (actual + int(params["name"] in db.enabled_triggers),)
        elif "FROM ALL_TAB_COLUMNS" in sql:
            if "MINUS" in sql:
                self.result = (int(db.bad_metadata),)
            elif "DATA_PRECISION" in sql:
                self.result = (2 if db.bad_metadata else 3,)
            else:
                columns = db.db.execute(f'PRAGMA table_info("{params["name"]}")').fetchall()
                self.result = (len(columns),)
        else:
            if role == "source" and not db.reader_allowed:
                raise RuntimeError("LEAK ORA access denied")
            translated = sql.replace('"DWH".', "").replace(" MINUS ", " EXCEPT ")
            translated = translated.replace("NUMBER(10,2)", "NUMERIC").replace("NUMBER(10)", "INTEGER")
            translated = translated.replace("VARCHAR2(40 BYTE)", "TEXT")
            # Bare NUMBER is ETL_BATCH_ID. INTEGER affinity keeps the copied batch id comparable.
            translated = translated.replace(" NUMBER", " INTEGER")
            values = {key: float(value) if isinstance(value, Decimal) else value for key, value in params.items()}
            cur = db.db.execute(translated, values)
            self.result = cur.fetchone() if cur.description else None
            self.rowcount = cur.rowcount
            if sql.startswith("CREATE TABLE"):
                # Oracle CREATE TABLE commits independently of the following seed DML.
                self.connection.snapshot = db.db.serialize()
            if sql.startswith(("INSERT", "UPDATE")) and db.bad_rowcount is not None:
                self.rowcount = db.bad_rowcount
        if db.bad_count is not None and sql.startswith("SELECT COUNT"):
            self.result = (db.bad_count,)

    def fetchone(self):
        return self.result

    def close(self):
        db = self.connection.database
        db.events.append((self.connection.role, "cursor_close"))
        if db.fail_cleanup == "cursor":
            raise RuntimeError("LEAK")


@pytest.fixture
def db():
    database = Database()
    yield database
    database.close()


def prepare(db, flow="F1", **kwargs):
    return fixture.prepare_source(flow, db.factory("writer"), db.factory("source"),
                                  approved=True, approval_scope=f"odi81_{flow.lower()}_prepare_source", **kwargs)


def mutate(db, flow="F1"):
    return fixture.mutate_source(flow, db.factory("writer"), db.factory("source"),
                                 approved=True, approval_scope=f"odi81_{flow.lower()}_mutate_source")


def verify(db, flow="F1", run=1, phase="after_run"):
    return fixture.verify_flow(flow, run, phase, db.factory("catalog"), db.factory("source"),
                               approved=True, approval_scope=f"odi81_{flow.lower()}_verify_flow")


def no_mutation(db):
    assert not any(sql.startswith(("CREATE", "INSERT", "UPDATE", "commit")) for _, sql in db.events)


@pytest.mark.parametrize("approved", [False, None, 1, "true", [], {}])
@pytest.mark.parametrize("action", ["prepare_source", "mutate_source", "verify_flow"])
def test_literal_approval_before_any_connection(db, approved, action):
    args = ["F1"] + ([1, "after_run"] if action == "verify_flow" else [])
    result = getattr(fixture, action)(*args, db.factory("writer"), db.factory("source"), approved=approved,
                                      approval_scope=f"odi81_f1_{action}")
    assert result["status"] == "blocked" and db.events == []


@pytest.mark.parametrize("flow", ["F4", "F5", "f1", "F1;DROP", "DWH", None, []])
def test_invalid_flow_never_reaches_factory(db, flow):
    result = fixture.prepare_source(flow, db.factory("writer"), db.factory("source"),
                                    approved=True, approval_scope="odi81_f1_prepare_source")
    assert result["status"] == "blocked" and db.events == []


def test_cross_flow_and_cross_operation_scope_block(db):
    for scope in ("odi81_f2_prepare_source", "odi81_f1_mutate_source", None):
        result = fixture.prepare_source("F1", db.factory("writer"), db.factory("source"),
                                        approved=True, approval_scope=scope)
        assert result["error_code"] == "ApprovalScopeMismatch"
    assert db.events == []


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_absent_creation_seed_commit_and_real_source_reader(db, flow):
    result = prepare(db, flow)
    assert result["status"] == "passed", result
    assert result["counts"]["rows"] == 3 and result["source_reader_verified"] is True
    assert result["commit_completed"] and result["ddl_attempted"] and result["object_may_exist"]
    assert sum(c.commits for c in db.connections) == 1
    assert all(c.closed for c in db.connections)
    rows = db.db.execute(f'SELECT COUNT(*) FROM "{fixture._FLOWS[flow][0]}"').fetchone()[0]
    assert rows == 3
    for i, (role, sql) in enumerate(db.events):
        if role == "source" and sql == "connect":
            assert db.events[i + 1] == ("source", "SET TRANSACTION READ ONLY")
    db.events.clear()
    assert prepare(db, flow)["error_code"] == "SourceAlreadyExists"
    no_mutation(db)


def test_reader_without_access_blocks_before_writer_connection(db):
    db.reader_capable = False
    assert prepare(db)["error_code"] == "SourceReaderCannotReadNewFixture"
    assert all(role == "source" for role, _ in db.events)
    no_mutation(db)


def test_actual_reader_failure_after_create_is_honest_partial(db):
    db.reader_allowed = False
    result = prepare(db)
    assert result["status"] == "failed" and result["commit_completed"]
    assert result["object_may_exist"] and result["manual_review_required"]
    assert not result["source_reader_verified"] and not result["automatic_ddl_rollback_performed"]
    assert "LEAK" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["CREATE TABLE", "INSERT INTO"])
def test_ddl_or_seed_failure_is_not_claimed_rolled_back(db, failure):
    db.fail_sql = failure
    result = prepare(db)
    assert result["status"] == "failed" and result["object_may_exist"]
    assert result["manual_review_required"] and not result["commit_completed"]
    assert not result["automatic_ddl_rollback_performed"]
    if failure == "INSERT INTO":
        assert db.db.execute('SELECT COUNT(*) FROM "ODI81_SRC_F1"').fetchone()[0] == 0


@pytest.mark.parametrize("committed", [False, True])
def test_commit_exception_is_unknown_even_if_rollback_succeeds(db, committed):
    db.fail_commit, db.commit_then_fail = True, committed
    result = prepare(db)
    assert result["outcome_unknown"] and result["commit_attempted"] and not result["commit_completed"]
    assert result["manual_review_required"]
    assert db.db.execute('SELECT COUNT(*) FROM "ODI81_SRC_F1"').fetchone()[0] == (3 if committed else 0)


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_one_transaction_mutation_requires_backups_and_cannot_repeat(db, flow):
    db.provision(flow)
    result = mutate(db, flow)
    assert result["status"] == "passed", result
    assert result["counts"] == {"rows": 4, "null_business_rows": 0, "duplicate_key_groups": 0, "updated": 1, "inserted": 1}
    assert sum(c.commits for c in db.connections) == 1
    assert not result["ddl_attempted"] and not result["target_mutation_performed"]
    assert [sql.split()[0] for _, sql in db.events if sql.startswith(("UPDATE", "INSERT"))] == ["UPDATE", "INSERT"]
    db.events.clear()
    assert mutate(db, flow)["status"] == "blocked"
    no_mutation(db)


@pytest.mark.parametrize("missing", ["ODI81_F1_S_R2_BKP", "ODI81_F1_T_R2_BKP"])
def test_missing_backup_blocks_source_mutation(db, missing):
    db.provision("F1")
    db.db.execute(f'DROP TABLE "{missing}"')
    assert mutate(db)["status"] == "blocked"
    no_mutation(db)


def test_actual_source_reader_no_fallback_on_existing_fixture(db):
    db.provision("F1")
    db.reader_allowed = False
    assert mutate(db)["status"] == "blocked"
    assert all(role == "source" for role, _ in db.events)
    no_mutation(db)


@pytest.mark.parametrize("failure", ["INSERT INTO", "AffectedCountMismatch"])
def test_mutation_rolls_back_update_if_insert_or_affected_count_fails(db, failure):
    db.provision("F1")
    db.fail_sql = failure if failure != "AffectedCountMismatch" else None
    db.bad_rowcount = 0 if failure == "AffectedCountMismatch" else None
    result = mutate(db)
    assert result["status"] == "failed" and not result["commit_attempted"]
    assert db.db.execute('SELECT COUNT(*) FROM "ODI81_SRC_F1"').fetchone()[0] == 3
    assert db.db.execute('SELECT ATTR_TEXT FROM "ODI81_SRC_F1" WHERE ENTITY_ID=101').fetchone()[0] == "FIXTURE_A"


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
@pytest.mark.parametrize("run", [1, 2, 3])
@pytest.mark.parametrize("phase", ["before_run", "after_run"])
def test_fixed_checkpoint_and_count_only_comparisons(db, flow, run, phase):
    db.provision(flow, state="seed" if run == 1 else "changed", run=run)
    if phase == "after_run":
        source, target = fixture._FLOWS[flow]
        db.db.execute(f'DELETE FROM "{target}"')
        db.db.execute(f'INSERT INTO "{target}" (ENTITY_ID,ATTR_TEXT,VALUE_AMT) SELECT ENTITY_ID,ATTR_TEXT,VALUE_AMT FROM "{source}"')
    result = verify(db, flow, run, phase)
    assert result["status"] == "passed", result
    assert result["physical_dml_count_proven"] is False
    if phase == "after_run" and run > 1:
        assert result["counts"]["changed_existing"] == (1 if run == 2 else 0)
        assert result["counts"]["inserted"] == (1 if run == 2 else 0)
        assert result["counts"]["unchanged_existing"] == (2 if run == 2 else 4)
    no_mutation(db)
    assert not any(c.commits for c in db.connections)
    for i, (_, sql) in enumerate(db.events):
        if sql == "connect":
            assert db.events[i + 1][1] == "SET TRANSACTION READ ONLY"
    assert all(type(v) is int and v >= 0 for v in result["counts"].values())
    text = json.dumps(result)
    assert all(secret not in text for secret in ("FIXTURE_A", "ENTITY_ID", "SELECT", "101", "LEAK"))


@pytest.mark.parametrize("corruption", ["duplicate", "null", "value", "backup", "metadata", "pk", "view"])
def test_corrupt_source_or_snapshot_never_proves_readiness(db, corruption):
    db.provision("F1")
    if corruption == "duplicate":
        db.db.execute('INSERT INTO ODI81_SRC_F1 SELECT * FROM ODI81_SRC_F1 WHERE ENTITY_ID=101')
    elif corruption == "null":
        db.db.execute('UPDATE ODI81_SRC_F1 SET ATTR_TEXT=NULL WHERE ENTITY_ID=101')
    elif corruption == "value":
        db.db.execute('UPDATE ODI81_SRC_F1 SET ATTR_TEXT="DRIFT" WHERE ENTITY_ID=101')
    elif corruption == "backup":
        db.db.execute('UPDATE ODI81_F1_T_R2_BKP SET ATTR_TEXT="DRIFT" WHERE ENTITY_ID=101')
    elif corruption == "metadata":
        db.bad_metadata = True
    elif corruption == "pk":
        db.bad_pk = True
    else:
        db.views.add("ODI81_F1_T_R2_BKP")
    result = mutate(db)
    assert result["status"] == "blocked", result
    no_mutation(db)


@pytest.mark.parametrize("bad_count", [True, "3", 3.0, -1, Decimal(3)])
def test_counts_are_not_coerced(db, bad_count):
    db.bad_count = bad_count
    assert prepare(db)["error_code"] == "InvalidCount"
    no_mutation(db)


@pytest.mark.parametrize("failure", ["rollback", "cursor", "connection"])
def test_reader_cleanup_failure_blocks_mutation_and_never_leaks(db, failure):
    db.fail_cleanup = failure
    result = prepare(db)
    assert result["status"] in {"blocked", "failed"} and result["error_code"] == "CleanupFailed"
    assert result["manual_review_required"]
    no_mutation(db)
    assert not any(role == "writer" for role, _ in db.events)
    assert "LEAK" not in json.dumps(result)


def test_autocommit_and_writer_identity_fail_closed(db):
    db.autocommit = True
    assert prepare(db)["error_code"] == "AutocommitNotDisabled"
    no_mutation(db)
    db.autocommit, db.writer_owner = False, False
    assert prepare(db)["error_code"] == "WriterMustBeDwh"
    no_mutation(db)


def test_source_has_no_config_import_cleanup_grant_or_target_dml():
    import inspect
    source = inspect.getsource(fixture)
    for forbidden in ("import config", "oracledb.connect", "DROP TABLE", "TRUNCATE", "DELETE FROM", "GRANT ", "print("):
        assert forbidden not in source
    assert re.findall(r'cursor.execute\(f[\'"]UPDATE (.*?) SET', source) == ["{_table(source)}"]


@pytest.mark.parametrize("run,phase", [(True, "after_run"), (0, "before_run"), (4, "after_run"),
                                     ("1", "after_run"), (1, "anything"), (1, [])])
def test_invalid_checkpoints_do_not_connect(db, run, phase):
    assert verify(db, run=run, phase=phase)["error_code"] == "InvalidCheckpoint"
    assert db.events == []


@pytest.mark.parametrize("failure", ["rollback", "cursor", "connection"])
def test_writer_cleanup_failure_after_confirmed_commit_is_not_success(db, monkeypatch, failure):
    original = Connection.commit
    def committed(self):
        original(self)
        db.fail_cleanup = failure
    monkeypatch.setattr(Connection, "commit", committed)
    result = prepare(db)
    assert result["status"] == "failed" and result["error_code"] == "CleanupFailed"
    assert result["commit_completed"] and not result["outcome_unknown"]
    assert result["manual_review_required"] and result["object_may_exist"]
    assert "LEAK" not in json.dumps(result)


def test_failed_rollback_after_mutation_failure_retains_uncertainty(db, monkeypatch):
    db.provision("F1")
    original = Cursor.execute
    def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO"):
            db.fail_cleanup = "rollback"
            raise RuntimeError("LEAK")
        return original(self, sql, params)
    monkeypatch.setattr(Cursor, "execute", execute)
    result = mutate(db)
    assert result["status"] == "failed" and result["rollback_failed"] and result["outcome_unknown"]
    assert not result["commit_completed"] and result["manual_review_required"]


def test_reader_post_mutation_failure_does_not_conceal_commit(db, monkeypatch):
    db.provision("F1")
    original = Connection.commit
    def committed(self):
        original(self)
        db.reader_allowed = False
    monkeypatch.setattr(Connection, "commit", committed)
    result = mutate(db)
    assert result["status"] == "failed" and result["commit_completed"]
    assert not result["source_reader_verified"] and result["manual_review_required"]
    assert db.db.execute('SELECT COUNT(*) FROM ODI81_SRC_F1').fetchone()[0] == 4


def test_forged_exception_message_is_not_an_error_code(db):
    def fails():
        raise fixture._Blocked("LEAK secret")
    result = fixture.prepare_source("F1", fails, fails, approved=True, approval_scope="odi81_f1_prepare_source")
    assert result["error_code"] == "OperationFailed" and "LEAK" not in json.dumps(result)


@pytest.mark.parametrize("flow", ["F1", "F2", "F3"])
def test_source_trigger_blocks_unrelated_committed_writes(db, flow):
    db.provision(flow)
    db.db.execute('CREATE TABLE UNRELATED_SENTINEL (N INTEGER)')
    db.db.execute('INSERT INTO UNRELATED_SENTINEL VALUES (0)')
    db.db.execute(f'CREATE TRIGGER FIXTURE_SIDE_EFFECT AFTER UPDATE ON {fixture._FLOWS[flow][0]} '
                  'BEGIN UPDATE UNRELATED_SENTINEL SET N=N+1; END')
    result = mutate(db, flow)
    assert result["status"] == "blocked" and result["error_code"] == "SourceTriggersPresent"
    assert not result["dml_attempted"] and not result["commit_attempted"]
    assert db.db.execute('SELECT N FROM UNRELATED_SENTINEL').fetchone()[0] == 0
    no_mutation(db)


def test_trigger_attached_at_create_blocks_before_seed(db, monkeypatch):
    original = Cursor.execute
    def execute(self, sql, params=None):
        original(self, sql, params)
        if sql.startswith("CREATE TABLE"):
            db.enabled_triggers.add("ODI81_SRC_F1")
    monkeypatch.setattr(Cursor, "execute", execute)
    result = prepare(db)
    assert result["status"] == "failed" and result["error_code"] == "SourceTriggersPresent"
    assert result["object_may_exist"] and result["manual_review_required"]
    assert not result["dml_attempted"] and not result["commit_attempted"]
    assert not any(sql.startswith("INSERT") for _, sql in db.events)


@pytest.mark.parametrize("name", ["ODI81_SRC_F1", "ODS_ODI81_F1", "ODI81_F1_S_R2_BKP", "ODI81_F1_T_R2_BKP"])
def test_non_table_rejected_before_any_lock(db, name):
    db.provision("F1")
    db.views.add(name)
    result = mutate(db)
    assert result["error_code"] == "NotPermanentTable"
    assert not any(sql.startswith("LOCK") for _, sql in db.events)
    no_mutation(db)


@pytest.mark.parametrize("exception_type", [KeyError, IndexError, ZeroDivisionError, Exception])
@pytest.mark.parametrize("action", ["prepare_source", "mutate_source", "verify_flow"])
def test_unexpected_factory_errors_are_sanitized(db, exception_type, action):
    def fail():
        raise exception_type("PRIVATE_SENTINEL")
    args = ["F1"] + ([1, "before_run"] if action == "verify_flow" else [])
    result = getattr(fixture, action)(*args, fail, fail, approved=True, approval_scope=f"odi81_f1_{action}")
    assert result["status"] == "blocked" and result["error_code"] == "OperationFailed"
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["rollback", "cursor", "connection"])
def test_unexpected_cleanup_errors_do_not_escape_or_skip_remaining_resources(db, monkeypatch, failure):
    db.provision("F1")
    def fail(self):
        db.events.append((self.role if hasattr(self, "role") else self.connection.role, "unexpected_cleanup"))
        raise KeyError("PRIVATE_SENTINEL")
    monkeypatch.setattr(Cursor if failure == "cursor" else Connection,
                        "close" if failure in ("cursor", "connection") else "rollback", fail)
    result = mutate(db)
    assert result["status"] == "failed" and result["error_code"] == "CleanupFailed"
    assert result["manual_review_required"] and "PRIVATE_SENTINEL" not in json.dumps(result)
    assert not result["dml_attempted"]
    assert any(sql in ("close", "unexpected_cleanup") for _, sql in db.events)


def test_trigger_added_after_reader_preflight_is_rejected_under_lock(db):
    db.provision("F1")
    def writer():
        db.enabled_triggers.add("ODI81_SRC_F1")
        return db.factory("writer")()
    result = fixture.mutate_source("F1", writer, db.factory("source"), approved=True,
                                    approval_scope="odi81_f1_mutate_source")
    assert result["error_code"] == "SourceTriggersPresent" and not result["dml_attempted"]
    assert any(sql.startswith("LOCK") for _, sql in db.events)
    no_mutation(db)


def test_all_kind_prechecks_precede_locks_and_recheck_after_them(db, monkeypatch):
    db.provision("F1")
    original = Cursor.execute
    checked = set()
    expected = {"ODI81_SRC_F1", "ODS_ODI81_F1", "ODI81_F1_S_R2_BKP", "ODI81_F1_T_R2_BKP"}
    def execute(self, sql, params=None):
        if self.connection.role == "writer" and "FROM ALL_TABLES" in sql:
            checked.add(params["name"])
        if sql.startswith("LOCK"):
            assert checked == expected
            if '"ODI81_F1_T_R2_BKP"' in sql:
                db.views.add("ODS_ODI81_F1")
        return original(self, sql, params)
    monkeypatch.setattr(Cursor, "execute", execute)
    result = mutate(db)
    assert result["error_code"] == "NotPermanentTable" and not result["dml_attempted"]


@pytest.mark.parametrize("point", ["cursor", "execute", "fetchone", "commit"])
def test_unexpected_resource_errors_are_redacted_and_cleaned(db, monkeypatch, point):
    class CallbackProblem(Exception):
        pass
    db.provision("F1")
    def fail(*args, **kwargs):
        raise CallbackProblem("PRIVATE_SENTINEL")
    monkeypatch.setattr(Connection if point in ("cursor", "commit") else Cursor, point, fail)
    result = mutate(db)
    assert result["error_code"] == "OperationFailed" and result["status"] != "passed"
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert all(c.closed for c in db.connections)
    if point == "commit":
        assert result["commit_attempted"] and result["outcome_unknown"]
        assert result["manual_review_required"] and not result["commit_completed"]


@pytest.mark.parametrize("failure", ["rollback", "cursor_close", "connection_close"])
def test_unexpected_cleanup_failure_still_attempts_every_resource(failure):
    events = []
    class Resource:
        def __init__(self, role):
            self.role = role
        def rollback(self):
            events.append((self.role, "rollback"))
            if failure == "rollback":
                raise KeyError("PRIVATE_SENTINEL")
        def close(self):
            events.append((self.role, "close"))
            if self.role.endswith("cursor") == (failure == "cursor_close") and failure != "rollback":
                raise KeyError("PRIVATE_SENTINEL")
    resources = [[Resource(f"{role}_connection"), Resource(f"{role}_cursor")] for role in ("guard", "writer")]
    report = fixture._report("test")
    report["status"] = "passed"
    fixture._cleanup(resources, report)
    assert events == [(f"{role}_{kind}", method) for role in ("writer", "guard")
                      for kind, method in (("connection", "rollback"), ("cursor", "close"), ("connection", "close"))]
    assert resources == [] and report["status"] == "failed"
    assert report["error_code"] == "CleanupFailed" and "PRIVATE_SENTINEL" not in json.dumps(report)


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_process_control_not_suppressed_but_acquired_resources_cleaned(db, error_type):
    resources = []
    report = fixture._report("test")
    with pytest.raises(error_type):
        with fixture._operation(report, resources):
            fixture._open(db.factory("source"), report, resources, readonly=True)
            raise error_type()
    assert resources == [] and all(c.closed for c in db.connections)
