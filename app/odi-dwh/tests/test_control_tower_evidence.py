from pathlib import Path

from control_tower_evidence import build_control_tower_evidence


def test_control_tower_evidence_is_redacted_and_side_effect_free(tmp_path: Path):
    (tmp_path / "error_pattern_memory.py").write_text("# helper", encoding="utf-8")
    (tmp_path / "reset_safety.py").write_text("# helper", encoding="utf-8")
    (tmp_path / "morning_report.py").write_text("# helper", encoding="utf-8")

    report = build_control_tower_evidence(tmp_path)

    assert report["status"] == "ready_for_preflight"
    assert report["safety"] == {
        "side_effects": False,
        "live_odi_touched": False,
        "oracle_ddl_or_dml": False,
        "raw_rows_included": False,
        "secret_printed": False,
    }
    assert report["summary"]["artifact_ready_count"] == 3
    assert any(item["status"] == "waiting" for item in report["chain"])
    assert "oracle1" not in str(report).lower()


def test_control_tower_evidence_endpoint_returns_shape(app_client):
    resp = app_client.get("/api/control-tower/evidence")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["environment"] == "local_dev_tst"
    assert data["safety"]["secret_printed"] is False
    assert isinstance(data["chain"], list)
    assert isinstance(data["artifacts"], list)
