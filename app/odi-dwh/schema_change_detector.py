"""
Schema Change Detector
━━━━━━━━━━━━━━━━━━━━━━━━━

Detects structural changes in Oracle tables (column additions/deletions/type changes)
and records them in the AUDIT_USER.SCHEMA_HASH table.

Usage:
  python schema_change_detector.py               # All DWH tables
  python schema_change_detector.py --owner ODS    # Specific schema
"""

import hashlib
import json
import logging
import re
from datetime import datetime

import oracledb
import pandas as pd
from config import (
    ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN,
    ODI_REPO_USER, ODI_REPO_PASSWORD, ODI_REPO_DSN, ODI_REPO_SCHEMA,
)
from db import get_source_table_schema, get_source_tables
from flow_inventory import expected_ods_table_name, get_ods_flow_inventory

logger = logging.getLogger("schema_change_detector")

_EXPRESSION_REF_RE = re.compile(r"\b([A-Z][A-Z0-9_$]*)\.([A-Z][A-Z0-9_$]*)\b")


# ── Connection ────────────────────────────────────────────────────────────────
def get_connection():
    """Connect as a regular user (not SYSDBA)."""
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


# ── Hash Calculation ──────────────────────────────────────────────────────────
def compute_column_hash(conn, owner: str, table_name: str) -> str:
    """
    Hash a table's column structure (name, type, length, precision, scale,
    nullable) with SHA-256.
    """
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH,
                   DATA_PRECISION, DATA_SCALE, NULLABLE
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :owner AND TABLE_NAME = :tname
            ORDER BY COLUMN_ID
        """, {"owner": owner.upper(), "tname": table_name.upper()})

        rows = cursor.fetchall()
        if not rows:
            return ""

        # Combine column information and hash it.
        structure = "|".join(
            f"{r[0]}:{r[1]}:{r[2]}:{r[3]}:{r[4]}:{r[5]}" for r in rows
        )
        return hashlib.sha256(structure.encode("utf-8")).hexdigest()
    finally:
        cursor.close()


# ── Get Previous Hash ─────────────────────────────────────────────────────────
def get_previous_hash(conn, owner: str, table_name: str) -> str | None:
    """Fetch the latest recorded hash from AUDIT_USER.SCHEMA_HASH."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COLUMN_HASH
            FROM AUDIT_USER.SCHEMA_HASH
            WHERE TABLE_OWNER = :owner AND TABLE_NAME = :tname
            ORDER BY CHECK_DATE DESC
            FETCH FIRST 1 ROWS ONLY
        """, {"owner": owner.upper(), "tname": table_name.upper()})
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception:
        # The table may not have been created yet.
        return None
    finally:
        cursor.close()


# ── Save Hash ─────────────────────────────────────────────────────────────────
def save_hash(conn, owner: str, table_name: str,
              current_hash: str, previous_hash: str | None,
              change_detected: bool):
    """Write a new hash record to AUDIT_USER.SCHEMA_HASH."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO AUDIT_USER.SCHEMA_HASH
                (TABLE_OWNER, TABLE_NAME, COLUMN_HASH, PREVIOUS_HASH, CHANGE_DETECTED)
            VALUES
                (:owner, :tname, :chash, :phash, :changed)
        """, {
            "owner":   owner.upper(),
            "tname":   table_name.upper(),
            "chash":   current_hash,
            "phash":   previous_hash,
            "changed": "Y" if change_detected else "N",
        })
        conn.commit()
    finally:
        cursor.close()


# ── Table List ────────────────────────────────────────────────────────────────
def get_tables(conn, owner: str) -> list[str]:
    """List all tables in the specified schema."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT TABLE_NAME
            FROM ALL_TABLES
            WHERE OWNER = :owner
            ORDER BY TABLE_NAME
        """, {"owner": owner.upper()})
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()


def _format_full_type(data_type, data_length, precision, scale) -> str:
    dtype = str(data_type or "").upper()
    if dtype in {"VARCHAR2", "VARCHAR", "CHAR", "NCHAR", "NVARCHAR2"} and data_length is not None:
        return f"{dtype}({int(data_length)})"
    if dtype == "NUMBER" and precision is not None:
        return f"NUMBER({precision},{scale})" if scale is not None else f"NUMBER({precision})"
    return dtype


def get_db_column_definitions(owner: str) -> list[dict]:
    """Read DB column definitions from ALL_TAB_COLUMNS for drift comparison."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                   DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :owner
            ORDER BY TABLE_NAME, COLUMN_ID
        """, {"owner": owner.upper()})
        rows = []
        for owner_name, table_name, column_name, dtype, length, precision, scale, nullable in cursor.fetchall():
            rows.append({
                "owner": owner_name,
                "table_name": table_name,
                "column_name": column_name,
                "data_type": dtype,
                "data_length": int(length) if length is not None else None,
                "data_precision": int(precision) if precision is not None else None,
                "data_scale": int(scale) if scale is not None else None,
                "nullable": nullable,
                "full_data_type": _format_full_type(dtype, length, precision, scale),
            })
        return rows
    finally:
        cursor.close()
        conn.close()


def _odi_schema_name() -> str:
    schema = (ODI_REPO_SCHEMA or "").strip().upper()
    if not schema or not schema.replace("_", "").isalnum():
        raise ValueError("Invalid ODI_REPO_SCHEMA")
    return schema


def get_odi_column_definitions(owner: str) -> list[dict]:
    """Read ODI datastore column definitions for the given physical owner hint.

    The ODI repository table names differ slightly between versions. This query
    uses the common SNP_TABLE/SNP_COL metadata shape used by the existing repo.
    """
    schema = _odi_schema_name()
    conn = oracledb.connect(
        user=ODI_REPO_USER,
        password=ODI_REPO_PASSWORD,
        dsn=ODI_REPO_DSN,
    )
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT
                :owner AS OWNER,
                t.TABLE_NAME,
                c.COL_NAME,
                c.SOURCE_DT,
                c.LONGC,
                c.SCALEC,
                NULL AS MAPPING_NAME
            FROM {schema}.SNP_TABLE t
            JOIN {schema}.SNP_COL c ON c.I_TABLE = t.I_TABLE
            ORDER BY t.TABLE_NAME, c.POS
        """, {"owner": owner.upper()})
        rows = []
        for owner_name, table_name, column_name, dtype, longc, scale, mapping_name in cursor.fetchall():
            dtype_upper = str(dtype or "").upper()
            length = longc if dtype_upper in {"VARCHAR2", "VARCHAR", "CHAR", "NCHAR", "NVARCHAR2"} else None
            precision = longc if dtype_upper == "NUMBER" else None
            rows.append({
                "owner": owner_name,
                "table_name": table_name,
                "column_name": column_name,
                "data_type": dtype,
                "data_length": int(length) if length is not None else None,
                "data_precision": int(precision) if precision is not None else None,
                "data_scale": int(scale) if scale is not None else None,
                "full_data_type": _format_full_type(dtype, length, precision, scale),
                "mapping_name": mapping_name,
            })
        return rows
    finally:
        cursor.close()
        conn.close()


