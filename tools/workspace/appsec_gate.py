from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


TOKEN_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PLACEHOLDER_VALUES = {
    "",
    "<redacted>",
    "changeme",
    "change_me",
    "example",
    "fake",
    "false",
    "local-shared-token",
    "none",
    "placeholder",
    "replace_me",
    "test",
    "true",
}

SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".tmp_appsec_tests",
    ".tmp_oracle_security_audit_tests",
    ".vite",
    "__pycache__",
    "archive",
    "build",
    "dist",
    "logs",
    "node_modules",
    "runtime",
    "tests",
}

SKIP_FILES = {
    ".env",
    ".env.dev",
    ".env.local",
    ".env.prod",
    ".env.test",
    "APPSEC_ENDPOINT_MATRIX.json",
    "APPSEC_ENDPOINT_MATRIX.md",
    "APPSEC_GATE_REPORT.json",
    "APPSEC_GATE_REPORT.md",
    "CLAUDE.md",
    "odi_connection.local.properties",
    "odi_connection.properties",
    "odi_connection.properties.generated",
    "package-lock.json",
    "rag_database.json",
}

TEXT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".cfg",
    ".css",
    ".groovy",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".properties",
    ".ps1",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b\s*[:=]\s*['\"]?([^'\"\s,;#]+)"
)
JDBC_RE = re.compile(r"(?i)\bjdbc:[^\s,)\"']*@[A-Z0-9_.:-]+[^\s,)\"']*")
URL_CREDENTIAL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@")
ORACLE_CREDENTIAL_RE = re.compile(
    r"(?i)\b[A-Z][A-Z0-9_]{1,}/[^/@\s'\"?:=]+@[A-Z0-9_.-]+(?::\d+)?\b"
)

APPROVAL_REQUIRED_PATHS = {
    "/api/airflow/run-dq",
    "/api/airflow/run-mapping",
    "/api/airflow/run-odi",
    "/api/execute",
    "/api/incremental/execute",
    "/api/mapping/delete",
    "/api/mapping/generate",
    "/api/mapping/run",
    "/api/mapping/snapshot",
    "/api/mapping/update",
    "/api/rollback/<batch_id>",
    "/api/lineage/refresh",
    "/api/schema-changes/detect",
    "/api/alerts/acknowledge",
    "/api/alerts/check",
    "/api/rag_knowledge",
    "/api/quality/autofix-all-columns",
    "/api/quality/autofix-column",
    "/api/quality/create-all-error-tables",
    "/api/quality/create-error-table",
    "/api/quality/sync-mapping",
    "/api/quality/validate-insert",
    "/api/schema/change/apply-db",
    "/api/schema/change/reconcile-scenario",
    "/api/schema/change/sync-odi",
    "/api/skey/create",
}


@dataclass(frozen=True)
class EndpointRow:
    path: str
    methods: list[str]
    function: str
    line: int
    risk: str
    approval_required: bool
    rate_limit: str
    csrf_exempt: bool


@dataclass(frozen=True)
class CheckResult:
    id: str
    passed: bool
    summary: str
    details: list[str]


def expand_workspace_tokens(raw_tokens: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}

    def resolve(name: str, stack: tuple[str, ...] = ()) -> str:
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise ValueError(f"Cycle in workspace path tokens: {' -> '.join((*stack, name))}")
        raw = raw_tokens[name]

        def replace(match: re.Match[str]) -> str:
            return resolve(match.group(1), (*stack, name))

        value = TOKEN_RE.sub(replace, raw)
        resolved[name] = value
        return value

    for token_name in raw_tokens:
        resolve(token_name)
    return resolved


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_active_app_root() -> Path:
    paths_file = workspace_root() / "WORKSPACE_PATHS.json"
    if not paths_file.exists():
        return workspace_root() / "app" / "odi-dwh"
    config = json.loads(paths_file.read_text(encoding="utf-8"))
    tokens = expand_workspace_tokens(config["tokens"])
    return Path(tokens["ACTIVE_APP_ROOT"])


