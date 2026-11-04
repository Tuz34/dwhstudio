"""WP-E2E-A Groovy contracts: source assertions plus a syntax-only parse.

The parse stops at Groovy's CONVERSION phase, so it never resolves the Oracle ODI
SDK and never contacts a repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


GROOVY_ROOT = Path(__file__).resolve().parents[1] / "odi_automation"
SCRIPTS = (
    "generate_scenario_metadata.groovy",
    "register_datastore.groovy",
    "create_mapping.groovy",
    "update_mapping.groovy",
)


def _text(name: str) -> str:
    return (GROOVY_ROOT / name).read_text(encoding="utf-8")


def _syntax_command(script: Path) -> list[str] | None:
    parse_script = (
        "import org.codehaus.groovy.control.CompilationUnit\n"
        "import org.codehaus.groovy.control.CompilerConfiguration\n"
        "import org.codehaus.groovy.control.Phases\n"
        "def unit = new CompilationUnit(new CompilerConfiguration())\n"
        "unit.addSource(new File(args[0]))\n"
        "unit.compile(Phases.CONVERSION)\n"
        "println 'GROOVY_SYNTAX_OK'\n"
    )
    groovy = shutil.which("groovy") or shutil.which("groovy.bat")
    if groovy:
        return [groovy, "-e", parse_script, str(script)]
    java_home = os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")
    java = shutil.which("java.exe" if os.name == "nt" else "java") or str(
        Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
    )
    odi_home = os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home")
    jar_name = os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    jar = Path(odi_home) / "oracle_common" / "modules" / "thirdparty" / jar_name
    if Path(java).is_file() and jar.is_file():
        return [java, "-cp", str(jar), "groovy.ui.GroovyMain", "-e", parse_script, str(script)]
    return None


@pytest.mark.parametrize("name", SCRIPTS)
def test_changed_scripts_parse(name):
    command = _syntax_command(GROOVY_ROOT / name)
    if command is None:
        pytest.skip("Groovy is not installed and the SDK groovy-all jar was not found.")
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=90, check=False
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, combined
    assert "GROOVY_SYNTAX_OK" in completed.stdout


# --- Scenario version check (Defect A) ---------------------------------------


def test_scenario_check_looks_at_every_version_not_only_the_latest():
    text = _text("generate_scenario_metadata.groovy")
    helper = text[text.index("def scenariosByName"): text.index("try {\n    try {")]

    assert "finder.findByName(scenarioName)" in helper
    assert "finder.findAll()" in helper
    assert "findLatestByName" not in helper
    assert "equalsIgnoreCase(version)" in helper


def test_scenario_exact_check_is_not_a_tautology_and_reports_latest_version():
    text = _text("generate_scenario_metadata.groovy")

    assert 'existingExact == null ? "pass" : "pass"' not in text
    assert "latest_version" in text
    # already_ready and passed envelopes both expose latest_version.
    assert text.count("latest_version: ") >= 3
    already_ready = text[text.index('status: "already_ready"'):]
    assert "latest_version: latestVersion" in already_ready[:400]
    assert "latest_version: newLatest" in text


def test_existing_scenario_version_short_circuits_before_generation():
    text = _text("generate_scenario_metadata.groovy")

    exact_at = text.index("if (existingExact != null) {")
    generate_at = text.index("ScenarioUtils.generateNewScen")
    assert exact_at < generate_at
    assert 'status: "already_ready"' in text[exact_at:generate_at]


# --- G1 update key ---------------------------------------------------------------


def test_register_datastore_key_parameters_are_read_and_validated():
    text = _text("register_datastore.groovy")

    for prop in ("odi.key_columns", "odi.key_name", "odi.key_type", "odi.key_only"):
        assert f'System.getProperty("{prop}"' in text
    assert "new OdiKey(targetDs, keyName)" in text
    assert "OdiKey.KeyType.valueOf" in text
    assert "Key column is missing from datastore" in text
    assert "Existing key columns do not match" in text
    assert '"odi.register_datastore"' in text


def test_register_datastore_key_only_never_removes_the_datastore():
    text = _text("register_datastore.groovy")
    key_only = text[text.index("if (keyOnly) {"): text.index("// 2. Check whether it already exists")]

    assert "em.remove" not in key_only
    assert "Datastore not found for key registration" in key_only
    assert 'mode: "key_only"' in key_only


def test_register_datastore_default_path_is_unchanged_without_key():
    text = _text("register_datastore.groovy")

    assert "em.remove(existing)" in text
    assert "if (keyRequested) {" in text
    assert "if (keyInfo != null) {" in text


def test_create_mapping_fails_closed_for_merge_ikm_without_key():
    text = _text("create_mapping.groovy")

    assert "Update key is required for a merge or update IKM" in text
    assert "ukColNames.isEmpty()" in text
    assert 'selectedIkmLower.contains("merge")' in text


# --- G4 integration type -------------------------------------------------------------


def test_create_mapping_integration_type_is_restricted_and_defaults_to_iu():
    text = _text("create_mapping.groovy")

    assert 'System.getProperty("odi.integration_type") ?: "IU"' in text
    assert 'in ["IU", "CONTROL_APPEND"]' in text
    assert "Invalid odi.integration_type" in text
    assert "boolean controlAppend" in text


def test_create_mapping_control_append_is_verified_and_insert_only():
    text = _text("create_mapping.groovy")

    assert "INTEGRATION_TYPE_APPEND" in text
    assert "Control append integration type is unavailable" in text
    assert "Control append cannot use a merge or update IKM" in text
    assert "if (tgtBoundDS != null && !controlAppend)" in text
    assert 'physItCode = controlAppend ? "CA" : "IU"' in text


def test_create_mapping_keeps_iu_update_key_contract():
    text = _text("create_mapping.groovy")

    assert "setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_UPDATE)" in text
    assert "Required update key is missing or mismatched" in text


# --- G4 truncate option -------------------------------------------------------------


def test_update_mapping_reports_unsupported_option_as_status_not_throw():
    text = _text("update_mapping.groovy")

    assert "class IkmOptionUnsupported extends RuntimeException" in text
    assert "throw new IkmOptionUnsupported(optionName)" in text
    catch_unsupported = text[text.index("} catch (IkmOptionUnsupported unsupported) {"):]
    first_block = catch_unsupported[: catch_unsupported.index("} catch (Exception e) {")]
    assert '"status":"ikm_option_unsupported"' in first_block
    assert "System.exit" not in first_block
    # The generic failure path is untouched.
    generic = catch_unsupported[catch_unsupported.index("} catch (Exception e) {"):]
    assert '"status":"error"' in generic and "System.exit(1)" in generic


def test_update_mapping_false_on_absent_option_is_a_noop():
    text = _text("update_mapping.groovy")

    assert 'if (normalized == "false")' in text
    assert "IKM OPTION ABSENT" in text
