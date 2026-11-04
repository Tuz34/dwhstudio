"""
Golden Path Test — End-to-End Scaffold
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

10 kayitlik insert → pipeline → dogrulama akisi.
Bu test scaffold'dur; Oracle DB erisimi gerektirir.

NOT: Bu testler gercek Oracle baglantisi gerektirir.
      CI/CD ortaminda skip edilmeli.
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
    """End-to-end entegrasyon testleri."""

    def test_validate_known_table(self, dqm):
        """Bilinen bir tablo uzerinde dogrulama calistirir."""
        result = dqm.validate_table("DWH", "DWH_ODS_PRODUCT")
        summary = result.summary()
        assert summary["total_rows"] >= 0
        assert "violations" in summary

    def test_create_error_table(self, dqm):
        """ERR_ tablosu olusturma (zaten varsa skip)."""
        # Bu test idempotent — tekrar calistirmada hata vermez
        dqm.create_error_table("DWH", "DWH_ODS_PRODUCT")

    def test_watermark_list(self, wm):
        """Watermark listesini alir."""
        marks = wm.list_all_watermarks()
        assert isinstance(marks, list)

    def test_schema_change_detection(self):
        """Sema degisiklik taramasi calistirir."""
        from schema_change_detector import detect_changes
        changes = detect_changes("DWH")
        assert isinstance(changes, list)

    def test_monitoring_freshness(self, app_client):
        """Monitoring freshness endpoint'i calisir."""
        resp = app_client.get("/api/monitoring/freshness")
        assert resp.status_code == 200

    def test_alert_check_runs(self, app_client):
        """Otomatik alert kontrolu calisir."""
        resp = app_client.post(
            "/api/alerts/check",
            json={"approved": True, "freshness_days": 2},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "created_alerts" in data
