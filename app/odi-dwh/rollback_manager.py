"""
Rollback Manager — DDL Islem Geri Alma
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MAPPING_LOG tablosundaki batch kayitlarini kullanarak
DDL islemlerini ters sirada geri alir.

Desteklenen islemler:
  CREATE_TABLE    → DROP TABLE
  CREATE_SEQUENCE → DROP SEQUENCE
"""

import logging
import re

import oracledb
from config import ORACLE_DDL_USER, ORACLE_DDL_PASSWORD, ORACLE_DSN

logger = logging.getLogger("rollback_manager")
_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _qualified_object_name(owner: str, name: str) -> str:
    owner_text = str(owner or "").strip().upper()
    name_text = str(name or "").strip().upper()
    if not _IDENTIFIER_RE.fullmatch(owner_text) or not _IDENTIFIER_RE.fullmatch(name_text):
        raise ValueError("Invalid rollback object identifier.")
    return f'"{owner_text}"."{name_text}"'


class RollbackManager:
    """DDL islem geri alma yonetimi."""

    # Ters islem haritasi
    REVERSE_OPS = {
        "CREATE_TABLE":    "DROP TABLE",
        "CREATE_SEQUENCE": "DROP SEQUENCE",
    }

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_DDL_USER,
                password=ORACLE_DDL_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "RollbackManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def get_batch_log(self, batch_id: str) -> list[dict]:
        """Batch icindeki tum MAPPING_LOG kayitlarini doner.

        Args:
            batch_id: SKEY veya diger DDL isleminin batch kimlik degeri.

        Returns:
            Her kayit icin LOG_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME,
            STATUS, ERROR_MESSAGE, CREATED_AT alanlari icerir.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT LOG_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME,
                       STATUS, ERROR_MESSAGE, CREATED_AT
                FROM AUDIT_USER.MAPPING_LOG
                WHERE BATCH_ID = :bid
                ORDER BY CREATED_AT ASC
            """, {"bid": batch_id})

            cols = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                for k, v in d.items():
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                rows.append(d)
            return rows
        finally:
            cursor.close()

    def rollback_batch(self, batch_id: str) -> dict:
        """Batch icindeki basarili CREATE islemlerini ters kronolojik sirada geri alir.

        Args:
            batch_id: Geri alinacak DDL batch'inin kimlik degeri.

        Returns:
            Sozluk: batch_id, rolled_back (geri alinan nesneler),
            skipped (atlanan nesneler) ve errors listelerini icerir.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        result = {"batch_id": batch_id, "rolled_back": [], "skipped": [], "errors": []}

        try:
            # Basarili islemleri ters kronolojik sirada al
            cursor.execute("""
                SELECT LOG_ID, OPERATION, OBJECT_OWNER, OBJECT_NAME
                FROM AUDIT_USER.MAPPING_LOG
                WHERE BATCH_ID = :bid AND STATUS = 'SUCCESS'
                ORDER BY CREATED_AT DESC
            """, {"bid": batch_id})

            operations = cursor.fetchall()

            if not operations:
                result["skipped"].append("Batch'te geri alinacak islem bulunamadi")
                return result

            for log_id, operation, obj_owner, obj_name in operations:
                reverse_op = self.REVERSE_OPS.get(operation)
                if not reverse_op:
                    result["skipped"].append(f"{operation} {obj_owner}.{obj_name} (ters islemi tanimli degil)")
                    continue

                try:
                    qualified_name = _qualified_object_name(obj_owner, obj_name)
                except ValueError as exc:
                    result["errors"].append({
                        "object": "invalid_identifier",
                        "error": "Rollback object validation failed.",
                        "error_type": type(exc).__name__,
                    })
                    continue

                ddl = f"{reverse_op} {qualified_name}"
                try:
                    cursor.execute(ddl)
                    conn.commit()

                    # MAPPING_LOG'daki orijinal kaydi ROLLED_BACK olarak isaretle
                    cursor.execute("""
                        UPDATE AUDIT_USER.MAPPING_LOG
                        SET STATUS = 'ROLLED_BACK'
                        WHERE LOG_ID = :lid
                    """, {"lid": log_id})
                    conn.commit()

                    result["rolled_back"].append(f"{reverse_op} {obj_owner}.{obj_name}")
                    logger.info("Rollback basarili: %s", ddl)

                except oracledb.DatabaseError as e:
                    err_str = str(e)
                    if "ORA-00942" in err_str or "ORA-02289" in err_str:
                        # Table/sequence zaten yok
                        result["skipped"].append(f"{obj_owner}.{obj_name} (zaten mevcut degil)")
                    else:
                        result["errors"].append({
                            "object": f"{obj_owner}.{obj_name}",
                            "error": "Rollback operation failed.",
                            "error_type": type(e).__name__,
                        })
                        logger.error("Rollback operation failed: %s", type(e).__name__)

        finally:
            cursor.close()

        logger.info(
            "Rollback sonucu batch=%s: %d geri alindi, %d atlandi, %d hata",
            batch_id, len(result["rolled_back"]), len(result["skipped"]), len(result["errors"])
        )
        return result
