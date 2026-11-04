"""
Mapping Snapshot Manager — ODI Mapping Version Comparison
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Stores ODI mapping snapshots in AUDIT_USER.MAPPING_SNAPSHOT
and analyzes differences between two snapshots.

Usage:
    with MappingSnapshotManager() as msm:
        msm.take_snapshot("MAP_PRODUCT_DIM", mapping_detail_dict)
        diff = msm.compare_latest("MAP_PRODUCT_DIM")
"""

import hashlib
import json
import logging
from typing import Any

import oracledb
from config import ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN

logger = logging.getLogger("mapping_snapshot_manager")


class MappingSnapshotManager:
    """ODI mapping snapshots and version comparison."""

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "MappingSnapshotManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ── DDL ───────────────────────────────────────────────────────────────────
    def ensure_table(self) -> None:
        """Verify the canonical snapshot table exists without runtime DDL."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM ALL_TABLES "
                "WHERE OWNER = 'AUDIT_USER' AND TABLE_NAME = 'MAPPING_SNAPSHOT'"
            )
            (exists,) = cursor.fetchone()
            if not exists:
                raise RuntimeError(
                    "Canonical audit schema is not initialized; review ddl/audit_schema_contract.sql."
                )
        finally:
            cursor.close()

    # ── Snapshot Alma ─────────────────────────────────────────────────────────
    def take_snapshot(self, mapping_name: str,
                      mapping_data: dict[str, Any],
                      label: str = "") -> int:
        """Save a snapshot of the mapping.

        Args:
            mapping_name: ODI mapping name.
            mapping_data: Mapping dictionary containing the column list
                (output of odi_get_mapping_details).
            label: Opsiyonel versiyon etiketi (orn. "before_refactor").

        Returns:
            Created SNAP_ID.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        snap_id_var = cursor.var(int)

        # Normalize only fields included in comparisons
        normalized = _normalize_mapping(mapping_data)
        snap_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        snap_hash = hashlib.sha256(snap_json.encode()).hexdigest()[:16]

        try:
            cursor.execute("""
                INSERT INTO AUDIT_USER.MAPPING_SNAPSHOT
                    (MAPPING_NAME, SNAP_LABEL, SNAP_HASH, SNAP_JSON)
                VALUES
                    (:mname, :label, :hash, :json_data)
                RETURNING SNAP_ID INTO :sid
            """, {
                "mname": mapping_name.upper(),
                "label": label or "",
                "hash":  snap_hash,
                "json_data": snap_json,
                "sid":   snap_id_var,
            })
            conn.commit()
            snap_id = snap_id_var.getvalue()[0]
            logger.info("Snapshot saved: %s (id=%d, hash=%s)", mapping_name, snap_id, snap_hash)
            return snap_id
        finally:
            cursor.close()

    # ── Snapshot Listesi ──────────────────────────────────────────────────────
    def get_snapshots(self, mapping_name: str, limit: int = 10) -> list[dict]:
        """Return the mapping snapshot history (newest first).

        Args:
            mapping_name: ODI mapping name.
            limit: Maximum number of snapshots to return.

        Returns:
            List containing SNAP_ID, SNAP_DATE, SNAP_LABEL, SNAP_HASH for each snapshot.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT SNAP_ID, SNAP_DATE, SNAP_LABEL, SNAP_HASH
                FROM AUDIT_USER.MAPPING_SNAPSHOT
                WHERE UPPER(MAPPING_NAME) = UPPER(:mname)
                ORDER BY SNAP_DATE DESC
                FETCH FIRST :lmt ROWS ONLY
            """, {"mname": mapping_name, "lmt": limit})

            cols = [d[0].lower() for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                if hasattr(d.get("snap_date"), "isoformat"):
                    d["snap_date"] = d["snap_date"].isoformat()
                rows.append(d)
            return rows
        except oracledb.DatabaseError as e:
            if "ORA-00942" in str(e):
                return []
            raise
        finally:
            cursor.close()

    def _get_snapshot_json(self, snap_id: int) -> dict | None:
        """Return the JSON content of the specified snapshot."""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT SNAP_JSON FROM AUDIT_USER.MAPPING_SNAPSHOT
                WHERE SNAP_ID = :sid
            """, {"sid": snap_id})
            row = cursor.fetchone()
            if row:
                raw = row[0].read() if hasattr(row[0], "read") else str(row[0])
                return json.loads(raw)
            return None
        finally:
            cursor.close()

    # ── Comparison ─────────────────────────────────────────────────────────
    def compare_snapshots(self, snap_id_old: int, snap_id_new: int) -> dict:
        """Analyze differences between two snapshots.

        Args:
            snap_id_old: Old (baseline) snapshot ID.
            snap_id_new: New (compared) snapshot ID.

        Returns:
            Dictionary: added, removed, changed, unchanged column lists.
        """
        old = self._get_snapshot_json(snap_id_old)
        new = self._get_snapshot_json(snap_id_new)

        if old is None or new is None:
            return {"error": "Snapshot not found."}

        return _diff_mappings(old, new)

    def compare_latest(self, mapping_name: str) -> dict:
        """Compare the two most recent snapshots of the mapping.

        Args:
            mapping_name: Mapping name to compare.

        Returns:
            diff dictionary (added, removed, changed, unchanged) or
            {"error": "..."} if there are insufficient snapshots.
        """
        snaps = self.get_snapshots(mapping_name, limit=2)
        if len(snaps) < 2:
            return {
                "error": "At least 2 snapshots are required for comparison.",
                "snap_count": len(snaps),
            }
        newer = snaps[0]  # En yeni
        older = snaps[1]  # Previous one
        diff = self.compare_snapshots(older["snap_id"], newer["snap_id"])
        diff["snap_old"] = older
        diff["snap_new"] = newer
        return diff


# ── Helper Functions ─────────────────────────────────────────────────────

def _normalize_mapping(data: dict[str, Any]) -> dict:
    """Convert mapping data into a comparable dictionary.

    Keep only TARGET columns and their expressions.
    """
    rows = data if isinstance(data, list) else data.get("rows", data.get("data", []))
    columns: dict[str, str] = {}

    for row in rows:
        comp_type = str(row.get("COMP_TYPE") or row.get("comp_type") or "").upper()
        attr_name = str(row.get("ATTR_NAME") or row.get("attr_name") or "").strip()
        expression = str(row.get("EXPRESSION") or row.get("expression") or "").strip()

        # TARGET rows only
        if comp_type == "DATASTORE" and expression and attr_name:
            columns[attr_name.upper()] = expression

    return {"columns": columns}


def _diff_mappings(old: dict, new: dict) -> dict:
    """Compare two normalized mapping dictionaries."""
    old_cols: dict[str, str] = old.get("columns", {})
    new_cols: dict[str, str] = new.get("columns", {})

    old_keys = set(old_cols)
    new_keys = set(new_cols)

    added = [
        {"column": k, "new_expression": new_cols[k]}
        for k in sorted(new_keys - old_keys)
    ]
    removed = [
        {"column": k, "old_expression": old_cols[k]}
        for k in sorted(old_keys - new_keys)
    ]
    changed = [
        {"column": k, "old_expression": old_cols[k], "new_expression": new_cols[k]}
        for k in sorted(old_keys & new_keys)
        if old_cols[k] != new_cols[k]
    ]
    unchanged = sorted(
        k for k in old_keys & new_keys if old_cols[k] == new_cols[k]
    )

    return {
        "added":     added,
        "removed":   removed,
        "changed":   changed,
        "unchanged": unchanged,
        "summary": {
            "added":     len(added),
            "removed":   len(removed),
            "changed":   len(changed),
            "unchanged": len(unchanged),
        },
    }
