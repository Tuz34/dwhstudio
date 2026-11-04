from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from flask import Flask

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.workspace import appsec_gate


IGNORED_FLASK_ENDPOINTS = {"static"}
IMPLICIT_METHODS = {"HEAD", "OPTIONS"}


def _default_app_py() -> Path:
    return Path(__file__).resolve().with_name("app.py")


def _public_methods(methods: set[str]) -> list[str]:
    return sorted(method for method in methods if method not in IMPLICIT_METHODS)


def _endpoint_function(endpoint: str) -> str:
    return endpoint.rsplit(".", 1)[-1]


def _route_key(row: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    return (row["path"], tuple(row["methods"]), row["function"])


def _runtime_route_rows(app: Flask) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in IGNORED_FLASK_ENDPOINTS:
            continue
        rows.append(
            {
                "path": rule.rule,
                "methods": _public_methods(set(rule.methods or ())),
                "function": _endpoint_function(rule.endpoint),
            }
        )
    return sorted(rows, key=lambda item: (item["path"], item["methods"], item["function"]))


def _assert_runtime_matches_static(static_rows: list[dict[str, Any]], runtime_rows: list[dict[str, Any]]) -> None:
    static_keys = {_route_key(row) for row in static_rows}
    runtime_keys = {_route_key(row) for row in runtime_rows}
    missing = sorted(static_keys - runtime_keys)
    extra = sorted(runtime_keys - static_keys)
    if missing or extra:
        raise AssertionError(
            "Runtime Flask routes differ from the static AppSec endpoint matrix: "
            f"missing={missing[:5]} extra={extra[:5]}"
        )


def route_contract_rows(app: Flask, app_py: Path | None = None) -> list[dict[str, Any]]:
    """Return the stable route contract rows used before blueprint refactors."""
    static_rows = appsec_gate.build_endpoint_matrix(app_py or _default_app_py())
    runtime_rows = _runtime_route_rows(app)
    _assert_runtime_matches_static(static_rows, runtime_rows)
    return static_rows


def build_route_contract(app: Flask, app_py: Path | None = None) -> dict[str, Any]:
    routes = route_contract_rows(app, app_py=app_py)
    return {
        "source": "flask_url_map_plus_appsec_endpoint_matrix",
        "route_count": len(routes),
        "routes": routes,
    }


def write_route_contract(path: Path, app: Flask, app_py: Path | None = None) -> None:
    contract = build_route_contract(app, app_py=app_py)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the current Flask route contract inventory.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--app-py", type=Path, default=_default_app_py())
    args = parser.parse_args(argv)

    from app import app

    write_route_contract(args.out, app, app_py=args.app_py)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
