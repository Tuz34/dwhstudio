"""Mapping replacement validates the new mapping before deleting the old one."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mapping_generator import (
    MappingCreationFailure,
    MappingGenerator,
    create_mapping_output_failed,
    mapping_replace_state_from_output,
)


GROOVY_PATH = Path(__file__).resolve().parents[1] / "odi_automation" / "create_mapping.groovy"


def _ready_params() -> dict:
    return {
        "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
        "src_model": "ORACLE_HR",
        "src_table": "HR.EMPLOYEES",
        "tgt_model": "ORACLE_ODS",
        "tgt_owner": "DWH",
        "tgt_table": "ODS_HR_EMPLOYEES",
        "needs_skey": False,
        "replace_existing_mapping": True,
        "_odi_repository": {"mapping_exists": True},
        "_odi_src_model_code": "ORACLE_HR",
        "_odi_tgt_model_code": "ORACLE_ODS",
        "_odi_src_datastore_name": "EMPLOYEES",
        "_odi_tgt_datastore_name": "ODS_HR_EMPLOYEES",
    }


def test_create_mapping_checks_validation_result_before_remove():
    """validate() is a real gate, and a thrown result still reaches the outer catch."""
    text = GROOVY_PATH.read_text(encoding="utf-8")
    validate_at = text.index("boolean hasValidationErrors = mappingHasErrors(")
    remove_at = text.index("em.remove(existingMapping)")
    between = text[validate_at:remove_at]

    assert "if (hasValidationErrors) {" in between
    assert 'throw new IllegalStateException("Mapping validation failed")' in between
    assert "try {" not in between
    assert "System.exit(1)" in text[text.index("} catch (Exception e) {"):]


def test_create_mapping_validates_before_delete_in_one_transaction():
    text = GROOVY_PATH.read_text(encoding="utf-8")
    validate_at = text.index("mappingHasErrors(mapping, validationIssues)")
    remove_at = text.index("em.remove(existingMapping)")
    commit_at = text.index("txnMgr.commit(txnStatus)")

    assert validate_at < remove_at < commit_at
    assert text.count("txnMgr.commit(") == 1
    assert "txnMgr.getTransaction" not in text[remove_at:commit_at]
    assert "Mapping already exists and replacement was not approved" in text
    assert "temporaryReplacementName" in text
    assert "DefaultTransactionDefinition" in text

    catch = text[text.index("} catch (Exception e) {"):]
    assert "txnMgr.rollback(txnStatus)" in catch
    assert '"status":"error"' in catch
    assert '"action":"create_mapping"' in catch
    assert '"error_type":"' in catch
    assert "kept_existing_on_failure" in catch
    assert "System.exit(1)" in catch


def _groovy_syntax_command() -> list[str] | None:
    """Build a groovy -e style syntax check that stops before class resolution."""
    parse_script = (
        "import org.codehaus.groovy.control.CompilationUnit\n"
        "import org.codehaus.groovy.control.CompilerConfiguration\n"
        "import org.codehaus.groovy.control.Phases\n"
        "def config = new CompilerConfiguration()\n"
        "def unit = new CompilationUnit(config)\n"
        "unit.addSource(new File(args[0]))\n"
        "unit.compile(Phases.CONVERSION)\n"
        "println 'GROOVY_SYNTAX_OK'\n"
    )
    groovy = shutil.which("groovy") or shutil.which("groovy.bat")
    if groovy:
        return [groovy, "-e", parse_script, str(GROOVY_PATH)]
    java_home = os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")
    java_name = "java.exe" if os.name == "nt" else "java"
    java = shutil.which(java_name) or str(Path(java_home) / "bin" / java_name)
    odi_home = os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home")
    jar_name = os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    jar = Path(odi_home) / "oracle_common" / "modules" / "thirdparty" / jar_name
    if Path(java).is_file() and jar.is_file():
        return [java, "-cp", str(jar), "groovy.ui.GroovyMain", "-e", parse_script, str(GROOVY_PATH)]
    return None


def test_create_mapping_groovy_syntax_check():
    """Parse the script with groovy -e semantics and do not open a repository.

    Compilation stops at Groovy's CONVERSION phase, which checks syntax without
    resolving the Oracle ODI SDK or contacting a repository.
    """
    command = _groovy_syntax_command()
    if command is None:
        pytest.skip("Groovy is not installed and the SDK groovy-all jar was not found.")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, combined
    assert "GROOVY_SYNTAX_OK" in completed.stdout


def test_structured_failure_reports_kept_existing_and_class_only():
    output = (
        '{"status":"error","action":"create_mapping","replaced":false,'
        '"kept_existing_on_failure":true,"error_type":"IllegalStateException"}\n'
        "password=secret-value jdbc:oracle:thin:@hidden"
    )

    state = mapping_replace_state_from_output(output)

    assert create_mapping_output_failed(output) is True
    assert state == {
        "replaced": False,
        "kept_existing_on_failure": True,
        "error_type": "IllegalStateException",
    }
    assert "secret-value" not in str(state)
    assert "jdbc:" not in str(state)


def test_process_error_without_structured_body_does_not_claim_replacement():
    output = "Hata: ODI Groovy action failed. error_type=GroovyProcessError"

    state = mapping_replace_state_from_output(output)

    assert state["replaced"] is False
    assert state["kept_existing_on_failure"] is False
    assert state["error_type"] == "GroovyProcessError"


def test_create_odi_mapping_parses_mocked_runner_output(monkeypatch):
    calls = []

    class FakeOrchestrator:
        def generate_mapping(self, **kwargs):
            calls.append(kwargs)
            return (
                "SUCCESS: Mapping 'MAP_HR_EMPLOYEES_TO_ODS' created successfully.\n"
                '{"status":"passed","action":"create_mapping","replaced":true,'
                '"kept_existing_on_failure":false}'
            )

    monkeypatch.setattr("odi_orchestrator.OdiOrchestrator", FakeOrchestrator)
    created = MappingGenerator()._create_odi_mapping(_ready_params())

    assert calls[0]["replace_existing_mapping"] is True
    assert created["replaced"] is True
    assert created["kept_existing_on_failure"] is False
    assert created["error_type"] is None


def test_create_odi_mapping_failure_keeps_existing_and_hides_runner_text(monkeypatch):
    class FakeOrchestrator:
        def generate_mapping(self, **kwargs):
            return (
                '{"status":"error","action":"create_mapping","replaced":false,'
                '"kept_existing_on_failure":true,"error_type":"IllegalStateException"}\n'
                "repository message with secret-token"
            )

    monkeypatch.setattr("odi_orchestrator.OdiOrchestrator", FakeOrchestrator)

    with pytest.raises(MappingCreationFailure) as captured:
        MappingGenerator()._create_odi_mapping(_ready_params())

    assert captured.value.replaced is False
    assert captured.value.kept_existing_on_failure is True
    assert captured.value.error_type == "IllegalStateException"
    assert "secret-token" not in str(captured.value)
