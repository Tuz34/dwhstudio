from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml


# parents[3] is the repository root in this clone. A delivered tree that
# contains only app/odi-dwh has no .github directory at that path. Reading
# ci.yml there must not fail the live suite.
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MISSING_WORKFLOW_REASON = (
    "ci.yml is not in this tree (delivered application layout has no "
    ".github/workflows/ci.yml); repository contract checks are skipped here."
)

PYTHON_GATE_STEP_NAMES = (
    None,
    None,
    "Install UnixODBC headers for pyodbc",
    "Install Python dependencies",
    "Run Python quality gate",
    "Run pytest with no deselections",
    "Run application security gate",
)

# Stand-in workflow for the hermetic "file present" check. It is not the
# repository file; it only has to satisfy the same assertions.
_PRESENT_WORKFLOW = textwrap.dedent(
    """\
    name: CI
    jobs:
      python-gates:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@fixture
          - uses: actions/setup-python@fixture
            with:
              python-version: '3.12'
          - name: Install UnixODBC headers for pyodbc
            shell: bash
            run: |
              attempt=1
              max_attempts=3
              installed=0
              while [[ "$attempt" -le "$max_attempts" ]]; do
                if sudo apt-get update && sudo apt-get install -y unixodbc-dev; then
                  installed=1
                  break
                fi
                if [[ "$attempt" -lt "$max_attempts" ]]; then
                  sleep_seconds=$((attempt * 10))
                  sleep "$sleep_seconds"
                fi
                attempt=$((attempt + 1))
              done
              if [[ "$installed" -eq 1 ]]; then
                exit 0
              fi
              set +o pipefail
              if ldconfig -p | grep -q 'libodbc\\.so\\.2'; then
                odbc_runtime_present=1
              else
                odbc_runtime_present=0
              fi
              set -o pipefail
              if [[ "$odbc_runtime_present" -eq 1 ]]; then
                echo "::warning::UnixODBC headers could not be installed (unixodbc-dev). The ODBC runtime library libodbc.so.2 is already present; pip must find a prebuilt pyodbc wheel."
                exit 0
              fi
              echo "The ODBC runtime library libodbc.so.2 is absent."
              exit 1
          - name: Install Python dependencies
            working-directory: app/odi-dwh
            run: python -m pip install -r requirements.txt -r requirements-dev.txt
          - name: Run Python quality gate
            working-directory: app/odi-dwh
            run: python python_quality_gate.py
          - name: Run pytest with no deselections
            working-directory: app/odi-dwh
            run: |
              echo "Environment-dependent pytest exclusions must stay empty (ODI-86)."
              echo "No pytest deselections"
              python -m pytest -q
          - name: Run application security gate
            run: python tools/workspace/appsec_gate.py --no-write
      frontend-build:
        runs-on: ubuntu-latest
    """
)


def _require_workflow_file() -> None:
    # A missing file in GitHub Actions must fail the suite. Skipping there
    # would hide a checkout that dropped .github/workflows/ci.yml.
    if WORKFLOW_PATH.is_file():
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail(
            "GITHUB_ACTIONS=true but the workflow file is missing at "
            f"{WORKFLOW_PATH}. Contract tests must fail in CI instead of skipping."
        )
    pytest.skip(MISSING_WORKFLOW_REASON)


def _workflow() -> dict:
    _require_workflow_file()
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _python_gates() -> dict:
    job = _workflow()["jobs"]["python-gates"]
    assert isinstance(job, dict)
    return job


