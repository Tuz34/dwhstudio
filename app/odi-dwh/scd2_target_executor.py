"""Absent-only target DDL for generic SCD2 targets (DDL user, no DROP path)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

import execution_identity
import scd2_builder

logger = logging.getLogger("dwh_app.scd2_target_executor")

_COLUMNS_SQL = """
SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE,
       CHAR_LENGTH, CHAR_USED
FROM ALL_TAB_COLUMNS
WHERE OWNER = :owner AND TABLE_NAME = :table_name
ORDER BY COLUMN_ID
"""
_GRANTS_SQL = """
SELECT GRANTEE, PRIVILEGE, TABLE_NAME
FROM ALL_TAB_PRIVS
WHERE TABLE_SCHEMA = :owner AND TABLE_NAME IN (:table_name, :sequence_name)
"""


def _full_type(
    data_type: object,
    length: object,
    precision: object,
    scale: object,
    char_length: object = None,
    char_used: object = None,
) -> str:
    """Render a catalog column as a SQL type.

    Character sizes come from CHAR_LENGTH (DATA_LENGTH is in bytes and is two
    to four times larger for multibyte national types). VARCHAR2 and CHAR keep
    their CHAR semantics when the catalog says so.
    """
    base = str(data_type or "").upper()
    if base.startswith("TIMESTAMP"):
        match = re.fullmatch(r"TIMESTAMP\(([0-9])\)", base)
        if match is not None and match.group(1) != "6":
            return f"TIMESTAMP({match.group(1)})"
        return "TIMESTAMP"
    if base in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"}:
        semantics = str(char_used or "").upper()
        size = int(char_length) if char_length and semantics != "B" else int(length)
        if base in {"VARCHAR2", "CHAR"} and semantics in {"B", "C"}:
            qualifier = "BYTE" if semantics == "B" else "CHAR"
            return f"{base}({size} {qualifier})"
        return f"{base}({size})"
    if base == "NUMBER" and precision is not None:
        if scale not in (None, 0):
            return f"NUMBER({int(precision)},{int(scale)})"
        return f"NUMBER({int(precision)})"
    return base


def read_catalog_columns(
    cursor: Any, owner: str, table: str
) -> list[dict[str, Any]]:
    """Read column metadata (never rows) from the Oracle catalog."""
    cursor.execute(_COLUMNS_SQL, {"owner": owner, "table_name": table})
    return [
        {
            "name": str(name),
            "data_type": _full_type(data_type, length, precision, scale, char_length, char_used),
            "nullable": nullable == "Y",
        }
        for name, data_type, length, precision, scale, nullable, char_length, char_used in cursor.fetchall()
    ]


def fetch_source_columns(
    owner: str, table: str, connection_factory: Callable[[], Any]
) -> list[dict[str, Any]]:
    """Return source column metadata; raises ``oracledb.Error`` on failure."""
    connection = connection_factory()
    try:
        cursor = connection.cursor()
        try:
            return read_catalog_columns(cursor, owner, table)
        finally:
            cursor.close()
    finally:
        connection.close()


def _base(status: str, plan: Mapping[str, Any], *, approved: object) -> dict[str, Any]:
    return {
        "mode": "scd2_target_ddl",
        "status": status,
        "approved": approved is True,
        "target": dict(plan["target"]),
        "sequence": plan["surrogate_key"]["sequence"],
        "connection_attempted": False,
        "write_operations_attempted": [],
        "target_created": False,
        "sequence_created": False,
        "outcome_unknown": False,
        "manual_review_required": False,
        "grants_applied": 0,
        "grants_failed": 0,
        "cleanup_attempted": False,
        "drop_supported": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _exists(cursor: Any, sql: str, owner: str, name: str) -> bool:
    cursor.execute(sql, {"owner": owner, "name": name})
    return int(cursor.fetchone()[0]) > 0


def _user_exists(cursor: Any, username: str) -> bool:
    cursor.execute("SELECT COUNT(*) FROM ALL_USERS WHERE USERNAME = :u", {"u": username})
    return int(cursor.fetchone()[0]) > 0


def _table_exists(cursor: Any, owner: str, table: str) -> bool:
    return _exists(
        cursor,
        "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER = :owner AND TABLE_NAME = :name",
        owner,
        table,
    )


def _sequence_exists(cursor: Any, owner: str, sequence: str) -> bool:
    return _exists(
        cursor,
        "SELECT COUNT(*) FROM ALL_SEQUENCES WHERE SEQUENCE_OWNER = :owner AND SEQUENCE_NAME = :name",
        owner,
        sequence,
    )


def _matches_contract(actual: list[dict[str, Any]], plan: Mapping[str, Any]) -> bool:
    expected = [
        {"name": n, "data_type": scd2_builder.oracle_ddl_type(t), "nullable": nullable}
        for n, t, nullable in scd2_builder.expected_target_metadata(plan)
    ]
    return actual == expected


def _apply_grants(
    cursor: Any,
    plan: Mapping[str, Any],
    grants: list[tuple[str, str, str]],
    result: dict[str, Any],
) -> None:
    for grant in grants:
        try:
            cursor.execute(scd2_builder.render_grant_statement(plan, grant))
            result["grants_applied"] += 1
        except oracledb.DatabaseError as exc:
            result["grants_failed"] += 1
            logger.warning("SCD2 grant failed: %s", type(exc).__name__)


def _recover_existing(
    cursor: Any,
    plan: Mapping[str, Any],
    result: dict[str, Any],
    execution_username: str | None,
) -> dict[str, Any]:
    """Existing matching table: verify the sequence and grants.

    Repairs stay inside the absent-only scope (create a missing sequence, add
    missing grants). The table itself is never altered or dropped.
    """
    owner = plan["target"]["owner"]
    sequence = plan["surrogate_key"]["sequence"]
    repaired: list[str] = []
    if not _sequence_exists(cursor, owner, sequence):
        result["write_operations_attempted"].append("create_sequence")
        try:
            cursor.execute(scd2_builder.render_create_sequence(plan))
        except oracledb.DatabaseError as exc:
            result["status"] = "blocked_target_incomplete"
            result["missing"] = ["sequence"]
            result["error_type"] = type(exc).__name__
            result.update(outcome_unknown=True, manual_review_required=True,
                          cleanup_requires_separate_approval=True)
            return result
        result["sequence_created"] = True
        repaired.append("sequence")
    required = scd2_builder.required_grants(plan, execution_username)
    missing: list[tuple[str, str, str]] = []
    if required:
        cursor.execute(
            _GRANTS_SQL,
            {"owner": owner, "table_name": plan["target"]["table"], "sequence_name": sequence},
        )
        present = {(str(table), str(grantee), str(privilege)) for grantee, privilege, table in cursor.fetchall()}
        missing = [g for g in required if g not in present]
    if missing:
        result["write_operations_attempted"].append("grants")
        _apply_grants(cursor, plan, missing, result)
        repaired.append("grants")
    if result["grants_failed"] > 0:
        result["status"] = "blocked_target_incomplete"
        result["missing"] = ["grants"]
        return result
    result["status"] = "repaired_incomplete_target" if repaired else "already_exists_matching"
    result["repaired"] = repaired
    return result


def _error_status(exc: oracledb.Error) -> str:
    text = str(exc)
    if "ORA-01031" in text or "ORA-01950" in text:
        return "blocked_ddl_privilege"
    if "ORA-00955" in text:
        return "blocked_name_in_use"
    return "failed"


def _proven_username(
    plan: Mapping[str, Any], identity_proof: execution_identity.ExecutionIdentityProof | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the proven account, or a redacted block. Config is not consulted."""
    if not scd2_builder.grants_enabled():
        return None, None
    if not isinstance(identity_proof, execution_identity.ExecutionIdentityProof):
        blocked = _base("blocked_execution_identity", plan, approved=True)
        blocked["code"] = "execution_identity_unverified"
        return None, blocked
    if plan["options"].get("exec_user_requested") and plan["options"].get("exec_user") != identity_proof.username:
        blocked = _base("blocked_execution_identity", plan, approved=True)
        blocked["code"] = "execution_identity_rejected"
        return None, blocked
    if identity_proof.schema_name != plan["target"]["owner"]:
        blocked = _base("blocked_execution_identity", plan, approved=True)
        blocked["code"] = "execution_identity_unverified"
        return None, blocked
    if identity_proof.model_code != plan["models"]["target"]:
        blocked = _base("blocked_execution_identity", plan, approved=True)
        blocked["code"] = "execution_identity_unverified"
        return None, blocked
    if identity_proof.context_code != execution_identity.EXECUTION_CONTEXT:
        blocked = _base("blocked_execution_identity", plan, approved=True)
        blocked["code"] = "execution_identity_unverified"
        return None, blocked
    return identity_proof.username, None