def _column_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("owner") or row.get("OWNER") or "").upper(),
        str(row.get("table_name") or row.get("TABLE_NAME") or "").upper(),
        str(row.get("column_name") or row.get("COLUMN_NAME") or "").upper(),
    )


def _column_value(row: dict, *names):
    for name in names:
        if name in row:
            return row[name]
        upper = name.upper()
        if upper in row:
            return row[upper]
    return None


def detect_db_odi_type_drift(owner: str = "DWH") -> list[dict]:
    """Return DB vs ODI datastore type/length mismatches without writing anything."""
    db_rows = {_column_key(row): row for row in get_db_column_definitions(owner)}
    odi_rows = {_column_key(row): row for row in get_odi_column_definitions(owner)}
    drift = []

    for key, db_row in sorted(db_rows.items()):
        odi_row = odi_rows.get(key)
        if not odi_row:
            continue

        db_type = str(_column_value(db_row, "full_data_type") or "").upper()
        odi_type = str(_column_value(odi_row, "full_data_type") or "").upper()
        db_len = _column_value(db_row, "data_length")
        odi_len = _column_value(odi_row, "data_length")
        db_prec = _column_value(db_row, "data_precision")
        odi_prec = _column_value(odi_row, "data_precision")
        db_scale = _column_value(db_row, "data_scale")
        odi_scale = _column_value(odi_row, "data_scale")

        in_sync = (
            db_type == odi_type
            and db_len == odi_len
            and db_prec == odi_prec
            and db_scale == odi_scale
        )
        if not in_sync:
            drift.append({
                "owner": key[0],
                "table": key[1],
                "column": key[2],
                "db_type": db_type,
                "odi_type": odi_type,
                "db_length": db_len,
                "odi_length": odi_len,
                "db_precision": db_prec,
                "odi_precision": odi_prec,
                "db_scale": db_scale,
                "odi_scale": odi_scale,
                "in_sync": False,
                "mapping_name": _column_value(odi_row, "mapping_name"),
            })

    return drift