def _decorator_source(decorator: ast.expr) -> str:
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        base = _decorator_source(decorator.value)
        return f"{base}.{decorator.attr}" if base else decorator.attr
    if isinstance(decorator, ast.Call):
        return _decorator_source(decorator.func)
    return ""


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_methods(call: ast.Call) -> list[str]:
    for keyword in call.keywords:
        if keyword.arg != "methods":
            continue
        if isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
            methods = [
                value.value.upper()
                for value in keyword.value.elts
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            ]
            return sorted(set(methods))
    return ["GET"]


def _risk_for(path: str, methods: list[str]) -> str:
    if not (set(methods) & WRITE_METHODS):
        return "read"
    if path in APPROVAL_REQUIRED_PATHS:
        return "persistent_write"
    return "write"


def _function_source(lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    start = max(node.lineno - 1, 0)
    end = getattr(node, "end_lineno", node.lineno)
    return "\n".join(lines[start:end])


def _called_function_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def _direct_approval_required(function_source: str) -> bool:
    return (
        "approved:true" in function_source
        or "_approval_error" in function_source
        or "approval_error" in function_source
        or 'body.get("approved") is True' in function_source
        or "body.get('approved') is True" in function_source
        or 'body.get("approved") is not True' in function_source
        or "body.get('approved') is not True" in function_source
    )


def _rate_limit(decorators: list[ast.expr]) -> str:
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        if _decorator_source(decorator.func) != "limiter.limit":
            continue
        if decorator.args:
            value = _literal_string(decorator.args[0])
            if value:
                return value
    return ""


def _is_route_call(decorator: ast.expr) -> ast.Call | None:
    if not isinstance(decorator, ast.Call):
        return None
    if _decorator_source(decorator.func).endswith(".route"):
        return decorator
    return None


def _python_source_files(app_py: Path) -> list[Path]:
    app_root = app_py.parent
    files: list[Path] = []
    for path in app_root.rglob("*.py"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(app_root)
        rel_parts = set(rel_path.parts)
        if rel_parts & SKIP_DIRS:
            continue
        if any(part.startswith(".venv_") for part in rel_path.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda path: (path != app_py, str(path.relative_to(app_root))))


def build_endpoint_matrix(app_py: Path) -> list[dict[str, Any]]:
    parsed_sources: list[tuple[Path, list[str], ast.Module]] = []
    for source_path in _python_source_files(app_py):
        source = source_path.read_text(encoding="utf-8-sig")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(source_path))
        parsed_sources.append((source_path, lines, tree))

    rows: list[EndpointRow] = []
    function_sources: dict[str, list[str]] = {}
    function_calls: dict[str, set[str]] = {}

    for _source_path, lines, tree in parsed_sources:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_sources.setdefault(node.name, []).append(_function_source(lines, node))
                function_calls.setdefault(node.name, set()).update(_called_function_names(node))

    approval_helpers = {
        name
        for name, sources in function_sources.items()
        if any(_direct_approval_required(function_source) for function_source in sources)
    }

    def function_has_approval_gate(function_name: str, seen: frozenset[str] = frozenset()) -> bool:
        if function_name in seen:
            return False
        sources = function_sources.get(function_name, [])
        if any(_direct_approval_required(source) for source in sources):
            return True
        if any(f"{helper}(" in source for source in sources for helper in approval_helpers):
            return True
        next_seen = seen | {function_name}
        return any(
            called_name in function_sources
            and function_has_approval_gate(called_name, next_seen)
            for called_name in function_calls.get(function_name, set())
        )

    def has_approval_gate(function_name: str, function_source: str) -> bool:
        if _direct_approval_required(function_source):
            return True
        if function_has_approval_gate(function_name):
            return True
        candidate_sources = [function_source, *function_sources.get(function_name, [])]
        return any(
            f"{helper}(" in source
            for source in candidate_sources
            for helper in approval_helpers
        )

    for _source_path, lines, tree in parsed_sources:
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route_calls = [call for decorator in node.decorator_list if (call := _is_route_call(decorator))]
            if not route_calls:
                continue
            function_source = _function_source(lines, node)
            approval_required = has_approval_gate(node.name, function_source)
            rate_limit = _rate_limit(node.decorator_list)
            csrf_exempt = any(_decorator_source(decorator) == "csrf.exempt" for decorator in node.decorator_list)
            for route_call in route_calls:
                if not route_call.args:
                    continue
                path = _literal_string(route_call.args[0])
                if not path:
                    continue
                methods = _route_methods(route_call)
                rows.append(
                    EndpointRow(
                        path=path,
                        methods=methods,
                        function=node.name,
                        line=node.lineno,
                        risk=_risk_for(path, methods),
                        approval_required=approval_required,
                        rate_limit=rate_limit,
                        csrf_exempt=csrf_exempt,
                    )
                )

    return [asdict(row) for row in sorted(rows, key=lambda item: (item.path, item.methods, item.function))]


def _text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root)
        rel_parts = set(rel_path.parts)
        if rel_parts & SKIP_DIRS:
            continue
        if len(rel_path.parts) >= 2 and rel_path.parts[0] == "static" and rel_path.parts[1] == "react":
            continue
        if any(part.startswith(".venv_") for part in rel_path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS or path.name.startswith(".env"):
            files.append(path)
    return sorted(files)


def _placeholder_value(value: str) -> bool:
    raw = value.strip().strip("'\"")
    normalized = raw.lower()
    if "{" in raw or "}" in raw:
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        return True
    if re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*", raw):
        return True
    if "(" in raw or ")" in raw:
        return True
    return normalized in PLACEHOLDER_VALUES or normalized.startswith("replace_")


def _redact_line(line: str) -> str:
    line = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", line)
    line = JDBC_RE.sub("jdbc:<redacted>", line)
    line = URL_CREDENTIAL_RE.sub("url:<redacted>@", line)
    line = ORACLE_CREDENTIAL_RE.sub("oracle_connect:<redacted>", line)
    return line.strip()[:220]


def _line_has_secret(line: str) -> bool:
    if "getenv(" in line or "os.environ" in line or "import.meta.env" in line:
        return False
    stripped = line.strip()
    if "<redacted>" in stripped:
        return False
    if "replaceAll(" in stripped or "re.compile(" in stripped or stripped.startswith(("r\"", "r'")):
        return False
    assignment = SECRET_ASSIGNMENT_RE.search(line)
    if assignment and not _placeholder_value(assignment.group(2)):
        return True
    return bool(JDBC_RE.search(line) or URL_CREDENTIAL_RE.search(line) or ORACLE_CREDENTIAL_RE.search(line))


def scan_secret_patterns(scan_root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in _text_files(scan_root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if not _line_has_secret(line):
                continue
            findings.append(
                {
                    "file": str(path.relative_to(scan_root)),
                    "line": line_number,
                    "evidence": _redact_line(line),
                }
            )
    return findings


def _check_no_csrf_exempt(app_py: Path, matrix: list[dict[str, Any]]) -> CheckResult:
    offenders = [f"{row['path']}:{row['function']}" for row in matrix if row["csrf_exempt"]]
    text_hit = "@csrf.exempt" in app_py.read_text(encoding="utf-8-sig")
    passed = not offenders and not text_hit
    return CheckResult(
        id="no_csrf_exempt",
        passed=passed,
        summary="No active Flask route bypasses CSRF.",
        details=offenders or (["@csrf.exempt text marker found"] if text_hit else []),
    )


def _check_host_debug_defaults(config_py: Path) -> CheckResult:
    content = config_py.read_text(encoding="utf-8")
    host_ok = 'APP_HOST          = os.getenv("APP_HOST", "127.0.0.1")' in content or (
        "APP_HOST" in content and "127.0.0.1" in content and "0.0.0.0" not in content
    )
    debug_ok = "APP_DEBUG" in content and "false" in content.lower()
    details: list[str] = []
    if not host_ok:
        details.append("APP_HOST default is not visibly loopback-only.")
    if not debug_ok:
        details.append("APP_DEBUG default is not visibly false.")
    return CheckResult(
        id="safe_host_debug_defaults",
        passed=host_ok and debug_ok,
        summary="Flask host/debug defaults are local-first and non-debug.",
        details=details,
    )


def _check_required_approval(matrix: list[dict[str, Any]]) -> CheckResult:
    missing = [
        f"{row['path']}:{','.join(row['methods'])}"
        for row in matrix
        if row["path"] in APPROVAL_REQUIRED_PATHS and not row["approval_required"]
    ]
    return CheckResult(
        id="critical_writes_require_approval",
        passed=not missing,
        summary="Critical write endpoints require backend approved:true checks.",
        details=missing,
    )


def _check_required_rate_limits(matrix: list[dict[str, Any]]) -> CheckResult:
    missing = [
        f"{row['path']}:{','.join(row['methods'])}"
        for row in matrix
        if row["path"] in APPROVAL_REQUIRED_PATHS and not row["rate_limit"]
    ]
    return CheckResult(
        id="critical_writes_are_rate_limited",
        passed=not missing,
        summary="Critical write endpoints have explicit Flask-Limiter limits.",
        details=missing,
    )


def _check_secret_scan(scan_root: Path) -> CheckResult:
    findings = scan_secret_patterns(scan_root)
    details = [f"{item['file']}:{item['line']} {item['evidence']}" for item in findings]
    return CheckResult(
        id="no_hardcoded_secret_patterns",
        passed=not findings,
        summary="No hardcoded secret or credential patterns found in scanned source files.",
        details=details,
    )


def run_gate(app_py: Path, config_py: Path, scan_root: Path) -> dict[str, Any]:
    matrix = build_endpoint_matrix(app_py)
    checks = [
        _check_no_csrf_exempt(app_py, matrix),
        _check_host_debug_defaults(config_py),
        _check_required_approval(matrix),
        _check_required_rate_limits(matrix),
        _check_secret_scan(scan_root),
    ]
    return {
        "schema_version": 1,
        "passed": all(check.passed for check in checks),
        "app_py": str(app_py),
        "config_py": str(config_py),
        "scan_root": str(scan_root),
        "endpoint_count": len(matrix),
        "checks": [asdict(check) for check in checks],
    }


def write_matrix_markdown(matrix: list[dict[str, Any]], output: Path) -> None:
    lines = [
        "# AppSec Endpoint Permission Matrix",
        "",
        "| Path | Methods | Risk | Approval | Rate limit | Function |",
        "|---|---:|---|---:|---|---|",
    ]
    for row in matrix:
        approval = "yes" if row["approval_required"] else "no"
        rate = row["rate_limit"] or "-"
        lines.append(
            f"| `{row['path']}` | `{','.join(row['methods'])}` | {row['risk']} | {approval} | {rate} | `{row['function']}` |"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report_markdown(report: dict[str, Any], output: Path) -> None:
    lines = [
        "# AppSec Gate Report",
        "",
        f"- Passed: `{str(report['passed']).lower()}`",
        f"- Endpoint count: `{report['endpoint_count']}`",
        "",
        "| Check | Passed | Details |",
        "|---|---:|---|",
    ]
    for check in report["checks"]:
        details = "; ".join(check["details"]) if check["details"] else "-"
        lines.append(f"| `{check['id']}` | `{str(check['passed']).lower()}` | {details} |")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    root = workspace_root()
    return argparse.ArgumentParser(description="Run local AppSec checks for the ODI-DWH workspace.").parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local AppSec checks for the ODI-DWH workspace.")
    parser.add_argument("--workspace-root", type=Path, default=workspace_root())
    parser.add_argument("--app-root", type=Path, default=None, help="Defaults to the active app root.")
    parser.add_argument("--matrix-json", type=Path, default=workspace_root() / "APPSEC_ENDPOINT_MATRIX.json")
    parser.add_argument("--matrix-md", type=Path, default=workspace_root() / "APPSEC_ENDPOINT_MATRIX.md")
    parser.add_argument("--report-json", type=Path, default=workspace_root() / "APPSEC_GATE_REPORT.json")
    parser.add_argument("--report-md", type=Path, default=workspace_root() / "APPSEC_GATE_REPORT.md")
    parser.add_argument("--no-write", action="store_true", help="Run checks without writing report files.")
    args = parser.parse_args(argv)

    app_root = (args.app_root if args.app_root is not None else default_active_app_root()).resolve()
    app_py = app_root / "app.py"
    config_py = app_root / "config.py"
    matrix = build_endpoint_matrix(app_py)
    report = run_gate(app_py=app_py, config_py=config_py, scan_root=app_root)

    if not args.no_write:
        args.matrix_json.write_text(json.dumps(matrix, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        args.report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        write_matrix_markdown(matrix, args.matrix_md)
        write_report_markdown(report, args.report_md)

    print(json.dumps({"passed": report["passed"], "endpoint_count": report["endpoint_count"]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
