from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, TypedDict

import oracledb

from exceptions import ReconciliationError

logger = logging.getLogger(__name__)


class Finding(TypedDict, total=False):
    type: str
    severity: str
    column: str
    message: str
    affected_count: int
    action: str
    error_type: str


class DataCompareResult(TypedDict):
    pk_col: str | None
    pk_cols: list[str]
    samples_checked: int
    compared_columns: list[str]
    diff_persistence: str
    pk_values_included: bool
    raw_rows_included: bool
    findings: list[Finding]


@dataclass(frozen=True)
class ReconciliationDependencies:
    get_connection: Callable[[], Any]
    close_resource: Callable[[Any, str], None]
    parse_source_table: Callable[[str], tuple[str, str]]
    source_schema_columns: Callable[[str, str], dict[str, dict[str, Any]]]
    source_count: Callable[[str, str], int]
    source_sample_rows: Callable[[str, str, list[str], list[str]], Any]
    infer_pk_columns: Callable[[str, str, dict[str, Any], dict[str, Any]], list[str]]
    q_oracle: Callable[[str], str]
    pk_key: Callable[[dict[str, Any], list[str]], str]
    is_lob_type: Callable[[dict[str, Any] | None], bool]
    norm_value: Callable[[Any], Any]
    values_equal: Callable[[Any, Any, dict[str, Any] | None, dict[str, Any] | None], bool]
    map_sql_to_oracle_type: Callable[[str], str]
    compare_data: Callable[[str, str, str, str, dict[str, Any], dict[str, Any]], DataCompareResult]
    tech_columns: set[str]
    compare_limit: int


def _empty_compare(pk_cols: list[str] | None = None, findings: list[Finding] | None = None) -> DataCompareResult:
    keys = pk_cols or []
    return {
        "pk_col": ",".join(keys) if keys else None,
        "pk_cols": keys,
        "samples_checked": 0,
        "compared_columns": [],
        "diff_persistence": "disabled",
        "pk_values_included": False,
        "raw_rows_included": False,
        "findings": findings or [],
    }


def _select_compare_columns(
    source_columns: dict[str, Any],
    target_columns: dict[str, Any],
    pk_columns: list[str],
    deps: ReconciliationDependencies,
) -> list[str]:
    return [
        column
        for column in source_columns
        if column in target_columns
        and column not in pk_columns
        and column not in deps.tech_columns
        and not deps.is_lob_type(source_columns.get(column))
        and not deps.is_lob_type(target_columns.get(column))
    ][:30]


def _source_index(source_frame: Any, columns: list[str], pk_columns: list[str], deps: ReconciliationDependencies) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for _, row in source_frame.iterrows():
        row_dict = {column: row.get(column) for column in [*pk_columns, *columns]}
        indexed[deps.pk_key(row_dict, pk_columns)] = row_dict
    return indexed


def _target_where_clause(
    source_rows: dict[str, dict[str, Any]],
    pk_columns: list[str],
    quote: Callable[[str], str],
) -> tuple[str, dict[str, Any]]:
    if len(pk_columns) == 1:
        values = [row[pk_columns[0]] for row in source_rows.values()]
        binds = {f"p{index}": value for index, value in enumerate(values)}
        placeholders = ", ".join(f":p{index}" for index in range(len(values)))
        return f"WHERE {quote(pk_columns[0])} IN ({placeholders})", binds
    binds: dict[str, Any] = {}
    clauses = []
    for row_index, source_row in enumerate(source_rows.values()):
        parts = []
        for column in pk_columns:
            bind_name = f"p{row_index}_{column}"
            binds[bind_name] = source_row.get(column)
            parts.append(f"{quote(column)} = :{bind_name}")
        clauses.append("(" + " AND ".join(parts) + ")")
    return "WHERE " + " OR ".join(clauses), binds


