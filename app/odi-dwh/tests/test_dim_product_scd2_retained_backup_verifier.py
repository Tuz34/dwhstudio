"""Offline retained verification with an exact SQL whitelist and no runtime config."""

from copy import deepcopy
from functools import partial

import oracledb
import pytest

from dim_product_scd2_backup_creator import verify_retained_dim_product_backup
from dim_product_scd2_scenario_backup import _proof_matches, run_dim_product_scd2_named_backup_gate
from tests.test_dim_product_scd2_scenario_backup import _preflight

SOURCE = "DIM_PRODUCT"
BACKUP = "ODI68_DIM_PRODUCT_BKP"
ODS = "ODI68_ODS_PRODUCTS_BKP"
EXISTS_SQL = "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name"
COLUMNS_SQL = "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name"
METADATA_SQL = (
    "SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE, DATA_TYPE_OWNER, DATA_LENGTH, "
    "DATA_PRECISION, DATA_SCALE, CHAR_LENGTH, CHAR_USED "
    "FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name ORDER BY COLUMN_ID"
)
NULL_SQL = 'SELECT COUNT(*) FROM "DWH"."ODI68_DIM_PRODUCT_BKP" WHERE "PRODUCT_SK" IS NULL'
DUPLICATE_SQL = (
    'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."ODI68_DIM_PRODUCT_BKP" '
    'GROUP BY "PRODUCT_SK" HAVING COUNT(*) > 1)'
)
FORWARD_SQL = (
    'SELECT COUNT(*) FROM (SELECT * FROM "DWH"."DIM_PRODUCT" '
    'MINUS SELECT * FROM "DWH"."ODI68_DIM_PRODUCT_BKP")'
)
REVERSE_SQL = (
    'SELECT COUNT(*) FROM (SELECT * FROM "DWH"."ODI68_DIM_PRODUCT_BKP" '
    'MINUS SELECT * FROM "DWH"."DIM_PRODUCT")'
)


class RetainedDb:
    def __init__(self):
        metadata = [
            ("PRODUCT_SK" if i == 1 else f"C{i}", i, "NUMBER", None, 22, 10, 0, 0, None)
            for i in range(1, 27)
        ]
        metadata[-1] = ("C26", 26, "VARCHAR2", None, 80, None, None, 20, "C")
        self.metadata = {SOURCE: metadata, BACKUP: deepcopy(metadata)}
        self.tables = {SOURCE: (26, 72), BACKUP: (26, 72), ODS: (24, 0)}
        self.counts = {NULL_SQL: 0, DUPLICATE_SQL: 0, FORWARD_SQL: 0, REVERSE_SQL: 0}
        self.statements = []
        self.events = []
        self.fail_at = None
        self.cleanup_failures = set()
        self.cleanup_error = oracledb.DatabaseError
        self.result = None
        self.active_sql = None
        self.connections = 0
        self.closed = 0

    def __call__(self):
        self.connections += 1
        self.events.append("connect")
        if self.fail_at == "connect":
            raise oracledb.DatabaseError("sensitive-value")
        return self

    def cursor(self):
        self.events.append("cursor")
        if self.fail_at == "cursor":
            raise oracledb.DatabaseError("sensitive-value")
        return self

    def execute(self, sql, params=None):
        params = params or {}
        self.statements.append(sql)
        self.events.append(sql)
        self.active_sql = sql
        if self.fail_at == sql:
            raise oracledb.DatabaseError("sensitive-value")
        if sql == "SET TRANSACTION READ ONLY":
            assert len(self.statements) == 1
            assert params == {}
        elif sql in {EXISTS_SQL, COLUMNS_SQL, METADATA_SQL}:
            assert set(params) == {"owner", "table_name"}
            assert params["owner"] == "DWH"
            table = params["table_name"]
            assert table in {SOURCE, BACKUP, ODS}
            if sql == EXISTS_SQL:
                self.result = [(int(table in self.tables),)]
            elif sql == COLUMNS_SQL:
                assert table == ODS
                self.result = [(self.tables[table][0],)]
            else:
                assert table in {SOURCE, BACKUP}
                self.result = self.metadata[table]
        elif sql in {f'SELECT COUNT(*) FROM "DWH"."{table}"' for table in self.tables}:
            assert params == {}
            table = sql.split('"')[3]
            self.result = [(self.tables[table][1],)]
        elif sql in self.counts:
            assert params == {}
            self.result = [(self.counts[sql],)]
        else:
            raise AssertionError(f"SQL outside fixed read-only whitelist: {sql}")

    def fetchone(self):
        assert self.active_sql != METADATA_SQL
        return self.result[0]

    def fetchall(self):
        assert self.active_sql == METADATA_SQL  # no full data rows can cross the boundary
        return self.result

    def rollback(self):
        self.events.append("rollback")
        if "rollback" in self.cleanup_failures:
            raise self.cleanup_error("sensitive-value")

    def close(self):
        self.closed += 1
        label = "cursor_close" if self.closed == 1 else "connection_close"
        self.events.append(label)
        if label in self.cleanup_failures:
            raise self.cleanup_error("sensitive-value")


