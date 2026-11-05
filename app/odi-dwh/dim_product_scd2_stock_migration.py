"""Offline, fixed-target migration of DWH.DIM_PRODUCT onto stock IKM literals.

The column type is already VARCHAR2(1) for CURRENT_FLAG and DATE for
EFFECTIVE_TO, so the cutover is one UPDATE. It does not alter types, touch
the sequence, or write the ODS source. The migration backup is the fixed
table DWH.ODI87_DIM_PRODUCT_MIG_BKP. It is created only when absent, checked
with set differences rather than a row count, and left in place afterwards.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import oracledb

from dim_product_scd2_candidate_preflight import DIM_PRODUCT_TRACKED_COLUMNS
from dim_product_scd2_stock_contract import (
    EFFECTIVE_TO_DATA_TYPE,
    EXPECTED_COLUMN_COUNT,
    EXPECTED_ROW_COUNT,
    FLAG_DATA_TYPE,
    PRE_MIGRATION_CONTRACT_ERA,
    STOCK_CONTRACT_ERA,
    oracle_column_type,
)
from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan

MIGRATION_APPROVAL_SCOPE = "odi87_dim_product_stock_migration"
RESTORE_APPROVAL_SCOPE = "odi87_dim_product_stock_migration_restore"
BACKUP_OWNER = "DWH"
BACKUP_TABLE = "ODI87_DIM_PRODUCT_MIG_BKP"
_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]*$")
_PLAN = build_dim_product_scd2_plan(
    {"name": name, "data_type": data_type, "nullable": nullable}
    for name, data_type, nullable in PRODUCTS_SOURCE_METADATA
)
EXPECTED_COLUMNS = tuple(_PLAN["target_columns"])
_PRESERVED_COLUMNS = (
    "PROD_ID",
    "EFFECTIVE_FROM",
    *DIM_PRODUCT_TRACKED_COLUMNS,
    "INSERT_DATE",
    "UPDATE_DATE",
    "SOURCE_SYSTEM",
    "ETL_BATCH_ID",
)
DATA_KEYS = (
    "target_column_count",
    "missing_column_count",
    "unexpected_column_count",
    "type_mismatch_count",
    "nullability_mismatch_count",
    "current_flag_type_safe",
    "effective_to_type_safe",
    "target_row_count",
    "legacy_current_flag_count",
    "legacy_historical_flag_count",
    "stock_current_flag_count",
    "stock_historical_flag_count",
    "null_flag_count",
    "other_flag_count",
    "legacy_open_ended_count",
    "stock_open_ended_count",
    "current_legacy_open_mismatch_count",
    "current_stock_open_mismatch_count",
    "historical_open_sentinel_count",
    "duplicate_product_sk_groups",
    "duplicate_legacy_current_groups",
    "duplicate_stock_current_groups",
    "invalid_date_order_count",
    "null_surrogate_key_count",
    "null_natural_key_count",
    "null_effective_from_count",
    "sequence_exists",
    "ods_row_count",
)
_BACKUP_COUNT_KEYS = (
    "backup_column_count",
    "backup_row_count",
    "target_minus_backup_count",
    "backup_minus_target_count",
    "backup_legacy_current_flag_count",
    "backup_legacy_historical_flag_count",
    "backup_legacy_open_ended_count",
    "backup_stock_flag_count",
    "preserved_payload_mismatch_count",
    "flag_mapping_mismatch_count",
    "date_mapping_mismatch_count",
    "sk_only_in_target_count",
    "sk_only_in_backup_count",
)
_BACKUP_STABLE_KEYS = (
    "backup_exists",
    "backup_column_count",
    "backup_row_count",
    "backup_metadata_mismatch_count",
    "backup_legacy_current_flag_count",
    "backup_legacy_historical_flag_count",
    "backup_legacy_open_ended_count",
    "backup_stock_flag_count",
)
EVIDENCE_KEYS = (
    *DATA_KEYS,
    "backup_exists",
    "backup_metadata_mismatch_count",
    *_BACKUP_COUNT_KEYS,
)
logger = logging.getLogger("dwh_app.dim_product_scd2_stock_migration")


class StockMigrationVerificationError(RuntimeError):
    """Raised when a fixed-object count or metadata invariant fails."""


def _marked(name: str, sql: str) -> str:
    return f"/* odi87:{name} */ {sql}"


def _target_count(name: str, predicate: str) -> str:
    return _marked(name, f'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" WHERE {predicate}')


def _backup_count(name: str, predicate: str) -> str:
    return _marked(
        name,
        f'SELECT COUNT(*) FROM "DWH"."{BACKUP_TABLE}" WHERE {predicate}',
    )


def _preserved_mismatch_sql() -> str:
    checks = " OR ".join(
        f'DECODE(t."{column}", b."{column}", 0, 1) = 1' for column in _PRESERVED_COLUMNS
    )
    return _marked(
        "preserved_payload_mismatch_count",
        'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" t '
        f'JOIN "DWH"."{BACKUP_TABLE}" b ON t."PRODUCT_SK" = b."PRODUCT_SK" '
        f"WHERE {checks}",
    )


def _metadata_sql(name: str, table: str) -> str:
    return _marked(
        name,
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE "
        "FROM ALL_TAB_COLUMNS "
        f"WHERE OWNER = 'DWH' AND TABLE_NAME = '{table}' ORDER BY COLUMN_ID",
    )


_TARGET_METADATA_SQL = _metadata_sql("target_metadata", "DIM_PRODUCT")
_BACKUP_METADATA_SQL = _metadata_sql("backup_metadata", BACKUP_TABLE)
_COUNT_SQL = {
    "target_row_count": _marked("target_row_count", 'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT"'),
    "legacy_current_flag_count": _target_count("legacy_current_flag_count", '"CURRENT_FLAG" = \'Y\''),
    "legacy_historical_flag_count": _target_count(
        "legacy_historical_flag_count", '"CURRENT_FLAG" = \'N\''
    ),
    "stock_current_flag_count": _target_count("stock_current_flag_count", '"CURRENT_FLAG" = \'1\''),
    "stock_historical_flag_count": _target_count(
        "stock_historical_flag_count", '"CURRENT_FLAG" = \'0\''
    ),
    "null_flag_count": _target_count("null_flag_count", '"CURRENT_FLAG" IS NULL'),
    "other_flag_count": _target_count(
        "other_flag_count",
        '"CURRENT_FLAG" IS NOT NULL AND "CURRENT_FLAG" NOT IN (\'Y\', \'N\', \'1\', \'0\')',
    ),
    "legacy_open_ended_count": _target_count(
        "legacy_open_ended_count", '"EFFECTIVE_TO" = DATE \'9999-12-31\''
    ),
    "stock_open_ended_count": _target_count(
        "stock_open_ended_count", '"EFFECTIVE_TO" = DATE \'2400-01-01\''
    ),
    "current_legacy_open_mismatch_count": _target_count(
        "current_legacy_open_mismatch_count",
        '"CURRENT_FLAG" = \'Y\' AND "EFFECTIVE_TO" <> DATE \'9999-12-31\'',
    ),
    "current_stock_open_mismatch_count": _target_count(
        "current_stock_open_mismatch_count",
        '"CURRENT_FLAG" = \'1\' AND "EFFECTIVE_TO" <> DATE \'2400-01-01\'',
    ),
    "historical_open_sentinel_count": _target_count(
        "historical_open_sentinel_count",
        '"CURRENT_FLAG" IN (\'N\', \'0\') AND ("EFFECTIVE_TO" = DATE \'9999-12-31\' '
        'OR "EFFECTIVE_TO" = DATE \'2400-01-01\')',
    ),
    "duplicate_product_sk_groups": _marked(
        "duplicate_product_sk_groups",
        'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."DIM_PRODUCT" '
        'GROUP BY "PRODUCT_SK" HAVING COUNT(*) > 1)',
    ),
    "duplicate_legacy_current_groups": _marked(
        "duplicate_legacy_current_groups",
        'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "DWH"."DIM_PRODUCT" '
        'WHERE "CURRENT_FLAG" = \'Y\' GROUP BY "PROD_ID" HAVING COUNT(*) > 1)',
    ),
    "duplicate_stock_current_groups": _marked(
        "duplicate_stock_current_groups",
        'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "DWH"."DIM_PRODUCT" '
        'WHERE "CURRENT_FLAG" = \'1\' GROUP BY "PROD_ID" HAVING COUNT(*) > 1)',
    ),
    "invalid_date_order_count": _target_count(
        "invalid_date_order_count", '"EFFECTIVE_FROM" > "EFFECTIVE_TO"'
    ),
    "null_surrogate_key_count": _target_count("null_surrogate_key_count", '"PRODUCT_SK" IS NULL'),
    "null_natural_key_count": _target_count("null_natural_key_count", '"PROD_ID" IS NULL'),
    "null_effective_from_count": _target_count(
        "null_effective_from_count", '"EFFECTIVE_FROM" IS NULL'
    ),
    "sequence_exists": _marked(
        "sequence_exists",
        "SELECT COUNT(*) FROM ALL_SEQUENCES "
        "WHERE SEQUENCE_OWNER = 'DWH' AND SEQUENCE_NAME = 'SEQ_DIM_PRODUCT'",
    ),
    "ods_row_count": _marked("ods_row_count", 'SELECT COUNT(*) FROM "DWH"."ODS_SH_PRODUCTS"'),
    "backup_exists": _marked(
        "backup_exists",
        "SELECT COUNT(*) FROM ALL_TABLES "
        f"WHERE OWNER = 'DWH' AND TABLE_NAME = '{BACKUP_TABLE}'",
    ),
    "backup_column_count": _marked(
        "backup_column_count",
        "SELECT COUNT(*) FROM ALL_TAB_COLUMNS "
        f"WHERE OWNER = 'DWH' AND TABLE_NAME = '{BACKUP_TABLE}'",
    ),
    "backup_row_count": _marked(
        "backup_row_count", f'SELECT COUNT(*) FROM "DWH"."{BACKUP_TABLE}"'
    ),
    "target_minus_backup_count": _marked(
        "target_minus_backup_count",
        'SELECT COUNT(*) FROM (SELECT * FROM "DWH"."DIM_PRODUCT" '
        f'MINUS SELECT * FROM "DWH"."{BACKUP_TABLE}")',
    ),
    "backup_minus_target_count": _marked(
        "backup_minus_target_count",
        f'SELECT COUNT(*) FROM (SELECT * FROM "DWH"."{BACKUP_TABLE}" '
        'MINUS SELECT * FROM "DWH"."DIM_PRODUCT")',
    ),
    "backup_legacy_current_flag_count": _backup_count(
        "backup_legacy_current_flag_count", '"CURRENT_FLAG" = \'Y\''
    ),
    "backup_legacy_historical_flag_count": _backup_count(
        "backup_legacy_historical_flag_count", '"CURRENT_FLAG" = \'N\''
    ),
    "backup_legacy_open_ended_count": _backup_count(
        "backup_legacy_open_ended_count", '"EFFECTIVE_TO" = DATE \'9999-12-31\''
    ),
    "backup_stock_flag_count": _backup_count(
        "backup_stock_flag_count", '"CURRENT_FLAG" IN (\'1\', \'0\')'
    ),
    "preserved_payload_mismatch_count": _preserved_mismatch_sql(),
    "flag_mapping_mismatch_count": _marked(
        "flag_mapping_mismatch_count",
        'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" t '
        f'JOIN "DWH"."{BACKUP_TABLE}" b ON t."PRODUCT_SK" = b."PRODUCT_SK" '
        'WHERE (b."CURRENT_FLAG" = \'Y\' AND t."CURRENT_FLAG" <> \'1\') '
        'OR (b."CURRENT_FLAG" = \'N\' AND t."CURRENT_FLAG" <> \'0\') '
        'OR b."CURRENT_FLAG" NOT IN (\'Y\', \'N\') '
        'OR t."CURRENT_FLAG" NOT IN (\'1\', \'0\')',
    ),
    "date_mapping_mismatch_count": _marked(
        "date_mapping_mismatch_count",
        'SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT" t '
        f'JOIN "DWH"."{BACKUP_TABLE}" b ON t."PRODUCT_SK" = b."PRODUCT_SK" '
        'WHERE (b."EFFECTIVE_TO" = DATE \'9999-12-31\' AND t."EFFECTIVE_TO" <> DATE \'2400-01-01\') '
        'OR (b."EFFECTIVE_TO" <> DATE \'9999-12-31\' '
        'AND DECODE(t."EFFECTIVE_TO", b."EFFECTIVE_TO", 0, 1) = 1)',
    ),
    "sk_only_in_target_count": _marked(
        "sk_only_in_target_count",
        'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."DIM_PRODUCT" '
        f'MINUS SELECT "PRODUCT_SK" FROM "DWH"."{BACKUP_TABLE}")',
    ),
    "sk_only_in_backup_count": _marked(
        "sk_only_in_backup_count",
        f'SELECT COUNT(*) FROM (SELECT "PRODUCT_SK" FROM "DWH"."{BACKUP_TABLE}" '
        'MINUS SELECT "PRODUCT_SK" FROM "DWH"."DIM_PRODUCT")',
    ),
}
CREATE_MIGRATION_BACKUP_SQL = _marked(
    "create_backup",
    f'CREATE TABLE "DWH"."{BACKUP_TABLE}" AS SELECT * FROM "DWH"."DIM_PRODUCT"',
)
MIGRATE_UPDATE_SQL = _marked(
    "migrate_update",
    'UPDATE "DWH"."DIM_PRODUCT" '
    'SET "CURRENT_FLAG" = CASE "CURRENT_FLAG" '
    "WHEN 'Y' THEN '1' WHEN 'N' THEN '0' ELSE \"CURRENT_FLAG\" END, "
    '"EFFECTIVE_TO" = CASE '
    "WHEN \"EFFECTIVE_TO\" = DATE '9999-12-31' THEN DATE '2400-01-01' "
    'ELSE "EFFECTIVE_TO" END '
    'WHERE "CURRENT_FLAG" IN (\'Y\', \'N\') '
    "OR \"EFFECTIVE_TO\" = DATE '9999-12-31'",
)
RESTORE_UPDATE_SQL = _marked(
    "restore_update",
    'UPDATE "DWH"."DIM_PRODUCT" t '
    'SET ("CURRENT_FLAG", "EFFECTIVE_TO") = ('
    'SELECT b."CURRENT_FLAG", b."EFFECTIVE_TO" '
    f'FROM "DWH"."{BACKUP_TABLE}" b WHERE b."PRODUCT_SK" = t."PRODUCT_SK") '
    "WHERE EXISTS (SELECT 1 "
    f'FROM "DWH"."{BACKUP_TABLE}" b WHERE b."PRODUCT_SK" = t."PRODUCT_SK")',
)
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
_SERIALIZABLE_SQL = "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"


def _column_row(column: Mapping[str, object]) -> tuple[object, ...]:
    name = str(column["name"])
    data_type = str(column["data_type"])
    nullable = "Y" if column["nullable"] else "N"
    if data_type == "DATE":
        return (name, "DATE", None, None, None, nullable)
    if data_type == "TIMESTAMP":
        return (name, "TIMESTAMP(6)", None, None, None, nullable)
    if data_type.startswith("VARCHAR2(") and data_type.endswith(")"):
        length = int(data_type.removeprefix("VARCHAR2(").removesuffix(")"))
        return (name, "VARCHAR2", length, None, None, nullable)
    if data_type == "NUMBER":
        return (name, "NUMBER", None, None, None, nullable)
    if data_type.startswith("NUMBER(") and data_type.endswith(")") and "," in data_type:
        precision, scale = data_type.removeprefix("NUMBER(").removesuffix(")").split(",")
        return (name, "NUMBER", None, int(precision), int(scale), nullable)
    if data_type.startswith("NUMBER(") and data_type.endswith(")"):
        precision = int(data_type.removeprefix("NUMBER(").removesuffix(")"))
        return (name, "NUMBER", None, precision, 0, nullable)
    raise StockMigrationVerificationError("Unreviewed fixed column type.")


def expected_column_metadata_rows() -> tuple[tuple[object, ...], ...]:
    """Return the reviewed ALL_TAB_COLUMNS shape for the 26 fixed columns."""
    if len(EXPECTED_COLUMNS) != EXPECTED_COLUMN_COUNT:
        raise StockMigrationVerificationError("Fixed column count drifted.")
    return tuple(_column_row(column) for column in EXPECTED_COLUMNS)


def validated_migration_evidence(evidence: object) -> dict[str, int] | None:
    """Accept only the fixed non-negative count keys."""
    if not isinstance(evidence, Mapping) or set(evidence) != set(EVIDENCE_KEYS):
        return None
    parsed: dict[str, int] = {}
    for key in EVIDENCE_KEYS:
        value = evidence[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        parsed[key] = value
    return parsed


def _count(cursor: Any, name: str) -> int:
    cursor.execute(_COUNT_SQL[name])
    value = cursor.fetchone()[0]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StockMigrationVerificationError("Aggregate was not a count.")
    return value


def _read_metadata(cursor: Any, sql: str) -> list[tuple[object, ...]]:
    cursor.execute(sql)
    return [tuple(row) for row in cursor.fetchall()]


def _metadata_difference(rows: list[tuple[object, ...]]) -> dict[str, int]:
    expected = {str(column["name"]): column for column in EXPECTED_COLUMNS}
    actual: dict[str, dict[str, object]] = {}
    unexpected = 0
    for row in rows:
        if len(row) != 6 or not isinstance(row[0], str) or not _IDENTIFIER.fullmatch(row[0]):
            unexpected += 1
            continue
        if row[0] in actual or row[5] not in {"Y", "N"}:
            unexpected += 1
            continue
        try:
            data_type = oracle_column_type(row[1], row[2], row[3], row[4])
        except (TypeError, ValueError):
            unexpected += 1
            continue
        actual[row[0]] = {"data_type": data_type, "nullable": row[5] == "Y"}
    missing = sum(name not in actual for name in expected)
    unexpected += sum(name not in expected for name in actual)
    type_mismatch = sum(
        name in actual and actual[name]["data_type"] != column["data_type"]
        for name, column in expected.items()
    )
    nullability_mismatch = sum(
        name in actual and actual[name]["nullable"] is not column["nullable"]
        for name, column in expected.items()
    )
    flag = actual.get("CURRENT_FLAG")
    effective_to = actual.get("EFFECTIVE_TO")
    return {
        "column_count": len(rows),
        "missing_column_count": missing,
        "unexpected_column_count": unexpected,
        "type_mismatch_count": type_mismatch,
        "nullability_mismatch_count": nullability_mismatch,
        "current_flag_type_safe": int(
            flag is not None and flag["data_type"] == FLAG_DATA_TYPE and flag["nullable"] is False
        ),
        "effective_to_type_safe": int(
            effective_to is not None
            and effective_to["data_type"] == EFFECTIVE_TO_DATA_TYPE
            and effective_to["nullable"] is False
        ),
    }


def collect_dim_product_stock_migration_evidence(cursor: Any) -> dict[str, int]:
    """Read fixed metadata and counts. No row values or key values are returned."""
    target = _metadata_difference(_read_metadata(cursor, _TARGET_METADATA_SQL))
    evidence = {
        "target_column_count": target["column_count"],
        "missing_column_count": target["missing_column_count"],
        "unexpected_column_count": target["unexpected_column_count"],
        "type_mismatch_count": target["type_mismatch_count"],
        "nullability_mismatch_count": target["nullability_mismatch_count"],
        "current_flag_type_safe": target["current_flag_type_safe"],
        "effective_to_type_safe": target["effective_to_type_safe"],
    }
    for key in DATA_KEYS:
        if key not in evidence:
            evidence[key] = _count(cursor, key)
    evidence["backup_exists"] = _count(cursor, "backup_exists")
    if evidence["backup_exists"] == 1:
        backup = _metadata_difference(_read_metadata(cursor, _BACKUP_METADATA_SQL))
        evidence["backup_metadata_mismatch_count"] = (
            backup["missing_column_count"]
            + backup["unexpected_column_count"]
            + backup["type_mismatch_count"]
            + backup["nullability_mismatch_count"]
        )
        for key in _BACKUP_COUNT_KEYS:
            evidence[key] = _count(cursor, key)
    else:
        evidence["backup_metadata_mismatch_count"] = 0
        for key in _BACKUP_COUNT_KEYS:
            evidence[key] = 0
    return evidence


def classify_dim_product_stock_migration(evidence: Mapping[str, int]) -> str:
    """Classify a count report without opening a connection."""
    parsed = validated_migration_evidence(evidence)
    if parsed is None:
        return "blocked_migration_evidence"
    if (
        parsed["target_column_count"] != EXPECTED_COLUMN_COUNT
        or parsed["missing_column_count"]
        or parsed["unexpected_column_count"]
        or parsed["nullability_mismatch_count"]
        or (parsed["type_mismatch_count"] and parsed["current_flag_type_safe"] and parsed["effective_to_type_safe"])
    ):
        return "blocked_unexpected_shape"
    if parsed["current_flag_type_safe"] != 1 or parsed["effective_to_type_safe"] != 1:
        return "blocked_unsafe_column_type"
    if parsed["type_mismatch_count"]:
        return "blocked_unexpected_shape"
    if parsed["target_row_count"] != EXPECTED_ROW_COUNT:
        return "blocked_unexpected_row_count"
    if parsed["sequence_exists"] != 1:
        return "blocked_sequence_state"
    if parsed["ods_row_count"] != EXPECTED_ROW_COUNT:
        return "blocked_ods_state"
    if (
        parsed["duplicate_product_sk_groups"]
        or parsed["null_surrogate_key_count"]
        or parsed["null_natural_key_count"]
        or parsed["null_effective_from_count"]
    ):
        return "blocked_duplicate_or_null_keys"
    if parsed["duplicate_legacy_current_groups"] or parsed["duplicate_stock_current_groups"]:
        return "blocked_duplicate_current"
    if parsed["invalid_date_order_count"]:
        return "blocked_invalid_dates"
    legacy_flags = parsed["legacy_current_flag_count"] + parsed["legacy_historical_flag_count"]
    stock_flags = parsed["stock_current_flag_count"] + parsed["stock_historical_flag_count"]
    residue = parsed["null_flag_count"] + parsed["other_flag_count"]
    if residue:
        return "blocked_invalid_flags"
    if legacy_flags + stock_flags != parsed["target_row_count"]:
        return "blocked_mixed_state"
    if legacy_flags and stock_flags:
        return "blocked_mixed_state"
    if legacy_flags == parsed["target_row_count"]:
        legacy_dates_ok = (
            parsed["current_legacy_open_mismatch_count"] == 0
            and parsed["historical_open_sentinel_count"] == 0
            and parsed["stock_open_ended_count"] == 0
            and parsed["legacy_open_ended_count"] == parsed["legacy_current_flag_count"]
        )
        return "legacy_ready" if legacy_dates_ok else "blocked_invalid_dates"
    stock_dates_ok = (
        parsed["current_stock_open_mismatch_count"] == 0
        and parsed["historical_open_sentinel_count"] == 0
        and parsed["legacy_open_ended_count"] == 0
        and parsed["stock_open_ended_count"] == parsed["stock_current_flag_count"]
    )
    return "already_stock" if stock_dates_ok else "blocked_invalid_dates"


def _same(left: Mapping[str, int], right: Mapping[str, int], keys: tuple[str, ...]) -> bool:
    return all(left[key] == right[key] for key in keys)


def _exact_before_image(evidence: Mapping[str, int]) -> bool:
    return (
        evidence["backup_exists"] == 1
        and evidence["backup_column_count"] == EXPECTED_COLUMN_COUNT
        and evidence["backup_row_count"] == EXPECTED_ROW_COUNT
        and evidence["backup_metadata_mismatch_count"] == 0
        and evidence["target_minus_backup_count"] == 0
        and evidence["backup_minus_target_count"] == 0
        and evidence["backup_stock_flag_count"] == 0
        and evidence["backup_legacy_current_flag_count"] == evidence["legacy_current_flag_count"]
        and evidence["backup_legacy_historical_flag_count"] == evidence["legacy_historical_flag_count"]
        and evidence["backup_legacy_open_ended_count"] == evidence["legacy_open_ended_count"]
        and evidence["preserved_payload_mismatch_count"] == 0
        and evidence["sk_only_in_target_count"] == 0
        and evidence["sk_only_in_backup_count"] == 0
    )


def _legacy_preimage(evidence: Mapping[str, int]) -> bool:
    return (
        evidence["backup_exists"] == 1
        and evidence["backup_column_count"] == EXPECTED_COLUMN_COUNT
        and evidence["backup_row_count"] == EXPECTED_ROW_COUNT
        and evidence["backup_metadata_mismatch_count"] == 0
        and evidence["backup_stock_flag_count"] == 0
        and evidence["backup_legacy_current_flag_count"] == evidence["stock_current_flag_count"]
        and evidence["backup_legacy_historical_flag_count"] == evidence["stock_historical_flag_count"]
        and evidence["backup_legacy_open_ended_count"] == evidence["stock_open_ended_count"]
        and evidence["preserved_payload_mismatch_count"] == 0
        and evidence["flag_mapping_mismatch_count"] == 0
        and evidence["date_mapping_mismatch_count"] == 0
        and evidence["sk_only_in_target_count"] == 0
        and evidence["sk_only_in_backup_count"] == 0
        and evidence["target_minus_backup_count"] > 0
        and evidence["backup_minus_target_count"] > 0
    )


def _report(
    status: str,
    *,
    mode: str,
    scope: str,
    approved: object,
    connection_attempted: bool = False,
    writes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "mode": mode,
        "status": status,
        "approved": approved is True,
        "requires_user_approval": True,
        "requires_independent_review": True,
        "approval_scope": scope,
        "contract_era": STOCK_CONTRACT_ERA,
        "pre_migration_contract_era": PRE_MIGRATION_CONTRACT_ERA,
        "target": {"owner": "DWH", "table": "DIM_PRODUCT"},
        "migration_backup": {"owner": BACKUP_OWNER, "table": BACKUP_TABLE},
        "connection_attempted": connection_attempted,
        "will_execute": False,
        "write_operations_attempted": list(writes or []),
        "committed": False,
        "rolled_back": False,
        "migration_backup_retained": False,
        "automatic_drop_performed": False,
        "ods_write_attempted": False,
        "sequence_write_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }
    payload.update(extra)
    return payload


def _approval_status(
    *,
    independent_review_status: object,
    approved: object,
    approval_scope: object,
    expected_scope: str,
) -> str | None:
    if independent_review_status != "approved":
        return "blocked_pending_independent_review"
    if approved is not True:
        return "blocked_until_exact_operator_approval"
    if approval_scope != expected_scope:
        return "blocked_invalid_approval_scope"
    return None


def _close(resource: Any, label: str) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except oracledb.Error as exc:
        logger.warning("%s close failed: %s", label, type(exc).__name__)


def _rollback(connection: Any) -> bool:
    try:
        connection.rollback()
        return True
    except oracledb.Error as exc:
        logger.error("DIM_PRODUCT migration rollback failed: %s", type(exc).__name__)
        return False


def _read(cursor: Any, connection: Any) -> dict[str, int]:
    cursor.execute(_READ_ONLY_SQL)
    evidence = collect_dim_product_stock_migration_evidence(cursor)
    if not _rollback(connection):
        raise StockMigrationVerificationError("Read-only rollback failed.")
    return evidence


def _require_rowcount(cursor: Any) -> None:
    if type(cursor.rowcount) is not int or cursor.rowcount != EXPECTED_ROW_COUNT:
        raise StockMigrationVerificationError("DML row count did not match the fixed population.")


def _assert_unchanged_neighbors(before: Mapping[str, int], after: Mapping[str, int]) -> None:
    if after["ods_row_count"] != before["ods_row_count"] or after["sequence_exists"] != 1:
        raise StockMigrationVerificationError("ODS or sequence changed.")
    if after["target_row_count"] != EXPECTED_ROW_COUNT:
        raise StockMigrationVerificationError("Target row count changed.")
    if any(after[key] != before[key] for key in _BACKUP_STABLE_KEYS):
        raise StockMigrationVerificationError("Migration backup changed during DML.")


def _assert_migrated(after: Mapping[str, int], before: Mapping[str, int]) -> None:
    if classify_dim_product_stock_migration(after) != "already_stock":
        raise StockMigrationVerificationError("Post-migration state is not the stock contract.")
    _assert_unchanged_neighbors(before, after)
    if not _legacy_preimage(after):
        raise StockMigrationVerificationError("Backup does not retain the before-state.")
    if (
        after["stock_current_flag_count"] != before["legacy_current_flag_count"]
        or after["stock_historical_flag_count"] != before["legacy_historical_flag_count"]
        or after["stock_open_ended_count"] != before["legacy_open_ended_count"]
    ):
        raise StockMigrationVerificationError("Flag or open-ended date counts were not preserved.")


def _assert_restored(after: Mapping[str, int], before: Mapping[str, int]) -> None:
    if classify_dim_product_stock_migration(after) != "legacy_ready":
        raise StockMigrationVerificationError("Restored state is not the pre-migration contract.")
    _assert_unchanged_neighbors(before, after)
    if not _exact_before_image(after):
        raise StockMigrationVerificationError("Restored target does not match the backup.")


def _migration_preview(reviewed: Mapping[str, int]) -> str | None:
    kind = classify_dim_product_stock_migration(reviewed)
    if kind not in {"legacy_ready", "already_stock"}:
        return kind
    if reviewed["backup_exists"] not in {0, 1}:
        return "blocked_backup_mismatch"
    if kind == "legacy_ready" and reviewed["backup_exists"] == 1 and not _exact_before_image(reviewed):
        return "blocked_backup_mismatch"
    if kind == "already_stock" and reviewed["backup_exists"] == 1 and not _legacy_preimage(reviewed):
        return "blocked_backup_mismatch"
    return None


def _restore_preview(reviewed: Mapping[str, int]) -> str | None:
    kind = classify_dim_product_stock_migration(reviewed)
    if kind not in {"legacy_ready", "already_stock"}:
        return kind
    if kind == "legacy_ready":
        return None if _exact_before_image(reviewed) else "blocked_backup_mismatch"
    return None if _legacy_preimage(reviewed) else "blocked_backup_mismatch"


def execute_dim_product_stock_migration(
    reviewed_evidence: Mapping[str, int],
    *,
    independent_review_status: object,
    approved: object,
    approval_scope: object,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Back up, when needed, and convert Y/N plus the 9999 sentinel in one transaction."""
    mode = "dim_product_stock_migration"
    approval = _approval_status(
        independent_review_status=independent_review_status,
        approved=approved,
        approval_scope=approval_scope,
        expected_scope=MIGRATION_APPROVAL_SCOPE,
    )
    if approval is not None:
        return _report(approval, mode=mode, scope=MIGRATION_APPROVAL_SCOPE, approved=approved)
    reviewed = validated_migration_evidence(reviewed_evidence)
    if reviewed is None:
        return _report(
            "blocked_migration_evidence", mode=mode, scope=MIGRATION_APPROVAL_SCOPE, approved=approved
        )
    preview = _migration_preview(reviewed)
    if preview is not None:
        return _report(preview, mode=mode, scope=MIGRATION_APPROVAL_SCOPE, approved=approved)
    if not callable(connection_factory):
        return _report(
            "blocked_connection_factory", mode=mode, scope=MIGRATION_APPROVAL_SCOPE, approved=approved
        )

    connection = None
    cursor = None
    writes: list[str] = []
    stage = "open_connection"
    backup_created = False
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "reread_before_write"
        live = _read(cursor, connection)
        if not _same(live, reviewed, EVIDENCE_KEYS):
            return _report(
                "blocked_stale_state",
                mode=mode,
                scope=MIGRATION_APPROVAL_SCOPE,
                approved=approved,
                connection_attempted=True,
                rolled_back=True,
            )
        if classify_dim_product_stock_migration(live) == "already_stock":
            return _report(
                "rerun_noop",
                mode=mode,
                scope=MIGRATION_APPROVAL_SCOPE,
                approved=approved,
                connection_attempted=True,
                rolled_back=True,
                migration_backup_retained=live["backup_exists"] == 1,
            )

        if live["backup_exists"] == 0:
            stage = "create_migration_backup"
            writes.append("create_migration_backup")
            backup_created = True
            cursor.execute(CREATE_MIGRATION_BACKUP_SQL)
            stage = "verify_created_backup"
            created = _read(cursor, connection)
            if not _same(created, reviewed, DATA_KEYS) or not _exact_before_image(created):
                status = "blocked_stale_state" if not _same(created, reviewed, DATA_KEYS) else "blocked_backup_mismatch"
                return _report(
                    status,
                    mode=mode,
                    scope=MIGRATION_APPROVAL_SCOPE,
                    approved=approved,
                    connection_attempted=True,
                    writes=writes,
                    rolled_back=True,
                    migration_backup_retained=True,
                    manual_recovery_review_required=True,
                )
            confirmed_base = created
        else:
            confirmed_base = live

        stage = "serializable_recheck"
        cursor.execute(_SERIALIZABLE_SQL)
        confirmed = collect_dim_product_stock_migration_evidence(cursor)
        if not _same(confirmed, confirmed_base, EVIDENCE_KEYS) or not _exact_before_image(confirmed):
            rolled = _rollback(connection)
            status = (
                "blocked_stale_state"
                if not _same(confirmed, confirmed_base, DATA_KEYS)
                else "blocked_backup_mismatch"
            )
            return _report(
                status,
                mode=mode,
                scope=MIGRATION_APPROVAL_SCOPE,
                approved=approved,
                connection_attempted=True,
                writes=writes,
                rolled_back=rolled,
                migration_backup_retained=True,
                manual_recovery_review_required=backup_created,
            )

        stage = "migrate_update"
        writes.append("migrate_current_flags_and_open_ended_dates")
        cursor.execute(MIGRATE_UPDATE_SQL)
        _require_rowcount(cursor)
        stage = "post_migration_dq"
        after = collect_dim_product_stock_migration_evidence(cursor)
        _assert_migrated(after, confirmed)
        stage = "commit"
        connection.commit()
        return _report(
            "passed",
            mode=mode,
            scope=MIGRATION_APPROVAL_SCOPE,
            approved=approved,
            connection_attempted=True,
            writes=writes,
            will_execute=True,
            committed=True,
            migration_backup_retained=True,
            migrated_row_count=EXPECTED_ROW_COUNT,
        )
    except (oracledb.Error, StockMigrationVerificationError, TypeError, ValueError) as exc:
        rolled = _rollback(connection) if connection is not None else False
        return _report(
            "failed",
            mode=mode,
            scope=MIGRATION_APPROVAL_SCOPE,
            approved=approved,
            connection_attempted=connection is not None,
            writes=writes,
            rolled_back=rolled,
            migration_backup_retained=backup_created or (
                reviewed is not None and reviewed["backup_exists"] == 1
            ),
            manual_recovery_review_required=backup_created,
            stage=stage,
            error_type=type(exc).__name__,
        )
    finally:
        _close(cursor, "cursor")
        _close(connection, "connection")