def _finding_summary(owner: str, table: str, column: str, message: str) -> str:
    return f"{owner}.{table}.{column} {message}"


def build_schema_sync_findings(owner: str = "DWH") -> dict:
    """Build a read-only Schema Sync findings report from DB and ODI metadata.

    The report intentionally contains only structural metadata: owner, table,
    column names, data types, lengths, precision/scale, and mapping names.
    It never reads or returns row data.
    """
    db_rows = {_column_key(row): row for row in get_db_column_definitions(owner)}
    odi_rows = {_column_key(row): row for row in get_odi_column_definitions(owner)}
    findings: list[dict] = []

    for key in sorted(db_rows):
        db_row = db_rows[key]
        odi_row = odi_rows.get(key)
        db_type = str(_column_value(db_row, "full_data_type") or "").upper()

        if not odi_row:
            findings.append({
                "kind": "target_column_missing_in_odi",
                "severity": "warning",
                "owner": key[0],
                "table": key[1],
                "column": key[2],
                "db_type": db_type,
                "odi_type": None,
                "mapping_name": None,
                "summary": _finding_summary(key[0], key[1], key[2], f"exists in DB as {db_type or '-'} but is missing in ODI metadata"),
                "next_step": "Sync ODI datastore metadata before treating the mapping as aligned.",
            })
            continue

        odi_type = str(_column_value(odi_row, "full_data_type") or "").upper()
        db_len = _column_value(db_row, "data_length")
        odi_len = _column_value(odi_row, "data_length")
        db_prec = _column_value(db_row, "data_precision")
        odi_prec = _column_value(odi_row, "data_precision")
        db_scale = _column_value(db_row, "data_scale")
        odi_scale = _column_value(odi_row, "data_scale")

        if (
            db_type != odi_type
            or db_len != odi_len
            or db_prec != odi_prec
            or db_scale != odi_scale
        ):
            findings.append({
                "kind": "column_type_size_mismatch",
                "severity": "warning",
                "owner": key[0],
                "table": key[1],
                "column": key[2],
                "db_type": db_type,
                "odi_type": odi_type,
                "db_length": db_len,
                "odi_length": odi_len,
                "db_precision": db_prec,
                "odi_precision": odi_prec,
                "db_scale": db_scale,
                "odi_scale": odi_scale,
                "mapping_name": _column_value(odi_row, "mapping_name"),
                "summary": _finding_summary(key[0], key[1], key[2], f"DB {db_type or '-'} != ODI {odi_type or '-'}"),
                "next_step": "Use Schema Sync preflight, then sync ODI metadata if the DB definition is correct.",
            })

    for key in sorted(odi_rows):
        if key in db_rows:
            continue
        odi_row = odi_rows[key]
        odi_type = str(_column_value(odi_row, "full_data_type") or "").upper()
        findings.append({
            "kind": "odi_column_missing_in_db",
            "severity": "warning",
            "owner": key[0],
            "table": key[1],
            "column": key[2],
            "db_type": None,
            "odi_type": odi_type,
            "mapping_name": _column_value(odi_row, "mapping_name"),
            "summary": _finding_summary(key[0], key[1], key[2], f"exists in ODI as {odi_type or '-'} but is missing in DB"),
            "next_step": "Review whether this is a stale ODI datastore column or a missing DB change.",
        })

    priority = {
        "column_type_size_mismatch": 0,
        "target_column_missing_in_odi": 1,
        "odi_column_missing_in_db": 2,
    }
    findings.sort(key=lambda item: (
        priority.get(str(item.get("kind")), 99),
        str(item.get("owner") or ""),
        str(item.get("table") or ""),
        str(item.get("column") or ""),
    ))

    return {
        "owner": owner.upper(),
        "finding_count": len(findings),
        "findings": findings,
        "read_only": True,
        "scope": "db_vs_odi_structural_metadata",
    }


