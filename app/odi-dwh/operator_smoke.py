"""Non-destructive local operator smoke checks.

The script reports status codes and local listener counts only. It intentionally
does not print response bodies, environment values, connection strings, raw
rows, or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


OPERATOR_SMOKE_BASE_URL_ENV = "OPERATOR_SMOKE_BASE_URL"
DEFAULT_TIMEOUT_SECONDS = 10.0

HTTP_CHECKS: tuple[tuple[str, str], ...] = (
    ("backend_status", "/api/status"),
    ("deployment_security_profile", "/api/deployment/security-profile"),
    ("odi_mappings_metadata", "/api/odi/mappings"),
    ("mapping_run_history", "/api/mapping/runs?limit=1"),
    ("incremental_preflight_read_only", "/api/incremental/preflight?spec_name=SH_SALES_ODS"),
)


@dataclass(frozen=True)
class HttpCheckResult:
    status_code: int | None
    error: str | None


def _base_port(base_url: str) -> int:
    parsed = urlparse(base_url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def default_base_url() -> str:
    configured_url = os.getenv(OPERATOR_SMOKE_BASE_URL_ENV) or os.getenv("APP_URL")
    if configured_url:
        return configured_url
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = os.getenv("APP_PORT", "5000")
    return f"http://{host}:{port}"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/") or default_base_url()


def default_http_get(url: str, timeout: float) -> HttpCheckResult:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpCheckResult(status_code=response.status, error=None)
    except urllib.error.HTTPError as exc:
        return HttpCheckResult(status_code=exc.code, error=None)
    except Exception as exc:
        return HttpCheckResult(status_code=None, error=exc.__class__.__name__)


def read_netstat() -> str:
    try:
        completed = subprocess.run(
            ["netstat", "-aon"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return completed.stdout or ""


def parse_netstat_for_port(text: str, port: int) -> dict[str, object]:
    pids: list[str] = []
    port_marker = f":{port}"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[1]
        state = parts[-2]
        pid = parts[-1]
        if local_addr.endswith(port_marker) and state.upper() == "LISTENING" and pid not in pids:
            pids.append(pid)
    return {"listener_count": len(pids), "listener_pids": pids}


def build_smoke_report(
    *,
    base_url: str | None = None,
    netstat_text: str | None = None,
    http_get=default_http_get,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    normalized_base = _normalize_base_url(base_url or default_base_url())
    port = _base_port(normalized_base)
    listener = parse_netstat_for_port(netstat_text if netstat_text is not None else read_netstat(), port)
    checks: list[dict[str, object]] = []

    for name, path in HTTP_CHECKS:
        url = urljoin(f"{normalized_base}/", path.lstrip("/"))
        result = http_get(url, timeout)
        checks.append(
            {
                "name": name,
                "url": url,
                "status_code": result.status_code,
                "ok": result.status_code is not None and 200 <= result.status_code < 300,
                "error": result.error,
            }
        )

    report: dict[str, object] = {
        "tool": "operator_smoke",
        "base_url": normalized_base,
        "port": listener,
        "checks": checks,
        "value_policy": "status-only; response bodies, env values, sensitive values, and raw rows are omitted",
    }
    report["status"] = overall_status(report)
    return report


def overall_status(report: dict[str, object]) -> str:
    checks = report.get("checks", [])
    port = report.get("port", {})
    listener_count = port.get("listener_count") if isinstance(port, dict) else None

    if not isinstance(checks, list) or any(not item.get("ok") for item in checks if isinstance(item, dict)):
        return "fail"
    if listener_count != 1:
        return "warn"
    return "pass"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run non-destructive local DWH Studio smoke checks.")
    parser.add_argument("--base-url", default=default_base_url())
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    report = build_smoke_report(base_url=args.base_url, timeout=args.timeout)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"pass", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