def _walk_keys(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def test_workflow_yaml_parses_and_python_gates_keep_their_order():
    _require_workflow_file()
    job = _python_gates()
    steps = job["steps"]

    assert WORKFLOW_PATH.is_file()
    assert [step.get("name") for step in steps] == list(PYTHON_GATE_STEP_NAMES)
    assert job["runs-on"] == "ubuntu-latest"
    setup = steps[1]
    assert str(setup["uses"]).startswith("actions/setup-python@")
    assert setup["with"]["python-version"] == "3.12"


def test_unixodbc_step_retries_then_checks_the_runtime_library():
    _require_workflow_file()
    step = _python_gates()["steps"][2]
    script = step["run"]
    library_check = "ldconfig -p | grep -q 'libodbc\\.so\\.2'"
    warning_at = script.index("::warning::")
    library_at = script.index(library_check)

    assert step["name"] == "Install UnixODBC headers for pyodbc"
    assert step["shell"] == "bash"
    assert "continue-on-error" not in step
    assert "sudo apt-get update" in script
    assert "sudo apt-get install -y unixodbc-dev" in script
    assert "max_attempts=3" in script
    assert "sleep_seconds=$((attempt * 10))" in script
    assert 'sleep "$sleep_seconds"' in script
    assert library_check in script
    assert "UnixODBC headers could not be installed" in script
    assert "pip must find a prebuilt pyodbc wheel" in script
    assert script.index("max_attempts=3") < script.index("sudo apt-get update")
    assert script.index("sudo apt-get install -y unixodbc-dev") < library_at
    assert library_at < warning_at
    assert warning_at < script.index("exit 0", warning_at) < script.rindex("exit 1")
    assert script.index("exit 0") < library_at
    assert "|| true" not in script


def test_python_gates_have_no_continue_on_error():
    _require_workflow_file()
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    job_text = text.split("  frontend-build:", 1)[0]

    assert "continue-on-error" not in text
    assert "continue-on-error" not in job_text
    assert "continue-on-error" not in set(_walk_keys(_python_gates()))


def test_pytest_step_runs_the_full_suite_without_deselections():
    _require_workflow_file()
    step = _python_gates()["steps"][5]
    script = step["run"]

    assert step["name"] == "Run pytest with no deselections"
    assert step["working-directory"] == "app/odi-dwh"
    assert "Environment-dependent pytest exclusions must stay empty (ODI-86)." in script
    assert "No pytest deselections" in script
    assert script.strip().endswith("python -m pytest -q")
    assert "--deselect" not in script
    assert "--ignore" not in script
    assert " -k " not in script
    assert "continue-on-error" not in step


def test_later_python_gates_stay_mandatory():
    _require_workflow_file()
    steps = _python_gates()["steps"]
    install, quality, pytest_step, appsec = steps[3:]

    assert install["run"] == "python -m pip install -r requirements.txt -r requirements-dev.txt"
    assert install["working-directory"] == "app/odi-dwh"
    assert quality["run"] == "python python_quality_gate.py"
    assert quality["working-directory"] == "app/odi-dwh"
    assert pytest_step["name"] == "Run pytest with no deselections"
    assert appsec["run"] == "python tools/workspace/appsec_gate.py --no-write"
    assert "working-directory" not in appsec
    for step in steps:
        assert step.get("continue-on-error") is None


# Every repository contract test above belongs in this tuple. The behaviour
# tests call these functions with a monkeypatched path and environment.
_REPOSITORY_CONTRACT_TESTS = (
    test_workflow_yaml_parses_and_python_gates_keep_their_order,
    test_unixodbc_step_retries_then_checks_the_runtime_library,
    test_python_gates_have_no_continue_on_error,
    test_pytest_step_runs_the_full_suite_without_deselections,
    test_later_python_gates_stay_mandatory,
)


def _point_at_workflow(monkeypatch: pytest.MonkeyPatch, path: Path, *, github_actions: str | None) -> None:
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOW_PATH", path)
    if github_actions is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)


def _run_contract_assertions() -> None:
    assert _REPOSITORY_CONTRACT_TESTS
    for test in _REPOSITORY_CONTRACT_TESTS:
        try:
            test()
        except pytest.skip.Exception as exc:
            pytest.fail(f"{test.__name__} skipped while the workflow file exists: {exc.msg}")


def test_missing_workflow_outside_ci_skips_with_reason(monkeypatch, tmp_path):
    missing = tmp_path / "ci.yml"
    _point_at_workflow(monkeypatch, missing, github_actions=None)

    assert _REPOSITORY_CONTRACT_TESTS
    for test in _REPOSITORY_CONTRACT_TESTS:
        with pytest.raises(pytest.skip.Exception) as exc_info:
            test()
        assert exc_info.value.msg == MISSING_WORKFLOW_REASON


def test_missing_workflow_on_github_actions_fails(monkeypatch, tmp_path):
    missing = tmp_path / "ci.yml"
    _point_at_workflow(monkeypatch, missing, github_actions="true")

    assert _REPOSITORY_CONTRACT_TESTS
    for test in _REPOSITORY_CONTRACT_TESTS:
        with pytest.raises(pytest.fail.Exception) as exc_info:
            test()
        message = exc_info.value.msg
        assert message != MISSING_WORKFLOW_REASON
        assert "GITHUB_ACTIONS=true" in message
        assert str(missing) in message


def test_present_workflow_runs_contract_assertions(monkeypatch, tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(_PRESENT_WORKFLOW, encoding="utf-8", newline="\n")
    assert workflow.is_file()

    for github_actions in (None, "true"):
        _point_at_workflow(monkeypatch, workflow, github_actions=github_actions)
        _run_contract_assertions()
