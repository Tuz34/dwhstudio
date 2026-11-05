"""F4/F5 SCD2 source, retained-backup, and target proof primitives.

Importing this module performs no IO. Connections are injected. Live Oracle
work happens only when a caller passes literal ``approved=True`` and the exact
scope. This module does not drop, truncate, alter, or repair an existing object.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from decimal import Decimal
from typing import Any

import dim_product_scd2_stock_contract as stock
import odi81_isolated_fixtures as isolated
import scd2_builder
import source_model_policy

Factory = Callable[[], Any]
BUSINESS: tuple[str, ...] = ("ENTITY_ID", "ATTR_TEXT", "VALUE_AMT")
AUDIT: tuple[str, ...] = tuple(name for name, _data_type, _nullable in scd2_builder.SCD2_COLUMNS)
OPEN_ENDED_DATE_SQL = "DATE '2400-01-01'"
CURRENT_FLAG = "1"
HISTORICAL_FLAG = "0"
FLOWS: dict[str, dict[str, str]] = {
    "F4": {
        "source_owner": "DWH",
        "source_table": "ODS_ODI81_F4",
        "target_owner": "DWH",
        "target_table": "DIM_ODI81_F4",
        "mapping_name": "MAP_ODI81_F4",
    },
    "F5": {
        "source_owner": "DWH",
        "source_table": "DWH_ODI81_F5",
        "target_owner": "DM",
        "target_table": "DIM_ODI81_F5",
        "mapping_name": "MAP_ODI81_F5",
    },
}
SK: dict[str, str] = {
    flow: scd2_builder.surrogate_key_name(spec["target_table"], BUSINESS)
    for flow, spec in FLOWS.items()
}
SCENARIO_VERSION = "001"
_SEED = (
    (101, "FIXTURE_A", Decimal("10.00")),
    (102, "FIXTURE_B", Decimal("20.00")),
    (103, "FIXTURE_C", Decimal("30.00")),
)
_CHANGED = (
    (_SEED[0][0], "FIXTURE_A_UPDATED", _SEED[0][2]),
    *_SEED[1:],
    (104, "FIXTURE_D", Decimal("40.00")),
)
_META = (
    "COLUMN_ID,COLUMN_NAME,DATA_TYPE,DATA_LENGTH,DATA_PRECISION,DATA_SCALE,"
    "CHAR_USED,CHAR_LENGTH,NULLABLE"
)
_CODES = isolated._ERRORS | frozenset({
    "WriterOwnerMismatch",
    "WriterFactoryMissing",
    "NullAuditWitness",
    "DuplicateSurrogate",
    "DuplicateNaturalKey",
    "HistoryMismatch",
    "CurrentFlagInvalid",
    "DateRangeInvalid",
    "BusinessParityMismatch",
    "SourceModelUnbound",
    "source_model_disagrees_with_app",
    "source_model_config_invalid",
    "HttpFailed",
    "VerificationFailed",
    "ScenarioVersionMismatch",
    "ContextMismatch",
    "BackupMissing",
    "TargetPrimaryKeyMismatch",
    "InvalidIdentifier",
    "InvalidCheckpoint",
    "DependencyMissing",
    "app_target_contract_gap",
})
_SHAPES = {
    "empty": (0, 0, 0),
    "seed": (3, 0, 3),
    "changed": (4, 1, 5),
}
_CLEANUP_FLAGS = ("rollback_failed", "cursor_close_failed", "connection_close_failed")


def _freeze_contract() -> None:
    if OPEN_ENDED_DATE_SQL != stock.STOCK_OPEN_ENDED_DATE_SQL:
        raise RuntimeError("open_ended_date")
    if (CURRENT_FLAG, HISTORICAL_FLAG) != (stock.STOCK_CURRENT_FLAG, stock.STOCK_HISTORICAL_FLAG):
        raise RuntimeError("stock_flags")
    if SK != {"F4": "ODI81_F4_SK", "F5": "ODI81_F5_SK"}:
        raise RuntimeError("surrogate_key")


_freeze_contract()


def _varchar(name: str, length: int, nullable: str, ddl: str) -> tuple[Any, ...]:
    return (name, "VARCHAR2", length, None, None, "B", length, nullable, ddl)


def _number(name: str, precision: int | None, scale: int | None, nullable: str, ddl: str) -> tuple[Any, ...]:
    return (name, "NUMBER", 22, precision, scale, None, None, nullable, ddl)


def _audit_defs() -> tuple[tuple[Any, ...], ...]:
    built: list[tuple[Any, ...]] = []
    for name, data_type, nullable in scd2_builder.SCD2_COLUMNS:
        flag = "Y" if nullable else "N"
        if data_type == "DATE":
            built.append((name, "DATE", 7, None, None, None, None, flag, "DATE"))
        elif data_type == "TIMESTAMP":
            built.append((name, "TIMESTAMP(6)", 11, None, 6, None, None, flag, "TIMESTAMP"))
        elif data_type == "NUMBER":
            built.append(_number(name, None, None, flag, "NUMBER"))
        elif data_type.startswith("VARCHAR2(") and data_type.endswith(")"):
            length = int(data_type[len("VARCHAR2("):-1])
            built.append(_varchar(name, length, flag, scd2_builder.oracle_ddl_type(data_type)))
        else:
            raise RuntimeError("unsupported_scd2_type")
    return tuple(built)


_SOURCE_DEFS = (
    _number("ENTITY_ID", 10, 0, "N", "NUMBER(10)"),
    _varchar("ATTR_TEXT", 40, "N", "VARCHAR2(40 BYTE)"),
    _number("VALUE_AMT", 10, 2, "N", "NUMBER(10,2)"),
)
_AUDIT_DEFS = _audit_defs()
SOURCE_CATALOG: tuple[tuple[Any, ...], ...] = tuple(
    (index, *item[:8]) for index, item in enumerate(_SOURCE_DEFS, start=1)
)


def _target_defs(flow: str) -> tuple[tuple[Any, ...], ...]:
    surrogate = _number(SK[flow], None, None, "N", "NUMBER")
    return (surrogate, *_SOURCE_DEFS, *_AUDIT_DEFS)


def target_catalog(flow: str) -> tuple[tuple[Any, ...], ...]:
    """Oracle dictionary rows for the frozen 11-column SCD2 target."""
    return tuple((index, *item[:8]) for index, item in enumerate(_target_defs(flow), start=1))


def backup_name(flow: str, kind: str, run: int) -> str:
    """Retained checkpoint name. Source checkpoints are run 2 only."""
    marker = "S" if kind == "source" else "T"
    return f"ODI81_{flow}_{marker}_R{run}_BKP"


def table_kind(name: str) -> tuple[str, str] | None:
    """Return ``(flow, source|target)`` for a frozen table or its backup."""
    for flow, spec in FLOWS.items():
        if name == spec["source_table"]:
            return flow, "source"
        if name == spec["target_table"]:
            return flow, "target"
        if name == backup_name(flow, "source", 2):
            return flow, "source"
        for run in (1, 2, 3):
            if name == backup_name(flow, "target", run):
                return flow, "target"
    return None


def catalog_rows(table: str) -> tuple[tuple[Any, ...], ...]:
    """Full dictionary projection for one frozen table or backup."""
    kind = table_kind(table)
    if kind is None:
        return SOURCE_CATALOG
    flow, family = kind
    if family == "source":
        return SOURCE_CATALOG
    return target_catalog(flow)


def sqlite_columns(table: str) -> tuple[str, ...]:
    """Column order used by offline parity tests."""
    return tuple(str(row[1]) for row in catalog_rows(table))


def _ident(value: str) -> str:
    if not isinstance(value, str) or not scd2_builder.IDENTIFIER_RE.fullmatch(value) or "$" in value:
        raise isolated._Blocked("InvalidIdentifier")
    return value


def _q(owner: str, name: str) -> str:
    return f'"{_ident(owner)}"."{_ident(name)}"'


def _projection(names: tuple[str, ...]) -> str:
    return ",".join(f'"{_ident(name)}"' for name in names)


def _column_ddl(defn: tuple[Any, ...]) -> str:
    suffix = "" if defn[7] == "Y" else " NOT NULL"
    return f'"{_ident(str(defn[0]))}" {defn[8]}{suffix}'


def _pk_name(table: str) -> str:
    return _ident(f"PK_{table}"[:30])


def source_create_sql(flow: str) -> str:
    """Absent-only source DDL. The primary-key name stays within 30 characters."""
    spec = FLOWS[flow]
    columns = ", ".join(_column_ddl(item) for item in _SOURCE_DEFS)
    return (
        f"CREATE TABLE {_q(spec['source_owner'], spec['source_table'])} ("
        f"{columns}, CONSTRAINT \"{_pk_name(spec['source_table'])}\" PRIMARY KEY (ENTITY_ID))"
    )


def backup_create_sql(flow: str, kind: str, run: int) -> str:
    """Explicit backup DDL so dictionary nullability matches the original."""
    spec = FLOWS[flow]
    owner = spec["source_owner"] if kind == "source" else spec["target_owner"]
    defs = _SOURCE_DEFS if kind == "source" else _target_defs(flow)
    columns = ", ".join(_column_ddl(item) for item in defs)
    return f"CREATE TABLE {_q(owner, backup_name(flow, kind, run))} ({columns})"


def _flag_sql(value: str) -> str:
    if value not in {CURRENT_FLAG, HISTORICAL_FLAG}:
        raise isolated._Blocked("CurrentFlagInvalid")
    return f"'{value}'"


def _report(action: str) -> dict[str, Any]:
    return {
        "tool": "odi81.scd2_fixtures",
        "action": action,
        "status": "blocked",
        "stage": "approval",
        "error_code": "ApprovalRequired",
        "flow": None,
        "counts": {},
        "connection_attempted": False,
        "source_reader_verified": False,
        "ddl_attempted": False,
        "object_may_exist": False,
        "dml_attempted": False,
        "commit_attempted": False,
        "commit_completed": False,
        "outcome_unknown": False,
        "manual_review_required": False,
        "rollback_failed": False,
        "cursor_close_failed": False,
        "connection_close_failed": False,
        "preexisting": False,
        "created": False,
        "verification_failed": False,
        "full_target_snapshot_equal": False,
        "logical_business_comparison_only": False,
        "rerun_semantics": None,
        "physical_dml_count_proven": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "automatic_ddl_rollback_performed": False,
        "target_mutation_performed": False,
    }


def _apply_failure(report: dict[str, Any], exc: Exception) -> None:
    """Redact a callback failure. The public boundary lives in the F1 helper."""
    if any(report[key] for key in _CLEANUP_FLAGS):
        report.update(status="failed", error_code="CleanupFailed", manual_review_required=True)
        if report["dml_attempted"] and not report["commit_completed"] and report["rollback_failed"]:
            report["outcome_unknown"] = True
        return
    code = exc.args[0] if type(exc) is isolated._Blocked and exc.args else None
    report["error_code"] = code if isinstance(code, str) and code in _CODES else "OperationFailed"
    wrote = report["ddl_attempted"] or report["dml_attempted"] or report["verification_failed"]
    report["status"] = "failed" if wrote else "blocked"
    report["manual_review_required"] |= report["ddl_attempted"] or report["dml_attempted"]
    report["outcome_unknown"] |= report["commit_attempted"] and not report["commit_completed"]


def _finish(report: dict[str, Any]) -> dict[str, Any]:
    if report["status"] != "passed":
        report["full_target_snapshot_equal"] = False
        report["logical_business_comparison_only"] = False
        report["rerun_semantics"] = None
    return report


def _authorize(report: dict[str, Any], flow: object, approved: object, scope: object, expected: str) -> bool:
    if approved is not True or not isinstance(flow, str) or flow not in FLOWS:
        return False
    report["flow"] = flow
    if scope != expected:
        report["error_code"] = "ApprovalScopeMismatch"
        return False
    return True


def _owners_for(flow: str) -> tuple[str, ...]:
    spec = FLOWS[flow]
    return tuple(dict.fromkeys((spec["source_owner"], spec["target_owner"])))


def prove_owners(owners: tuple[str, ...], context: object, raw: object) -> None:
    """Prove the injected binding matches the app model map. No connection is opened."""
    for owner in owners:
        if owner not in scd2_builder.SOURCE_MODEL_BY_OWNER:
            raise isolated._Blocked("source_model_disagrees_with_app")
        expected = scd2_builder.SOURCE_MODEL_BY_OWNER[owner]
        target_expected = scd2_builder.TARGET_MODEL_BY_OWNER.get(owner)
        if target_expected is not None and target_expected != expected:
            raise isolated._Blocked("source_model_disagrees_with_app")
        try:
            proven = source_model_policy.trusted_source_model_code(context, owner, raw)
        except source_model_policy.SourceModelConfigError:
            raise isolated._Blocked("source_model_config_invalid") from None
        if proven is None:
            raise isolated._Blocked("SourceModelUnbound")
        if proven != expected:
            raise isolated._Blocked("source_model_disagrees_with_app")


def _gate_models(report: dict[str, Any], owners: tuple[str, ...], context: object, raw: object) -> bool:
    try:
        prove_owners(owners, context, raw)
    except isolated._Blocked as exc:
        code = exc.args[0] if exc.args else None
        report["error_code"] = code if isinstance(code, str) and code in _CODES else "OperationFailed"
        report["status"] = "blocked"
        return False
    return True


def _factory(factories: object, owner: str) -> Factory:
    if not isinstance(factories, Mapping) or not callable(factories.get(owner)):
        raise isolated._Blocked("WriterFactoryMissing")
    return factories[owner]


def _release_step(report: dict[str, Any], flag: str, resource: Any, method: str) -> None:
    if resource is None:
        return
    report[flag] = True
    getattr(resource, method)()
    report[flag] = False


@contextmanager
def _session(report: dict[str, Any], factory: Factory, *, readonly: bool) -> Iterator[tuple[Any, Any]]:
    """Open one injected connection and close it even after process-control signals."""
    report["connection_attempted"] = True
    connection = factory()
    stack = ExitStack()
    stack.callback(_release_step, report, "connection_close_failed", connection, "close")
    try:
        if connection.autocommit is not False:
            raise isolated._Blocked("AutocommitNotDisabled")
        cursor = connection.cursor()
        stack.callback(_release_step, report, "cursor_close_failed", cursor, "close")
        stack.callback(_release_step, report, "rollback_failed", connection, "rollback")
        if readonly:
            cursor.execute("SET TRANSACTION READ ONLY")
        yield connection, cursor
    finally:
        stack.close()


def _exists(cursor: Any, owner: str, name: str) -> int:
    return isolated._count(
        cursor,
        "SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name",
        {"owner": owner, "name": name},
    )


def _permanent(cursor: Any, owner: str, name: str) -> None:
    isolated._need(
        isolated._count(
            cursor,
            "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:name AND TEMPORARY='N'",
            {"owner": owner, "name": name},
        ) == 1,
        "NotPermanentTable",
    )


def _triggers(cursor: Any, owner: str, name: str) -> None:
    isolated._need(
        isolated._count(
            cursor,
            "SELECT COUNT(*) FROM ALL_TRIGGERS WHERE TABLE_OWNER=:owner AND TABLE_NAME=:name "
            "AND STATUS='ENABLED'",
            {"owner": owner, "name": name},
        ) == 0,
        "SourceTriggersPresent",
    )


def _metadata(cursor: Any, owner: str, name: str, family: str, flow: str) -> None:
    expected = SOURCE_CATALOG if family == "source" else target_catalog(flow)
    isolated._need(
        isolated._count(
            cursor,
            "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name",
            {"owner": owner, "table_name": name},
        ) == len(expected),
        "ColumnContractMismatch",
    )
    statement = (
        "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name "
        "AND COLUMN_ID=:column_id AND COLUMN_NAME=:column_name AND DATA_TYPE=:data_type "
        "AND DATA_LENGTH=:data_length "
        "AND ((DATA_PRECISION IS NULL AND :data_precision IS NULL) OR DATA_PRECISION=:data_precision) "
        "AND ((DATA_SCALE IS NULL AND :data_scale IS NULL) OR DATA_SCALE=:data_scale) "
        "AND ((CHAR_USED IS NULL AND :char_used IS NULL) OR CHAR_USED=:char_used) "
        "AND ((CHAR_LENGTH IS NULL AND :char_length IS NULL) OR CHAR_LENGTH=:char_length) "
        "AND NULLABLE=:nullable"
    )
    for row in expected:
        params = {
            "owner": owner,
            "table_name": name,
            "column_id": row[0],
            "column_name": row[1],
            "data_type": row[2],
            "data_length": row[3],
            "data_precision": row[4],
            "data_scale": row[5],
            "char_used": row[6],
            "char_length": row[7],
            "nullable": row[8],
        }
        isolated._need(isolated._count(cursor, statement, params) == 1, "ColumnContractMismatch")


def _metadata_equal(cursor: Any, owner: str, left: str, right: str) -> None:
    statement = (
        "SELECT COUNT(*) FROM ("
        f"SELECT {_META} FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:left "
        f"MINUS SELECT {_META} FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:right)"
    )
    for first, second in ((left, right), (right, left)):
        isolated._need(
            isolated._count(cursor, statement, {"owner": owner, "left": first, "right": second}) == 0,
            "BackupMetadataMismatch",
        )


def _primary_key(cursor: Any, owner: str, table: str, column: str, code: str) -> None:
    statement = (
        "SELECT COUNT(*) FROM ALL_CONSTRAINTS c WHERE c.OWNER=:owner AND c.TABLE_NAME=:table_name "
        "AND c.CONSTRAINT_TYPE='P' AND c.STATUS='ENABLED' AND c.VALIDATED='VALIDATED' "
        "AND (SELECT COUNT(*) FROM ALL_CONS_COLUMNS k WHERE k.OWNER=c.OWNER "
        "AND k.CONSTRAINT_NAME=c.CONSTRAINT_NAME)=1 "
        "AND EXISTS (SELECT 1 FROM ALL_CONS_COLUMNS k WHERE k.OWNER=c.OWNER "
        "AND k.CONSTRAINT_NAME=c.CONSTRAINT_NAME AND k.COLUMN_NAME=:column_name AND k.POSITION=1)"
    )
    isolated._need(
        isolated._count(
            cursor,
            statement,
            {"owner": owner, "table_name": table, "column_name": column},
        ) == 1,
        code,
    )


def _writer(cursor: Any, owner: str) -> None:
    isolated._need(
        isolated._count(
            cursor,
            "SELECT COUNT(*) FROM DUAL WHERE SYS_CONTEXT('USERENV','SESSION_USER')=:owner",
            {"owner": owner},
        ) == 1,
        "WriterOwnerMismatch",
    )


def _reader_capable(cursor: Any, owner: str) -> None:
    isolated._need(
        isolated._count(
            cursor,
            "SELECT COUNT(*) FROM DUAL WHERE SYS_CONTEXT('USERENV','SESSION_USER')=:owner "
            "OR EXISTS (SELECT 1 FROM SESSION_PRIVS WHERE PRIVILEGE='SELECT ANY TABLE')",
            {"owner": owner},
        ) == 1,
        "SourceReaderCannotReadNewFixture",
    )


def _lock(cursor: Any, owner: str, name: str, mode: str) -> None:
    if mode not in {"EXCLUSIVE", "SHARE"}:
        raise isolated._Blocked("InvalidIdentifier")
    cursor.execute(f"LOCK TABLE {_q(owner, name)} IN {mode} MODE NOWAIT")


def _expected_business(state: str) -> tuple[str, dict[str, Any]]:
    rows = _SEED if state == "seed" else _CHANGED
    params: dict[str, Any] = {}
    selects: list[str] = []
    for index, row in enumerate(rows):
        params.update({f"k{index}": row[0], f"t{index}": row[1], f"v{index}": row[2]})
        selects.append(f"SELECT :k{index} ENTITY_ID,:t{index} ATTR_TEXT,:v{index} VALUE_AMT FROM DUAL")
    return "SELECT ENTITY_ID,ATTR_TEXT,VALUE_AMT FROM (" + " UNION ALL ".join(selects) + ")", params


def _projection_equal(cursor: Any, left_sql: str, right_sql: str, params: dict[str, Any], code: str) -> None:
    for first, second in ((left_sql, right_sql), (right_sql, left_sql)):
        isolated._need(
            isolated._count(cursor, f"SELECT COUNT(*) FROM ({first} MINUS {second})", params) == 0,
            code,
        )


def _source_state(cursor: Any, owner: str, name: str, state: str) -> dict[str, int]:
    table_sql = _q(owner, name)
    total = isolated._count(cursor, f"SELECT COUNT(*) FROM {table_sql}")
    nulls = isolated._count(
        cursor,
        f'SELECT COUNT(*) FROM {table_sql} WHERE "ENTITY_ID" IS NULL OR "ATTR_TEXT" IS NULL '
        'OR "VALUE_AMT" IS NULL',
    )
    duplicates = isolated._count(
        cursor,
        f'SELECT COUNT(*) FROM (SELECT "ENTITY_ID" FROM {table_sql} GROUP BY "ENTITY_ID" HAVING COUNT(*)>1)',
    )
    isolated._need(
        total == {"empty": 0, "seed": 3, "changed": 4}[state] and nulls == 0 and duplicates == 0,
        "FixtureStateMismatch",
    )
    if state != "empty":
        expected_sql, params = _expected_business(state)
        actual = f"SELECT {_projection(BUSINESS)} FROM {table_sql}"
        _projection_equal(cursor, actual, expected_sql, params, "FixtureStateMismatch")
    return {"rows": total, "null_business_rows": nulls, "duplicate_key_groups": duplicates}


def _reader_check(
    report: dict[str, Any], factory: Factory, owner: str, table: str, flow: str, state: str | None,
) -> None:
    report["source_reader_verified"] = False
    with _session(report, factory, readonly=True) as (_connection, cursor):
        if state is None:
            _reader_capable(cursor, owner)
            return
        _permanent(cursor, owner, table)
        _metadata(cursor, owner, table, "source", flow)
        _source_state(cursor, owner, table, state)
        report["source_reader_verified"] = True


def _commit(report: dict[str, Any], connection: Any) -> None:
    report.update(stage="commit", commit_attempted=True)
    connection.commit()
    report["commit_completed"] = True


def _seed(cursor: Any, owner: str, table: str) -> None:
    for key, text, amount in _SEED:
        cursor.execute(
            f"INSERT INTO {_q(owner, table)} ({_projection(BUSINESS)}) VALUES (:key,:text,:amount)",
            {"key": key, "text": text, "amount": amount},
        )
        isolated._need(type(cursor.rowcount) is int and cursor.rowcount == 1, "AffectedCountMismatch")


def prepare_source(
    flow: str,
    writer_factories: Mapping[str, Factory],
    source_reader_factory: Factory,
    *,
    approved: object = False,
    approval_scope: object = None,
    context: object = "GLOBAL",
    source_model_bindings: object = None,
) -> dict[str, Any]:
    """Create and seed one absent F4/F5 source. DDL is not rolled back."""
    report = _report("prepare_source")
    expected = f"odi81_{flow.lower()}_prepare_source" if isinstance(flow, str) else ""
    if not _authorize(report, flow, approved, approval_scope, expected):
        return report
    if not _gate_models(report, _owners_for(flow), context, source_model_bindings):
        return report
    resources: list[Any] = []
    with isolated._operation(report, resources, failure=_apply_failure):
        spec = FLOWS[flow]
        owner, table = spec["source_owner"], spec["source_table"]
        report["stage"] = "source_reader_preflight"
        _reader_check(report, source_reader_factory, owner, table, flow, None)
        report["stage"] = "absence_preflight"
        with _session(report, _factory(writer_factories, owner), readonly=False) as (connection, cursor):
            _writer(cursor, owner)
            isolated._need(_exists(cursor, owner, table) == 0, "SourceAlreadyExists")
            report.update(stage="create_source", ddl_attempted=True, object_may_exist=True)
            cursor.execute(source_create_sql(flow))
            _permanent(cursor, owner, table)
            _lock(cursor, owner, table, "EXCLUSIVE")
            _metadata(cursor, owner, table, "source", flow)
            _primary_key(cursor, owner, table, "ENTITY_ID", "SourcePrimaryKeyMismatch")
            _triggers(cursor, owner, table)
            report.update(stage="seed_source", dml_attempted=True)
            _seed(cursor, owner, table)
            _metadata(cursor, owner, table, "source", flow)
            report["counts"] = _source_state(cursor, owner, table, "seed")
            _commit(report, connection)
        report["stage"] = "source_reader_postflight"
        _reader_check(report, source_reader_factory, owner, table, flow, "seed")
        report.update(status="passed", stage="completed", error_code=None, created=True)
    return _finish(report)


def _lock_named(cursor: Any, owner: str, name: str, mode: str, missing: str) -> None:
    isolated._need(_exists(cursor, owner, name) == 1, missing)
    _permanent(cursor, owner, name)
    _lock(cursor, owner, name, mode)


def _mutate_locked(cursor: Any, flow: str, *, lock_target: bool) -> None:
    spec = FLOWS[flow]
    source_owner, source_name = spec["source_owner"], spec["source_table"]
    backup = backup_name(flow, "source", 2)
    _lock_named(cursor, source_owner, source_name, "EXCLUSIVE", "NotPermanentTable")
    _lock_named(cursor, source_owner, backup, "SHARE", "BackupMissing")
    if lock_target:
        _lock_named(cursor, spec["target_owner"], spec["target_table"], "SHARE", "NotPermanentTable")
    _metadata(cursor, source_owner, source_name, "source", flow)
    _triggers(cursor, source_owner, source_name)
    _metadata_equal(cursor, source_owner, source_name, backup)
    _source_state(cursor, source_owner, source_name, "seed")
    _source_state(cursor, source_owner, backup, "seed")


def mutate_source(
    flow: str,
    writer_factories: Mapping[str, Factory],
    source_reader_factory: Factory,
    *,
    approved: object = False,
    approval_scope: object = None,
    context: object = "GLOBAL",
    source_model_bindings: object = None,
) -> dict[str, Any]:
    """Update one source row and insert one, after the retained seed backup matches."""
    report = _report("mutate_source")
    expected = f"odi81_{flow.lower()}_mutate_source" if isinstance(flow, str) else ""
    if not _authorize(report, flow, approved, approval_scope, expected):
        return report
    if not _gate_models(report, _owners_for(flow), context, source_model_bindings):
        return report
    resources: list[Any] = []
    with isolated._operation(report, resources, failure=_apply_failure):
        spec = FLOWS[flow]
        owner, table = spec["source_owner"], spec["source_table"]
        report["stage"] = "source_reader_preflight"
        _reader_check(report, source_reader_factory, owner, table, flow, "seed")
        report["stage"] = "backup_preflight"
        same_owner = spec["target_owner"] == owner
        target_session = (
            _session(report, _factory(writer_factories, spec["target_owner"]), readonly=False)
            if not same_owner else nullcontext((None, None))
        )
        with target_session as (_target_connection, target_cursor):
            if target_cursor is not None:
                _writer(target_cursor, spec["target_owner"])
                _lock_named(target_cursor, spec["target_owner"], spec["target_table"], "SHARE", "NotPermanentTable")
            with _session(report, _factory(writer_factories, owner), readonly=False) as (connection, cursor):
                _writer(cursor, owner)
                _mutate_locked(cursor, flow, lock_target=same_owner)
                report.update(stage="mutate_source", dml_attempted=True)
                cursor.execute(
                    f'UPDATE {_q(owner, table)} SET "ATTR_TEXT"=:text WHERE "ENTITY_ID"=:key',
                    {"text": _CHANGED[0][1], "key": _SEED[0][0]},
                )
                isolated._need(type(cursor.rowcount) is int and cursor.rowcount == 1, "AffectedCountMismatch")
                cursor.execute(
                    f"INSERT INTO {_q(owner, table)} ({_projection(BUSINESS)}) VALUES (:key,:text,:amount)",
                    {"key": _CHANGED[-1][0], "text": _CHANGED[-1][1], "amount": _CHANGED[-1][2]},
                )
                isolated._need(type(cursor.rowcount) is int and cursor.rowcount == 1, "AffectedCountMismatch")
                report["counts"] = {**_source_state(cursor, owner, table, "changed"), "updated": 1, "inserted": 1}
                _commit(report, connection)
        report["stage"] = "source_reader_postflight"
        _reader_check(report, source_reader_factory, owner, table, flow, "changed")
        report.update(status="passed", stage="completed", error_code=None)
    return _finish(report)


def _copy_rows(cursor: Any, owner: str, original: str, backup: str, columns: tuple[str, ...], expected: int) -> None:
    projection = _projection(columns)
    cursor.execute(
        f"INSERT INTO {_q(owner, backup)} ({projection}) SELECT {projection} FROM {_q(owner, original)}"
    )
    isolated._need(type(cursor.rowcount) is int and cursor.rowcount == expected, "AffectedCountMismatch")


def _checkpoint_matches(cursor: Any, flow: str, kind: str, owner: str, original: str, backup: str) -> None:
    columns = BUSINESS if kind == "source" else (SK[flow], *BUSINESS, *AUDIT)
    _metadata_equal(cursor, owner, original, backup)
    left = f"SELECT {_projection(columns)} FROM {_q(owner, original)}"
    right = f"SELECT {_projection(columns)} FROM {_q(owner, backup)}"
    _projection_equal(cursor, left, right, {}, "InvalidCheckpoint")


def create_backup(
    flow: str,
    kind: str,
    run: int,
    writer_factories: Mapping[str, Factory],
    *,
    approved: object = False,
    approval_scope: object = None,
    context: object = "GLOBAL",
    source_model_bindings: object = None,
) -> dict[str, Any]:
    """Create one absent retained checkpoint, or keep an exact existing one."""
    report = _report("create_backup")
    if kind not in {"source", "target"} or type(run) is not int:
        report["error_code"] = "InvalidCheckpoint"
        return report
    if kind == "source" and run != 2 or kind == "target" and run not in (1, 2, 3):
        report["error_code"] = "InvalidCheckpoint"
        return report
    action = "backup_source" if kind == "source" else f"backup_target_r{run}"
    expected = f"odi81_{flow.lower()}_{action}" if isinstance(flow, str) else ""
    if not _authorize(report, flow, approved, approval_scope, expected):
        return report
    if not _gate_models(report, _owners_for(flow), context, source_model_bindings):
        return report
    resources: list[Any] = []
    with isolated._operation(report, resources, failure=_apply_failure):
        spec = FLOWS[flow]
        owner = spec["source_owner"] if kind == "source" else spec["target_owner"]
        original = spec["source_table"] if kind == "source" else spec["target_table"]
        family = "source" if kind == "source" else "target"
        backup = backup_name(flow, kind, run)
        columns = BUSINESS if kind == "source" else (SK[flow], *BUSINESS, *AUDIT)
        report["stage"] = "backup_preflight"
        with _session(report, _factory(writer_factories, owner), readonly=False) as (connection, cursor):
            _writer(cursor, owner)
            isolated._need(_exists(cursor, owner, original) == 1, "BackupMissing")
            _permanent(cursor, owner, original)
            _lock(cursor, owner, original, "EXCLUSIVE")
            _metadata(cursor, owner, original, family, flow)
            if kind == "source":
                _primary_key(cursor, owner, original, "ENTITY_ID", "SourcePrimaryKeyMismatch")
                _triggers(cursor, owner, original)
            else:
                _primary_key(cursor, owner, original, SK[flow], "TargetPrimaryKeyMismatch")
            original_count = isolated._count(cursor, f"SELECT COUNT(*) FROM {_q(owner, original)}")
            if _exists(cursor, owner, backup) != 0:
                report.update(stage="retain_backup", preexisting=True)
                _permanent(cursor, owner, backup)
                _lock(cursor, owner, backup, "SHARE")
                _checkpoint_matches(cursor, flow, kind, owner, original, backup)
                report["counts"] = {"rows": original_count}
                report.update(status="passed", stage="completed", error_code=None, created=False)
            else:
                report.update(stage="create_backup", ddl_attempted=True, object_may_exist=True)
                cursor.execute(backup_create_sql(flow, kind, run))
                _permanent(cursor, owner, backup)
                _lock(cursor, owner, backup, "EXCLUSIVE")
                _metadata_equal(cursor, owner, original, backup)
                report.update(stage="copy_backup", dml_attempted=True)
                _copy_rows(cursor, owner, original, backup, columns, original_count)
                _checkpoint_matches(cursor, flow, kind, owner, original, backup)
                report["counts"] = {"rows": original_count}
                _commit(report, connection)
                report.update(status="passed", stage="completed", error_code=None, created=True)
    return _finish(report)


def _null_audit(cursor: Any, owner: str, table: str, columns: tuple[str, ...]) -> int:
    predicate = " OR ".join(f'"{_ident(name)}" IS NULL' for name in columns)
    return isolated._count(cursor, f"SELECT COUNT(*) FROM {_q(owner, table)} WHERE {predicate}")


def _rule_counts(cursor: Any, flow: str, owner: str, table: str) -> dict[str, int]:
    sql = _q(owner, table)
    sk = _ident(SK[flow])
    current = _flag_sql(CURRENT_FLAG)
    historical = _flag_sql(HISTORICAL_FLAG)
    duplicate_sk = isolated._count(
        cursor,
        f'SELECT COUNT(*) FROM (SELECT "{sk}" FROM {sql} GROUP BY "{sk}" HAVING COUNT(*)>1)',
    )
    duplicate_nk = isolated._count(
        cursor,
        "SELECT COUNT(*) FROM (SELECT \"ENTITY_ID\" FROM "
        f"{sql} GROUP BY \"ENTITY_ID\" HAVING SUM(CASE WHEN \"CURRENT_FLAG\"={current} THEN 1 ELSE 0 END)<>1)",
    )
    invalid_flags = isolated._count(
        cursor,
        f'SELECT COUNT(*) FROM {sql} WHERE "CURRENT_FLAG" IS NULL OR "CURRENT_FLAG" NOT IN ({current}, {historical})',
    )
    invalid_dates = isolated._count(
        cursor,
        f'SELECT COUNT(*) FROM {sql} WHERE "EFFECTIVE_FROM" > "EFFECTIVE_TO" '
        f'OR ("CURRENT_FLAG"={current} AND "EFFECTIVE_TO" <> {OPEN_ENDED_DATE_SQL}) '
        f'OR ("CURRENT_FLAG"={historical} AND "EFFECTIVE_TO" >= {OPEN_ENDED_DATE_SQL})',
    )
    history_mismatch = isolated._count(
        cursor,
        f'SELECT COUNT(*) FROM {sql} h WHERE h."CURRENT_FLAG"={historical} AND NOT EXISTS ('
        f'SELECT 1 FROM {sql} c WHERE c."ENTITY_ID"=h."ENTITY_ID" AND c."CURRENT_FLAG"={current} '
        'AND c."EFFECTIVE_FROM"=h."EFFECTIVE_TO")',
    )
    return {
        "duplicate_sk_groups": duplicate_sk,
        "duplicate_nk_groups": duplicate_nk,
        "invalid_flag_rows": invalid_flags,
        "invalid_date_rows": invalid_dates,
        "history_mismatch_rows": history_mismatch,
    }


def _require_rules(counts: dict[str, int]) -> None:
    isolated._need(counts["null_audit_rows"] == 0, "NullAuditWitness")
    isolated._need(counts["invalid_flag_rows"] == 0, "CurrentFlagInvalid")
    isolated._need(counts["invalid_date_rows"] == 0, "DateRangeInvalid")
    isolated._need(counts["duplicate_sk_groups"] == 0, "DuplicateSurrogate")
    isolated._need(counts["duplicate_nk_groups"] == 0, "DuplicateNaturalKey")
    isolated._need(counts["history_mismatch_rows"] == 0, "HistoryMismatch")


def _business_parity(
    cursor: Any, target_owner: str, target: str, source_sql: str, params: dict[str, Any],
) -> tuple[int, int]:
    current_sql = (
        f"SELECT {_projection(BUSINESS)} FROM {_q(target_owner, target)} "
        f"WHERE \"CURRENT_FLAG\"={_flag_sql(CURRENT_FLAG)}"
    )
    left = isolated._count(cursor, f"SELECT COUNT(*) FROM ({current_sql} MINUS {source_sql})", params)
    right = isolated._count(cursor, f"SELECT COUNT(*) FROM ({source_sql} MINUS {current_sql})", params)
    isolated._need(left == 0 and right == 0, "BusinessParityMismatch")
    return left, right


def _prove_target_shape(
    cursor: Any,
    flow: str,
    owner: str,
    table: str,
    state: str,
    source_sql: str | None,
    params: dict[str, Any],
) -> dict[str, int]:
    columns = (SK[flow], *BUSINESS, *AUDIT)
    table_sql = _q(owner, table)
    current = isolated._count(
        cursor, f'SELECT COUNT(*) FROM {table_sql} WHERE "CURRENT_FLAG"={_flag_sql(CURRENT_FLAG)}',
    )
    historical = isolated._count(
        cursor, f'SELECT COUNT(*) FROM {table_sql} WHERE "CURRENT_FLAG"={_flag_sql(HISTORICAL_FLAG)}',
    )
    total = isolated._count(cursor, f"SELECT COUNT(*) FROM {table_sql}")
    counts = {
        "current_rows": current,
        "historical_rows": historical,
        "target_rows": total,
        "null_audit_rows": _null_audit(cursor, owner, table, columns),
        **_rule_counts(cursor, flow, owner, table),
    }
    if total == 0 and state == "empty":
        isolated._need(counts["null_audit_rows"] == 0, "NullAuditWitness")
        isolated._need((current, historical, total) == _SHAPES["empty"], "FixtureStateMismatch")
        return counts
    _require_rules(counts)
    isolated._need((current, historical, total) == _SHAPES[state], "FixtureStateMismatch")
    if source_sql is not None:
        minus_current, minus_source = _business_parity(cursor, owner, table, source_sql, params)
        counts["current_minus_source"] = minus_current
        counts["source_minus_current"] = minus_source
    return counts


def _snapshot_equal(cursor: Any, flow: str, owner: str, table: str, backup: str) -> None:
    columns = (SK[flow], *BUSINESS, *AUDIT)
    projection = _projection(columns)
    left = f"SELECT {projection} FROM {_q(owner, table)}"
    right = f"SELECT {projection} FROM {_q(owner, backup)}"
    _projection_equal(cursor, left, right, {}, "SnapshotMismatch")


def _verify_scope(flow: object, run: object, phase: object) -> str | None:
    if not isinstance(flow, str) or type(run) is not int or run not in (1, 2, 3):
        return None
    if phase == "before_run":
        return f"odi81_{flow.lower()}_verify_before_r{run}"
    if phase == "after_run":
        return f"odi81_{flow.lower()}_verify_after_r{run}"
    return None


def verify_target(
    flow: str,
    run: int,
    phase: str,
    reader_factories: Mapping[str, Factory],
    source_reader_factory: Factory,
    *,
    approved: object = False,
    approval_scope: object = None,
    context: object = "GLOBAL",
    source_model_bindings: object = None,
) -> dict[str, Any]:
    """Read-only SCD2 evidence. Run 3 after_run is the only full-snapshot witness."""
    report = _report("verify_target")
    expected = _verify_scope(flow, run, phase)
    if expected is None:
        report["error_code"] = "InvalidCheckpoint"
        return report
    if not _authorize(report, flow, approved, approval_scope, expected):
        return report
    if not _gate_models(report, _owners_for(flow), context, source_model_bindings):
        return report
    resources: list[Any] = []
    with isolated._operation(report, resources, failure=_apply_failure):
        report["verification_failed"] = True
        spec = FLOWS[flow]
        source_state = "seed" if run == 1 else "changed"
        target_state = {
            ("before_run", 1): "empty",
            ("after_run", 1): "seed",
            ("before_run", 2): "seed",
            ("after_run", 2): "changed",
            ("before_run", 3): "changed",
            ("after_run", 3): "changed",
        }[(phase, run)]
        backup_state = {1: "empty", 2: "seed", 3: "changed"}[run]
        report["stage"] = "source_reader_preflight"
        _reader_check(
            report, source_reader_factory, spec["source_owner"], spec["source_table"], flow, source_state,
        )
        report["stage"] = "snapshot_verification"
        with _session(report, _factory(reader_factories, spec["target_owner"]), readonly=True) as (
            _connection, cursor,
        ):
            _permanent(cursor, spec["target_owner"], spec["target_table"])
            _metadata(cursor, spec["target_owner"], spec["target_table"], "target", flow)
            _primary_key(cursor, spec["target_owner"], spec["target_table"], SK[flow], "TargetPrimaryKeyMismatch")
            backup = backup_name(flow, "target", run)
            isolated._need(_exists(cursor, spec["target_owner"], backup) == 1, "BackupMissing")
            _permanent(cursor, spec["target_owner"], backup)
            _metadata_equal(cursor, spec["target_owner"], spec["target_table"], backup)
            source_sql = f"SELECT {_projection(BUSINESS)} FROM {_q(spec['source_owner'], spec['source_table'])}"
            live_parity = None if target_state == "empty" else source_sql
            counts = _prove_target_shape(
                cursor, flow, spec["target_owner"], spec["target_table"], target_state, live_parity, {},
            )
            constant_sql, params = (None, {}) if backup_state == "empty" else _expected_business(backup_state)
            _prove_target_shape(
                cursor, flow, spec["target_owner"], backup, backup_state, constant_sql, params,
            )
            compare_now = phase == "before_run" or (phase == "after_run" and run == 3)
            if compare_now:
                _snapshot_equal(cursor, flow, spec["target_owner"], spec["target_table"], backup)
            report["counts"] = counts
            report["run"] = run
            report["phase"] = phase
            if phase == "after_run" and run == 3:
                report["full_target_snapshot_equal"] = True
                report["logical_business_comparison_only"] = False
                report["rerun_semantics"] = "incremental"
            else:
                report["full_target_snapshot_equal"] = False
                report["logical_business_comparison_only"] = True
                report["rerun_semantics"] = None
            report["physical_dml_count_proven"] = False
            report.update(status="passed", stage="completed", error_code=None)
    return _finish(report)
