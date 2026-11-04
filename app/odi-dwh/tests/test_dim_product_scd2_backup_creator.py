"""Mocked contract for the absent-only DIM_PRODUCT named-backup creator."""

from __future__ import annotations

from pathlib import Path

import oracledb
import pytest

from dim_product_scd2_backup_creator import create_dim_product_named_backup
from dim_product_scd2_scenario_backup import (
    BACKUP_TABLE,
    ODS_BACKUP_TABLE,
    run_dim_product_scd2_named_backup_gate,
)


class FakeDb:
    """Tiny stateful Oracle double keyed on the fixed DWH tables."""

    def __init__(self, *, backup_exists=False, source=(26, 72), ods=(24, 0),
                 ctas_cols=None, ctas_rows=None, ctas_error=None, ods_after=None):
        self.tables = {"DIM_PRODUCT": source, ODS_BACKUP_TABLE: ods}
        if backup_exists:
            self.tables[BACKUP_TABLE] = (26, 72)
        self.ctas_cols, self.ctas_rows = ctas_cols, ctas_rows
        self.ctas_error, self.ods_after = ctas_error, ods_after
        self.statements = []
        self.closed = 0
        self._result = None

    def __call__(self):
        return self

    def cursor(self):
        return self

    def close(self):
        self.closed += 1

    def execute(self, sql, params=None):
        self.statements.append(sql)
        params = params or {}
        upper = sql.upper()
        if upper.startswith("CREATE TABLE"):
            if self.ctas_error:
                raise self.ctas_error
            cols, rows = self.tables["DIM_PRODUCT"]
            self.tables[BACKUP_TABLE] = (
                self.ctas_cols if self.ctas_cols is not None else cols,
                self.ctas_rows if self.ctas_rows is not None else rows,
            )
            if self.ods_after is not None:
                self.tables[ODS_BACKUP_TABLE] = self.ods_after
            self._result = None
        elif "FROM ALL_TABLES" in upper:
            self._result = 1 if params["table_name"] in self.tables else 0
        elif "FROM ALL_TAB_COLUMNS" in upper:
            self._result = self.tables.get(params["table_name"], (0, 0))[0]
        elif upper.startswith("SELECT COUNT(*) FROM"):
            table = sql.split('"."')[1].rstrip('"')
            self._result = self.tables[table][1]
        else:  # pragma: no cover - any other statement is a test failure
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return (self._result,)


def _writes(db):
    return [s for s in db.statements if not s.upper().startswith("SELECT")]


def test_absent_backup_is_created_and_verified_in_exact_gate_shape():
    db = FakeDb()
    result = create_dim_product_named_backup(db)
    assert result["status"] == "passed"
    assert result["backup"]["created_absent_only"] is True
    assert (result["backup"]["column_count"], result["backup"]["row_count"]) == (26, 72)
    assert result["ods_backup"]["untouched"] is True
    assert _writes(db) == [
        'CREATE TABLE "DWH"."ODI68_DIM_PRODUCT_BKP" AS SELECT * FROM "DWH"."DIM_PRODUCT"'
    ]
    assert db.closed == 2


def test_result_is_accepted_by_the_real_backup_gate():
    zero = (
        "source_duplicate_key_group_count",
        "target_duplicate_surrogate_key_group_count",
        "target_multiple_current_key_group_count",
        "target_invalid_current_flag_count",
        "target_invalid_effective_range_count", "source_missing_current_count",
        "insert_candidate_count", "changed_candidate_count", "candidate_count",
        "backup_row_count",
    )
    preflight = {
        "status": "rerun_noop", "connection_attempted": True, "write_attempted": False,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
        "source_count": 72, "target_count": 72, "current_target_count": 72,
        "unchanged_count": 72, "backup_exists": 1, "backup_column_count": 24,
        **{key: 0 for key in zero},
    }
    db = FakeDb()
    gate = run_dim_product_scd2_named_backup_gate(
        preflight, backup_creator=lambda **kw: create_dim_product_named_backup(db, **kw)
    )
    assert gate["status"] == "passed"
    assert gate["backup_row_count"] == 72


def test_existing_backup_fails_closed_without_any_write():
    db = FakeDb(backup_exists=True)
    result = create_dim_product_named_backup(db, require_absent=True)
    assert result["status"] == "blocked_backup_exists"
    assert "backup" not in result
    assert _writes(db) == []


def test_require_absent_false_is_refused_without_connecting():
    db = FakeDb()
    result = create_dim_product_named_backup(db, require_absent=False)
    assert result["status"] == "blocked_require_absent_required"
    assert db.statements == []


def test_unreviewed_target_arguments_are_refused():
    db = FakeDb()
    result = create_dim_product_named_backup(db, backup_table="OTHER_BKP")
    assert result["status"] == "blocked_unreviewed_backup_target"
    assert db.statements == []


@pytest.mark.parametrize(
    "kwargs",
    [{"source": (26, 71)}, {"source": (25, 72)}, {"ods": (24, 3)}, {"ods": (23, 0)}],
)
def test_bad_preconditions_fail_before_any_ddl(kwargs):
    db = FakeDb(**kwargs)
    result = create_dim_product_named_backup(db)
    assert result["status"] == "failed"
    assert result["error_type"] == "BackupVerificationError"
    assert result["side_effects"] is False and result["backup_may_exist"] is False
    assert _writes(db) == []


def test_missing_ods_backup_fails_before_ddl():
    db = FakeDb()
    del db.tables[ODS_BACKUP_TABLE]
    assert create_dim_product_named_backup(db)["status"] == "failed"
    assert _writes(db) == []


def test_post_create_count_mismatch_reports_actuals_and_leaves_table_without_drop():
    db = FakeDb(ctas_rows=70)
    result = create_dim_product_named_backup(db)
    assert result["status"] == "failed"
    assert result["backup_row_count"] == 70
    assert result["backup_may_exist"] is True and result["side_effects"] is True
    assert result["automatic_drop_performed"] is False
    assert not any("DROP" in s.upper() for s in db.statements)


def test_ods_backup_changed_after_create_fails():
    db = FakeDb(ods_after=(24, 5))
    result = create_dim_product_named_backup(db)
    assert result["status"] == "failed" and result["backup_may_exist"] is True


def test_driver_error_is_class_only_and_flags_possible_object():
    db = FakeDb(ctas_error=oracledb.DatabaseError("ORA-00955 secret detail"))
    result = create_dim_product_named_backup(db)
    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert result["backup_may_exist"] is True
    assert "ORA-" not in repr(result) and "secret detail" not in repr(result)


def test_connection_failure_reports_no_object():
    def factory():
        raise oracledb.DatabaseError("boom")

    result = create_dim_product_named_backup(factory)
    assert result["status"] == "failed" and result["backup_may_exist"] is False
    assert result["stage"] == "open_connection"


def test_source_has_no_destructive_sql_and_no_config_access():
    import ast

    import dim_product_scd2_backup_creator as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    literals = " ".join(
        node.value.upper() for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    )
    for word in ("DROP", "TRUNCATE", "DELETE", "INSERT", "UPDATE", "MERGE", "ALTER"):
        assert word not in literals, word
    assert "CREATE TABLE" in literals
    imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert "config" not in imports
