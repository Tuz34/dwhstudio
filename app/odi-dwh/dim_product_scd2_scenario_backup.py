"""Connectionless, fixed named-backup gate for the DIM_PRODUCT ODI scenario."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

BACKUP_OWNER = "DWH"
SOURCE_TABLE = "DIM_PRODUCT"
BACKUP_TABLE = "ODI68_DIM_PRODUCT_BKP"
ODS_BACKUP_TABLE = "ODI68_ODS_PRODUCTS_BKP"
EXPECTED_COLUMN_COUNT = 26
EXPECTED_ROW_COUNT = 72

_ZERO_PREFLIGHT_FIELDS = (
    "source_duplicate_key_group_count",
    "target_duplicate_surrogate_key_group_count",
    "target_multiple_current_key_group_count",
    "target_invalid_current_flag_count",
    "target_invalid_effective_range_count",
    "source_missing_current_count",
    "insert_candidate_count",
    "changed_candidate_count",
    "candidate_count",
    "backup_row_count",
)
_PREFLIGHT_KEYS = frozenset(
    {
        "status",
        "source_count",
        "target_count",
        "current_target_count",
        "unchanged_count",
        "backup_exists",
        "backup_column_count",
        "write_attempted",
        "raw_rows_included",
        "pk_values_included",
        "secret_printed",
        "mode",
        "requires_user_approval_for_persistent_action",
        "connection_attempted",
        "will_execute",
        "observed_at",
    }
    | set(_ZERO_PREFLIGHT_FIELDS)
)


def _count(report: Mapping[str, Any], name: str) -> int | None:
    value = report.get(name)
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _preflight_matches(report: object) -> bool:
    if not isinstance(report, Mapping):
        return False
    return (
        set(report) <= _PREFLIGHT_KEYS
        and report.get("status") == "rerun_noop"
        and report.get("connection_attempted") is True
        and report.get("write_attempted") is False
        and report.get("raw_rows_included") is False
        and report.get("pk_values_included") is False
        and report.get("secret_printed") is False
        and all(
            _count(report, key) == EXPECTED_ROW_COUNT
            for key in (
                "source_count",
                "target_count",
                "current_target_count",
                "unchanged_count",
            )
        )
        and all(_count(report, key) == 0 for key in _ZERO_PREFLIGHT_FIELDS)
        and _count(report, "backup_exists") == 1
        and _count(report, "backup_column_count") == 24
    )


def _proof_matches(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    backup = payload.get("backup")
    ods_backup = payload.get("ods_backup")
    return (
        set(payload)
        == {
            "status",
            "side_effects",
            "backup",
            "ods_backup",
            "raw_rows_included",
            "pk_values_included",
            "secret_printed",
        }
        and payload.get("status") == "passed"
        and payload.get("side_effects") is True
        and payload.get("raw_rows_included") is False
        and payload.get("pk_values_included") is False
        and payload.get("secret_printed") is False
        and isinstance(backup, Mapping)
        and set(backup)
        == {
            "owner",
            "table",
            "source_table",
            "exists",
            "created_absent_only",
            "column_count",
            "row_count",
        }
        and backup.get("owner") == BACKUP_OWNER
        and backup.get("table") == BACKUP_TABLE
        and backup.get("source_table") == SOURCE_TABLE
        and backup.get("exists") is True
        and backup.get("created_absent_only") is True
        and _count(backup, "column_count") == EXPECTED_COLUMN_COUNT
        and _count(backup, "row_count") == EXPECTED_ROW_COUNT
        and isinstance(ods_backup, Mapping)
        and set(ods_backup)
        == {
            "owner",
            "table",
            "exists",
            "column_count",
            "row_count",
            "untouched",
        }
        and ods_backup.get("owner") == BACKUP_OWNER
        and ods_backup.get("table") == ODS_BACKUP_TABLE
        and ods_backup.get("exists") is True
        and _count(ods_backup, "column_count") == 24
        and _count(ods_backup, "row_count") == 0
        and ods_backup.get("untouched") is True
    )


def _base(status: str, *, attempted: bool = False) -> dict[str, Any]:
    return {
        "mode": "dim_product_scd2_scenario_named_backup_gate",
        "status": status,
        "backup": {"owner": BACKUP_OWNER, "table": BACKUP_TABLE},
        "backup_creation_attempted": attempted,
        "backup_may_exist": attempted,
        "named_backup_retained": None if attempted else False,
        "automatic_restore_performed": False,
        "manual_recovery_review_required": attempted,
        "side_effects_may_have_occurred": attempted,
        "raw_rows_included": False,
        "pk_values_included": False,
        "session_id_included": False,
        "secret_printed": False,
    }


def run_dim_product_scd2_named_backup_gate(
    preflight: Mapping[str, Any],
    *,
    backup_creator: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Require an absent-only 26-column backup before any scenario run.

    The caller supplies a freshly collected read-only preflight and a separately
    approved callback that creates the fixed backup only when absent. The gate
    has no database connection or configured runtime of its own.
    """
    if not _preflight_matches(preflight):
        return _base("blocked_candidate_preflight")
    if not callable(backup_creator):
        return _base("blocked_backup_creator")

    try:
        proof = backup_creator(
            owner=BACKUP_OWNER,
            source_table=SOURCE_TABLE,
            backup_table=BACKUP_TABLE,
            expected_column_count=EXPECTED_COLUMN_COUNT,
            expected_row_count=EXPECTED_ROW_COUNT,
            require_absent=True,
        )
    except (RuntimeError, TypeError, ValueError, OSError, AttributeError, KeyError):
        return {
            **_base("failed", attempted=True),
            "stage": "named_backup_gate",
            "error_type": "BackupGateError",
        }

    if not _proof_matches(proof):
        return {
            **_base("failed", attempted=True),
            "stage": "named_backup_gate",
            "error_type": "BackupGateError",
        }

    return {
        **_base("passed", attempted=True),
        "backup_created_absent_only": True,
        "named_backup_retained": True,
        "backup_column_count": EXPECTED_COLUMN_COUNT,
        "backup_row_count": EXPECTED_ROW_COUNT,
        "ods_backup_untouched": True,
        "ods_backup_column_count": 24,
        "ods_backup_row_count": 0,
        "manual_recovery_review_required": False,
    }
