from pathlib import Path
import json
import subprocess
import sys

from reset_safety import ResetTarget, build_dry_run_report, inventory_scripts, validate_reset_target


def test_metadata_only_allows_odi_metadata_target():
    result = validate_reset_target(
        ResetTarget(action="DELETE_MAPPING", object_ref="ODI:MAP_HR_EMPLOYEES_TO_ODS"),
        level="metadata_only",
    )

    assert result["status"] == "allowed"
    assert result["reason"] == "odi_metadata_allowlist"


def test_metadata_only_rejects_oracle_object():
    result = validate_reset_target(
        ResetTarget(action="DROP", object_ref="DWH.ODS_HR_EMPLOYEES"),
        level="metadata_only",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "metadata_only_rejects_oracle_object"


def test_ods_tables_only_allows_dev_tst_dwh_prefixes():
    result = validate_reset_target(
        ResetTarget(action="TRUNCATE", object_ref="DWH.ODS_HR_EMPLOYEES"),
        level="ods_tables_only",
    )

    assert result["status"] == "allowed"
    assert result["reason"] == "dwh_dev_tst_object_allowlist"


def test_ods_tables_only_rejects_source_schema():
    result = validate_reset_target(
        ResetTarget(action="DELETE", object_ref="HR.EMPLOYEES"),
        level="ods_tables_only",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "owner_forbidden_for_ods_reset"


def test_ods_tables_only_rejects_non_allowlisted_dwh_object():
    result = validate_reset_target(
        ResetTarget(action="DROP", object_ref="DWH.DIM_CUSTOMER"),
        level="ods_tables_only",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "object_not_in_dev_tst_allowlist"


def test_full_reseed_requires_explicit_scope():
    blocked = validate_reset_target(
        ResetTarget(action="DROP", object_ref="DEV_ODI_REPO.*"),
        level="full_dev_tst_reseed",
    )
    allowed = validate_reset_target(
        ResetTarget(action="DROP", object_ref="DEV_ODI_REPO.*"),
        level="full_dev_tst_reseed",
        explicit_scope=True,
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "explicit_scope_required"
    assert allowed["status"] == "allowed"


def test_dry_run_report_is_blocked_if_any_target_is_blocked():
    report = build_dry_run_report(
        [
            ResetTarget(action="TRUNCATE", object_ref="DWH.ODS_HR_EMPLOYEES"),
            ResetTarget(action="DROP", object_ref="SYSTEM.AUD$"),
        ],
        level="ods_tables_only",
    )

    assert report["side_effects"] is False
    assert report["status"] == "blocked"
    assert report["allowed_count"] == 1
    assert report["blocked_count"] == 1


def test_inventory_scripts_reports_destructive_tokens(tmp_path: Path):
    (tmp_path / "safe.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "dropper.py").write_text("cursor.execute('DROP TABLE DWH.ODS_X')", encoding="utf-8")
    odi_dir = tmp_path / "odi_automation"
    odi_dir.mkdir()
    (odi_dir / "delete_mapping.groovy").write_text("em.remove(mapping)\ntxnMgr.commit(txn)", encoding="utf-8")

    inventory = inventory_scripts(tmp_path)

    paths = {item["path"] for item in inventory}
    assert "dropper.py" in paths
    assert "odi_automation/delete_mapping.groovy" in paths
    assert "safe.py" not in paths
    assert all(item["review_required"] for item in inventory)


def test_cli_targets_file_avoids_shell_json_quoting(tmp_path: Path):
    targets_file = tmp_path / "targets.json"
    targets_file.write_text(
        json.dumps(
            [
                {"action": "TRUNCATE", "object_ref": "DWH.ODS_HR_EMPLOYEES"},
                {"action": "DROP", "object_ref": "HR.EMPLOYEES"},
            ]
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "reset_safety.py",
            "--level",
            "ods_tables_only",
            "--targets-file",
            str(targets_file),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0
    report = json.loads(proc.stdout)["dry_run"]
    assert report["status"] == "blocked"
    assert report["allowed_count"] == 1
    assert report["blocked_count"] == 1