def test_retained_verifier_passes_exact_opt_in_gate_without_changing_any_table():
    db = RetainedDb()
    before = deepcopy(db.tables)
    proof = verify_retained_dim_product_backup(db)
    assert _proof_matches(proof, retained_backup=True)
    assert not _proof_matches(proof)
    assert proof["backup"]["verified_retained"] is True
    assert "created_absent_only" not in proof["backup"]
    assert proof["side_effects"] is False
    assert db.statements[0] == "SET TRANSACTION READ ONLY"
    assert db.statements[-2:] == [FORWARD_SQL, REVERSE_SQL]
    assert db.events[-3:] == ["rollback", "cursor_close", "connection_close"]
    assert db.tables == before
    gate_db = RetainedDb()
    result = run_dim_product_scd2_named_backup_gate(
        _preflight(), backup_creator=partial(verify_retained_dim_product_backup, gate_db),
        retained_backup=True,
    )
    assert result["status"] == "passed"
    assert result["backup_reused_verified"] is True
    assert result["backup_created_absent_only"] is False
    assert result["backup_creation_attempted"] is False
    assert result["side_effects_may_have_occurred"] is False


@pytest.mark.parametrize("table", [SOURCE, BACKUP, ODS])
def test_missing_table_fails_closed_and_always_cleans_up(table):
    db = RetainedDb()
    del db.tables[table]
    result = verify_retained_dim_product_backup(db)
    assert result["status"] == "failed"
    assert result["side_effects"] is False
    assert result["backup_creation_attempted"] is False
    assert result["automatic_restore_performed"] is False
    assert result["automatic_drop_performed"] is False
    assert db.events[-3:] == ["rollback", "cursor_close", "connection_close"]


@pytest.mark.parametrize("index,value", [
    (0, "OTHER_NAME"), (1, 27), (2, "BINARY_FLOAT"), (3, "OTHER_OWNER"),
    (4, 23), (5, 9), (6, 2), (7, 1), (8, "B"),
])
def test_every_metadata_field_is_compared(index, value):
    db = RetainedDb()
    column = list(db.metadata[BACKUP][0])
    column[index] = value
    db.metadata[BACKUP][0] = tuple(column)
    assert verify_retained_dim_product_backup(db)["status"] == "failed"
    assert FORWARD_SQL not in db.statements
    assert db.closed == 2


@pytest.mark.parametrize("table", [SOURCE, BACKUP])
def test_column_count_or_order_drift_fails(table):
    db = RetainedDb()
    db.metadata[table] = db.metadata[table][:-1]
    assert verify_retained_dim_product_backup(db)["status"] == "failed"
    db = RetainedDb()
    db.metadata[table] = list(reversed(db.metadata[table]))
    assert verify_retained_dim_product_backup(db)["status"] == "failed"


def test_matching_but_missing_surrogate_key_or_invalid_metadata_fails():
    for metadata in (
        [(f"C{i}", i, "NUMBER", None, 22, 10, 0, 0, None) for i in range(1, 27)],
        [("PRODUCT_SK", 1)] * 26,
    ):
        db = RetainedDb()
        db.metadata = {SOURCE: metadata, BACKUP: deepcopy(metadata)}
        assert verify_retained_dim_product_backup(db)["status"] == "failed"