def execute_dim_product_stock_migration_restore(
    reviewed_evidence: Mapping[str, int],
    *,
    independent_review_status: object,
    approved: object,
    approval_scope: object,
    connection_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Copy only flag and effective-to values back from the retained migration backup."""
    mode = "dim_product_stock_migration_restore"
    approval = _approval_status(
        independent_review_status=independent_review_status,
        approved=approved,
        approval_scope=approval_scope,
        expected_scope=RESTORE_APPROVAL_SCOPE,
    )
    if approval is not None:
        return _report(approval, mode=mode, scope=RESTORE_APPROVAL_SCOPE, approved=approved)
    reviewed = validated_migration_evidence(reviewed_evidence)
    if reviewed is None:
        return _report(
            "blocked_migration_evidence", mode=mode, scope=RESTORE_APPROVAL_SCOPE, approved=approved
        )
    preview = _restore_preview(reviewed)
    if preview is not None:
        return _report(preview, mode=mode, scope=RESTORE_APPROVAL_SCOPE, approved=approved)
    if not callable(connection_factory):
        return _report(
            "blocked_connection_factory", mode=mode, scope=RESTORE_APPROVAL_SCOPE, approved=approved
        )

    connection = None
    cursor = None
    writes: list[str] = []
    stage = "open_connection"
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        stage = "reread_before_restore"
        live = _read(cursor, connection)
        if not _same(live, reviewed, EVIDENCE_KEYS):
            return _report(
                "blocked_stale_state",
                mode=mode,
                scope=RESTORE_APPROVAL_SCOPE,
                approved=approved,
                connection_attempted=True,
                rolled_back=True,
                migration_backup_retained=live["backup_exists"] == 1,
            )
        if classify_dim_product_stock_migration(live) == "legacy_ready":
            return _report(
                "rerun_noop",
                mode=mode,
                scope=RESTORE_APPROVAL_SCOPE,
                approved=approved,
                connection_attempted=True,
                rolled_back=True,
                migration_backup_retained=True,
            )

        stage = "serializable_recheck"
        cursor.execute(_SERIALIZABLE_SQL)
        confirmed = collect_dim_product_stock_migration_evidence(cursor)
        if not _same(confirmed, live, EVIDENCE_KEYS) or not _legacy_preimage(confirmed):
            rolled = _rollback(connection)
            return _report(
                "blocked_stale_state" if not _same(confirmed, live, DATA_KEYS) else "blocked_backup_mismatch",
                mode=mode,
                scope=RESTORE_APPROVAL_SCOPE,
                approved=approved,
                connection_attempted=True,
                rolled_back=rolled,
                migration_backup_retained=True,
            )
        stage = "restore_update"
        writes.append("restore_flags_and_effective_to")
        cursor.execute(RESTORE_UPDATE_SQL)
        _require_rowcount(cursor)
        stage = "post_restore_dq"
        after = collect_dim_product_stock_migration_evidence(cursor)
        _assert_restored(after, confirmed)
        stage = "commit"
        connection.commit()
        return _report(
            "passed",
            mode=mode,
            scope=RESTORE_APPROVAL_SCOPE,
            approved=approved,
            connection_attempted=True,
            writes=writes,
            will_execute=True,
            committed=True,
            rolled_back=False,
            migration_backup_retained=True,
            restored_row_count=EXPECTED_ROW_COUNT,
        )
    except (oracledb.Error, StockMigrationVerificationError, TypeError, ValueError) as exc:
        rolled = _rollback(connection) if connection is not None else False
        return _report(
            "failed",
            mode=mode,
            scope=RESTORE_APPROVAL_SCOPE,
            approved=approved,
            connection_attempted=connection is not None,
            writes=writes,
            rolled_back=rolled,
            migration_backup_retained=True,
            stage=stage,
            error_type=type(exc).__name__,
        )
    finally:
        _close(cursor, "cursor")
        _close(connection, "connection")
