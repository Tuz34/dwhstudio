"""Count-only postflight evidence for generic SCD2 targets."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import oracledb

import scd2_builder

logger = logging.getLogger("dwh_app.scd2_postflight")

CURRENT_VALUES_SQL = "('1','Y')"
# Open-ended rows carry a far-future sentinel (2400-01-01 or 9999-12-31).
CLOSED_BEFORE_SQL = "DATE '2300-01-01'"


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