# ── Main Detector ─────────────────────────────────────────────────────────────
def _clean_schema(value: str, label: str) -> str:
    cleaned = str(value or "").strip().upper()
    if not cleaned.replace("_", "").isalnum():
        raise ValueError(f"Invalid {label}")
    return cleaned


def _normalize_nullable(value) -> str:
    text = str(value or "").strip().upper()
    if text in {"Y", "YES", "TRUE", "1"}:
        return "Y"
    if text in {"N", "NO", "FALSE", "0"}:
        return "N"
    return text


def _source_column_rows(source_schema: str) -> list[dict]:
    source_schema = _clean_schema(source_schema, "source_schema")
    rows: list[dict] = []
    for full_table in sorted({str(table).upper() for table in get_source_tables()}):
        if "." not in full_table:
            continue
        owner, table_name = full_table.split(".", 1)
        if owner != source_schema:
            continue
        schema_df = get_source_table_schema(full_table)
        for _, row in schema_df.iterrows():
            column_name = str(row.get("COLUMN_NAME") or "").upper()
            if not column_name:
                continue
            rows.append({
                "source_schema": owner,
                "source_table_name": table_name,
                "source_table": f"{owner}.{table_name}",
                "target_table_name": expected_ods_table_name(owner, table_name),
                "column_name": column_name,
                "data_type": row.get("DATA_TYPE"),
                "data_length": int(row.get("DATA_LENGTH")) if row.get("DATA_LENGTH") is not None and not pd.isna(row.get("DATA_LENGTH")) else None,
                "data_precision": int(row.get("DATA_PRECISION")) if row.get("DATA_PRECISION") is not None and not pd.isna(row.get("DATA_PRECISION")) else None,
                "data_scale": int(row.get("DATA_SCALE")) if row.get("DATA_SCALE") is not None and not pd.isna(row.get("DATA_SCALE")) else None,
                "nullable": _normalize_nullable(row.get("NULLABLE")),
                "full_data_type": str(row.get("FULL_DATA_TYPE") or "").upper(),
            })
    return rows


def _source_target_key(row: dict, target_owner: str | None = None) -> tuple[str, str, str]:
    owner = str(target_owner or row.get("owner") or row.get("OWNER") or "").upper()
    table = str(row.get("target_table_name") or row.get("table_name") or row.get("TABLE_NAME") or "").upper()
    column = str(row.get("column_name") or row.get("COLUMN_NAME") or "").upper()
    return owner, table, column


def _source_target_summary(source_table: str, column: str, message: str) -> str:
    return f"{source_table}.{column} {message}"