def _read_target_rows(
    owner: str,
    table: str,
    source_rows: dict[str, dict[str, Any]],
    pk_columns: list[str],
    compare_columns: list[str],
    deps: ReconciliationDependencies,
) -> dict[str, dict[str, Any]]:
    connection = cursor = None
    try:
        connection = deps.get_connection()
        cursor = connection.cursor()
        where_clause, binds = _target_where_clause(source_rows, pk_columns, deps.q_oracle)
        selected = ", ".join(deps.q_oracle(column) for column in [*pk_columns, *compare_columns])
        cursor.execute(
            f"SELECT {selected} FROM {deps.q_oracle(owner)}.{deps.q_oracle(table)} {where_clause}",
            binds,
        )
        indexed = {}
        for row in cursor.fetchall():
            values = {column: row[index] for index, column in enumerate([*pk_columns, *compare_columns])}
            indexed[deps.pk_key(values, pk_columns)] = values
        return indexed
    finally:
        deps.close_resource(cursor, "reconciliation target cursor")
        deps.close_resource(connection, "reconciliation target connection")


def _data_findings(
    source_rows: dict[str, dict[str, Any]],
    target_rows: dict[str, dict[str, Any]],
    pk_columns: list[str],
    compare_columns: list[str],
    source_columns: dict[str, Any],
    target_columns: dict[str, Any],
    deps: ReconciliationDependencies,
) -> list[Finding]:
    pk_label = ",".join(pk_columns)
    findings: list[Finding] = []
    missing_count = sum(key not in target_rows for key in source_rows)
    if missing_count:
        findings.append({
            "type": "MISSING_TARGET_ROW", "severity": "ERROR", "column": pk_label,
            "message": f"The target is missing {missing_count} sampled source rows.",
            "affected_count": missing_count, "action": "SYNC_ODI_MAPPING",
        })
    null_counts: dict[str, int] = {}
    value_counts: dict[str, int] = {}
    for key, source_row in source_rows.items():
        target_row = target_rows.get(key)
        if not target_row:
            continue
        for column in compare_columns:
            source_value, target_value = source_row.get(column), target_row.get(column)
            if deps.norm_value(source_value) is not None and deps.norm_value(target_value) is None:
                null_counts[column] = null_counts.get(column, 0) + 1
            elif not deps.values_equal(source_value, target_value, source_columns.get(column), target_columns.get(column)):
                value_counts[column] = value_counts.get(column, 0) + 1
    findings.extend(
        {"type": "DATA_NULL_MISMATCH", "severity": "ERROR", "column": column,
         "message": f"The target has NULL for {count} sampled rows where source {column} is populated.",
         "affected_count": count, "action": "SYNC_ODI_MAPPING"}
        for column, count in null_counts.items()
    )
    findings.extend(
        {"type": "DATA_VALUE_MISMATCH", "severity": "ERROR", "column": column,
         "message": f"Source and target values differ for {count} sampled rows in {column}.",
         "affected_count": count, "action": "SYNC_ODI_MAPPING"}
        for column, count in value_counts.items()
    )
    return findings


def compare_data_by_pk(
    owner: str,
    table: str,
    source_schema: str,
    source_table: str,
    source_columns: dict[str, Any],
    target_columns: dict[str, Any],
    deps: ReconciliationDependencies,
) -> DataCompareResult:
    pk_columns: list[str] = []
    compare_columns: list[str] = []
    samples_checked = 0
    try:
        pk_columns = deps.infer_pk_columns(source_schema, source_table, source_columns, target_columns)
        if not pk_columns:
            return _empty_compare(findings=[{
                "type": "PK_NOT_FOUND", "severity": "WARNING", "column": "-",
                "message": "No primary or natural key was detected; row comparison was skipped.",
            }])
        compare_columns = _select_compare_columns(source_columns, target_columns, pk_columns, deps)
        if not compare_columns:
            return _empty_compare(pk_columns)
        source_frame = deps.source_sample_rows(source_schema, source_table, pk_columns, compare_columns)
        samples_checked = len(source_frame.index)
        if source_frame.empty:
            result = _empty_compare(pk_columns)
            result["compared_columns"] = compare_columns
            return result
        source_rows = _source_index(source_frame, compare_columns, pk_columns, deps)
        target_rows = _read_target_rows(owner, table, source_rows, pk_columns, compare_columns, deps)
        findings = _data_findings(
            source_rows, target_rows, pk_columns, compare_columns, source_columns, target_columns, deps
        )
        if not findings:
            findings.append({
                "type": "DATA_SAMPLE_MATCH", "severity": "INFO", "column": ",".join(pk_columns),
                "message": f"The sampled comparison matched across {samples_checked} rows and {len(compare_columns)} columns.",
            })
        return {
            "pk_col": ",".join(pk_columns), "pk_cols": pk_columns, "samples_checked": samples_checked,
            "compared_columns": compare_columns, "diff_persistence": "disabled",
            "pk_values_included": False, "raw_rows_included": False, "findings": findings,
        }
    except Exception as exc:
        logger.warning("PK comparison skipped: %s", type(exc).__name__)
        result = _empty_compare(pk_columns, [{
            "type": "DATA_COMPARE_SKIPPED", "severity": "WARNING",
            "column": ",".join(pk_columns) or "-",
            "message": "The sampled row comparison could not be completed.",
            "error_type": type(exc).__name__,
        }])
        result["samples_checked"] = samples_checked
        result["compared_columns"] = compare_columns
        return result


