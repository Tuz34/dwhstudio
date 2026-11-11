from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PYTHON_GATE_STEP_NAMES = (
    None,
    None,
    "Install UnixODBC headers for pyodbc",
    "Install Python dependencies",
    "Run Python quality gate",
    "Run pytest with no deselections",
    "Run application security gate",
)


def _workflow() -> dict:
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
    job = _python_gates()
    steps = job["steps"]

    assert WORKFLOW_PATH.is_file()
    assert [step.get("name") for step in steps] == list(PYTHON_GATE_STEP_NAMES)
    assert job["runs-on"] == "ubuntu-latest"
    setup = steps[1]
    assert str(setup["uses"]).startswith("actions/setup-python@")
    assert setup["with"]["python-version"] == "3.12"


def test_unixodbc_step_retries_then_checks_the_runtime_library():
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
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    job_text = text.split("  frontend-build:", 1)[0]

    assert "continue-on-error" not in text
    assert "continue-on-error" not in job_text
    assert "continue-on-error" not in set(_walk_keys(_python_gates()))


def test_pytest_step_runs_the_full_suite_without_deselections():
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
