from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import oracledb
import pytest

from dim_product_scd2_stock_migration import (
    EVIDENCE_KEYS,
    GUARD_COUNT_NAMES,
    MIGRATE_UPDATE_SQL,
    MIGRATION_APPROVAL_SCOPE,
    PASSING_GUARD_COUNTS,
    RESTORE_APPROVAL_SCOPE,
    RESTORE_UPDATE_SQL,
    classify_dim_product_stock_migration,
    collect_dim_product_stock_migration_evidence,
    execute_dim_product_stock_migration,
    execute_dim_product_stock_migration_restore,
    expected_column_metadata_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def _legacy_counts(**overrides: int) -> dict[str, int]:
    counts = dict.fromkeys(EVIDENCE_KEYS, 0)
    counts.update(
        {
            "target_row_count": 72,
            "legacy_current_flag_count": 72,
            "legacy_open_ended_count": 72,
            "sequence_exists": 1,
            "ods_row_count": 72,
        }
    )
    counts.update(overrides)
    return counts


class _Cursor:
    def __init__(self, evidence: dict[str, int], metadata, **options):
        self.evidence = dict(evidence)
        self.metadata = list(metadata)
        self.backup_metadata = list(options.get("backup_metadata", metadata))
        self.overrides = dict(options.get("overrides", {}))
        self.after_update = dict(options.get("after_update", {}))
        self.after_create = dict(options.get("after_create", {}))
        self.raise_secret = bool(options.get("raise_secret", False))
        self.guard_overrides = dict(options.get("guard_overrides", {}))
        self.statements: list[str] = []
        self.rowcount = -1
        self._rows: list[tuple] = []

    def _value(self, name: str) -> int:
        if name in self.overrides:
            return self.overrides[name]
        return self.evidence[name]

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.raise_secret:
            raise oracledb.DatabaseError("SECRET_TOKEN password=hidden")
        if sql.startswith("SET TRANSACTION"):
            self._rows = []
            return
        if "odi87:lock_target" in sql or "odi87:lock_backup" in sql:
            self._rows = []
            return
        guard_name = next((name for name in GUARD_COUNT_NAMES if f"odi87:{name}" in sql), None)
        if guard_name is not None:
            self._rows = [(self.guard_overrides.get(guard_name, PASSING_GUARD_COUNTS[guard_name]),)]
            return
        if "odi87:target_metadata" in sql:
            self._rows = list(self.metadata)
            return
        if "odi87:backup_metadata" in sql:
            self._rows = list(self.backup_metadata)
            return
        if "odi87:create_backup" in sql:
            self.evidence.update(
                {
                    "backup_exists": 1,
                    "backup_column_count": 26,
                    "backup_row_count": 72,
                    "backup_legacy_current_flag_count": self.evidence["legacy_current_flag_count"],
                    "backup_legacy_historical_flag_count": self.evidence["legacy_historical_flag_count"],
                    "backup_legacy_open_ended_count": self.evidence["legacy_open_ended_count"],
                    "backup_stock_flag_count": 0,
                    "target_minus_backup_count": 0,
                    "backup_minus_target_count": 0,
                    "preserved_payload_mismatch_count": 0,
                    "flag_mapping_mismatch_count": 0,
                    "date_mapping_mismatch_count": 0,
                    "sk_only_in_target_count": 0,
                    "sk_only_in_backup_count": 0,
                }
            )
            self.evidence.update(self.after_create)
            self.rowcount = -1
            return
        if "odi87:migrate_update" in sql:
            current = self.evidence["legacy_current_flag_count"]
            historical = self.evidence["legacy_historical_flag_count"]
            opened = self.evidence["legacy_open_ended_count"]
            self.evidence.update(
                {
                    "legacy_current_flag_count": 0,
                    "legacy_historical_flag_count": 0,
                    "stock_current_flag_count": current,
                    "stock_historical_flag_count": historical,
                    "legacy_open_ended_count": 0,
                    "stock_open_ended_count": opened,
                    "current_legacy_open_mismatch_count": 0,
                    "current_stock_open_mismatch_count": 0,
                    "historical_open_sentinel_count": 0,
                    "target_minus_backup_count": 72,
                    "backup_minus_target_count": 72,
                    "flag_mapping_mismatch_count": 0,
                    "date_mapping_mismatch_count": 0,
                    "preserved_payload_mismatch_count": 0,
                    "sk_only_in_target_count": 0,
                    "sk_only_in_backup_count": 0,
                }
            )
            self.evidence.update(self.after_update)
            self.rowcount = self.after_update.get("rowcount", 72)
            return
        if "odi87:restore_update" in sql:
            self.evidence.update(
                {
                    "legacy_current_flag_count": self.evidence["backup_legacy_current_flag_count"],
                    "legacy_historical_flag_count": self.evidence["backup_legacy_historical_flag_count"],
                    "legacy_open_ended_count": self.evidence["backup_legacy_open_ended_count"],
                    "stock_current_flag_count": 0,
                    "stock_historical_flag_count": 0,
                    "stock_open_ended_count": 0,
                    "current_legacy_open_mismatch_count": 0,
                    "current_stock_open_mismatch_count": 0,
                    "historical_open_sentinel_count": 0,
                    "target_minus_backup_count": 0,
                    "backup_minus_target_count": 0,
                    "flag_mapping_mismatch_count": 0,
                    "date_mapping_mismatch_count": 0,
                    "preserved_payload_mismatch_count": 0,
                }
            )
            self.evidence.update(self.after_update)
            self.rowcount = self.after_update.get("rowcount", 72)
            return
        marker = next(name for name in EVIDENCE_KEYS if f"odi87:{name}" in sql)
        self._rows = [(self._value(marker),)]

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _Connection:
    def __init__(self, cursor: _Cursor):
        self.cursor_instance = cursor
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _Factory:
    def __init__(self, connection: _Connection | None = None):
        self.connection = connection
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.connection


def _collect(counts: dict[str, int] | None = None, **options):
    metadata = options.pop("metadata", expected_column_metadata_rows())
    cursor = _Cursor(_legacy_counts(**(counts or {})), metadata, **options)
    return collect_dim_product_stock_migration_evidence(cursor), cursor


def _migrate(counts=None, **options):
    reviewed, _cursor = _collect(counts)
    live = _Cursor(_legacy_counts(**(counts or {})), expected_column_metadata_rows(), **options)
    connection = _Connection(live)
    factory = _Factory(connection)
    result = execute_dim_product_stock_migration(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=MIGRATION_APPROVAL_SCOPE,
        connection_factory=factory,
    )
    return result, factory, connection


def _restore(counts=None, **options):
    reviewed, _cursor = _collect(counts)
    live = _Cursor(_legacy_counts(**(counts or {})), expected_column_metadata_rows(), **options)
    connection = _Connection(live)
    factory = _Factory(connection)
    result = execute_dim_product_stock_migration_restore(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=RESTORE_APPROVAL_SCOPE,
        connection_factory=factory,
    )
    return result, factory, connection


def test_legacy_population_migrates_in_one_transaction_and_keeps_backup():
    result, factory, connection = _migrate()
    sql = "\n".join(connection.cursor_instance.statements)
    upper = sql.upper()

    assert result["status"] == "passed"
    assert result["committed"] is True
    assert result["rolled_back"] is False
    assert result["migrated_row_count"] == 72
    assert result["migration_backup_retained"] is True
    assert result["automatic_drop_performed"] is False
    assert result["ods_write_attempted"] is False
    assert result["sequence_write_attempted"] is False
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["secret_printed"] is False
    assert result["write_operations_attempted"] == [
        "create_migration_backup",
        "migrate_current_flags_and_open_ended_dates",
    ]
    assert result["backup_create_attempted"] is True
    assert result["backup_verified"] is True
    assert result["post_commit_verification"] == "verified"
    assert connection.commits == 1
    assert connection.closed is True
    assert factory.calls == 2
    assert "SERIALIZABLE" not in upper
    assert "READ COMMITTED" in upper
    assert "SEARCH_CONDITION" not in sql
    assert sql.index("odi87:enabled_target_trigger_count") < sql.index("odi87:create_backup")
    assert sql.index("odi87:create_backup") < sql.index("odi87:lock_target")
    assert sql.index("odi87:lock_target") < sql.index("odi87:lock_backup")
    assert sql.index("odi87:lock_backup") < sql.index("odi87:migrate_update")
    assert sql.index("odi87:create_backup") < sql.index("odi87:migrate_update")
    assert 'CREATE TABLE "DWH"."ODI87_DIM_PRODUCT_MIG_BKP"' in sql
    assert "WHEN 'Y' THEN '1'" in sql
    assert "WHEN 'N' THEN '0'" in sql
    assert "DATE '9999-12-31'" in sql
    assert "DATE '2400-01-01'" in sql
    assert "MINUS" in upper
    for forbidden in ("DROP", "TRUNCATE", "DELETE", "ALTER", "NEXTVAL", "ODI68_DIM_PRODUCT_BKP"):
        assert forbidden not in upper
    assert 'UPDATE "DWH"."ODS_SH_PRODUCTS"' not in upper


def test_historical_closed_dates_stay_out_of_the_flag_update_shape():
    result, _factory, connection = _migrate(
        {
            "legacy_current_flag_count": 70,
            "legacy_historical_flag_count": 2,
            "legacy_open_ended_count": 70,
        }
    )

    assert result["status"] == "passed"
    assert "PROD_ID" not in MIGRATE_UPDATE_SQL
    assert "EFFECTIVE_FROM" not in MIGRATE_UPDATE_SQL
    assert "INSERT_DATE" not in MIGRATE_UPDATE_SQL
    assert "PRODUCT_SK" not in MIGRATE_UPDATE_SQL
    assert "DECODE" in "\n".join(connection.cursor_instance.statements)


def test_already_stock_is_a_noop_without_creating_a_backup():
    result, factory, connection = _migrate(
        {
            "legacy_current_flag_count": 0,
            "legacy_open_ended_count": 0,
            "stock_current_flag_count": 72,
            "stock_open_ended_count": 72,
        }
    )
    sql = "\n".join(connection.cursor_instance.statements)

    assert result["status"] == "rerun_noop"
    assert result["committed"] is False
    assert result["migration_backup_retained"] is False
    assert result["write_operations_attempted"] == []
    assert connection.commits == 0
    assert "odi87:create_backup" not in sql
    assert "odi87:migrate_update" not in sql
    assert factory.calls == 1


def test_existing_legacy_backup_is_reused_without_create_or_drop():
    result, _factory, connection = _migrate(
        {
            "backup_exists": 1,
            "backup_column_count": 26,
            "backup_row_count": 72,
            "backup_legacy_current_flag_count": 72,
            "backup_legacy_open_ended_count": 72,
        }
    )
    sql = "\n".join(connection.cursor_instance.statements).upper()

    assert result["status"] == "passed"
    assert result["migration_backup_retained"] is True
    assert "CREATE TABLE" not in sql
    assert "DROP" not in sql


def test_stock_rerun_keeps_a_verified_legacy_preimage():
    result, _factory, connection = _migrate(
        {
            "legacy_current_flag_count": 0,
            "legacy_open_ended_count": 0,
            "stock_current_flag_count": 72,
            "stock_open_ended_count": 72,
            "backup_exists": 1,
            "backup_column_count": 26,
            "backup_row_count": 72,
            "backup_legacy_current_flag_count": 72,
            "backup_legacy_open_ended_count": 72,
            "target_minus_backup_count": 72,
            "backup_minus_target_count": 72,
        }
    )

    assert result["status"] == "rerun_noop"
    assert result["migration_backup_retained"] is True
    assert connection.commits == 0
    assert "odi87:migrate_update" not in "\n".join(connection.cursor_instance.statements)


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"legacy_current_flag_count": 70, "stock_current_flag_count": 2, "legacy_open_ended_count": 70}, "blocked_mixed_state"),
        ({"other_flag_count": 1}, "blocked_invalid_flags"),
        ({"current_legacy_open_mismatch_count": 1}, "blocked_invalid_dates"),
        ({"historical_open_sentinel_count": 1, "legacy_historical_flag_count": 1, "legacy_current_flag_count": 71, "legacy_open_ended_count": 71}, "blocked_invalid_dates"),
        ({"duplicate_legacy_current_groups": 1}, "blocked_duplicate_current"),
        ({"duplicate_product_sk_groups": 1}, "blocked_duplicate_or_null_keys"),
        ({"null_natural_key_count": 1}, "blocked_duplicate_or_null_keys"),
        ({"null_effective_from_count": 1}, "blocked_duplicate_or_null_keys"),
        ({"target_row_count": 73}, "blocked_unexpected_row_count"),
        (
            {
                "historical_effective_to_on_or_after_stock_open_count": 1,
                "legacy_historical_flag_count": 1,
                "legacy_current_flag_count": 71,
                "legacy_open_ended_count": 71,
            },
            "blocked_invalid_dates",
        ),
        ({"current_effective_from_after_stock_open_count": 1}, "blocked_invalid_dates"),
    ],
)
def test_invalid_and_mixed_states_block_before_the_factory(counts, expected):
    reviewed, _cursor = _collect(counts)
    factory = _Factory()

    result = execute_dim_product_stock_migration(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=MIGRATION_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert classify_dim_product_stock_migration(reviewed) == expected
    assert result["status"] == expected
    assert factory.calls == 0
    assert result["connection_attempted"] is False


def test_unsafe_flag_type_blocks_before_write_and_hides_the_column_sample():
    rows = [
        ("CURRENT_FLAG", "NUMBER", None, None, None, "N") if row[0] == "CURRENT_FLAG" else row
        for row in expected_column_metadata_rows()
    ]
    evidence, _cursor = _collect(metadata=rows)

    assert classify_dim_product_stock_migration(evidence) == "blocked_unsafe_column_type"
    assert evidence["current_flag_type_safe"] == 0
    assert "NUMBER" not in str(evidence)


def test_unexpected_metadata_is_count_only():
    rows = list(expected_column_metadata_rows()) + [("SECRET_COL", "VARCHAR2", 20, None, None, "Y")]
    evidence, _cursor = _collect(metadata=rows)

    assert classify_dim_product_stock_migration(evidence) == "blocked_unexpected_shape"
    assert evidence["unexpected_column_count"] == 1
    assert "SECRET_COL" not in str(evidence)


def test_backup_mismatch_blocks_before_the_factory():
    reviewed, _cursor = _collect()
    reviewed = deepcopy(reviewed)
    reviewed["backup_exists"] = 1
    reviewed["backup_row_count"] = 72
    reviewed["backup_column_count"] = 26
    reviewed["target_minus_backup_count"] = 4
    factory = _Factory()

    result = execute_dim_product_stock_migration(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=MIGRATION_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert result["status"] == "blocked_backup_mismatch"
    assert factory.calls == 0


def test_stale_state_rolls_back_without_backup_or_update():
    result, factory, connection = _migrate(overrides={"target_row_count": 73})
    sql = "\n".join(connection.cursor_instance.statements)

    assert result["status"] == "blocked_stale_state"
    assert result["committed"] is False
    assert result["rolled_back"] is True
    assert "odi87:create_backup" not in sql
    assert "odi87:migrate_update" not in sql
    assert connection.commits == 0
    assert factory.calls == 1


def test_created_backup_that_does_not_match_is_retained_and_not_followed_by_dml():
    result, _factory, connection = _migrate(after_create={"target_minus_backup_count": 5})
    sql = "\n".join(connection.cursor_instance.statements)

    assert result["status"] == "blocked_backup_mismatch"
    assert result["migration_backup_retained"] is True
    assert result["backup_verified"] is False
    assert result["automatic_drop_performed"] is False
    assert result["manual_recovery_review_required"] is True
    assert "odi87:create_backup" in sql
    assert "odi87:migrate_update" not in sql
    assert connection.commits == 0


def test_post_migration_preservation_failure_rolls_back_without_commit():
    result, _factory, connection = _migrate(after_update={"preserved_payload_mismatch_count": 1})

    assert result["status"] == "failed"
    assert result["committed"] is False
    assert result["rolled_back"] is True
    assert result["error_type"] == "StockMigrationVerificationError"
    assert connection.commits == 0
    assert connection.rollbacks >= 1
    assert "odi87:migrate_update" in "\n".join(connection.cursor_instance.statements)


def test_approval_and_review_block_before_any_factory_call():
    factory = _Factory()
    kwargs = {
        "reviewed_evidence": {},
        "independent_review_status": "approved",
        "approved": False,
        "approval_scope": MIGRATION_APPROVAL_SCOPE,
        "connection_factory": factory,
    }

    unapproved = execute_dim_product_stock_migration(**kwargs)
    wrong_scope = execute_dim_product_stock_migration(
        **{**kwargs, "approved": True, "approval_scope": "other_scope"}
    )
    unreviewed = execute_dim_product_stock_migration(
        **{**kwargs, "approved": True, "independent_review_status": "pending"}
    )

    assert unapproved["status"] == "blocked_until_exact_operator_approval"
    assert wrong_scope["status"] == "blocked_invalid_approval_scope"
    assert unreviewed["status"] == "blocked_pending_independent_review"
    assert factory.calls == 0
    assert inspect.signature(execute_dim_product_stock_migration).parameters.keys() == {
        "reviewed_evidence",
        "independent_review_status",
        "approved",
        "approval_scope",
        "connection_factory",
    }


def test_database_errors_do_not_echo_secrets():
    result, _factory, _connection = _migrate(raise_secret=True)
    rendered = str(result)

    assert result["status"] == "failed"
    assert result["error_type"] == "DatabaseError"
    assert result["secret_printed"] is False
    assert "SECRET_TOKEN" not in rendered
    assert "password" not in rendered


def test_restore_rewrites_only_flag_and_end_date_and_keeps_backup():
    result, factory, connection = _restore(
        {
            "legacy_current_flag_count": 0,
            "legacy_open_ended_count": 0,
            "stock_current_flag_count": 72,
            "stock_open_ended_count": 72,
            "backup_exists": 1,
            "backup_column_count": 26,
            "backup_row_count": 72,
            "backup_legacy_current_flag_count": 72,
            "backup_legacy_open_ended_count": 72,
            "target_minus_backup_count": 72,
            "backup_minus_target_count": 72,
        }
    )
    sql = "\n".join(connection.cursor_instance.statements)
    upper = sql.upper()

    assert result["status"] == "passed"
    assert result["committed"] is True
    assert result["restored_row_count"] == 72
    assert result["migration_backup_retained"] is True
    assert result["automatic_drop_performed"] is False
    assert result["write_operations_attempted"] == ["restore_flags_and_effective_to"]
    assert "odi87:restore_update" in sql
    assert "odi87:migrate_update" not in sql
    assert "CREATE TABLE" not in upper
    assert "NEXTVAL" not in upper
    assert 'UPDATE "DWH"."ODS_SH_PRODUCTS"' not in upper
    assert '("CURRENT_FLAG", "EFFECTIVE_TO")' in RESTORE_UPDATE_SQL
    assert "INSERT_DATE" not in RESTORE_UPDATE_SQL
    assert "DELETE" not in RESTORE_UPDATE_SQL.upper()
    assert factory.calls == 2
    assert connection.commits == 1


def test_restore_of_an_already_legacy_backup_match_is_a_noop():
    result, _factory, connection = _restore(
        {
            "backup_exists": 1,
            "backup_column_count": 26,
            "backup_row_count": 72,
            "backup_legacy_current_flag_count": 72,
            "backup_legacy_open_ended_count": 72,
        }
    )

    assert result["status"] == "rerun_noop"
    assert connection.commits == 0
    assert "odi87:restore_update" not in "\n".join(connection.cursor_instance.statements)


def test_restore_gate_blocks_wrong_scope_and_a_missing_preimage():
    factory = _Factory()
    reviewed, _cursor = _collect(
        {
            "legacy_current_flag_count": 0,
            "legacy_open_ended_count": 0,
            "stock_current_flag_count": 72,
            "stock_open_ended_count": 72,
        }
    )

    wrong_scope = execute_dim_product_stock_migration_restore(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=MIGRATION_APPROVAL_SCOPE,
        connection_factory=factory,
    )
    missing_backup = execute_dim_product_stock_migration_restore(
        reviewed,
        independent_review_status="approved",
        approved=True,
        approval_scope=RESTORE_APPROVAL_SCOPE,
        connection_factory=factory,
    )

    assert wrong_scope["status"] == "blocked_invalid_approval_scope"
    assert missing_backup["status"] == "blocked_backup_mismatch"
    assert factory.calls == 0


def test_restore_failure_does_not_commit():
    result, _factory, connection = _restore(
        {
            "legacy_current_flag_count": 0,
            "legacy_open_ended_count": 0,
            "stock_current_flag_count": 72,
            "stock_open_ended_count": 72,
            "backup_exists": 1,
            "backup_column_count": 26,
            "backup_row_count": 72,
            "backup_legacy_current_flag_count": 72,
            "backup_legacy_open_ended_count": 72,
            "target_minus_backup_count": 72,
            "backup_minus_target_count": 72,
        },
        after_update={"target_minus_backup_count": 3},
    )

    assert result["status"] == "failed"
    assert result["committed"] is False
    assert result["rolled_back"] is True
    assert connection.commits == 0


def test_stock_contract_agrees_across_sql_proof_and_mapping_expressions():
    from dim_product_scd2_attribute_change_proof import build_rollback_proof_insert_statement
    from dim_product_scd2_candidate_preflight import collect_dim_product_scd2_candidate_evidence
    from dim_product_scd2_load_executor import build_expire_statement, build_insert_statement
    from dim_product_scd2_stock_contract import (
        MAPPING_CURRENT_FLAG_EXPRESSION,
        MAPPING_EFFECTIVE_TO_EXPRESSION,
        MAPPING_ETL_BATCH_ID_EXPRESSION,
        MAPPING_INSERT_DATE_EXPRESSION,
        MAPPING_SOURCE_SYSTEM_EXPRESSION,
        MAPPING_UPDATE_DATE_EXPRESSION,
        STOCK_OPEN_ENDED_DATE_SQL,
    )

    class _Probe:
        def __init__(self):
            self.statements = []
            self._row = (0,)

        def execute(self, sql, params=None):
            self.statements.append(sql)
            self._row = (1 if "ODI68_ODS_PRODUCTS_BKP" in sql and "ALL_TABLES" in sql else 0,)

        def fetchone(self):
            return self._row

    probe = _Probe()
    collect_dim_product_scd2_candidate_evidence(probe)
    candidate_sql = "\n".join(probe.statements)
    expire = build_expire_statement()
    insert = build_insert_statement()
    proof = build_rollback_proof_insert_statement()
    groovy = (ROOT / "odi_automation" / "dim_product_scd2_mapping_create.groovy").read_text(encoding="utf-8")
    register = (ROOT / "odi_automation" / "dim_product_scd2_datastore_register.groovy").read_text(encoding="utf-8")

    for statement in (expire, insert, proof, candidate_sql):
        assert "'1'" in statement
        assert "DATE '9999-12-31'" not in statement or any(
            token in statement for token in ("NOT IN", "<>", ">=")
        )
        assert "'Y'" not in statement
    assert STOCK_OPEN_ENDED_DATE_SQL in insert
    assert STOCK_OPEN_ENDED_DATE_SQL in proof
    assert f'EFFECTIVE_TO: "{MAPPING_EFFECTIVE_TO_EXPRESSION}"' in groovy
    assert f'CURRENT_FLAG: "{MAPPING_CURRENT_FLAG_EXPRESSION}"' in groovy
    assert f'INSERT_DATE: "{MAPPING_INSERT_DATE_EXPRESSION}"' in groovy
    assert f'UPDATE_DATE: "{MAPPING_UPDATE_DATE_EXPRESSION}"' in groovy
    assert f'SOURCE_SYSTEM: "{MAPPING_SOURCE_SYSTEM_EXPRESSION}"' in groovy
    assert MAPPING_ETL_BATCH_ID_EXPRESSION == '<%=odiRef.getSession("SESS_NO")%>'
    assert 'ETL_BATCH_ID: "<%=odiRef.getSession(\\"SESS_NO\\")%>"' in groovy
    assert '[name: "CURRENT_FLAG", dataType: "VARCHAR2", length: 1,' in register
    assert '[name: "EFFECTIVE_TO", dataType: "DATE",' in register


def test_stock_candidate_rejection_blocks_scenario_run_readiness():
    from dim_product_odi_scd2_scenario import build_dim_product_scd2_scenario_run_readiness

    started = datetime.now(timezone.utc)
    candidate = {
        "status": "rerun_noop",
        "observed_at": started.isoformat(),
        "connection_attempted": True,
        "write_attempted": False,
        "source_count": 72,
        "target_count": 72,
        "current_target_count": 72,
        "source_duplicate_key_group_count": 0,
        "target_duplicate_surrogate_key_group_count": 0,
        "target_multiple_current_key_group_count": 0,
        "target_invalid_current_flag_count": 72,
        "target_invalid_effective_range_count": 0,
        "source_missing_current_count": 0,
        "insert_candidate_count": 0,
        "changed_candidate_count": 0,
        "unchanged_count": 72,
        "candidate_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    mapping = {
        "tool": "odi.dim_product_scd2_mapping_probe",
        "status": "passed",
        "mode": "normalized_fixed_scd2_mapping_metadata",
        "side_effects": False,
        "repository_connection_attempted": True,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "mapping": {
            "name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT",
            "source_component_count": 1,
            "target_component_count": 1,
            "target_attribute_count": 26,
            "mapped_attribute_count": 26,
            "integration_type": "SCD",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "access_point_count": 0,
            "lkm_assigned_count": 0,
        },
    }
    scenario = {
        "status": "passed",
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "scenario": {"name": "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN", "version": "001", "exists": True},
    }

    blocked = build_dim_product_scd2_scenario_run_readiness(
        mapping,
        scenario,
        candidate,
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope="odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof",
        executor_started_at=started,
    )
    candidate["target_invalid_current_flag_count"] = 0
    ready = build_dim_product_scd2_scenario_run_readiness(
        mapping,
        scenario,
        candidate,
        scenario_generation_review_status="approved",
        approved=True,
        approval_scope="odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof",
        executor_started_at=started,
    )

    assert blocked["status"] == "blocked_candidate_preflight"
    assert ready["status"] == "ready_for_separate_scenario_run_executor"
