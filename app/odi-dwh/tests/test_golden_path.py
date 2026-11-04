"""
Golden Path Test — End-to-End Scaffold
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Insert 10 records → pipeline → validation flow.
This is a test scaffold; it requires Oracle DB access.

NOTE: These tests require a real Oracle connection.
      They should be skipped in CI/CD environments.
"""

import pytest
import os
from pathlib import Path

# Live DB integration is opt-in even when local credentials are configured.
skip_no_oracle = pytest.mark.skipif(
    not os.getenv("ORACLE_DSN") or os.getenv("RUN_LIVE_DB_TESTS", "").lower() != "true",
    reason="Live Oracle tests require RUN_LIVE_DB_TESTS=true",
)


def test_live_golden_path_requires_explicit_opt_in():
    source = Path(__file__).read_text(encoding="utf-8")

    assert 'os.getenv("RUN_LIVE_DB_TESTS", "").lower() != "true"' in source


@skip_no_oracle
class TestGoldenPath:
    """End-to-end integration tests."""

    def test_validate_known_table(self, dqm):
        """Run validation on a known table."""
        result = dqm.validate_table("DWH", "DWH_ODS_PRODUCT")
        summary = result.summary()
        assert summary["total_rows"] >= 0
        assert "violations" in summary

    def test_create_error_table(self, dqm):
        """Create the ERR_ table (skip if it already exists)."""
        # This test is idempotent — rerunning it does not fail.
        dqm.create_error_table("DWH", "DWH_ODS_PRODUCT")

    def test_watermark_list(self, wm):
        """Get the watermark list."""
        marks = wm.list_all_watermarks()
        assert isinstance(marks, list)

    def test_schema_change_detection(self):
        """Run a schema change scan."""
        from schema_change_detector import detect_changes
        changes = detect_changes("DWH")
        assert isinstance(changes, list)

    def test_monitoring_freshness(self, app_client):
        """Check that the monitoring freshness endpoint runs."""
        resp = app_client.get("/api/monitoring/freshness")
        assert resp.status_code == 200

    def test_alert_check_runs(self, app_client):
        """Check that the automatic alert check runs."""
        resp = app_client.post(
            "/api/alerts/check",
            json={"approved": True, "freshness_days": 2},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "created_alerts" in data
