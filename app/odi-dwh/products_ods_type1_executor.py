"""Review-ready Type 1 preflight, backup proof, and load executor for ODI-68."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from scd2_manager import PRODUCTS_SOURCE_METADATA

SOURCE_OWNER = "SH"
SOURCE_TABLE = "PRODUCTS"
TARGET_OWNER = "DWH"
TARGET_TABLE = "ODS_SH_PRODUCTS"
BACKUP_TABLE = "ODI68_ODS_PRODUCTS_BKP"
RESTORE_PROOF_TABLE = "ODI68_ODS_PRODUCTS_RST"

BACKUP_PROOF_APPROVAL_SCOPE = "odi68_products_ods_named_backup_restore_proof"
TYPE1_LOAD_APPROVAL_SCOPE = "odi68_products_ods_type1_load_and_rerun_proof"

_SOURCE_COLUMNS = tuple(name for name, _data_type, _nullable in PRODUCTS_SOURCE_METADATA)
_TRACKED_COLUMNS = tuple(name for name in _SOURCE_COLUMNS if name != "PROD_ID")
logger = logging.getLogger("dwh_app.products_ods_type1_executor")


class Type1VerificationError(RuntimeError):
    """Raised when count/status/metadata invariants fail before commit."""


def _base(status: str, *, approved: object = False, connection_attempted: bool = False) -> dict[str, Any]:
    return {
        "mode": "products_ods_type1_executor",
        "status": status,
        "approved": approved is True,
        "requires_user_approval": True,
        "connection_attempted": connection_attempted,
        "will_execute": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _metadata_ready(preflight: Mapping[str, Any]) -> bool:
    source = preflight.get("source")
    target = preflight.get("target")
    return (
        preflight.get("status") == "ready_for_named_backup"
        and isinstance(source, Mapping)
        and source.get("metadata_status") == "metadata_ready_for_review"
        and source.get("duplicate_key_group_count") == 0
        and isinstance(target, Mapping)
        and target.get("exists") is True
        and target.get("metadata_status") == "metadata_ready_for_review"
        and target.get("duplicate_key_group_count") == 0
    )


def _change_predicate() -> str:
    return " OR ".join(
        f'DECODE(t."{column}", s."{column}", 0, 1) = 1'
        for column in _TRACKED_COLUMNS
    )


def build_type1_merge_statement() -> str:
    """Render one fixed MERGE statement from the frozen PRODUCTS metadata."""
    updates = ",\n    ".join(
        f't."{column}" = s."{column}"' for column in _TRACKED_COLUMNS
    )
    insert_columns = ", ".join(f'"{column}"' for column in _SOURCE_COLUMNS)
    insert_values = ", ".join(f's."{column}"' for column in _SOURCE_COLUMNS)
    return f'''MERGE INTO "{TARGET_OWNER}"."{TARGET_TABLE}" t
USING "{SOURCE_OWNER}"."{SOURCE_TABLE}" s
ON (t."PROD_ID" = s."PROD_ID")
WHEN MATCHED THEN UPDATE SET
    {updates},
    t."ODS_UPDATED_AT" = SYSTIMESTAMP
WHERE {_change_predicate()}
WHEN NOT MATCHED THEN INSERT (
    {insert_columns}, "ODS_CREATED_AT", "ODS_UPDATED_AT"
) VALUES (
    {insert_values}, SYSTIMESTAMP, NULL
)'''


def _scalar(cursor: Any, sql: str, params: Mapping[str, object] | None = None) -> int:
    cursor.execute(sql, params)
    return int(cursor.fetchone()[0])


def _candidate_evidence(cursor: Any) -> dict[str, int]:
    predicate = _change_predicate()
    source_count = _scalar(cursor, f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}"')
    target_count = _scalar(cursor, f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{TARGET_TABLE}"')
    source_duplicates = _scalar(
        cursor,
        f'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" '
        'GROUP BY "PROD_ID" HAVING COUNT(*) > 1)',
    )
    target_duplicates = _scalar(
        cursor,
        f'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "{TARGET_OWNER}"."{TARGET_TABLE}" '
        'GROUP BY "PROD_ID" HAVING COUNT(*) > 1)',
    )
    insert_count = _scalar(
        cursor,
        f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
        f'LEFT JOIN "{TARGET_OWNER}"."{TARGET_TABLE}" t ON t."PROD_ID" = s."PROD_ID" '
        'WHERE t."PROD_ID" IS NULL',
    )
    update_count = _scalar(
        cursor,
        f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
        f'JOIN "{TARGET_OWNER}"."{TARGET_TABLE}" t ON t."PROD_ID" = s."PROD_ID" '
        f'WHERE {predicate}',
    )
    unchanged_count = _scalar(
        cursor,
        f'SELECT COUNT(*) FROM "{SOURCE_OWNER}"."{SOURCE_TABLE}" s '
        f'JOIN "{TARGET_OWNER}"."{TARGET_TABLE}" t ON t."PROD_ID" = s."PROD_ID" '
        f'WHERE NOT ({predicate})',
    )
    return {
        "source_count": source_count,
        "target_count": target_count,
        "source_duplicate_key_group_count": source_duplicates,
        "target_duplicate_key_group_count": target_duplicates,
        "insert_candidate_count": insert_count,
        "update_candidate_count": update_count,
        "unchanged_count": unchanged_count,
    }


def _close(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def run_products_ods_type1_read_only_preflight(
    metadata_preflight: Mapping[str, Any],
    *,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Count Type 1 candidates through the target connection without returning keys."""
    if not _metadata_ready(metadata_preflight):
        return _base("blocked_metadata_preflight")
    connection = None
    cursor = None
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "begin_read_only"
        cursor.execute("SET TRANSACTION READ ONLY")
        stage = "count_candidates"
        evidence = _candidate_evidence(cursor)
        if evidence["source_duplicate_key_group_count"] != 0:
            status = "blocked_source_duplicate_keys"
        elif evidence["target_duplicate_key_group_count"] != 0:
            status = "blocked_target_duplicate_keys"
        elif (
            evidence["insert_candidate_count"]
            + evidence["update_candidate_count"]
            + evidence["unchanged_count"]
            != evidence["source_count"]
        ):
            status = "blocked_candidate_count_mismatch"
        else:
            status = "ready_for_type1_backup"
        return {
            **_base(status, connection_attempted=True),
            **evidence,
            "will_execute": False,
        }
    except oracledb.Error as exc:
        return {
            **_base("failed", connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
        }
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except oracledb.Error as exc:
                logger.warning("Type 1 preflight rollback failed: %s", type(exc).__name__)
        _close(cursor, "cursor")
        _close(connection, "connection")


def _write_gate(
    candidate_preflight: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    expected_scope: str,
) -> dict[str, Any] | None:
    if candidate_preflight.get("status") != "ready_for_type1_backup":
        return _base("blocked_candidate_preflight", approved=approved)
    if executor_review_status != "approved":
        return _base("blocked_pending_executor_review", approved=approved)
    if approved is not True:
        return _base("blocked_until_exact_operator_approval", approved=approved)
    if approval_scope != expected_scope:
        return _base("blocked_invalid_approval_scope", approved=approved)
    return None


def _object_exists(cursor: Any, table: str) -> bool:
    return _scalar(
        cursor,
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": TARGET_OWNER, "table_name": table},
    ) > 0


def _object_column_count(cursor: Any, table: str) -> int:
    return _scalar(
        cursor,
        "SELECT COUNT(*) FROM ALL_TAB_COLUMNS WHERE OWNER=:owner AND TABLE_NAME=:table_name",
        {"owner": TARGET_OWNER, "table_name": table},
    )


def _object_row_count(cursor: Any, table: str) -> int:
    if table not in {TARGET_TABLE, BACKUP_TABLE, RESTORE_PROOF_TABLE}:
        raise ValueError("Unreviewed PRODUCTS ODS proof object.")
    return _scalar(cursor, f'SELECT COUNT(*) FROM "{TARGET_OWNER}"."{table}"')


def _drop_fixed(cursor: Any, table: str) -> None:
    if table not in {BACKUP_TABLE, RESTORE_PROOF_TABLE}:
        raise ValueError("Unreviewed PRODUCTS ODS cleanup object.")
    cursor.execute(f'DROP TABLE "{TARGET_OWNER}"."{table}" PURGE')


def execute_products_ods_named_backup_restore_proof(
    candidate_preflight: Mapping[str, Any],
    *,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Create a fixed data backup and test a disposable restore copy."""
    blocked = _write_gate(
        candidate_preflight,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        expected_scope=BACKUP_PROOF_APPROVAL_SCOPE,
    )
    if blocked is not None:
        return blocked

    connection = None
    cursor = None
    created_backup = False
    created_restore = False
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "check_proof_objects_absent"
        if _object_exists(cursor, BACKUP_TABLE) or _object_exists(cursor, RESTORE_PROOF_TABLE):
            return _base("blocked_backup_or_restore_object_exists", approved=approved, connection_attempted=True)

        stage = "create_named_backup"
        cursor.execute(
            f'CREATE TABLE "{TARGET_OWNER}"."{BACKUP_TABLE}" AS '
            f'SELECT * FROM "{TARGET_OWNER}"."{TARGET_TABLE}"'
        )
        created_backup = True
        backup_columns = _object_column_count(cursor, BACKUP_TABLE)
        backup_count = _object_row_count(cursor, BACKUP_TABLE)
        expected_count = int(candidate_preflight["target_count"])
        if backup_columns != 24 or backup_count != expected_count:
            raise Type1VerificationError("Named backup verification failed.")

        stage = "create_restore_proof"
        cursor.execute(
            f'CREATE TABLE "{TARGET_OWNER}"."{RESTORE_PROOF_TABLE}" AS '
            f'SELECT * FROM "{TARGET_OWNER}"."{BACKUP_TABLE}"'
        )
        created_restore = True
        restore_columns = _object_column_count(cursor, RESTORE_PROOF_TABLE)
        restore_count = _object_row_count(cursor, RESTORE_PROOF_TABLE)
        if restore_columns != backup_columns or restore_count != backup_count:
            raise Type1VerificationError("Restore proof verification failed.")

        stage = "cleanup_restore_proof"
        _drop_fixed(cursor, RESTORE_PROOF_TABLE)
        created_restore = False
        if _object_exists(cursor, RESTORE_PROOF_TABLE):
            raise Type1VerificationError("Restore proof cleanup verification failed.")
        return {
            **_base("passed", approved=approved, connection_attempted=True),
            "will_execute": True,
            "write_operations_attempted": [
                "create_named_backup",
                "create_restore_proof",
                "remove_restore_proof",
            ],
            "backup_column_count": backup_columns,
            "backup_row_count": backup_count,
            "restore_proof_column_count": restore_columns,
            "restore_proof_row_count": restore_count,
            "backup_exists": True,
            "restore_proof_exists_after_cleanup": False,
        }
    except (oracledb.Error, Type1VerificationError) as exc:
        for table, created in ((RESTORE_PROOF_TABLE, created_restore), (BACKUP_TABLE, created_backup)):
            if created and cursor is not None:
                try:
                    _drop_fixed(cursor, table)
                except oracledb.Error as cleanup_exc:
                    logger.error("%s cleanup failed: %s", table, type(cleanup_exc).__name__)
        return {
            **_base("failed", approved=approved, connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
            "cleanup_attempted": created_backup or created_restore,
        }
    finally:
        _close(cursor, "cursor")
        _close(connection, "connection")


def execute_products_ods_type1_load_and_rerun_proof(
    candidate_preflight: Mapping[str, Any],
    *,
    backup_proof_status: str,
    executor_review_status: str,
    approved: object,
    approval_scope: str,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Run the fixed MERGE twice in one transaction and commit only a clean no-op rerun."""
    blocked = _write_gate(
        candidate_preflight,
        executor_review_status=executor_review_status,
        approved=approved,
        approval_scope=approval_scope,
        expected_scope=TYPE1_LOAD_APPROVAL_SCOPE,
    )
    if blocked is not None:
        return blocked
    if backup_proof_status != "passed":
        return _base("blocked_pending_named_backup_restore_proof", approved=approved)

    connection = None
    cursor = None
    stage = "open_connection"
    write_attempted = False
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "begin_serializable"
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        stage = "verify_named_backup"
        if not _object_exists(cursor, BACKUP_TABLE):
            return _base("blocked_named_backup_missing", approved=approved, connection_attempted=True)
        if _object_column_count(cursor, BACKUP_TABLE) != 24:
            return _base("blocked_named_backup_metadata", approved=approved, connection_attempted=True)
        if _object_row_count(cursor, BACKUP_TABLE) != int(candidate_preflight["target_count"]):
            return _base("blocked_named_backup_count", approved=approved, connection_attempted=True)

        stage = "recheck_candidates"
        before = _candidate_evidence(cursor)
        if before != {key: int(candidate_preflight[key]) for key in before}:
            return _base("blocked_candidate_state_changed", approved=approved, connection_attempted=True)

        merge_sql = build_type1_merge_statement()
        stage = "type1_merge"
        cursor.execute(merge_sql)
        write_attempted = True
        first_merge_affected_count = int(cursor.rowcount)
        stage = "type1_rerun_proof"
        cursor.execute(merge_sql)
        rerun_affected_count = int(cursor.rowcount)
        stage = "post_load_dq"
        after = _candidate_evidence(cursor)
        if (
            first_merge_affected_count
            != before["insert_candidate_count"] + before["update_candidate_count"]
            or rerun_affected_count != 0
            or after["target_count"] != after["source_count"]
            or after["source_duplicate_key_group_count"] != 0
            or after["target_duplicate_key_group_count"] != 0
            or after["insert_candidate_count"] != 0
            or after["update_candidate_count"] != 0
            or after["unchanged_count"] != after["source_count"]
        ):
            raise Type1VerificationError("Type 1 post-load DQ verification failed.")
        stage = "commit"
        connection.commit()
        return {
            **_base("passed", approved=approved, connection_attempted=True),
            "will_execute": True,
            "write_operations_attempted": ["type1_merge", "same_transaction_rerun_proof"],
            "inserted_count": before["insert_candidate_count"],
            "refreshed_count": before["update_candidate_count"],
            "unchanged_before_count": before["unchanged_count"],
            "first_merge_affected_count": first_merge_affected_count,
            "rerun_affected_count": rerun_affected_count,
            "source_count": after["source_count"],
            "target_count": after["target_count"],
            "target_duplicate_key_group_count": after["target_duplicate_key_group_count"],
            "remaining_insert_candidate_count": after["insert_candidate_count"],
            "remaining_update_candidate_count": after["update_candidate_count"],
        }
    except (oracledb.Error, Type1VerificationError) as exc:
        if connection is not None:
            try:
                connection.rollback()
            except oracledb.Error as rollback_exc:
                logger.error("Type 1 rollback failed: %s", type(rollback_exc).__name__)
        return {
            **_base("failed", approved=approved, connection_attempted=connection is not None),
            "stage": stage,
            "error_type": type(exc).__name__,
            "write_operations_attempted": ["type1_merge"] if write_attempted else [],
            "rolled_back": True,
        }
    finally:
        _close(cursor, "cursor")
        _close(connection, "connection")
