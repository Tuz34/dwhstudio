"""Golden record regression helpers.

This module keeps the regression contract independent from Oracle/ODI runtime
so it can be used in unit tests, dry-runs, and later live source/target probes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class GoldenRecordSpec:
    """Expected regression contract for one source-to-target mapping."""

    name: str
    source_table: str
    target_table: str
    key_columns: tuple[str, ...]
    aggregate_columns: tuple[str, ...] = ()
    tolerances: dict[str, Decimal] = field(default_factory=dict)


def default_specs() -> dict[str, GoldenRecordSpec]:
    """Baseline backlog targets for Golden Record Regression."""

    return {
        "HR.EMPLOYEES": GoldenRecordSpec(
            name="HR_EMPLOYEES_ODS",
            source_table="HR.EMPLOYEES",
            target_table="DWH.ODS_HR_EMPLOYEES",
            key_columns=("EMPLOYEE_ID",),
            aggregate_columns=("SALARY",),
        ),
        "SH.SALES": GoldenRecordSpec(
            name="SH_SALES_ODS",
            source_table="SH.SALES",
            target_table="DWH.ODS_SH_SALES",
            key_columns=("PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"),
            aggregate_columns=("AMOUNT_SOLD", "QUANTITY_SOLD"),
            tolerances={"AMOUNT_SOLD": Decimal("0.01")},
        ),
    }


def build_snapshot(rows: Iterable[dict[str, Any]], spec: GoldenRecordSpec) -> dict[str, Any]:
    """Create a deterministic row-count, aggregate, and checksum snapshot."""

    normalized_rows = [_normalize_row(row) for row in rows]
    normalized_rows.sort(key=lambda row: _row_key(row, spec.key_columns))
    return {
        "spec": spec.name,
        "source_table": spec.source_table,
        "target_table": spec.target_table,
        "row_count": len(normalized_rows),
        "aggregates": {
            col: _decimal_to_json(sum(_decimal_value(row.get(col)) for row in normalized_rows))
            for col in spec.aggregate_columns
        },
        "checksum": _checksum(normalized_rows),
        "row_checksums": {
            _row_key(row, spec.key_columns): _checksum([row])
            for row in normalized_rows
        },
    }


def compare_snapshots(
    expected: dict[str, Any],
    actual: dict[str, Any],
    spec: GoldenRecordSpec,
) -> dict[str, Any]:
    """Compare expected and actual snapshots and return a test-style report."""

    checks = [
        {
            "check": "row_count",
            "expected": expected.get("row_count"),
            "actual": actual.get("row_count"),
            "passed": expected.get("row_count") == actual.get("row_count"),
        },
        {
            "check": "checksum",
            "expected": expected.get("checksum"),
            "actual": actual.get("checksum"),
            "passed": expected.get("checksum") == actual.get("checksum"),
        },
    ]

    for column in spec.aggregate_columns:
        expected_value = _decimal_value((expected.get("aggregates") or {}).get(column))
        actual_value = _decimal_value((actual.get("aggregates") or {}).get(column))
        tolerance = spec.tolerances.get(column, Decimal("0"))
        diff = abs(expected_value - actual_value)
        checks.append({
            "check": f"aggregate:{column}",
            "expected": _decimal_to_json(expected_value),
            "actual": _decimal_to_json(actual_value),
            "difference": _decimal_to_json(diff),
            "tolerance": _decimal_to_json(tolerance),
            "passed": diff <= tolerance,
        })

    row_diff = compare_row_checksums(
        expected.get("row_checksums") or {},
        actual.get("row_checksums") or {},
    )
    checks.append({
        "check": "row_checksums",
        "expected": len(expected.get("row_checksums") or {}),
        "actual": len(actual.get("row_checksums") or {}),
        "passed": not row_diff["missing"] and not row_diff["extra"] and not row_diff["changed"],
    })

    passed = all(check["passed"] for check in checks)
    return {
        "spec": spec.name,
        "source_table": spec.source_table,
        "target_table": spec.target_table,
        "passed": passed,
        "checks": checks,
        "row_diff": row_diff,
    }


def compare_row_checksums(
    expected: dict[str, str],
    actual: dict[str, str],
    limit: int = 20,
) -> dict[str, list[str]]:
    """Return missing, extra, and changed row keys from checksum maps."""

    expected_keys = set(expected)
    actual_keys = set(actual)
    common_keys = expected_keys & actual_keys
    changed = sorted(key for key in common_keys if expected[key] != actual[key])
    return {
        "missing": sorted(expected_keys - actual_keys)[:limit],
        "extra": sorted(actual_keys - expected_keys)[:limit],
        "changed": changed[:limit],
    }


def run_live_regression(
    spec: GoldenRecordSpec,
    *,
    source_conn: Any,
    target_conn: Any,
    row_limit: int | None = None,
) -> dict[str, Any]:
    """Run a read-only source/target comparison without returning raw rows."""

    selected_columns = _selected_columns(spec)
    try:
        source_rows = _fetch_projection(source_conn, spec.source_table, selected_columns, row_limit=row_limit)
    except Exception as exc:
        return _blocked_report(spec, "source_error", exc)

    try:
        target_rows = _fetch_projection(target_conn, spec.target_table, selected_columns, row_limit=row_limit)
    except Exception as exc:
        status = "missing_target" if _is_missing_table_error(exc) else "target_error"
        return _blocked_report(spec, status, exc)

    expected = build_snapshot(source_rows, spec)
    actual = build_snapshot(target_rows, spec)
    comparison = compare_snapshots(expected, actual, spec)
    return {
        "status": "passed" if comparison["passed"] else "failed",
        "spec": spec.name,
        "source_table": spec.source_table,
        "target_table": spec.target_table,
        "selected_columns": selected_columns,
        "row_limit": row_limit,
        "secret_printed": False,
        "raw_rows_included": False,
        "source_snapshot": _redacted_snapshot(expected),
        "target_snapshot": _redacted_snapshot(actual),
        "comparison": comparison,
    }


def run_live_regressions(
    specs: Iterable[GoldenRecordSpec],
    *,
    source_conn: Any,
    target_conn: Any,
    row_limit: int | None = None,
) -> dict[str, Any]:
    """Run multiple live regressions and return a compact status report."""

    results = [
        run_live_regression(spec, source_conn=source_conn, target_conn=target_conn, row_limit=row_limit)
        for spec in specs
    ]
    return {
        "status": "passed" if all(item["status"] == "passed" for item in results) else "attention_required",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "secret_printed": False,
        "raw_rows_included": False,
        "results": results,
    }


def build_post_odi_gate_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Build a compact Linear/CLAUDE-safe post-ODI regression gate summary."""

    results = [_summarize_post_odi_result(item) for item in report.get("results") or []]
    passed = results and all(item["status"] == "passed" for item in results)
    return {
        "tool": "odi.post_run_golden_record_gate",
        "status": "passed" if passed else "failed",
        "generated_at": report.get("generated_at"),
        "secret_printed": False,
        "raw_rows_included": False,
        "results": results,
    }


