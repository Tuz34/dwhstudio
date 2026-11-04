from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_FILENAME = "KM_IKM Oracle Slowly Changing Dimension.xml"


def _write_reference(
    path: Path,
    *,
    name: str = "IKM Oracle Slowly Changing Dimension",
    technology: str = "ORACLE",
    integration_type: str = "SLOWLY_CHANGING_DIMENSION",
) -> Path:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<SunopsisExport>
  <Object class="com.sunopsis.dwg.dbobj.SnpTrt">
    <Field name="GlobalId" type="java.lang.String"><![CDATA[must-not-be-returned]]></Field>
    <Field name="TrtName" type="java.lang.String"><![CDATA[%s]]></Field>
    <Field name="KmTechno" type="java.lang.String"><![CDATA[%s]]></Field>
    <Field name="IntgType" type="java.lang.String"><![CDATA[%s]]></Field>
    <Field name="Txt" type="java.lang.String"><![CDATA[option and implementation details]]></Field>
  </Object>
</SunopsisExport>
"""
        % (name, technology, integration_type),
        encoding="utf-8",
    )
    return path


def _missing_repository_capability():
    return {
        "status": "passed",
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "target_technology": None,
        "target_ikm": None,
        "target_ikm_installed": False,
        "target_ikm_assignable": False,
        "lkm": "LKM SQL to Oracle (Built-In)",
        "scd2_role_assignment_supported": True,
    }


def test_offline_reference_probe_returns_only_allowlisted_metadata(tmp_path):
    from dim_product_odi_scd2_ikm_resolution import inspect_scd2_ikm_reference

    result = inspect_scd2_ikm_reference(
        _write_reference(tmp_path / REFERENCE_FILENAME)
    )

    assert result == {
        "status": "reference_ready_for_review",
        "mode": "offline_allowlisted_odi_sdk_reference_probe",
        "reference_filename": REFERENCE_FILENAME,
        "ikm_name": "IKM Oracle Slowly Changing Dimension",
        "technology": "ORACLE",
        "integration_type": "SLOWLY_CHANGING_DIMENSION",
        "model_call_attempted": False,
        "repository_connection_attempted": False,
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    assert "GlobalId" not in result
    assert "Txt" not in result


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"name": "IKM Oracle Insert"}, "blocked_reference_identity"),
        ({"technology": "MSSQL"}, "blocked_reference_identity"),
        ({"integration_type": "INCREMENTAL_UPDATE"}, "blocked_reference_identity"),
    ],
)
def test_offline_reference_probe_rejects_non_allowlisted_candidate(
    tmp_path, overrides, expected
):
    from dim_product_odi_scd2_ikm_resolution import inspect_scd2_ikm_reference

    result = inspect_scd2_ikm_reference(
        _write_reference(tmp_path / REFERENCE_FILENAME, **overrides)
    )

    assert result["status"] == expected
    assert result["repository_connection_attempted"] is False
    assert "IKM Oracle Insert" not in str(result)


def test_offline_reference_probe_blocks_wrong_filename_before_xml_read(tmp_path):
    from dim_product_odi_scd2_ikm_resolution import inspect_scd2_ikm_reference

    path = _write_reference(tmp_path / "unapproved.xml")
    result = inspect_scd2_ikm_reference(path)

    assert result["status"] == "blocked_reference_filename"
    assert result["repository_connection_attempted"] is False


def test_resolution_plan_separates_installed_template_from_repository_import(tmp_path):
    from dim_product_odi_scd2_ikm_resolution import (
        build_dim_product_odi_scd2_ikm_resolution_plan,
        inspect_scd2_ikm_reference,
    )

    reference = inspect_scd2_ikm_reference(
        _write_reference(tmp_path / REFERENCE_FILENAME)
    )
    plan = build_dim_product_odi_scd2_ikm_resolution_plan(
        repository_capability=_missing_repository_capability(),
        installed_reference=reference,
    )

    assert plan["status"] == "ready_for_separate_ikm_import_contract_review"
    assert plan["official_reference_available"] is True
    assert plan["repository_ikm_installed"] is False
    assert plan["required_ikm"] == "IKM Oracle Slowly Changing Dimension"
    assert plan["import_authorized"] is False
    assert plan["will_execute"] is False
    assert plan["repository_connection_attempted"] is False
    assert plan["write_operations_attempted"] == []


def test_resolution_plan_fails_closed_when_reference_is_not_proven():
    from dim_product_odi_scd2_ikm_resolution import (
        build_dim_product_odi_scd2_ikm_resolution_plan,
    )

    plan = build_dim_product_odi_scd2_ikm_resolution_plan(
        repository_capability=_missing_repository_capability(),
        installed_reference={"status": "blocked_reference_identity"},
    )

    assert plan["status"] == "blocked_installed_reference"
    assert plan["import_authorized"] is False


def test_resolution_plan_reports_existing_repository_ikm_without_import_authority(tmp_path):
    from dim_product_odi_scd2_ikm_resolution import (
        build_dim_product_odi_scd2_ikm_resolution_plan,
        inspect_scd2_ikm_reference,
    )

    capability = _missing_repository_capability()
    capability.update(
        {
            "target_technology": "ORACLE",
            "target_ikm": "IKM Oracle Slowly Changing Dimension",
            "target_ikm_installed": True,
            "target_ikm_assignable": True,
        }
    )
    plan = build_dim_product_odi_scd2_ikm_resolution_plan(
        repository_capability=capability,
        installed_reference=inspect_scd2_ikm_reference(
            _write_reference(tmp_path / REFERENCE_FILENAME)
        ),
    )

    assert plan["status"] == "repository_ikm_already_available"
    assert plan["repository_ikm_installed"] is True
    assert plan["import_authorized"] is False


def test_resolution_module_and_runbook_have_no_live_or_import_surface():
    module = (
        PROJECT_ROOT / "dim_product_odi_scd2_ikm_resolution.py"
    ).read_text(encoding="utf-8")
    runbook = (
        PROJECT_ROOT / "docs" / "dwh-dim-product-odi-scd2-ikm-resolution.md"
    ).read_text(encoding="utf-8")
    combined = f"{module}\n{runbook}".lower()

    for forbidden in (
        "run_groovy",
        "oracledb",
        "connect(",
        "importservice",
        "smartimport",
        "em.persist",
        "commit(",
        "@app.route",
        "blueprint",
        "generate_scenario",
        "startscenario",
    ):
        assert forbidden not in combined

    for required in (
        "separate import implementation",
        "separate independent review",
        "exact operator approval",
        "no raw rows",
        "no pk values",
        "no repository ids",
        "does not authorize mapping creation",
        "does not authorize scenario run",
    ):
        assert required in runbook.lower()
