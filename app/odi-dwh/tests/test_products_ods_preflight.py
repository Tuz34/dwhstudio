from __future__ import annotations

from pathlib import Path

import pytest

from dwh_load_pattern_manager import PRODUCTS_ODS_AUDIT_METADATA
from scd2_manager import PRODUCTS_SOURCE_METADATA


def _rows(metadata):
    rows = []
    for name, full_type, nullable in metadata:
        if full_type.startswith("VARCHAR2("):
            data_type = "VARCHAR2"
            length = int(full_type.removeprefix("VARCHAR2(").removesuffix(")"))
            precision = scale = None
        elif full_type.startswith("NUMBER("):
            data_type = "NUMBER"
            body = full_type.removeprefix("NUMBER(").removesuffix(")")
            parts = [int(part) for part in body.split(",")]
            precision = parts[0]
            scale = parts[1] if len(parts) == 2 else 0
            length = 22
        elif full_type == "TIMESTAMP":
            data_type = "TIMESTAMP(6)"
            length = 11
            precision = scale = None
        else:
            data_type = full_type
            length = 22 if data_type == "NUMBER" else 7
            precision = scale = None
        rows.append((name, data_type, length, precision, scale, "Y" if nullable else "N"))
    return rows


class _Cursor:
    def __init__(self, metadata, row_count, duplicate_count):
        self.metadata = metadata
        self.row_count = row_count
        self.duplicate_count = duplicate_count
        self.result = []
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        upper = sql.upper()
        if upper.startswith("SET TRANSACTION READ ONLY"):
            self.result = []
        elif "FROM ALL_TAB_COLUMNS" in upper:
            self.result = list(self.metadata)
        elif "HAVING COUNT(*) > 1" in upper:
            self.result = [(self.duplicate_count,)]
        elif upper.startswith("SELECT COUNT(*)"):
            self.result = [(self.row_count,)]
        else:
            raise AssertionError(f"Unexpected statement: {sql}")

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0]

    def close(self):
        return None


class _Connection:
    def __init__(self, metadata, row_count=0, duplicate_count=0):
        self.cursor_instance = _Cursor(metadata, row_count, duplicate_count)
        self.rollback_count = 0

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollback_count += 1


def test_live_preflight_reports_source_counts_and_blocks_missing_target():
    from products_ods_preflight import run_products_ods_read_only_preflight

    source = _Connection(_rows(PRODUCTS_SOURCE_METADATA), row_count=72, duplicate_count=0)
    target = _Connection([], row_count=0, duplicate_count=0)

    report = run_products_ods_read_only_preflight(source, target)

    assert report["status"] == "blocked_target_missing"
    assert report["source"]["row_count"] == 72
    assert report["source"]["duplicate_key_group_count"] == 0
    assert report["source"]["metadata_status"] == "metadata_ready_for_review"
    assert report["target"]["exists"] is False
    assert report["target"]["row_count"] is None
    assert report["backup_restore_readiness"]["status"] == "absent_target_baseline_required"
    assert report["raw_rows_included"] is False
    assert report["pk_values_included"] is False
    assert report["write_attempted"] is False
    assert source.rollback_count == 1
    assert target.rollback_count == 1


def test_live_preflight_accepts_aligned_existing_target():
    from products_ods_preflight import run_products_ods_read_only_preflight

    source = _Connection(_rows(PRODUCTS_SOURCE_METADATA), row_count=72, duplicate_count=0)
    target_metadata = (*PRODUCTS_SOURCE_METADATA, *PRODUCTS_ODS_AUDIT_METADATA)
    target = _Connection(_rows(target_metadata), row_count=72, duplicate_count=0)

    report = run_products_ods_read_only_preflight(source, target)

    assert report["status"] == "ready_for_named_backup"
    assert report["target"]["metadata_status"] == "metadata_ready_for_review"
    assert report["target"]["column_count"] == 24
    assert report["target"]["row_count"] == 72
    assert report["target"]["duplicate_key_group_count"] == 0
    assert report["backup_restore_readiness"]["status"] == "named_backup_required"


def test_live_preflight_requires_ods_audit_columns_on_existing_target():
    from products_ods_preflight import run_products_ods_read_only_preflight

    source = _Connection(_rows(PRODUCTS_SOURCE_METADATA), row_count=72, duplicate_count=0)
    target = _Connection(_rows(PRODUCTS_SOURCE_METADATA), row_count=0, duplicate_count=0)

    report = run_products_ods_read_only_preflight(source, target)

    assert report["status"] == "blocked_target_metadata_drift"
    assert report["target"]["metadata_drift"]["missing_columns"] == [
        "ODS_CREATED_AT",
        "ODS_UPDATED_AT",
    ]


def test_live_preflight_fails_closed_on_source_metadata_drift():
    from products_ods_preflight import run_products_ods_read_only_preflight

    drifted = tuple(item for item in PRODUCTS_SOURCE_METADATA if item[0] != "PROD_STATUS")
    source = _Connection(_rows(drifted), row_count=72, duplicate_count=0)
    target = _Connection([], row_count=0, duplicate_count=0)

    report = run_products_ods_read_only_preflight(source, target)

    assert report["status"] == "blocked_source_metadata_drift"
    assert report["source"]["metadata_drift"]["missing_columns"] == ["PROD_STATUS"]


def test_absent_target_backup_plan_is_named_but_does_not_claim_live_restore():
    from products_ods_preflight import (
        build_products_ods_backup_restore_plan,
        run_products_ods_read_only_preflight,
    )

    preflight = run_products_ods_read_only_preflight(
        _Connection(_rows(PRODUCTS_SOURCE_METADATA), row_count=72),
        _Connection([]),
    )
    plan = build_products_ods_backup_restore_plan(
        preflight,
        artifact_id="ODI68_PRODUCTS_ODS_20260713T152758",
    )

    assert plan["status"] == "static_restore_contract_ready"
    assert plan["baseline_artifact_id"] == "ODI68_PRODUCTS_ODS_20260713T152758"
    assert plan["existing_target_backup_object"] is None
    assert plan["previous_state"] == "target_absent"
    assert plan["restore_actions"] == [
        "verify_target_created_by_approved_run",
        "verify_downstream_dim_not_promoted",
        "remove_new_target_after_separate_approval",
        "confirm_target_absent_by_metadata",
    ]
    assert plan["static_contract_tested"] is True
    assert plan["live_restore_tested"] is False
    assert plan["requires_separate_persistent_approval"] is True
    assert plan["will_execute"] is False


def test_backup_plan_rejects_unreviewed_artifact_identifier():
    from products_ods_preflight import build_products_ods_backup_restore_plan

    with pytest.raises(ValueError, match="artifact"):
        build_products_ods_backup_restore_plan(
            {"target": {"exists": False}},
            artifact_id="unsafe/name",
        )


def test_preflight_module_has_no_persistent_sql_surface():
    source = (Path(__file__).parents[1] / "products_ods_preflight.py").read_text(encoding="utf-8")
    lowered = source.lower()

    assert "set transaction read only" in lowered
    for forbidden in (
        "create table",
        "insert into",
        "merge into",
        "update dwh",
        "delete from",
        "truncate table",
        "drop table",
    ):
        assert forbidden not in lowered
