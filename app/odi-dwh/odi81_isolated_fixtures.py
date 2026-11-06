"""Fixed F1/F2/F3 preparation primitives. Importing this module performs no IO."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

Factory = Callable[[], Any]
# Fixed fresh acceptance namespace; never caller-selected and never an existing ODI81_* alias.
NAMESPACE = "ODI81N"
_FLOWS = {
    "F1": ("ODI81N_SRC_F1", "ODS_ODI81N_F1"),
    "F2": ("ODI81N_SRC_F2", "ODS_ODI81N_F2"),
    "F3": ("ODI81N_SRC_F3", "DWH_ODI81N_F3"),
}
_BUSINESS = ("ENTITY_ID", "ATTR_TEXT", "VALUE_AMT")
_ODS_EXTRA = ("INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID")
_EDW_EXTRA = ("INSERT_DATE", "UPDATE_DATE", "CURRENT_FLAG", "EFFECTIVE_FROM",
              "EFFECTIVE_TO", "SOURCE_SYSTEM", "ETL_BATCH_ID")
_SEED = ((101, "FIXTURE_A", Decimal("10.00")), (102, "FIXTURE_B", Decimal("20.00")),
         (103, "FIXTURE_C", Decimal("30.00")))
_CHANGED = ((_SEED[0][0], "FIXTURE_A_UPDATED", _SEED[0][2]), *_SEED[1:],
            (104, "FIXTURE_D", Decimal("40.00")))
_META = "COLUMN_ID,COLUMN_NAME,DATA_TYPE,DATA_LENGTH,DATA_PRECISION,DATA_SCALE,CHAR_USED,CHAR_LENGTH,NULLABLE"
_ERRORS = frozenset({"InvalidCount", "AutocommitNotDisabled", "CleanupFailed", "WriterMustBeDwh",
                     "ColumnContractMismatch", "SourcePrimaryKeyMismatch", "FixtureStateMismatch",
                     "SnapshotMismatch", "BackupMetadataMismatch", "SourceReaderCannotReadNewFixture",
                     "SourceAlreadyExists", "AffectedCountMismatch", "DeltaMismatch", "NotPermanentTable",
                     "SourceTriggersPresent"})


class _Blocked(RuntimeError):
    pass


def backup_table(flow: str, kind: str, run: int) -> str:
    """Retained checkpoint name in the fixed fresh acceptance namespace."""
    return f"{NAMESPACE}_{flow}_{'S' if kind == 'source' else 'T'}_R{run}_BKP"


def _freeze_namespace() -> None:
    # Historical ODI81_* objects stay untouched; acceptance owns only fresh ODI81N names.
    if NAMESPACE != "ODI81N":
        raise RuntimeError("namespace")
    for flow, names in _FLOWS.items():
        backups = [backup_table(flow, "source", 2), *(backup_table(flow, "target", r) for r in (1, 2, 3))]
        for name in (*names, *backups):
            if f"{NAMESPACE}_" not in name or len(name) > 30:
                raise RuntimeError("namespace")


_freeze_namespace()


def _report(action: str) -> dict[str, Any]:
    return {
        "tool": "odi81.isolated_fixtures", "action": action, "status": "blocked",
        "stage": "approval", "error_code": "ApprovalRequired", "counts": {},
        "connection_attempted": False, "source_reader_verified": False,
        "ddl_attempted": False, "object_may_exist": False,
        "dml_attempted": False, "commit_attempted": False, "commit_completed": False,
        "outcome_unknown": False, "manual_review_required": False,
        "rollback_failed": False, "cursor_close_failed": False, "connection_close_failed": False,
        "automatic_ddl_rollback_performed": False, "target_mutation_performed": False,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
        "full_target_snapshot_equal": False,
        "rerun_semantics": None,
        "physical_dml_count_proven": False,
    }


def _authorize(report: dict, flow: object, approved: object, scope: object, suffix: str) -> bool:
    if approved is not True or not isinstance(flow, str) or flow not in _FLOWS:
        return False
    if scope != f"odi81_{flow.lower()}_{suffix}":
        report["error_code"] = "ApprovalScopeMismatch"
        return False
    report["flow"] = flow
    return True


def _table(name: str) -> str:
    return f'"DWH"."{name}"'


def _columns(names: tuple[str, ...]) -> str:
    return ",".join(f'"{name}"' for name in names)


def _count(cursor: Any, sql: str, params: dict | None = None) -> int:
    cursor.execute(sql, params or {})
    row = cursor.fetchone()
    if not isinstance(row, (list, tuple)) or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        raise _Blocked("InvalidCount")
    return row[0]


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise _Blocked(code)


def _open(factory: Factory, report: dict, resources: list, *, readonly: bool) -> Any:
    report["connection_attempted"] = True
    connection = factory()
    resources.append([connection, None])
    _need(connection.autocommit is False, "AutocommitNotDisabled")
    cursor = connection.cursor()
    resources[-1][1] = cursor
    # This must precede every query in each aggregate-reader transaction.
    if readonly:
        cursor.execute("SET TRANSACTION READ ONLY")
    return cursor


def _release(resource: Any, method: str, flag: str, report: dict) -> None:
    if resource is None:
        return
    try:
        getattr(resource, method)()
    except Exception:
        # User-supplied resources can raise arbitrary ordinary exceptions.
        # Never expose their text or skip the remaining cleanup attempts.
        report[flag] = True


def _release_one(connection: Any, cursor: Any, report: dict) -> None:
    # Nested finally still runs the later releases when an earlier one raises
    # KeyboardInterrupt, SystemExit, or GeneratorExit.
    try:
        try:
            _release(connection, "rollback", "rollback_failed", report)
        finally:
            _release(cursor, "close", "cursor_close_failed", report)
    finally:
        _release(connection, "close", "connection_close_failed", report)


def _cleanup(resources: list, report: dict) -> None:
    pending: BaseException | None = None
    for connection, cursor in reversed(resources):
        try:
            _release_one(connection, cursor, report)
        except BaseException as exc:
            if pending is not None:
                exc.__context__ = pending
            pending = exc
    resources.clear()
    if any(report[key] for key in ("rollback_failed", "cursor_close_failed", "connection_close_failed")):
        report.update(status="failed", error_code="CleanupFailed", manual_review_required=True)
        if report["dml_attempted"] and not report["commit_completed"] and report["rollback_failed"]:
            report["outcome_unknown"] = True
    if pending is not None:
        raise pending


def _fail(report: dict, exc: Exception) -> None:
    # Only our internal fixed codes are emitted, never an exception class/message.
    code = exc.args[0] if type(exc) is _Blocked and exc.args else None
    report["error_code"] = code if isinstance(code, str) and code in _ERRORS else "OperationFailed"
    wrote = report["ddl_attempted"] or report["dml_attempted"] or report.get("verification_failed") is True
    report["status"] = "failed" if wrote else "blocked"
    report["manual_review_required"] |= report["ddl_attempted"] or report["dml_attempted"]
    report["outcome_unknown"] |= report["commit_attempted"] and not report["commit_completed"]


@contextmanager
def _operation(report: dict, resources: list, *, failure: Callable = _fail) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        # Deliberately broad at the public callback boundary, not an error-message logger.
        failure(report, exc)
    finally:
        _cleanup(resources, report)


def _owner(cursor: Any) -> None:
    _need(_count(cursor, "SELECT COUNT(*) FROM DUAL WHERE SYS_CONTEXT('USERENV','SESSION_USER')='DWH'") == 1,
          "WriterMustBeDwh")


def _exists(cursor: Any, name: str) -> int:
    return _count(cursor, "SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER='DWH' AND OBJECT_NAME=:name",
                  {"name": name})


def _business_metadata(cursor: Any, name: str, *, source: bool, flow: str) -> None:
    _permanent_table(cursor, name)
    names = _BUSINESS if source else _BUSINESS + (_EDW_EXTRA if flow == "F3" else _ODS_EXTRA)
    binds = {"name": name, **{f"n{i}": col for i, col in enumerate(names)}}
    permitted = ",".join(f":n{i}" for i in range(len(names)))
    _need(_count(cursor, "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER='DWH' AND TABLE_NAME=:name",
                 {"name": name}) == len(names), "ColumnContractMismatch")
    _need(_count(cursor, f"SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER='DWH' AND TABLE_NAME=:name "
                 f"AND COLUMN_NAME IN ({permitted})", binds) == len(names), "ColumnContractMismatch")
    _need(_count(cursor, """SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER='DWH' AND TABLE_NAME=:name AND (
        (COLUMN_ID=1 AND COLUMN_NAME='ENTITY_ID' AND DATA_TYPE='NUMBER' AND DATA_PRECISION=10 AND DATA_SCALE=0 AND NULLABLE='N') OR
        (COLUMN_ID=2 AND COLUMN_NAME='ATTR_TEXT' AND DATA_TYPE='VARCHAR2' AND DATA_LENGTH=40 AND CHAR_USED='B' AND CHAR_LENGTH=40 AND NULLABLE='N') OR
        (COLUMN_ID=3 AND COLUMN_NAME='VALUE_AMT' AND DATA_TYPE='NUMBER' AND DATA_PRECISION=10 AND DATA_SCALE=2 AND NULLABLE='N'))""",
                 {"name": name}) == 3, "ColumnContractMismatch")
    if source:
        _need(_count(cursor, """SELECT COUNT(*) FROM ALL_CONSTRAINTS c
            WHERE c.OWNER='DWH' AND c.TABLE_NAME=:name AND c.CONSTRAINT_TYPE='P'
            AND c.STATUS='ENABLED' AND c.VALIDATED='VALIDATED'
            AND (SELECT COUNT(*) FROM ALL_CONS_COLUMNS k WHERE k.OWNER=c.OWNER AND k.CONSTRAINT_NAME=c.CONSTRAINT_NAME)=1
            AND EXISTS (SELECT 1 FROM ALL_CONS_COLUMNS k WHERE k.OWNER=c.OWNER
                AND k.CONSTRAINT_NAME=c.CONSTRAINT_NAME AND k.COLUMN_NAME='ENTITY_ID' AND k.POSITION=1)""",
                     {"name": name}) == 1, "SourcePrimaryKeyMismatch")


def _expected(state: str) -> tuple[str, dict]:
    rows = _SEED if state == "seed" else _CHANGED
    params = {}
    selects = []
    for i, row in enumerate(rows):
        params.update({f"k{i}": row[0], f"t{i}": row[1], f"v{i}": row[2]})
        selects.append(f"SELECT :k{i} ENTITY_ID,:t{i} ATTR_TEXT,:v{i} VALUE_AMT FROM DUAL")
    return "SELECT ENTITY_ID,ATTR_TEXT,VALUE_AMT FROM (" + " UNION ALL ".join(selects) + ")", params


def _state(cursor: Any, name: str, state: str) -> dict[str, int]:
    table = _table(name)
    total = _count(cursor, f"SELECT COUNT(*) FROM {table}")
    nulls = _count(cursor, f'SELECT COUNT(*) FROM {table} WHERE "ENTITY_ID" IS NULL OR "ATTR_TEXT" IS NULL OR "VALUE_AMT" IS NULL')
    duplicates = _count(cursor, f'SELECT COUNT(*) FROM (SELECT "ENTITY_ID" FROM {table} GROUP BY "ENTITY_ID" HAVING COUNT(*)>1)')
    _need(total == {"empty": 0, "seed": 3, "changed": 4}[state] and nulls == duplicates == 0,
          "FixtureStateMismatch")
    if state != "empty":
        expected, params = _expected(state)
        actual = f"SELECT {_columns(_BUSINESS)} FROM {table}"
        for left, right in ((actual, expected), (expected, actual)):
            _need(_count(cursor, f"SELECT COUNT(*) FROM ({left} MINUS {right})", params) == 0,
                  "FixtureStateMismatch")
    return {"rows": total, "null_business_rows": nulls, "duplicate_key_groups": duplicates}


def _equal(cursor: Any, left: str, right: str, columns: tuple[str, ...], *, require_match: bool = True) -> bool:
    projection = _columns(columns)
    equal = True
    for a, b in ((left, right), (right, left)):
        equal = (_count(cursor, f"SELECT COUNT(*) FROM (SELECT {projection} FROM {_table(a)} "
                        f"MINUS SELECT {projection} FROM {_table(b)})") == 0) and equal
    if require_match:
        _need(equal, "SnapshotMismatch")
    return equal


def _permanent_table(cursor: Any, name: str) -> None:
    _need(_count(cursor, "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER='DWH' AND TABLE_NAME=:name AND TEMPORARY='N'",
                 {"name": name}) == 1, "NotPermanentTable")


def _no_source_triggers(cursor: Any, name: str) -> None:
    _need(_count(cursor, "SELECT COUNT(*) FROM ALL_TRIGGERS WHERE TABLE_OWNER='DWH' "
                 "AND TABLE_NAME=:name AND STATUS='ENABLED'", {"name": name}) == 0,
          "SourceTriggersPresent")


def _backup(cursor: Any, flow: str, *, source: bool, run: int, state: str, compare_current: bool) -> str:
    original = _FLOWS[flow][0 if source else 1]
    backup = backup_table(flow, 'source' if source else 'target', run)
    columns = _BUSINESS if source else _BUSINESS + (_EDW_EXTRA if flow == "F3" else _ODS_EXTRA)
    # No PK constraint is required on a CTAS backup; key uniqueness is counted instead.
    _permanent_table(cursor, backup)
    if not source:
        _business_metadata(cursor, backup, source=False, flow=flow)
    if source:
        _need(_count(cursor, "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER='DWH' AND TABLE_NAME=:name",
                     {"name": backup}) == 3, "BackupMetadataMismatch")
    for a, b in ((original, backup), (backup, original)):
        _need(_count(cursor, f"SELECT COUNT(*) FROM (SELECT {_META} FROM ALL_TAB_COLUMNS "
                     "WHERE OWNER='DWH' AND TABLE_NAME=:a MINUS "
                     f"SELECT {_META} FROM ALL_TAB_COLUMNS WHERE OWNER='DWH' AND TABLE_NAME=:b)",
                     {"a": a, "b": b}) == 0, "BackupMetadataMismatch")
    _state(cursor, backup, state)
    if compare_current:
        _state(cursor, original, state)
        _equal(cursor, original, backup, columns)
    return backup


def _reader_check(factory: Factory, flow: str, report: dict, *, state: str | None) -> None:
    resources: list = []
    report["source_reader_verified"] = False
    try:
        cursor = _open(factory, report, resources, readonly=True)
        if state is None:
            # An absent object's per-object grant cannot be tested before CREATE. Fail closed
            # unless this actual reader already has schema ownership or SELECT ANY TABLE.
            _need(_count(cursor, """SELECT COUNT(*) FROM DUAL WHERE SYS_CONTEXT('USERENV','SESSION_USER')='DWH'
                OR EXISTS (SELECT 1 FROM SESSION_PRIVS WHERE PRIVILEGE='SELECT ANY TABLE')""") == 1,
                  "SourceReaderCannotReadNewFixture")
        else:
            name = _FLOWS[flow][0]
            _business_metadata(cursor, name, source=True, flow=flow)
            _state(cursor, name, state)
            report["source_reader_verified"] = True
    finally:
        _cleanup(resources, report)
    _need(report["error_code"] != "CleanupFailed", "CleanupFailed")


def prepare_source(flow: str, writer_factory: Factory, source_reader_factory: Factory, *,
                   approved: object = False, approval_scope: object = None) -> dict[str, Any]:
    """Create/seed one absent fixed source. DDL is not rollback-safe; never retry/reset."""
    report = _report("prepare_source")
    if not _authorize(report, flow, approved, approval_scope, "prepare_source"):
        return report
    resources: list = []
    with _operation(report, resources):
        report["stage"] = "source_reader_preflight"
        _reader_check(source_reader_factory, flow, report, state=None)
        report["stage"] = "absence_preflight"
        cursor = _open(writer_factory, report, resources, readonly=False)
        _owner(cursor)
        source = _FLOWS[flow][0]
        _need(_exists(cursor, source) == 0, "SourceAlreadyExists")
        report.update(stage="create_source", ddl_attempted=True, object_may_exist=True)
        cursor.execute(f"CREATE TABLE {_table(source)} (ENTITY_ID NUMBER(10) NOT NULL, "
                       "ATTR_TEXT VARCHAR2(40 BYTE) NOT NULL, VALUE_AMT NUMBER(10,2) NOT NULL, "
                       f"CONSTRAINT PK_{source} PRIMARY KEY (ENTITY_ID))")
        _permanent_table(cursor, source)
        cursor.execute(f"LOCK TABLE {_table(source)} IN EXCLUSIVE MODE NOWAIT")
        _business_metadata(cursor, source, source=True, flow=flow)
        _no_source_triggers(cursor, source)
        report.update(stage="seed_source", dml_attempted=True)
        for key, text, amount in _SEED:
            cursor.execute(f"INSERT INTO {_table(source)} ({_columns(_BUSINESS)}) VALUES (:key,:text,:amount)",
                           {"key": key, "text": text, "amount": amount})
            _need(type(cursor.rowcount) is int and cursor.rowcount == 1, "AffectedCountMismatch")
        _business_metadata(cursor, source, source=True, flow=flow)
        report["counts"] = _state(cursor, source, "seed")
        report.update(stage="commit", commit_attempted=True)
        resources[0][0].commit()
        report["commit_completed"] = True
        _cleanup(resources, report)
        _need(report["error_code"] != "CleanupFailed", "CleanupFailed")
        report["stage"] = "source_reader_postflight"
        _reader_check(source_reader_factory, flow, report, state="seed")
        report.update(status="passed", stage="completed", error_code=None)
    return report


def _checkpoint_state(kind: str, run: int) -> str:
    if kind == "source":
        return "seed"
    return {1: "empty", 2: "seed", 3: "changed"}[run]


def _backup_columns(flow: str, *, source: bool) -> tuple[str, ...]:
    if source:
        return _BUSINESS
    return _BUSINESS + (_EDW_EXTRA if flow == "F3" else _ODS_EXTRA)


def _backup_ddl(flow: str, *, source: bool) -> str:
    """Explicit column DDL so a retained copy keeps the frozen nullability."""
    business = ('"ENTITY_ID" NUMBER(10) NOT NULL, "ATTR_TEXT" VARCHAR2(40 BYTE) NOT NULL, '
                '"VALUE_AMT" NUMBER(10,2) NOT NULL')
    if source:
        return business
    if flow == "F3":
        extra = ('"INSERT_DATE" TIMESTAMP, "UPDATE_DATE" TIMESTAMP, "CURRENT_FLAG" VARCHAR2(1), '
                 '"EFFECTIVE_FROM" DATE, "EFFECTIVE_TO" DATE, "SOURCE_SYSTEM" VARCHAR2(50), '
                 '"ETL_BATCH_ID" NUMBER')
    else:
        extra = ('"INSERT_DATE" TIMESTAMP, "UPDATE_DATE" TIMESTAMP, "SOURCE_SYSTEM" VARCHAR2(50), '
                 '"ETL_BATCH_ID" NUMBER')
    return f"{business}, {extra}"


def create_retained_backup(flow: str, kind: str, run: int, writer_factory: Factory, *,
                           approved: object = False, approval_scope: object = None) -> dict[str, Any]:
    """Copy one absent checkpoint. An existing object is kept only when it already matches."""
    report = _report("create_retained_backup")
    if approved is not True or not isinstance(flow, str) or flow not in _FLOWS:
        return report
    if kind not in {"source", "target"} or type(run) is not int:
        report["error_code"] = "InvalidCheckpoint"
        return report
    if (kind == "source" and run != 2) or (kind == "target" and run not in (1, 2, 3)):
        report["error_code"] = "InvalidCheckpoint"
        return report
    suffix = "backup_source" if kind == "source" else f"backup_target_r{run}"
    if not _authorize(report, flow, approved, approval_scope, suffix):
        return report
    resources: list = []
    with _operation(report, resources):
        report["stage"] = "backup_preflight"
        cursor = _open(writer_factory, report, resources, readonly=False)
        _owner(cursor)
        source_name, target_name = _FLOWS[flow]
        original = source_name if kind == "source" else target_name
        backup = backup_table(flow, kind, run)
        state = _checkpoint_state(kind, run)
        _permanent_table(cursor, original)
        cursor.execute(f"LOCK TABLE {_table(original)} IN EXCLUSIVE MODE NOWAIT")
        _business_metadata(cursor, original, source=kind == "source", flow=flow)
        _state(cursor, original, state)
        if _exists(cursor, backup) != 0:
            report.update(stage="retain_backup", preexisting=True)
            _permanent_table(cursor, backup)
            cursor.execute(f"LOCK TABLE {_table(backup)} IN SHARE MODE NOWAIT")
            _backup(cursor, flow, source=kind == "source", run=run, state=state, compare_current=True)
            report["counts"] = {"rows": _state(cursor, backup, state)["rows"]}
            report.update(status="passed", stage="completed", error_code=None, created=False)
        else:
            columns = _backup_columns(flow, source=kind == "source")
            report.update(stage="create_backup", ddl_attempted=True, object_may_exist=True)
            cursor.execute(f"CREATE TABLE {_table(backup)} ({_backup_ddl(flow, source=kind == 'source')})")
            _permanent_table(cursor, backup)
            cursor.execute(f"LOCK TABLE {_table(backup)} IN EXCLUSIVE MODE NOWAIT")
            report.update(stage="copy_backup", dml_attempted=True)
            projection = _columns(columns)
            cursor.execute(
                f"INSERT INTO {_table(backup)} ({projection}) SELECT {projection} FROM {_table(original)}"
            )
            expected = {"empty": 0, "seed": 3, "changed": 4}[state]
            _need(type(cursor.rowcount) is int and cursor.rowcount == expected, "AffectedCountMismatch")
            _backup(cursor, flow, source=kind == "source", run=run, state=state, compare_current=True)
            report["counts"] = {"rows": expected}
            report.update(stage="commit", commit_attempted=True)
            resources[0][0].commit()
            report["commit_completed"] = True
            report.update(status="passed", stage="completed", error_code=None, created=True)
    return report


def mutate_source(flow: str, writer_factory: Factory, source_reader_factory: Factory, *,
                  approved: object = False, approval_scope: object = None) -> dict[str, Any]:
    """Exactly one update + insert, after fixed source/target R2 backup comparisons."""
    report = _report("mutate_source")
    if not _authorize(report, flow, approved, approval_scope, "mutate_source"):
        return report
    resources: list = []
    with _operation(report, resources):
        report["stage"] = "source_reader_preflight"
        _reader_check(source_reader_factory, flow, report, state="seed")
        report["stage"] = "backup_preflight"
        cursor = _open(writer_factory, report, resources, readonly=False)
        _owner(cursor)
        source, target = _FLOWS[flow]
        # Reject views/synonyms before any lock could reach their underlying objects.
        retained = (backup_table(flow, "source", 2), backup_table(flow, "target", 2))
        for name in (source, target, *retained):
            _permanent_table(cursor, name)
        # Keep the source, target and retained checkpoints stable through this transaction.
        cursor.execute(f"LOCK TABLE {_table(source)} IN EXCLUSIVE MODE NOWAIT")
        for name in (target, *retained):
            cursor.execute(f"LOCK TABLE {_table(name)} IN SHARE MODE NOWAIT")
        _business_metadata(cursor, source, source=True, flow=flow)
        _no_source_triggers(cursor, source)
        _business_metadata(cursor, target, source=False, flow=flow)
        _backup(cursor, flow, source=True, run=2, state="seed", compare_current=True)
        _backup(cursor, flow, source=False, run=2, state="seed", compare_current=True)
        report.update(stage="mutate_source", dml_attempted=True)
        cursor.execute(f'UPDATE {_table(source)} SET "ATTR_TEXT"=:text WHERE "ENTITY_ID"=:key',
                       {"text": _CHANGED[0][1], "key": _SEED[0][0]})
        _need(type(cursor.rowcount) is int and cursor.rowcount == 1, "AffectedCountMismatch")
        cursor.execute(f"INSERT INTO {_table(source)} ({_columns(_BUSINESS)}) VALUES (:key,:text,:amount)",
                       dict(zip(("key", "text", "amount"), _CHANGED[-1])))
        _need(type(cursor.rowcount) is int and cursor.rowcount == 1, "AffectedCountMismatch")
        report["counts"] = {**_state(cursor, source, "changed"), "updated": 1, "inserted": 1}
        report.update(stage="commit", commit_attempted=True)
        resources[0][0].commit()
        report["commit_completed"] = True
        _cleanup(resources, report)
        _need(report["error_code"] != "CleanupFailed", "CleanupFailed")
        report["stage"] = "source_reader_postflight"
        _reader_check(source_reader_factory, flow, report, state="changed")
        report.update(status="passed", stage="completed", error_code=None)
    return report


def verify_flow(flow: str, run: int, phase: str, reader_factory: Factory, source_reader_factory: Factory, *,
                approved: object = False, approval_scope: object = None) -> dict[str, Any]:
    """Read-only before/after evidence; never authorizes or invokes a target run."""
    report = _report("verify_flow")
    if approved is not True or not isinstance(flow, str) or flow not in _FLOWS:
        return report
    if type(run) is not int or run not in (1, 2, 3) or phase not in ("before_run", "after_run"):
        report["error_code"] = "InvalidCheckpoint"
        return report
    label = "before" if phase == "before_run" else "after"
    suffix = "verify_flow"
    if approval_scope == f"odi81_{flow.lower()}_verify_{label}_r{run}":
        suffix = f"verify_{label}_r{run}"
    if not _authorize(report, flow, approved, approval_scope, suffix):
        return report
    resources: list = []
    with _operation(report, resources):
        state = "seed" if run == 1 else "changed"
        report["stage"] = "source_reader_preflight"
        _reader_check(source_reader_factory, flow, report, state=state)
        report["stage"] = "snapshot_verification"
        cursor = _open(reader_factory, report, resources, readonly=True)
        # A disagreement after the reader opens is a failed witness, not a preflight block.
        report["verification_failed"] = True
        source, target = _FLOWS[flow]
        _business_metadata(cursor, source, source=True, flow=flow)
        _business_metadata(cursor, target, source=False, flow=flow)
        source_counts = _state(cursor, source, state)
        target_state = ("empty" if run == 1 else "seed" if run == 2 else "changed") if phase == "before_run" else state
        target_counts = _state(cursor, target, target_state)
        checkpoint_state = {1: "empty", 2: "seed", 3: "changed"}[run]
        backup = _backup(cursor, flow, source=False, run=run, state=checkpoint_state,
                         compare_current=phase == "before_run")
        report["counts"] = {"source_rows": source_counts["rows"], "target_rows": target_counts["rows"],
                            "null_business_rows": 0, "duplicate_key_groups": 0}
        if phase == "after_run":
            _equal(cursor, source, target, _BUSINESS)
            report["counts"].update(source_minus_target=0, target_minus_source=0)
            if run > 1:
                changed = _count(cursor, f'SELECT COUNT(*) FROM {_table(target)} t JOIN {_table(backup)} b '
                                 'ON t.ENTITY_ID=b.ENTITY_ID WHERE t.ATTR_TEXT<>b.ATTR_TEXT OR t.VALUE_AMT<>b.VALUE_AMT')
                unchanged = _count(cursor, f'SELECT COUNT(*) FROM {_table(target)} t JOIN {_table(backup)} b '
                                   'ON t.ENTITY_ID=b.ENTITY_ID WHERE t.ATTR_TEXT=b.ATTR_TEXT AND t.VALUE_AMT=b.VALUE_AMT')
                inserted = _count(cursor, f'SELECT COUNT(*) FROM {_table(target)} t WHERE NOT EXISTS '
                                  f'(SELECT 1 FROM {_table(backup)} b WHERE b.ENTITY_ID=t.ENTITY_ID)')
                _need((changed, unchanged, inserted) == ((1, 2, 1) if run == 2 else (0, 4, 0)), "DeltaMismatch")
                report["counts"].update(changed_existing=changed, unchanged_existing=unchanged, inserted=inserted)
            if run == 3:
                columns = _BUSINESS + (_EDW_EXTRA if flow == "F3" else _ODS_EXTRA)
                report["full_target_snapshot_equal"] = _equal(
                    cursor, target, backup, columns, require_match=flow != "F2"
                )
        passed_update: dict[str, Any] = {
            "status": "passed",
            "stage": "completed",
            "error_code": None,
            "run": run,
            "phase": phase,
            "logical_business_comparison_only": not report["full_target_snapshot_equal"],
            "physical_dml_count_proven": False,
        }
        # Only a finished after_run of run 3 has rerun evidence. Earlier passes do not.
        if phase == "after_run" and run == 3:
            passed_update["rerun_semantics"] = "full_reload" if flow == "F2" else "incremental"
        report.update(passed_update)
    if report["status"] != "passed":
        report["full_target_snapshot_equal"] = False
        report["logical_business_comparison_only"] = False
        report["rerun_semantics"] = None
    return report
