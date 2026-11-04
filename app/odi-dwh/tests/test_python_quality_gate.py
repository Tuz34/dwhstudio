from __future__ import annotations

import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_development_quality_tooling_is_pinned_outside_runtime_requirements():
    runtime = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    development = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    config = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "ruff" not in runtime.lower()
    assert "ruff==0.15.21" in development
    assert "[tool.ruff]" in config
    assert "[tool.ruff.lint]" in config


def test_quality_metrics_have_zero_bare_except_and_respect_reviewed_budget():
    from python_quality_gate import collect_quality_metrics, load_quality_baseline

    baseline = load_quality_baseline(PROJECT_ROOT / "python_quality_baseline.json")
    metrics = collect_quality_metrics(PROJECT_ROOT)

    assert metrics.bare_except == 0
    assert metrics.broad_except <= baseline.broad_except_max
    assert metrics.swallowed_handlers <= baseline.swallowed_handlers_max
    assert metrics.production_files >= 70
    assert metrics.return_annotation_ratio >= baseline.return_annotation_ratio_min


def test_quality_gate_does_not_scan_runtime_or_sensitive_files(monkeypatch):
    import python_quality_gate

    visited = []
    original = Path.read_text

    def recording_read(path, *args, **kwargs):
        visited.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read)

    result = python_quality_gate.run_quality_gate(PROJECT_ROOT, run_ruff=False)

    assert result["passed"] is True
    assert visited
    assert all(path.suffix == ".py" or path.name == "python_quality_baseline.json" for path in visited)
    assert all(path.name not in {".env", ".env.dev", ".env.local"} for path in visited)


def test_quality_baseline_contains_only_counts_and_budgets():
    payload = json.loads((PROJECT_ROOT / "python_quality_baseline.json").read_text(encoding="utf-8"))
    serialized = json.dumps(payload).lower()

    assert payload["schema_version"] == 1
    assert set(payload) == {
        "schema_version",
        "broad_except_max",
        "swallowed_handlers_max",
        "return_annotation_ratio_min",
        "ruff_max_by_code",
        "function_line_max",
    }
    for blocked in ("password", "secret", "connection_string", "raw_rows", "pk_value"):
        assert blocked not in serialized


@pytest.mark.parametrize(
    ("relative_path", "function_name", "maximum"),
    [
        ("mapping_generator.py", "_create_target_table", 80),
        ("routes/misc_write.py", "api_odi_mapping_detail", 45),
        ("routes/mapping_write.py", "api_mapping_run", 80),
        ("reconciliator.py", "reconcile_table", 45),
        ("reconciliator.py", "_compare_data_by_pk", 45),
    ],
)
def test_selected_hotspots_stay_below_reviewed_function_budget(
    relative_path, function_name, maximum
):
    from python_quality_gate import function_line_count

    assert function_line_count(PROJECT_ROOT / relative_path, function_name) <= maximum
