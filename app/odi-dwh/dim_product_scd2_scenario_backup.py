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


def call_sanitized(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a live callback; return None on any exception, never echoing it.

    This is the single broad ``except Exception`` for the ODI-68 callback
    boundary (shared with the run executor). Driver or subprocess errors
    (``oracledb.Error``, ``subprocess.TimeoutExpired``) must become a structured
    partial-state report rather than escape; the exception text is discarded.
    """
    try:
        return callback(*args, **kwargs)
    except Exception:  # noqa: BLE001 - class-only report, text intentionally dropped
        return None


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


def _proof_matches(payload: object, *, retained_backup: bool = False) -> bool:
    if type(retained_backup) is not bool:
        return False
    if not isinstance(payload, Mapping):
        return False
    backup = payload.get("backup")
    ods_backup = payload.get("ods_backup")
    mode_keys = (
        {
            "verified_retained", "metadata_matches", "surrogate_key_null_count",
            "surrogate_key_duplicate_group_count", "source_minus_backup_count",
            "backup_minus_source_count",
        }
        if retained_backup else {"created_absent_only"}
    )
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
        and payload.get("side_effects") is (not retained_backup)
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
            "column_count",
            "row_count",
        } | mode_keys
        and backup.get("owner") == BACKUP_OWNER
        and backup.get("table") == BACKUP_TABLE
        and backup.get("source_table") == SOURCE_TABLE
        and backup.get("exists") is True
        and (
            (
                backup.get("verified_retained") is True
                and backup.get("metadata_matches") is True
                and all(_count(backup, key) == 0 for key in (
                    "surrogate_key_null_count", "surrogate_key_duplicate_group_count",
                    "source_minus_backup_count", "backup_minus_source_count",
                ))
            ) if retained_backup else backup.get("created_absent_only") is True
        )
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


def _base(
    status: str, *, attempted: bool = False,
    retained_backup: bool = False, verification_attempted: bool = False,
) -> dict[str, Any]:
    return {
        "mode": "dim_product_scd2_scenario_named_backup_gate",
        "status": status,
        "backup": {"owner": BACKUP_OWNER, "table": BACKUP_TABLE},
        "backup_creation_attempted": attempted,
        "backup_verification_attempted": verification_attempted,
        "backup_mode": "retained_verification" if retained_backup else "absent_only_creation",
        "backup_reused_verified": False,
        "backup_may_exist": attempted or retained_backup,
        "named_backup_retained": None if attempted or retained_backup else False,
        "automatic_restore_performed": False,
        "manual_recovery_review_required": attempted or verification_attempted,
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
    retained_backup: bool = False,
) -> dict[str, Any]:
    """Require an absent-only backup, or explicitly opt into retained verification.

    The caller supplies a freshly collected read-only preflight and a separately
    approved callback that creates the fixed backup only when absent. The gate
    has no database connection or configured runtime of its own. With
    retained_backup=True, backup_creator must instead be the separate read-only
    verifier callback; it is invoked with require_absent=False. Proofs cannot be
    interchanged between modes.
    """
    if type(retained_backup) is not bool:
        return _base("blocked_retained_backup_type")
    if not _preflight_matches(preflight):
        return _base("blocked_candidate_preflight", retained_backup=retained_backup)
    if not callable(backup_creator):
        return _base("blocked_backup_creator", retained_backup=retained_backup)

    proof = call_sanitized(
        backup_creator,
        owner=BACKUP_OWNER,
        source_table=SOURCE_TABLE,
        backup_table=BACKUP_TABLE,
        expected_column_count=EXPECTED_COLUMN_COUNT,
        expected_row_count=EXPECTED_ROW_COUNT,
        require_absent=not retained_backup,
    )

    state = _base(
        "passed", attempted=not retained_backup,
        retained_backup=retained_backup, verification_attempted=retained_backup,
    )
    if not _proof_matches(proof, retained_backup=retained_backup):
        return {
            **state,
            "status": "failed",
            "stage": "named_backup_gate",
            "error_type": "BackupGateError",
        }

    return {
        **state,
        "backup_created_absent_only": not retained_backup,
        "backup_reused_verified": retained_backup,
        "named_backup_retained": True,
        "backup_column_count": EXPECTED_COLUMN_COUNT,
        "backup_row_count": EXPECTED_ROW_COUNT,
        "ods_backup_untouched": True,
        "ods_backup_column_count": 24,
        "ods_backup_row_count": 0,
        "manual_recovery_review_required": False,
    }
