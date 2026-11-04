"""
Mapping Snapshot Manager — ODI Mapping Versiyon Karşılaştırma
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ODI mapping'lerinin anlık görüntülerini (snapshot) AUDIT_USER.MAPPING_SNAPSHOT
tablosuna kaydeder ve iki snapshot arasındaki farkları analiz eder.

Kullanım:
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
    """ODI mapping snapshot alma ve versiyon karşılaştırma."""

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
        """Mapping'in anlık görüntüsünü kaydeder.

        Args:
            mapping_name: ODI mapping adı.
            mapping_data: Kolon listesini içeren mapping sözlüğü
                (odi_get_mapping_details çıktısı).
            label: Opsiyonel versiyon etiketi (orn. "before_refactor").

        Returns:
            Oluşturulan SNAP_ID.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        snap_id_var = cursor.var(int)

        # Sadece karşılaştırmaya dahil edilecek alanları normalize et
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
            logger.info("Snapshot alındı: %s (id=%d, hash=%s)", mapping_name, snap_id, snap_hash)
            return snap_id
        finally:
            cursor.close()

    # ── Snapshot Listesi ──────────────────────────────────────────────────────
    def get_snapshots(self, mapping_name: str, limit: int = 10) -> list[dict]:
        """Mapping'e ait snapshot geçmişini döner (en yeni önce).

        Args:
            mapping_name: ODI mapping adı.
            limit: Döndürülecek maksimum snapshot sayısı.

        Returns:
            Her snapshot için SNAP_ID, SNAP_DATE, SNAP_LABEL, SNAP_HASH içeren liste.
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
        """Belirtilen snapshot'un JSON içeriğini döner."""
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

    # ── Karşılaştırma ─────────────────────────────────────────────────────────
    def compare_snapshots(self, snap_id_old: int, snap_id_new: int) -> dict:
        """İki snapshot arasındaki farkları analiz eder.

        Args:
            snap_id_old: Eski (baz alınan) snapshot ID.
            snap_id_new: Yeni (karşılaştırılan) snapshot ID.

        Returns:
            Sözlük: added, removed, changed, unchanged kolon listeleri.
        """
        old = self._get_snapshot_json(snap_id_old)
        new = self._get_snapshot_json(snap_id_new)

        if old is None or new is None:
            return {"error": "Snapshot bulunamadı."}

        return _diff_mappings(old, new)

    def compare_latest(self, mapping_name: str) -> dict:
        """Mapping'in en son iki snapshot'ını karşılaştırır.

        Args:
            mapping_name: Karşılaştırılacak mapping adı.

        Returns:
            diff sözlüğü (added, removed, changed, unchanged) ya da
            yeterli snapshot yoksa {"error": "..."}.
        """
        snaps = self.get_snapshots(mapping_name, limit=2)
        if len(snaps) < 2:
            return {
                "error": "Karşılaştırma için en az 2 snapshot gerekli.",
                "snap_count": len(snaps),
            }
        newer = snaps[0]  # En yeni
        older = snaps[1]  # Bir önceki
        diff = self.compare_snapshots(older["snap_id"], newer["snap_id"])
        diff["snap_old"] = older
        diff["snap_new"] = newer
        return diff


# ── Yardımcı Fonksiyonlar ─────────────────────────────────────────────────────

def _normalize_mapping(data: dict[str, Any]) -> dict:
    """Mapping datasını karşılaştırılabilir sözlüğe dönüştürür.

    Yalnızca TARGET kolonları + expression'larını tutar.
    """
    rows = data if isinstance(data, list) else data.get("rows", data.get("data", []))
    columns: dict[str, str] = {}

    for row in rows:
        comp_type = str(row.get("COMP_TYPE") or row.get("comp_type") or "").upper()
        attr_name = str(row.get("ATTR_NAME") or row.get("attr_name") or "").strip()
        expression = str(row.get("EXPRESSION") or row.get("expression") or "").strip()

        # Yalnızca TARGET satırları
        if comp_type == "DATASTORE" and expression and attr_name:
            columns[attr_name.upper()] = expression

    return {"columns": columns}


def _diff_mappings(old: dict, new: dict) -> dict:
    """İki normalize edilmiş mapping sözlüğünü karşılaştırır."""
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
