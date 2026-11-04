"""Mock-only tests for the fixed DIM_PRODUCT scenario backup boundary."""

from __future__ import annotations

from copy import deepcopy

import pytest


def _preflight():
    return {
        "status": "rerun_noop",
        "connection_attempted": True,
        "source_count": 72,
        "target_count": 72,
        "current_target_count": 72,
        "unchanged_count": 72,
        "source_duplicate_key_group_count": 0,
        "target_duplicate_surrogate_key_group_count": 0,
        "target_multiple_current_key_group_count": 0,
        "target_invalid_current_flag_count": 0,
        "target_invalid_effective_range_count": 0,
        "source_missing_current_count": 0,
        "insert_candidate_count": 0,
        "changed_candidate_count": 0,
        "candidate_count": 0,
        "backup_exists": 1,
        "backup_column_count": 24,
        "backup_row_count": 0,
        "write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _proof():
    return {
        "status": "passed",
        "side_effects": True,
        "backup": {
            "owner": "DWH",
            "table": "ODI68_DIM_PRODUCT_BKP",
            "source_table": "DIM_PRODUCT",
            "exists": True,
            "created_absent_only": True,
            "column_count": 26,
            "row_count": 72,
        },
        "ods_backup": {
            "owner": "DWH",
            "table": "ODI68_ODS_PRODUCTS_BKP",
            "exists": True,
            "column_count": 24,
            "row_count": 0,
            "untouched": True,
        },
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


class _Creator:
    def __init__(self, result=None):
        self.result = _proof() if result is None else result
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _gate(preflight=None, creator=None):
    from dim_product_scd2_scenario_backup import run_dim_product_scd2_named_backup_gate

    creator = _Creator() if creator is None else creator
    result = run_dim_product_scd2_named_backup_gate(
        _preflight() if preflight is None else preflight,
        backup_creator=creator,
    )
    return result, creator


def test_backup_gate_calls_fixed_absent_only_callback_and_returns_counts():
    result, creator = _gate()

    assert creator.calls == [
        {
            "owner": "DWH",
            "source_table": "DIM_PRODUCT",
            "backup_table": "ODI68_DIM_PRODUCT_BKP",
            "expected_column_count": 26,
            "expected_row_count": 72,
            "require_absent": True,
        }
    ]
    assert result["status"] == "passed"
    assert result["backup_created_absent_only"] is True
    assert result["backup_column_count"] == 26
    assert result["backup_row_count"] == 72
    assert result["ods_backup_untouched"] is True
    assert result["ods_backup_column_count"] == 24
    assert result["ods_backup_row_count"] == 0
    assert result["backup_may_exist"] is True
    assert result["named_backup_retained"] is True
    assert result["automatic_restore_performed"] is False
    assert result["raw_rows_included"] is False
    assert result["pk_values_included"] is False
    assert result["session_id_included"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "ready_for_scd2_load_review"),
        ("source_count", 71),
        ("target_count", 73),
        ("current_target_count", 71),
        ("unchanged_count", 71),
        ("source_duplicate_key_group_count", 1),
        ("target_duplicate_surrogate_key_group_count", 1),
        ("target_multiple_current_key_group_count", 1),
        ("target_invalid_current_flag_count", 1),
        ("target_invalid_effective_range_count", 1),
        ("source_missing_current_count", 1),
        ("insert_candidate_count", 1),
        ("changed_candidate_count", 1),
        ("candidate_count", 1),
        ("backup_exists", 0),
        ("backup_column_count", 25),
        ("backup_row_count", 1),
        ("write_attempted", True),
        ("raw_rows_included", True),
        ("pk_values_included", True),
        ("secret_printed", True),
        ("source_count", True),
        ("target_count", "72"),
    ],
)
def test_backup_gate_rejects_invalid_preflight_without_callback(field, value):
    preflight = _preflight()
    preflight[field] = value
    result, creator = _gate(preflight)

    assert result["status"] == "blocked_candidate_preflight"
    assert result["backup_creation_attempted"] is False
    assert result["backup_may_exist"] is False
    assert creator.calls == []


def test_backup_gate_rejects_missing_preflight_field():
    preflight = _preflight()
    del preflight["candidate_count"]
    result, creator = _gate(preflight)

    assert result["status"] == "blocked_candidate_preflight"
    assert creator.calls == []


def test_backup_gate_rejects_extra_preflight_data_without_echoing_it():
    preflight = _preflight()
    preflight["raw_row"] = "sensitive-value"
    result, creator = _gate(preflight)

    assert result["status"] == "blocked_candidate_preflight"
    assert creator.calls == []
    assert "sensitive-value" not in str(result)


def test_backup_gate_rejects_noncallable_creator_before_attempt():
    from dim_product_scd2_scenario_backup import run_dim_product_scd2_named_backup_gate

    result = run_dim_product_scd2_named_backup_gate(_preflight(), backup_creator=None)
    assert result["status"] == "blocked_backup_creator"
    assert result["backup_creation_attempted"] is False


@pytest.mark.parametrize(
    "path,value",
    [
        (("status",), "blocked_backup_already_exists"),
        (("side_effects",), False),
        (("backup", "table"), "ODI68_ODS_PRODUCTS_BKP"),
        (("backup", "source_table"), "ODS_SH_PRODUCTS"),
        (("backup", "exists"), False),
        (("backup", "created_absent_only"), False),
        (("backup", "column_count"), 25),
        (("backup", "row_count"), 71),
        (("ods_backup", "table"), "ODI68_DIM_PRODUCT_BKP"),
        (("ods_backup", "column_count"), 26),
        (("ods_backup", "row_count"), 1),
        (("ods_backup", "untouched"), False),
        (("raw_rows_included",), True),
        (("pk_values_included",), True),
        (("secret_printed",), True),
    ],
)
def test_backup_gate_rejects_invalid_callback_proof_with_partial_state(path, value):
    proof = deepcopy(_proof())
    target = proof
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result, creator = _gate(creator=_Creator(proof))

    assert len(creator.calls) == 1
    assert result["status"] == "failed"
    assert result["stage"] == "named_backup_gate"
    assert result["error_type"] == "BackupGateError"
    assert result["backup_creation_attempted"] is True
    assert result["backup_may_exist"] is True
    assert result["manual_recovery_review_required"] is True
    assert proof != result


def test_backup_gate_rejects_extra_payload_without_echoing_it():
    proof = _proof()
    proof["repository_id"] = "sensitive-id"
    result, _ = _gate(creator=_Creator(proof))

    assert result["status"] == "failed"
    assert "sensitive-id" not in str(result)


def test_backup_gate_sanitizes_callback_exception_and_marks_partial_state():
    class BrokenCreator:
        def __call__(self, **_kwargs):
            raise RuntimeError("secret or raw row should not be returned")

    result, _ = _gate(creator=BrokenCreator())

    assert result["status"] == "failed"
    assert result["error_type"] == "BackupGateError"
    assert result["backup_may_exist"] is True
    assert "secret or raw row" not in str(result)


def test_backup_gate_sanitizes_unexpected_exception_type():
    class DriverError(Exception):
        pass

    class BrokenCreator:
        def __call__(self, **_kwargs):
            raise DriverError("sensitive-value")

    result, _ = _gate(creator=BrokenCreator())

    assert result["status"] == "failed"
    assert result["error_type"] == "BackupGateError"
    assert result["backup_may_exist"] is True
    assert result["manual_recovery_review_required"] is True
    assert "sensitive-value" not in str(result)
    assert "DriverError" not in str(result)