def run_configured_post_odi_gate(spec_names: list[str] | None = None, *, row_limit: int | None = None) -> dict[str, Any]:
    """Run configured live regressions and return the compact post-ODI gate summary."""

    return build_post_odi_gate_summary(run_configured_live_regressions(spec_names, row_limit=row_limit))


def run_configured_live_regressions(spec_names: list[str] | None = None, *, row_limit: int | None = None) -> dict[str, Any]:
    """Open configured Oracle connections lazily and run requested specs."""

    import oracledb
    import config

    specs_by_key = default_specs()
    selected = _resolve_specs(spec_names, specs_by_key)
    source_conn = oracledb.connect(
        user=config.SOURCE_ORACLE_USER,
        password=config.SOURCE_ORACLE_PASSWORD,
        dsn=config.SOURCE_ORACLE_DSN,
    )
    target_conn = oracledb.connect(
        user=config.TARGET_ORACLE_USER,
        password=config.TARGET_ORACLE_PASSWORD,
        dsn=config.TARGET_ORACLE_DSN,
    )
    try:
        return run_live_regressions(selected, source_conn=source_conn, target_conn=target_conn, row_limit=row_limit)
    finally:
        source_conn.close()
        target_conn.close()


def _selected_columns(spec: GoldenRecordSpec) -> list[str]:
    return list(dict.fromkeys([*spec.key_columns, *spec.aggregate_columns]))


def _fetch_projection(conn: Any, table_name: str, columns: list[str], *, row_limit: int | None = None) -> list[dict[str, Any]]:
    if not columns:
        raise ValueError("At least one selected column is required.")
    cursor = conn.cursor()
    try:
        column_sql = ", ".join(_quote_identifier(column) for column in columns)
        sql = f"SELECT {column_sql} FROM {_qualified_name(table_name)}"
        if row_limit:
            sql = f"{sql} FETCH FIRST {int(row_limit)} ROWS ONLY"
        cursor.execute(sql)
        names = [desc[0].upper() for desc in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _qualified_name(name: str) -> str:
    parts = [part.strip().upper() for part in name.split(".") if part.strip()]
    if len(parts) != 2:
        raise ValueError(f"Expected OWNER.TABLE name, got {name!r}")
    return ".".join(_quote_identifier(part) for part in parts)


def _quote_identifier(name: str) -> str:
    text = str(name).strip().upper()
    if not text.replace("_", "").isalnum() or not text:
        raise ValueError(f"Unsafe Oracle identifier: {name!r}")
    return text


def _redacted_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec": snapshot.get("spec"),
        "source_table": snapshot.get("source_table"),
        "target_table": snapshot.get("target_table"),
        "row_count": snapshot.get("row_count"),
        "aggregates": snapshot.get("aggregates", {}),
        "checksum": snapshot.get("checksum"),
        "row_checksum_count": len(snapshot.get("row_checksums") or {}),
    }