def _read_target_metadata(owner: str, table: str, deps: ReconciliationDependencies) -> tuple[dict[str, Any], int]:
    connection = cursor = None
    try:
        connection = deps.get_connection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE "
            "FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :tname",
            {"owner": owner, "tname": table},
        )
        columns = {
            row[0].upper(): {"type": row[1], "len": row[2], "prec": row[3], "scale": row[4]}
            for row in cursor.fetchall()
        }
        cursor.execute(f"SELECT COUNT(*) FROM {deps.q_oracle(owner)}.{deps.q_oracle(table)}")
        return columns, int(cursor.fetchone()[0])
    except Exception as exc:
        raise ReconciliationError("Target reconciliation metadata read failed") from exc
    finally:
        deps.close_resource(cursor, "reconciliation metadata cursor")
        deps.close_resource(connection, "reconciliation metadata connection")


def _read_mapping_expressions(table: str, close_resource: Callable[[Any, str], None]) -> dict[str, str]:
    from config import ODI_REPO_DSN, ODI_REPO_PASSWORD, ODI_REPO_SCHEMA, ODI_REPO_USER, ORACLE_DSN

    connection = cursor = None
    try:
        connection = oracledb.connect(
            user=ODI_REPO_USER, password=ODI_REPO_PASSWORD, dsn=ODI_REPO_DSN or ORACLE_DSN
        )
        cursor = connection.cursor()
        cursor.execute(
            f"SELECT a.NAME, e.TXT FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m "
            f"JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP c ON m.I_MAPPING = c.I_OWNER_MAPPING "
            f"JOIN {ODI_REPO_SCHEMA}.SNP_MAP_CP cp ON cp.I_OWNER_MAP_COMP = c.I_MAP_COMP AND cp.DIRECTION = 'O' "
            f"JOIN {ODI_REPO_SCHEMA}.SNP_MAP_ATTR a ON a.I_OWNER_MAP_CP = cp.I_MAP_CP "
            f"LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR "
            f"JOIN {ODI_REPO_SCHEMA}.SNP_MAP_REF r ON c.I_MAP_REF = r.I_MAP_REF "
            "WHERE UPPER(r.QUALIFIED_NAME) LIKE :tname",
            {"tname": f"%{table}%"},
        )
        return {
            str(target).upper(): str(expression).upper() if expression else ""
            for target, expression in cursor.fetchall()
            if target
        }
    except Exception as exc:
        logger.warning("Mapping metadata read skipped: %s", type(exc).__name__)
        return {}
    finally:
        close_resource(cursor, "reconciliation ODI cursor")
        close_resource(connection, "reconciliation ODI connection")


def _count_findings(source_count: int, target_count: int) -> list[Finding]:
    if source_count != target_count:
        return [{
            "type": "ROW_COUNT_MISMATCH", "severity": "ERROR", "column": "-",
            "message": f"Row counts differ by {source_count - target_count}.",
        }]
    return [{
        "type": "ROW_COUNT_MATCH", "severity": "INFO", "column": "-",
        "message": f"Source and target row counts match at {source_count}.",
    }]


