"""Count-only postflight evidence for generic SCD2 targets."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

import oracledb

import scd2_builder
from dim_product_scd2_scenario_backup import call_sanitized

logger = logging.getLogger("dwh_app.scd2_postflight")

CURRENT_VALUES_SQL = "('1','Y')"
# Open-ended rows carry a far-future sentinel (2400-01-01 or 9999-12-31).
CLOSED_BEFORE_SQL = "DATE '2300-01-01'"

_STOCK_ZERO_FIELDS = (
    "duplicate_current_groups", "invalid_date_order_rows", "invalid_current_flag_rows",
    "invalid_open_end_rows", "invalid_closed_end_rows", "null_natural_key_rows",
)


def _quoted(owner: str, table: str) -> str:
    return f'"{owner}"."{table}"'


def build_postflight_statements(
    owner: str,
    table: str,
    natural_key: Sequence[str],
    *,
    source: tuple[str, str] | None = None,
) -> dict[str, str]:
    """Return count-only statements keyed by evidence name.

    Every identifier is revalidated here, so a caller cannot widen the query
    beyond counts and one date-only aggregate.
    """
    if owner not in scd2_builder.TARGET_OWNERS:
        raise scd2_builder.Scd2PlanError("target_owner_not_allowed")
    names = [owner, table, *natural_key]
    if not natural_key or not all(scd2_builder.IDENTIFIER_RE.fullmatch(n) for n in names):
        raise scd2_builder.Scd2PlanError("invalid_identifier")
    target = _quoted(owner, table)
    nk_list = ", ".join(f'"{name}"' for name in natural_key)
    statements = {
        "target_total": f"SELECT COUNT(*) FROM {target}",
        "target_current": (
            f'SELECT COUNT(*) FROM {target} WHERE "CURRENT_FLAG" IN {CURRENT_VALUES_SQL}'
        ),
        "max_effective_from": (
            f'SELECT TO_CHAR(MAX("EFFECTIVE_FROM"), \'YYYY-MM-DD\') FROM {target}'
        ),
        "rows_with_effective_to_set": (
            f'SELECT COUNT(*) FROM {target} WHERE "EFFECTIVE_TO" < {CLOSED_BEFORE_SQL}'
        ),
        "duplicate_current_groups": (
            f'SELECT COUNT(*) FROM (SELECT 1 FROM {target} '
            f'WHERE "CURRENT_FLAG" IN {CURRENT_VALUES_SQL} '
            f"GROUP BY {nk_list} HAVING COUNT(*) > 1)"
        ),
        "invalid_date_order_rows": (
            f'SELECT COUNT(*) FROM {target} WHERE "EFFECTIVE_FROM" > "EFFECTIVE_TO"'
        ),
    }
    if source is not None:
        src_owner, src_table = source
        if src_owner not in scd2_builder.SOURCE_MODEL_BY_OWNER or not scd2_builder.IDENTIFIER_RE.fullmatch(
            src_table
        ):
            raise scd2_builder.Scd2PlanError("invalid_identifier")
        statements["source_count"] = f"SELECT COUNT(*) FROM {_quoted(src_owner, src_table)}"
    return statements


def collect_postflight(
    owner: str,
    table: str,
    natural_key: Sequence[str],
    connection_factory: Callable[[], Any],
    *,
    source: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Run the count-only statements and return integers plus one date string."""
    result: dict[str, Any] = {
        "mode": "scd2_postflight",
        "status": "error",
        "counts": {},
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    try:
        statements = build_postflight_statements(owner, table, natural_key, source=source)
    except scd2_builder.Scd2PlanError as exc:
        result["status"] = "invalid_request"
        result["error_code"] = exc.code
        return result
    result["target"] = {"owner": owner, "table": table}  # echoed only after validation
    connection = cursor = None
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        counts: dict[str, Any] = {}
        for name, sql in statements.items():
            cursor.execute(sql)
            value = cursor.fetchone()[0]
            counts[name] = value if name == "max_effective_from" else int(value or 0)
        result["counts"] = counts
        result["status"] = "ok"
        return result
    except oracledb.Error as exc:
        result["error_type"] = type(exc).__name__
        return result
    finally:
        for resource in (cursor, connection):
            if resource is None:
                continue
            try:
                resource.close()
            except oracledb.Error as exc:
                logger.warning("SCD2 postflight close failed: %s", type(exc).__name__)


def build_stock_postflight_statements(
    owner: str, table: str, natural_key: Sequence[str], *, source: tuple[str, str] | None = None,
) -> dict[str, str]:
    """Strict stock-profile counts; legacy Y/N and 9999 are never valid here."""
    if (
        type(owner) is not str or type(table) is not str
        or type(natural_key) not in (list, tuple)
        or not 1 <= len(natural_key) <= 128
        or any(type(key) is not str for key in natural_key)
        or (source is not None and (
            type(source) is not tuple or len(source) != 2
            or any(type(part) is not str for part in source)
        ))
    ):
        raise scd2_builder.Scd2PlanError("invalid_identifier")
    natural_key = tuple(natural_key)
    statements = build_postflight_statements(owner, table, natural_key, source=source)
    target = _quoted(owner, table)
    keys = ", ".join(f'"{key}"' for key in natural_key)
    null_keys = " OR ".join(f'"{key}" IS NULL' for key in natural_key)
    statements.update({
        "target_current": f'SELECT COUNT(*) FROM {target} WHERE "CURRENT_FLAG"=\'1\'',
        "rows_with_effective_to_set": f'SELECT COUNT(*) FROM {target} WHERE "CURRENT_FLAG"=\'0\'',
        "duplicate_current_groups": (
            f'SELECT COUNT(*) FROM (SELECT 1 FROM {target} WHERE "CURRENT_FLAG"=\'1\' '
            f"GROUP BY {keys} HAVING COUNT(*)>1)"
        ),
        "invalid_date_order_rows": (
            f'SELECT COUNT(*) FROM {target} WHERE "EFFECTIVE_FROM" IS NULL '
            'OR "EFFECTIVE_TO" IS NULL OR "EFFECTIVE_FROM">"EFFECTIVE_TO"'
        ),
        "invalid_current_flag_rows": (
            f'SELECT COUNT(*) FROM {target} WHERE "CURRENT_FLAG" IS NULL '
            'OR "CURRENT_FLAG" NOT IN (\'1\',\'0\')'
        ),
        "invalid_open_end_rows": (
            f'SELECT COUNT(*) FROM {target} WHERE "CURRENT_FLAG"=\'1\' '
            'AND ("EFFECTIVE_TO" IS NULL OR "EFFECTIVE_TO"<>DATE \'2400-01-01\')'
        ),
        "invalid_closed_end_rows": (
            f'SELECT COUNT(*) FROM {target} WHERE "CURRENT_FLAG"=\'0\' '
            'AND ("EFFECTIVE_TO" IS NULL OR "EFFECTIVE_TO">=DATE \'2400-01-01\')'
        ),
        "null_natural_key_rows": f"SELECT COUNT(*) FROM {target} WHERE {null_keys}",
    })
    return statements


def _read_stock_counts(
    connection_factory: Callable[[], Any], statements: dict[str, str], resources: list[Any],
) -> dict[str, Any]:
    connection = connection_factory()
    resources.append(connection)
    if connection.autocommit is not False:
        raise ValueError("Autocommit must be disabled")
    cursor = connection.cursor()
    resources.append(cursor)
    cursor.execute("SET TRANSACTION READ ONLY")
    counts: dict[str, Any] = {}
    for name, sql in statements.items():
        cursor.execute(sql)
        row = cursor.fetchone()
        if not isinstance(row, (tuple, list)) or len(row) != 1:
            raise ValueError("Invalid aggregate shape")
        value = row[0]
        if name == "max_effective_from":
            if value is not None:
                if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError("Invalid date aggregate")
                date.fromisoformat(value)
        elif type(value) is not int or value < 0:
            raise ValueError("Invalid aggregate count")
        counts[name] = value
    return counts


def _finish_stock_resource(resource: Any, method: str) -> bool:
    getattr(resource, method)()
    return True


def _cleanup_stock_resources(resources: list[Any]) -> bool:
    # Even process-control exceptions must attempt every remaining cleanup step.
    connection = resources[0] if resources else None
    cursor = resources[1] if len(resources) > 1 else None
    try:
        rolled = connection is None or call_sanitized(_finish_stock_resource, connection, "rollback") is True
    finally:
        try:
            cursor_closed = cursor is None or call_sanitized(_finish_stock_resource, cursor, "close") is True
        finally:
            connection_closed = connection is None or call_sanitized(_finish_stock_resource, connection, "close") is True
    return rolled and cursor_closed and connection_closed


def collect_stock_postflight(
    owner: str, table: str, natural_key: Sequence[str], connection_factory: Callable[[], Any], *,
    source: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """A read-only, strict stock witness. Existing legacy collection is unchanged.

    The app's stock run gate uses this witness; collection success must never
    be confused with native scenario success or business-row parity.
    """
    result: dict[str, Any] = {
        "mode": "stock_scd2_postflight", "status": "error", "counts": {},
        "contract_profile": "stock_1_0_2400", "stock_contract_valid": False,
        "business_parity_proven": False, "physical_noop_proven": False,
        "history_overlap_checked": False, "source_current_count_equal": None,
        "connection_attempted": False, "read_only_transaction": False,
        "raw_rows_included": False, "pk_values_included": False, "secret_printed": False,
    }
    try:
        statements = build_stock_postflight_statements(owner, table, natural_key, source=source)
    except scd2_builder.Scd2PlanError as exc:
        return {**result, "status": "invalid_request", "error_code": exc.code}
    result["target"] = {"owner": owner, "table": table}
    resources: list[Any] = []
    result["connection_attempted"] = True
    try:
        counts = call_sanitized(_read_stock_counts, connection_factory, statements, resources)
    finally:
        clean = _cleanup_stock_resources(resources)
    if counts is None or not clean:
        return {**result, "error_type": "StockPostflightReadError"}
    valid = (
        all(counts[name] == 0 for name in _STOCK_ZERO_FIELDS)
        and counts["target_total"] == counts["target_current"] + counts["rows_with_effective_to_set"]
    )
    return {
        **result, "status": "ok" if valid else "invalid_stock_contract", "counts": counts,
        "stock_contract_valid": valid, "read_only_transaction": True,
        "source_current_count_equal": counts["source_count"] == counts["target_current"] if source is not None else None,
    }