def execute_target_ddl(
    plan: Mapping[str, Any],
    *,
    approved: object,
    connection_factory: Callable[[], Any],
    ddl_user: str = "APP_USER",  # kept for call compatibility; grants no longer depend on it
    identity_proof: execution_identity.ExecutionIdentityProof | None = None,
) -> dict[str, Any]:
    """Create the SCD2 sequence and table only when both are absent.

    Existing objects are never altered or dropped. Partial progress is reported
    explicitly so an operator can decide on cleanup separately.
    """
    if approved is not True:
        return _base("blocked_until_approval", plan, approved=approved)
    username, identity_block = _proven_username(plan, identity_proof)
    if identity_block is not None:
        return identity_block
    owner = plan["target"]["owner"]
    table = plan["target"]["table"]
    sequence = plan["surrogate_key"]["sequence"]
    result = _base("failed", plan, approved=approved)
    connection = cursor = None
    stage = "open_connection"
    try:
        connection = connection_factory()
        result["connection_attempted"] = True
        cursor = connection.cursor()
        stage = "preflight_grantees"
        result.update(scd2_builder.grant_skip_flags(plan, username))
        missing_users = [
            user for user in scd2_builder.required_grantees(plan, username) if not _user_exists(cursor, user)
        ]
        if missing_users:
            result["status"] = "blocked_grantee_missing"
            result["missing_grantee_count"] = len(missing_users)
            result["message"] = "A required grantee does not exist. No DDL was executed."
            return result
        stage = "preflight_table"
        if _table_exists(cursor, owner, table):
            actual = read_catalog_columns(cursor, owner, table)
            result["column_count"] = len(actual)
            if not _matches_contract(actual, plan):
                result["status"] = "blocked_target_exists_mismatch"
                return result
            stage = "recover_existing"
            return _recover_existing(cursor, plan, result, username)
        stage = "preflight_sequence"
        sequence_present = _sequence_exists(cursor, owner, sequence)
        result["sequence_already_existed"] = sequence_present

        if not sequence_present:
            stage = "create_sequence"
            result["write_operations_attempted"].append("create_sequence")
            cursor.execute(scd2_builder.render_create_sequence(plan))
            result["sequence_created"] = True
        stage = "create_target"
        result["write_operations_attempted"].append("create_target")
        cursor.execute(scd2_builder.render_create_table(plan))
        result["target_created"] = True

        stage = "verify_target"
        actual = read_catalog_columns(cursor, owner, table)
        if not _matches_contract(actual, plan):
            result["status"] = "partial_failure"
            result["stage"] = stage
            result["cleanup_requires_separate_approval"] = True
            return result
        result["column_count"] = len(actual)

        stage = "grants"
        grants = scd2_builder.required_grants(plan, username)
        if grants:
            result["write_operations_attempted"].append("grants")
            _apply_grants(cursor, plan, grants, result)
        result["status"] = "created" if result["grants_failed"] == 0 else "created_grants_incomplete"
        return result
    except oracledb.Error as exc:
        created_any = result["target_created"] or result["sequence_created"]
        unacknowledged = any(operation in result["write_operations_attempted"] and not result[flag]
                             for operation, flag in (("create_sequence", "sequence_created"), ("create_target", "target_created")))
        result["status"] = "partial_failure" if created_any else _error_status(exc)
        result["stage"] = stage
        result["error_type"] = type(exc).__name__
        result.update(outcome_unknown=unacknowledged, manual_review_required=created_any or unacknowledged)
        if created_any or unacknowledged:
            result["cleanup_requires_separate_approval"] = True
        return result
    finally:
        _close(cursor)
        _close(connection)


def _close(resource: Any) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("SCD2 DDL resource close failed: %s", type(exc).__name__)