def _column_findings(
    source_columns: dict[str, dict[str, Any]],
    target_columns: dict[str, dict[str, Any]],
    mapping_expressions: dict[str, str],
    deps: ReconciliationDependencies,
) -> list[Finding]:
    findings: list[Finding] = []
    for column, source_props in source_columns.items():
        if column not in target_columns:
            mapped_target = next(
                (target for target, expression in mapping_expressions.items() if re.search(r"\b" + re.escape(column) + r"\b", expression)),
                "",
            )
            status = f"Referenced by target column {mapped_target}." if mapped_target else "Excluded from the current mapping."
            findings.append({
                "type": "MAPPED_OR_EXCLUDED", "severity": "INFO", "column": column,
                "message": f"The target does not contain {column}. ODI metadata: {status}",
            })
            continue
        if not mapping_expressions.get(column, "").strip():
            findings.append({
                "type": "UNMAPPED_COLUMN", "severity": "ERROR", "column": column,
                "message": "The target column exists but has no ODI mapping expression.",
            })
        target_props = target_columns[column]
        mapped_type = deps.map_sql_to_oracle_type(source_props["type"])
        if mapped_type == "VARCHAR2" and target_props["type"] == "VARCHAR2":
            source_length, target_length = source_props["len"], target_props["len"]
            if source_length and source_length != -1 and target_length and target_length < source_length:
                findings.append({
                    "type": "DATA_TRUNCATION_RISK", "severity": "WARNING", "column": column,
                    "message": f"Target length {target_length} is smaller than source length {source_length}.",
                })
        if mapped_type == "NUMBER" and target_props["type"] == "NUMBER":
            source_precision, target_precision = source_props["prec"], target_props["prec"]
            if source_precision and target_precision and target_precision < source_precision:
                findings.append({
                    "type": "PRECISION_LOSS_RISK", "severity": "WARNING", "column": column,
                    "message": f"Target precision {target_precision} is smaller than source precision {source_precision}.",
                })
    findings.extend(
        {"type": "ADDITIONAL_COLUMN", "severity": "INFO", "column": column,
         "message": "The target contains an additional business column that is absent from the source."}
        for column in target_columns
        if column not in source_columns and column not in deps.tech_columns
    )
    return findings


def reconcile_table(owner: str, table: str, deps: ReconciliationDependencies) -> dict[str, Any]:
    source_schema, source_table = deps.parse_source_table(table)
    target_columns, target_count = _read_target_metadata(owner, table, deps)
    try:
        source_columns = deps.source_schema_columns(source_schema, source_table)
        if not source_columns:
            return {
                "findings": [{"type": "TABLE_NOT_FOUND", "severity": "ERROR", "message": "The configured source table was not found."}],
                "source": f"{source_schema}.{source_table}", "target": f"{owner}.{table}",
                "pk_values_included": False, "raw_rows_included": False, "write_operations_attempted": [],
            }
        source_count = deps.source_count(source_schema, source_table)
    except Exception as exc:
        raise ReconciliationError("Source reconciliation metadata read failed") from exc
    mapping_expressions = _read_mapping_expressions(table, deps.close_resource)
    findings = _count_findings(source_count, target_count)
    findings.extend(_column_findings(source_columns, target_columns, mapping_expressions, deps))
    data_compare = deps.compare_data(owner, table, source_schema, source_table, source_columns, target_columns)
    findings.extend(data_compare["findings"])
    return {
        "source": f"{source_schema}.{source_table}", "target": f"{owner}.{table}",
        "source_count": source_count, "target_count": target_count,
        "data_compare": {
            "pk_col": data_compare["pk_col"], "pk_cols": data_compare["pk_cols"],
            "samples_checked": data_compare["samples_checked"],
            "compared_columns": data_compare["compared_columns"],
            "diff_persistence": "disabled", "limit": deps.compare_limit,
        },
        "findings": findings, "pk_values_included": False, "raw_rows_included": False,
        "write_operations_attempted": [],
    }