def _normalize_mapping_table_ref(value, *, source_schema: str, target_owner: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        owner, table = text.rsplit(".", 1)
        owner = owner.split(".")[-1]
        if owner in {source_schema, f"SRC_{source_schema}"}:
            return f"{source_schema}.{table}"
        if owner in {target_owner, f"TGT_{target_owner}", "ODS"}:
            return f"{target_owner}.{table}"
        return f"{owner}.{table}"
    if text.startswith("ODS_"):
        return f"{target_owner}.{text}"
    return text


def _short_table_name(value) -> str:
    text = str(value or "").strip().upper()
    return text.rsplit(".", 1)[-1] if "." in text else text


def build_odi_mapping_column_links(rows: list[dict], source_schema: str = "HR", target_owner: str = "DWH") -> list[dict]:
    """Build sanitized source-to-target column links from ODI mapping metadata.

    The returned links intentionally omit expression text. Expressions are used
    only to infer structural column references.
    """
    source_schema = _clean_schema(source_schema, "source_schema")
    target_owner = _clean_schema(target_owner, "target_owner")
    source_components: dict[str, str] = {}
    links: list[dict] = []
    seen: set[tuple] = set()

    for row in rows or []:
        comp_name = str(_column_value(row, "comp_name") or "").strip().upper()
        qname = _normalize_mapping_table_ref(
            _column_value(row, "table_qualified_name"),
            source_schema=source_schema,
            target_owner=target_owner,
        )
        if not qname.startswith(f"{source_schema}."):
            continue
        short = _short_table_name(qname)
        for key in {comp_name, short, qname}:
            if key:
                source_components[key] = qname

    for row in rows or []:
        mapping_name = str(_column_value(row, "mapping_name") or "").strip().upper()
        target_column = str(_column_value(row, "attr_name") or "").strip().upper()
        expression = str(_column_value(row, "expression") or "").strip()
        if not mapping_name or not target_column or not expression:
            continue

        target_ref = _normalize_mapping_table_ref(
            _column_value(row, "table_qualified_name") or _column_value(row, "comp_name"),
            source_schema=source_schema,
            target_owner=target_owner,
        )
        if not target_ref.startswith(f"{target_owner}."):
            continue

        expr_upper = expression.upper()
        matched_source = False
        for comp_ref, source_column in _EXPRESSION_REF_RE.findall(expr_upper):
            source_table = source_components.get(comp_ref.upper())
            if not source_table:
                continue
            matched_source = True
            direct_exprs = {f"{comp_ref.upper()}.{source_column.upper()}", f"{_short_table_name(source_table)}.{source_column.upper()}"}
            link_type = "direct" if expr_upper in direct_exprs else "expression"
            item = {
                "mapping_name": mapping_name,
                "source_table": source_table,
                "source_column": source_column.upper(),
                "target_table": target_ref,
                "target_column": target_column,
                "link_type": link_type,
            }
            key = tuple(item.values())
            if key not in seen:
                seen.add(key)
                links.append(item)

        if not matched_source:
            item = {
                "mapping_name": mapping_name,
                "source_table": None,
                "source_column": None,
                "target_table": target_ref,
                "target_column": target_column,
                "link_type": "derived_expression",
            }
            key = tuple(item.values())
            if key not in seen:
                seen.add(key)
                links.append(item)

    return sorted(links, key=lambda item: (
        item["mapping_name"],
        item["target_table"],
        item["target_column"],
        item["source_table"] or "",
        item["source_column"] or "",
    ))


def get_odi_mapping_column_links(mapping_names: list[str], source_schema: str = "HR", target_owner: str = "DWH") -> list[dict]:
    """Read ODI mapping column metadata and return sanitized link summaries."""
    clean_names = sorted({
        str(name or "").strip().upper()
        for name in (mapping_names or [])
        if str(name or "").strip()
    })
    if not clean_names:
        return []

    from odi_mcp import odi_get_mapping_details

    rows: list[dict] = []
    for mapping_name in clean_names:
        payload = json.loads(odi_get_mapping_details(mapping_name))
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
    return build_odi_mapping_column_links(rows, source_schema=source_schema, target_owner=target_owner)


def build_source_target_metadata_findings(source_schema: str = "HR", target_owner: str = "DWH") -> dict:
    """Compare source metadata to expected target metadata without reading row data."""
    source_schema = _clean_schema(source_schema, "source_schema")
    target_owner = _clean_schema(target_owner, "target_owner")
    source_rows = {_source_target_key(row, target_owner): row for row in _source_column_rows(source_schema)}
    target_rows = {
        _source_target_key(row): row
        for row in get_db_column_definitions(target_owner)
        if str(row.get("table_name") or "").upper().startswith(f"ODS_{source_schema}_")
    }
    table_mapping_names: dict[tuple[str, str], str] = {}
    mapping_links: list[dict] = []
    mapping_link_aware = False
    try:
        flow_rows = get_ods_flow_inventory(source_schema=source_schema, target_owner=target_owner)
        for row in flow_rows:
            mapping_name = str(row.get("MAPPING_NAME") or "").strip().upper()
            source_table = str(row.get("SOURCE_TABLE") or "").strip().upper()
            target_table = str(row.get("EXPECTED_TARGET_TABLE") or "").strip().upper()
            if mapping_name and source_table and target_table:
                table_mapping_names[(source_table, target_table)] = mapping_name
        mapping_names = sorted(set(table_mapping_names.values()))
        if mapping_names:
            mapping_links = get_odi_mapping_column_links(mapping_names, source_schema=source_schema, target_owner=target_owner)
            mapping_link_aware = True
    except Exception:
        mapping_links = []
        mapping_link_aware = False

    source_link_index: dict[tuple[str, str, str], list[dict]] = {}
    target_link_index: dict[tuple[str, str], list[dict]] = {}
    for link in mapping_links:
        source_table = str(link.get("source_table") or "").upper()
        source_column = str(link.get("source_column") or "").upper()
        target_table = str(link.get("target_table") or "").upper()
        target_column = str(link.get("target_column") or "").upper()
        if target_table and target_column:
            target_link_index.setdefault((target_table, target_column), []).append(link)
        if source_table and source_column and target_table:
            source_link_index.setdefault((source_table, source_column, target_table), []).append(link)

    findings: list[dict] = []

    for key in sorted(source_rows):
        source_row = source_rows[key]
        target_row = target_rows.get(key)
        source_table = source_row["source_table"]
        target_table = f"{target_owner}.{source_row['target_table_name']}"
        source_type = str(_column_value(source_row, "full_data_type") or "").upper()
        table_mapping_name = table_mapping_names.get((source_table, target_table))
        source_links = source_link_index.get((source_table, key[2], target_table), [])

        if not target_row:
            if source_links:
                continue
            findings.append({
                "kind": "source_column_missing_in_target",
                "severity": "warning",
                "source_schema": source_schema,
                "target_owner": target_owner,
                "source_table": source_table,
                "target_table": target_table,
                "column": key[2],
                "source_type": source_type,
                "target_type": None,
                "source_nullable": _column_value(source_row, "nullable"),
                "target_nullable": None,
                "mapping_name": table_mapping_name,
                "mapping_link_aware": mapping_link_aware,
                "mapping_status": "unmapped_source_column" if mapping_link_aware else "same_name_missing",
                "summary": _source_target_summary(source_table, key[2], f"exists in source as {source_type or '-'} but has no same-name column in {target_table}"),
                "next_step": "Confirm ODI mapping-link coverage before treating this as a missing target column.",
            })
            continue

        target_type = str(_column_value(target_row, "full_data_type") or "").upper()
        source_nullable = _normalize_nullable(_column_value(source_row, "nullable"))
        target_nullable = _normalize_nullable(_column_value(target_row, "nullable"))

        if source_type != target_type:
            findings.append({
                "kind": "source_target_type_size_mismatch",
                "severity": "warning",
                "source_schema": source_schema,
                "target_owner": target_owner,
                "source_table": source_table,
                "target_table": target_table,
                "column": key[2],
                "source_type": source_type,
                "target_type": target_type,
                "source_nullable": source_nullable,
                "target_nullable": target_nullable,
                "mapping_name": table_mapping_name,
                "mapping_link_aware": mapping_link_aware,
                "mapping_status": "same_name_present",
                "summary": _source_target_summary(source_table, key[2], f"source {source_type or '-'} != target {target_type or '-'}"),
                "next_step": "Review source-to-target metadata alignment before PK parity checks.",
            })

        if source_nullable and target_nullable and source_nullable != target_nullable:
            findings.append({
                "kind": "source_target_nullability_mismatch",
                "severity": "warning",
                "source_schema": source_schema,
                "target_owner": target_owner,
                "source_table": source_table,
                "target_table": target_table,
                "column": key[2],
                "source_type": source_type,
                "target_type": target_type,
                "source_nullable": source_nullable,
                "target_nullable": target_nullable,
                "mapping_name": table_mapping_name,
                "mapping_link_aware": mapping_link_aware,
                "mapping_status": "same_name_present",
                "summary": _source_target_summary(source_table, key[2], f"source nullable {source_nullable} != target nullable {target_nullable}"),
                "next_step": "Review nullability before enforcing parity or load gates.",
            })

    for key in sorted(target_rows):
        if key in source_rows:
            continue
        target_row = target_rows[key]
        target_type = str(_column_value(target_row, "full_data_type") or "").upper()
        target_table_name = str(_column_value(target_row, "table_name") or "").upper()
        target_table = f"{target_owner}.{target_table_name}"
        target_links = target_link_index.get((target_table, key[2]), [])
        if target_links:
            continue
        table_mapping_name = next(
            (
                mapping_name
                for (_source_table, mapped_target_table), mapping_name in table_mapping_names.items()
                if mapped_target_table == target_table
            ),
            None,
        )
        findings.append({
            "kind": "target_column_missing_in_source",
            "severity": "info",
            "source_schema": source_schema,
            "target_owner": target_owner,
            "source_table": None,
            "target_table": target_table,
            "column": key[2],
            "source_type": None,
            "target_type": target_type,
            "source_nullable": None,
            "target_nullable": _normalize_nullable(_column_value(target_row, "nullable")),
            "mapping_name": table_mapping_name,
            "mapping_link_aware": mapping_link_aware,
            "mapping_status": "unmapped_target_column" if mapping_link_aware else "same_name_no_source",
            "summary": f"{target_owner}.{target_table_name}.{key[2]} exists in target as {target_type or '-'} but has no same-name source column",
            "next_step": "Confirm whether this is derived, audit, technical, or stale target metadata.",
        })

    priority = {
        "source_target_type_size_mismatch": 0,
        "source_target_nullability_mismatch": 1,
        "source_column_missing_in_target": 2,
        "target_column_missing_in_source": 3,
    }
    findings.sort(key=lambda item: (
        priority.get(str(item.get("kind")), 99),
        str(item.get("source_table") or ""),
        str(item.get("target_table") or ""),
        str(item.get("column") or ""),
    ))

    return {
        "source_schema": source_schema,
        "target_owner": target_owner,
        "finding_count": len(findings),
        "findings": findings,
        "read_only": True,
        "scope": "source_to_target_structural_metadata",
        "mapping_link_aware": mapping_link_aware,
        "mapping_link_count": len(mapping_links),
    }


def detect_changes(owner: str = "DWH") -> list[dict]:
    """
    Check all tables in the specified schema for schema changes.
    Return the list of tables where changes were detected.
    """
    changes = []
    conn = get_connection()

    try:
        tables = get_tables(conn, owner)
        logger.info("📋 Schema %s: checking %d tables...", owner, len(tables))

        for table_name in tables:
            current_hash = compute_column_hash(conn, owner, table_name)
            if not current_hash:
                logger.warning("⚠️  %s.%s — column information unavailable; skipping.", owner, table_name)
                continue

            previous_hash = get_previous_hash(conn, owner, table_name)

            if previous_hash is None:
                # First record.
                logger.info("🆕 %s.%s — recording the first hash.", owner, table_name)
                save_hash(conn, owner, table_name, current_hash, None, False)

            elif current_hash != previous_hash:
                # CHANGE DETECTED!
                logger.warning(
                    "🔴 CHANGE: %s.%s — Hash: %s → %s",
                    owner, table_name, previous_hash[:12], current_hash[:12]
                )
                save_hash(conn, owner, table_name, current_hash, previous_hash, True)
                changes.append({
                    "owner":         owner,
                    "table":         table_name,
                    "previous_hash": previous_hash,
                    "current_hash":  current_hash,
                    "detected_at":   datetime.now().isoformat(),
                })

            else:
                # No change.
                logger.debug("✅ %s.%s — no change.", owner, table_name)

        logger.info(
            "━━━ Result: %d tables checked, %d changes detected.",
            len(tables), len(changes)
        )
    finally:
        conn.close()

    return changes


# ── Get Recent Changes (for UI) ───────────────────────────────────────────────
def get_recent_changes(limit: int = 20) -> list[dict]:
    """Return the latest N schema changes for display in the UI."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT TABLE_OWNER, TABLE_NAME, COLUMN_HASH, PREVIOUS_HASH, CHECK_DATE
            FROM AUDIT_USER.SCHEMA_HASH
            WHERE CHANGE_DETECTED = 'Y'
            ORDER BY CHECK_DATE DESC
            FETCH FIRST :lim ROWS ONLY
        """, {"lim": limit})
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as exc:
        logger.error("Schema change history read failed: %s", type(exc).__name__)
        raise
    finally:
        cursor.close()
        conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Oracle Schema Change Detector")
    parser.add_argument("--owner", default="DWH", help="Schema to check (default: DWH)")
    args = parser.parse_args()

    results = detect_changes(args.owner)

    if results:
        print(f"\n🔴 Schema changes detected in {len(results)} tables:\n")
        for r in results:
            print(f"  • {r['owner']}.{r['table']}")
            print(f"    Old: {r['previous_hash'][:16]}...")
            print(f"    New: {r['current_hash'][:16]}...")
            print()
    else:
        print("\n✅ No schema changes detected.\n")