@pytest.mark.parametrize("table,shape", [
    (SOURCE, (26, 71)), (BACKUP, (26, 73)), (ODS, (23, 0)), (ODS, (24, 1)),
])
def test_row_and_ods_shape_drift_fails(table, shape):
    db = RetainedDb()
    db.tables[table] = shape
    assert verify_retained_dim_product_backup(db)["status"] == "failed"
    assert db.events[-3:] == ["rollback", "cursor_close", "connection_close"]


@pytest.mark.parametrize("sql", [NULL_SQL, DUPLICATE_SQL, FORWARD_SQL, REVERSE_SQL])
def test_null_duplicate_or_either_full_row_difference_fails(sql):
    db = RetainedDb()
    db.counts[sql] = 1
    result = verify_retained_dim_product_backup(db)
    assert result["status"] == "failed"
    assert result["raw_rows_included"] is result["pk_values_included"] is False
    assert "backup" not in result
    assert db.closed == 2


@pytest.mark.parametrize("kwargs", [
    {"owner": "OTHER"}, {"source_table": ODS}, {"backup_table": SOURCE},
    {"expected_column_count": 25}, {"expected_column_count": 26.0},
    {"expected_row_count": True}, {"expected_row_count": 72.0},
    {"require_absent": True}, {"require_absent": 0}, {"require_absent": None},
    {"require_absent": "false"},
])
def test_unreviewed_or_coerced_arguments_never_connect(kwargs):
    db = RetainedDb()
    assert verify_retained_dim_product_backup(db, **kwargs)["status"].startswith("blocked_")
    assert db.connections == 0


@pytest.mark.parametrize("failure", [
    "connect", "cursor", "SET TRANSACTION READ ONLY", EXISTS_SQL, METADATA_SQL,
    COLUMNS_SQL, f'SELECT COUNT(*) FROM "DWH"."{SOURCE}"',
    f'SELECT COUNT(*) FROM "DWH"."{BACKUP}"', f'SELECT COUNT(*) FROM "DWH"."{ODS}"',
    NULL_SQL, DUPLICATE_SQL, FORWARD_SQL, REVERSE_SQL,
])
def test_driver_failures_are_sanitized_and_cleanup_is_attempted(failure):
    db = RetainedDb()
    db.fail_at = failure
    result = verify_retained_dim_product_backup(db)
    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert "sensitive-value" not in repr(result)
    assert db.closed == (0 if failure == "connect" else 1 if failure == "cursor" else 2)
    assert ("rollback" in db.events) == (failure != "connect")
    if failure == "SET TRANSACTION READ ONLY":
        assert db.statements == [failure]


@pytest.mark.parametrize("failures", [
    {"rollback"}, {"cursor_close"}, {"connection_close"},
    {"rollback", "cursor_close", "connection_close"},
])
@pytest.mark.parametrize("error", [oracledb.DatabaseError, RuntimeError])
def test_cleanup_failure_invalidates_proof_but_still_attempts_all_cleanup(failures, error):
    db = RetainedDb()
    db.cleanup_failures = failures
    db.cleanup_error = error
    result = verify_retained_dim_product_backup(db)
    assert result["status"] == "failed"
    assert result["stage"] == "read_only_cleanup"
    assert db.events[-3:] == ["rollback", "cursor_close", "connection_close"]
    assert "sensitive-value" not in repr(result)


def test_verifier_source_contains_no_mutating_sql_or_runtime_configuration():
    import ast
    from pathlib import Path

    import dim_product_scd2_retained_backup_verifier as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.upper().startswith((
                "CREATE ", "DROP ", "DELETE ", "INSERT ", "UPDATE ", "MERGE ",
                "ALTER ", "TRUNCATE ", "COMMIT", "BEGIN ", "DECLARE ",
            ))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [node.module] if isinstance(node, ast.ImportFrom) else [n.name for n in node.names]
            assert not {"config", "dotenv", "odi_orchestrator"}.intersection(names)
    assert "NULLABLE" not in METADATA_SQL  # CTAS nullability is deliberately not compared