def _summarize_post_odi_result(result: dict[str, Any]) -> dict[str, Any]:
    source_snapshot = result.get("source_snapshot") or {}
    target_snapshot = result.get("target_snapshot") or {}
    comparison = result.get("comparison") or {}
    checks = comparison.get("checks") or []
    checks_by_name = {item.get("check"): item for item in checks}
    row_count_check = checks_by_name.get("row_count") or {}
    checksum_check = checks_by_name.get("checksum") or {}
    row_diff = comparison.get("row_diff") or {}

    return {
        "spec": result.get("spec"),
        "status": result.get("status"),
        "source_table": result.get("source_table"),
        "target_table": result.get("target_table"),
        "raw_rows_included": False,
        "row_counts": {
            "source": source_snapshot.get("row_count"),
            "target": target_snapshot.get("row_count"),
            "matched": bool(row_count_check.get("passed")),
        },
        "checksum_status": _check_status(checksum_check),
        "aggregate_checks": [
            {
                "column": str(item.get("check", "")).split(":", 1)[1],
                "expected": item.get("expected"),
                "actual": item.get("actual"),
                "difference": item.get("difference"),
                "tolerance": item.get("tolerance"),
                "status": "passed" if item.get("passed") else "failed",
            }
            for item in checks
            if str(item.get("check", "")).startswith("aggregate:")
        ],
        "row_diff_counts": {
            "missing": len(row_diff.get("missing") or []),
            "extra": len(row_diff.get("extra") or []),
            "changed": len(row_diff.get("changed") or []),
        },
        "row_diff_sample_included": False,
        "error_class": result.get("error_class"),
    }


def _check_status(check: dict[str, Any]) -> str:
    if not check:
        return "not_available"
    return "passed" if check.get("passed") else "failed"


def _blocked_report(spec: GoldenRecordSpec, status: str, exc: Exception) -> dict[str, Any]:
    return {
        "status": status,
        "spec": spec.name,
        "source_table": spec.source_table,
        "target_table": spec.target_table,
        "secret_printed": False,
        "raw_rows_included": False,
        "error_class": exc.__class__.__name__,
        "error": _safe_error_message(exc),
    }


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).splitlines()[0]
    return text[:300]


def _is_missing_table_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return "ORA-00942" in text or "TABLE OR VIEW DOES NOT EXIST" in text


def _resolve_specs(names: list[str] | None, specs_by_key: dict[str, GoldenRecordSpec]) -> list[GoldenRecordSpec]:
    if not names:
        return list(specs_by_key.values())
    selected = []
    for name in names:
        key = name.upper()
        if key not in specs_by_key:
            raise KeyError(f"Unknown golden record spec: {name}")
        selected.append(specs_by_key[key])
    return selected


def _row_key(row: dict[str, Any], key_columns: tuple[str, ...]) -> str:
    return "|".join(str(row.get(column.upper(), "")) for column in key_columns)


def _normalize_row(row: dict[str, Any]) -> dict[str, str | None]:
    normalized = {}
    for key, value in row.items():
        col = str(key).upper()
        if value is None:
            normalized[col] = None
        elif hasattr(value, "isoformat"):
            normalized[col] = value.isoformat()
        elif isinstance(value, Decimal):
            normalized[col] = _decimal_to_json(value)
        else:
            normalized[col] = str(value).strip()
    return normalized


def _checksum(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decimal_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _decimal_to_json(value: Decimal) -> str:
    return format(value.normalize(), "f")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run redacted Golden Record Regression checks.")
    parser.add_argument("--spec", action="append", help="Spec key such as HR.EMPLOYEES. Omit for all specs.")
    parser.add_argument("--row-limit", type=int, default=None, help="Optional row limit for smoke runs.")
    parser.add_argument("--out", default="", help="Optional JSON output path.")
    parser.add_argument("--post-odi-gate", action="store_true", help="Emit compact post-ODI gate summary.")
    args = parser.parse_args()

    report = (
        run_configured_post_odi_gate(args.spec, row_limit=args.row_limit)
        if args.post_odi_gate
        else run_configured_live_regressions(args.spec, row_limit=args.row_limit)
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
