from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


BASELINE_NAME = "python_quality_baseline.json"
PRODUCTION_DIRS = ("routes", "odi_automation")


@dataclass(frozen=True)
class QualityBaseline:
    broad_except_max: int
    swallowed_handlers_max: int
    return_annotation_ratio_min: float
    ruff_max_by_code: dict[str, int]
    function_line_max: dict[str, int]


@dataclass(frozen=True)
class QualityMetrics:
    production_files: int
    production_lines: int
    functions: int
    annotated_returns: int
    return_annotation_ratio: float
    broad_except: int
    bare_except: int
    swallowed_handlers: int


def production_python_files(root: Path) -> list[Path]:
    files = list(root.glob("*.py"))
    for relative_dir in PRODUCTION_DIRS:
        files.extend((root / relative_dir).glob("*.py"))
    return sorted(path for path in files if path.is_file())


def _is_broad_exception(handler: ast.ExceptHandler) -> bool:
    if isinstance(handler.type, ast.Name):
        return handler.type.id == "Exception"
    if isinstance(handler.type, ast.Tuple):
        return any(isinstance(item, ast.Name) and item.id == "Exception" for item in handler.type.elts)
    return False


def _is_swallowed_handler(handler: ast.ExceptHandler) -> bool:
    return bool(handler.body) and all(isinstance(item, (ast.Pass, ast.Continue)) for item in handler.body)


def collect_quality_metrics(root: Path) -> QualityMetrics:
    production_lines = 0
    functions = 0
    annotated_returns = 0
    broad_except = 0
    bare_except = 0
    swallowed_handlers = 0
    files = production_python_files(root)

    for path in files:
        source = path.read_text(encoding="utf-8-sig")
        production_lines += len(source.splitlines())
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions += 1
                annotated_returns += node.returns is not None
            elif isinstance(node, ast.ExceptHandler):
                bare_except += node.type is None
                broad_except += _is_broad_exception(node)
                swallowed_handlers += _is_swallowed_handler(node)

    ratio = annotated_returns / functions if functions else 1.0
    return QualityMetrics(
        production_files=len(files),
        production_lines=production_lines,
        functions=functions,
        annotated_returns=annotated_returns,
        return_annotation_ratio=round(ratio, 4),
        broad_except=broad_except,
        bare_except=bare_except,
        swallowed_handlers=swallowed_handlers,
    )


def load_quality_baseline(path: Path) -> QualityBaseline:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported Python quality baseline schema.")
    return QualityBaseline(
        broad_except_max=int(payload["broad_except_max"]),
        swallowed_handlers_max=int(payload["swallowed_handlers_max"]),
        return_annotation_ratio_min=float(payload["return_annotation_ratio_min"]),
        ruff_max_by_code={str(key): int(value) for key, value in payload["ruff_max_by_code"].items()},
        function_line_max={str(key): int(value) for key, value in payload["function_line_max"].items()},
    )


def function_line_count(path: Path, function_name: str) -> int:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one function named {function_name} in {path.name}.")
    node = matches[0]
    return int(node.end_lineno or node.lineno) - node.lineno + 1


def _ruff_counts(root: Path, files: list[Path]) -> dict[str, int]:
    relative_files = [str(path.relative_to(root)) for path in files]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(root / "pyproject.toml"),
            "--output-format",
            "json",
            *relative_files,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError("Ruff execution failed. Install requirements-dev.txt and retry.")
    findings = json.loads(completed.stdout or "[]")
    return dict(sorted(Counter(str(item.get("code") or "UNKNOWN") for item in findings).items()))


def _function_budget_failures(root: Path, baseline: QualityBaseline) -> list[str]:
    failures = []
    for key, maximum in baseline.function_line_max.items():
        relative_path, function_name = key.split(":", 1)
        actual = function_line_count(root / relative_path, function_name)
        if actual > maximum:
            failures.append(f"function_budget:{key}:{actual}>{maximum}")
    return failures


def run_quality_gate(root: Path, *, run_ruff: bool = True) -> dict[str, Any]:
    baseline = load_quality_baseline(root / BASELINE_NAME)
    metrics = collect_quality_metrics(root)
    failures = _function_budget_failures(root, baseline)

    if metrics.bare_except:
        failures.append(f"bare_except:{metrics.bare_except}>0")
    if metrics.broad_except > baseline.broad_except_max:
        failures.append(f"broad_except:{metrics.broad_except}>{baseline.broad_except_max}")
    if metrics.swallowed_handlers > baseline.swallowed_handlers_max:
        failures.append(
            f"swallowed_handlers:{metrics.swallowed_handlers}>{baseline.swallowed_handlers_max}"
        )
    if metrics.return_annotation_ratio < baseline.return_annotation_ratio_min:
        failures.append(
            "return_annotation_ratio:"
            f"{metrics.return_annotation_ratio}<{baseline.return_annotation_ratio_min}"
        )

    ruff_counts: dict[str, int] = {}
    if run_ruff:
        ruff_counts = _ruff_counts(root, production_python_files(root))
        for code, count in ruff_counts.items():
            maximum = baseline.ruff_max_by_code.get(code, 0)
            if count > maximum:
                failures.append(f"ruff:{code}:{count}>{maximum}")

    return {
        "passed": not failures,
        "metrics": asdict(metrics),
        "ruff_counts": ruff_counts,
        "failure_count": len(failures),
        "failures": failures,
        "sensitive_files_read": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Python quality budget.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--skip-ruff", action="store_true", help="Run AST budgets only.")
    args = parser.parse_args(argv)
    report = run_quality_gate(args.root.resolve(), run_ruff=not args.skip_ruff)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
