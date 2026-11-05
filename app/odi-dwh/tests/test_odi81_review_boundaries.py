"""Regression cases found by independent review of the first harness."""

from __future__ import annotations

import json

import pytest

import odi81_app_acceptance as driver
import odi81_scd2_fixtures as fixtures
from tests.odi81_harness_support import Database
from tests.test_odi81_app_acceptance import BINDINGS, Client, factories
from tests.test_odi81_scd2_fixtures import backup, verify


@pytest.fixture
def db():
    state = Database()
    yield state
    state.close()


@pytest.mark.parametrize("flow", ["F4", "F5"])
def test_duplicate_identical_retained_backup_rows_are_not_a_checkpoint(db, flow):
    db.install_source(flow, "seed")
    db.install_target(flow, "seed")
    db.install_backup(flow, "target", 2, "seed")
    name, key = fixtures.backup_name(flow, "target", 2), fixtures.SK[flow]
    db.db.execute(f'INSERT INTO "{name}" SELECT * FROM "{name}" WHERE "{key}"=1')
    report = backup(db, flow, "target", 2)
    assert report["status"] != "passed" and report["full_target_snapshot_equal"] is False


@pytest.mark.parametrize("flow", ["F4", "F5"])
@pytest.mark.parametrize("column,value", [
    ("ATTR_TEXT", "CORRUPT"), ("VALUE_AMT", 999), ("SK", 900),
    ("INSERT_DATE", "2025-01-01T00:00:00"), ("UPDATE_DATE", "2025-01-01T00:00:00"),
    ("SOURCE_SYSTEM", "CORRUPT"), ("ETL_BATCH_ID", 999), ("EFFECTIVE_FROM", "2019-01-01"),
])
def test_expired_version_preserves_the_retained_before_image(db, flow, column, value):
    db.install_source(flow, "changed")
    db.install_target(flow, "changed")
    db.install_backup(flow, "target", 2, "seed")
    field = fixtures.SK[flow] if column == "SK" else column
    target = fixtures.FLOWS[flow]["target_table"]
    db.db.execute(f'UPDATE "{target}" SET "{field}"=? WHERE "CURRENT_FLAG"=\'0\'', (value,))
    report = verify(db, flow, 2)
    assert report["status"] != "passed"


@pytest.mark.parametrize("flow", ["F4", "F5"])
def test_unchanged_current_versions_keep_audit_and_surrogate_values_on_run2(db, flow):
    db.install_source(flow, "changed")
    db.install_target(flow, "changed")
    db.install_backup(flow, "target", 2, "seed")
    target, key = fixtures.FLOWS[flow]["target_table"], fixtures.SK[flow]
    db.db.execute(f'UPDATE "{target}" SET "UPDATE_DATE"=? WHERE "{key}"=2', ("2025-01-01T00:00:00",))
    assert verify(db, flow, 2)["status"] != "passed"


@pytest.mark.parametrize("flow", ["F4", "F5"])
def test_run2_preflight_validates_seed_checkpoint_not_already_changed_source(db, flow):
    db.install_source(flow, "changed")
    db.install_target(flow, "seed")
    db.install_backup(flow, "target", 2, "seed")
    report = verify(db, flow, 2, "before_run")
    assert report["status"] == "passed" and report["full_target_snapshot_equal"] is False


@pytest.mark.parametrize("response", ["unknown", "raises"])
def test_uncertain_http_writes_require_reconciliation(db, response):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.fail_on = 2 if response == "unknown" else None
    client.raises = response == "raises"
    report = driver.run_acceptance("F4", live=True, approvals=driver.full_approvals("F4"),
        http_client=client, writer_factories=writers, reader_factories=readers,
        source_reader_factory=source, context="GLOBAL", source_model_bindings=BINDINGS)
    assert report["status"] != "passed" and report["downstream_stopped"] is True
    assert report["outcome_unknown"] is True and report["manual_review_required"] is True
    assert "app_run_2" not in report["calls"]


def test_even_a_configured_other_context_is_rejected_before_callbacks(db):
    writers, readers, source = factories(db)
    client = Client(db, "F4")
    client.context = "DEV"
    bindings = json.dumps({"DEV": {"DWH": "TGT_DWH", "DM": "TGT_DM"}})
    report = driver.run_acceptance("F4", live=True, approvals=driver.full_approvals("F4"),
        http_client=client, writer_factories=writers, reader_factories=readers,
        source_reader_factory=source, context="DEV", source_model_bindings=bindings)
    assert report["status"] == "blocked" and db.events == [] and client.paths == []


def test_every_scd2_run_has_a_before_run_scope_and_step():
    for run in (1, 2, 3):
        assert f"verify_before_r{run}" in driver.CANONICAL_CALLS
        assert f"odi81_f4_verify_before_r{run}" in driver.required_scopes("F4")
