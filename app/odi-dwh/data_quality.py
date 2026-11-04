"""
Veri Kalitesi Yöneticisi
━━━━━━━━━━━━━━━━━━━━━━━━

ODS'e yüklenmeden önce veri kalitesi kontrollerini çalıştırır.
Kural tabanlı doğrulama: NULL kontrol, tip kontrol, domain kontrol,
benzersizlik kontrol, aralık kontrol.

Kullanım:
  from data_quality import DataQualityManager
  dqm = DataQualityManager()
  result = dqm.validate_table("DWH", "SALES")
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import oracledb
import pandas as pd
from config import (
    ORACLE_USER,
    ORACLE_PASSWORD,
    ORACLE_DSN,
    ORACLE_DDL_USER,
    ORACLE_DDL_PASSWORD,
    AUDIT_ORACLE_USER,
    TARGET_ORACLE_USER,
    TARGET_ORACLE_PASSWORD,
    TARGET_ORACLE_DSN,
)

logger = logging.getLogger("data_quality")


# ── Kural Tanımları ───────────────────────────────────────────────────────────

@dataclass
class QualityRule:
    """Tek bir veri kalitesi kuralı.

    Attributes:
        rule_name: Kural adı (ör. "PK_NOT_NULL").
        column: Hedef kolon adı.
        rule_type: 'not_null', 'data_type', 'domain', 'unique', 'range', 'regex'.
        params: Kural parametreleri (ör. {"min": 0}).
        severity: 'ERROR' (yüklemeyi durdur) veya 'WARNING' (sadece logla).
    """

    rule_name: str
    column: str
    rule_type: str
    params: dict = field(default_factory=dict)
    severity: str = "ERROR"


SUPPORTED_QUALITY_RULE_TYPES = frozenset({"not_null", "date_range"})


def _quality_rule_type_label(rule_type: object) -> str:
    """Return a short rule-type name safe to put in a fail-closed reason."""
    if isinstance(rule_type, str):
        label = rule_type.strip()
        if label:
            return label[:80]
    return "unknown"


def _is_supported_quality_rule(rule: QualityRule) -> bool:
    return isinstance(rule.rule_type, str) and rule.rule_type in SUPPORTED_QUALITY_RULE_TYPES


@dataclass(frozen=True)
class PostLoadDQSpec:
    """Read-only post-load data-quality contract for one ODS target."""

    name: str
    target_table: str
    key_columns: tuple[str, ...]
    required_columns: tuple[str, ...] = ()
    minimum_rows: int = 1


def default_post_load_dq_specs() -> dict[str, PostLoadDQSpec]:
    """Controlled HR/SH post-load DQ specs for ODI-30."""

    return {
        "HR_EMPLOYEES_ODS": PostLoadDQSpec(
            name="HR_EMPLOYEES_ODS",
            target_table="DWH.ODS_HR_EMPLOYEES",
            key_columns=("EMPLOYEE_ID",),
            required_columns=("EMPLOYEE_ID", "FIRST_NAME", "LAST_NAME", "EMAIL", "HIRE_DATE", "JOB_ID"),
        ),
        "SH_SALES_ODS": PostLoadDQSpec(
            name="SH_SALES_ODS",
            target_table="DWH.ODS_SH_SALES",
            key_columns=("PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID"),
            required_columns=("PROD_ID", "CUST_ID", "TIME_ID", "CHANNEL_ID", "PROMO_ID", "AMOUNT_SOLD", "QUANTITY_SOLD"),
        ),
    }


def run_configured_post_load_dq_gate(spec_names: list[str] | None = None) -> dict[str, Any]:
    """Open the configured target Oracle connection and run the read-only DQ gate."""

    specs = _resolve_post_load_dq_specs(spec_names, default_post_load_dq_specs())
    conn = oracledb.connect(
        user=TARGET_ORACLE_USER,
        password=TARGET_ORACLE_PASSWORD,
        dsn=TARGET_ORACLE_DSN,
    )
    try:
        return run_post_load_dq_gate(specs, conn=conn)
    finally:
        conn.close()


def run_post_load_dq_gate(specs: list[PostLoadDQSpec], *, conn: Any) -> dict[str, Any]:
    """Run read-only post-load DQ checks and return a redacted summary."""

    results = [_run_one_post_load_dq_spec(spec, conn=conn) for spec in specs]
    return {
        "tool": "odi.post_load_dq_gate",
        "status": "passed" if results and all(item["status"] == "passed" for item in results) else "failed",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "secret_printed": False,
        "raw_rows_included": False,
        "results": results,
    }


def _resolve_post_load_dq_specs(
    names: list[str] | None,
    specs_by_name: dict[str, PostLoadDQSpec],
) -> list[PostLoadDQSpec]:
    if not names:
        return list(specs_by_name.values())
    selected = []
    for name in names:
        key = str(name).strip().upper()
        if key not in specs_by_name:
            raise KeyError(f"Unknown post-load DQ spec: {name}")
        selected.append(specs_by_name[key])
    return selected


def _run_one_post_load_dq_spec(spec: PostLoadDQSpec, *, conn: Any) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        owner, table = _split_qualified_name(spec.target_table)
        row_count = _dq_row_count(conn, owner, table)
    except Exception as exc:
        return {
            "spec": spec.name,
            "status": "missing_target" if _is_missing_target_error(exc) else "error",
            "target_table": spec.target_table,
            "row_count": None,
            "secret_printed": False,
            "raw_rows_included": False,
            "checks": [
                {
                    "name": "target_exists",
                    "type": "metadata",
                    "status": "failed",
                    "detail": _safe_dq_error(exc),
                }
            ],
        }

    checks.append({
        "name": "target_exists",
        "type": "metadata",
        "status": "passed",
        "violation_count": 0,
    })
    checks.append({
        "name": "row_count_minimum",
        "type": "row_count",
        "status": "passed" if row_count >= spec.minimum_rows else "failed",
        "violation_count": 0 if row_count >= spec.minimum_rows else 1,
        "actual": row_count,
        "minimum": spec.minimum_rows,
    })

    existing_columns = _dq_table_columns(conn, owner, table)
    checked_columns = _normalized_columns(spec.required_columns)
    missing_columns = [column for column in checked_columns if column not in existing_columns]
    checks.append({
        "name": "required_columns_present",
        "type": "metadata",
        "status": "passed" if not missing_columns else "failed",
        "violation_count": len(missing_columns),
        "columns_checked": checked_columns,
        "missing_columns": missing_columns,
    })

    key_columns = _normalized_columns(spec.key_columns)
    for column in key_columns:
        violation_count = _dq_null_count(conn, owner, table, column)
        checks.append({
            "name": f"not_null:{column}",
            "type": "not_null",
            "status": "passed" if violation_count == 0 else "failed",
            "violation_count": violation_count,
            "column": column,
        })

    duplicate_count = _dq_duplicate_key_count(conn, owner, table, key_columns)
    checks.append({
        "name": "duplicate_key",
        "type": "unique",
        "status": "passed" if duplicate_count == 0 else "failed",
        "violation_count": duplicate_count,
        "columns": key_columns,
    })

    status = "passed" if all(check["status"] == "passed" for check in checks) else "failed"
    return {
        "spec": spec.name,
        "status": status,
        "target_table": spec.target_table,
        "row_count": row_count,
        "secret_printed": False,
        "raw_rows_included": False,
        "checks": checks,
    }


def _dq_row_count(conn: Any, owner: str, table: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {_qname(owner, table)}")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def _dq_table_columns(conn: Any, owner: str, table: str) -> set[str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :owner AND TABLE_NAME = :tname
            """,
            {"owner": owner, "tname": table},
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}
    finally:
        cursor.close()


def _dq_null_count(conn: Any, owner: str, table: str, column: str) -> int:
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {_qname(owner, table)} WHERE {_qident(column)} IS NULL")
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def _dq_duplicate_key_count(conn: Any, owner: str, table: str, key_columns: list[str]) -> int:
    if not key_columns:
        return 0
    cursor = conn.cursor()
    try:
        key_sql = ", ".join(_qident(column) for column in key_columns)
        cursor.execute(f"""
            SELECT COUNT(*)
            FROM (
                SELECT {key_sql}
                FROM {_qname(owner, table)}
                GROUP BY {key_sql}
                HAVING COUNT(*) > 1
            )
        """)
        return int(cursor.fetchone()[0])
    finally:
        cursor.close()


def _split_qualified_name(table_name: str) -> tuple[str, str]:
    parts = [part.strip().upper() for part in str(table_name).split(".") if part.strip()]
    if len(parts) != 2:
        raise ValueError(f"Expected OWNER.TABLE name, got {table_name!r}")
    return _validate_identifier(parts[0]), _validate_identifier(parts[1])


def _normalized_columns(columns: tuple[str, ...]) -> list[str]:
    return [_validate_identifier(column) for column in columns]


def _qname(owner: str, table: str) -> str:
    return f"{_qident(owner)}.{_qident(table)}"


def _qident(name: str) -> str:
    return f'"{_validate_identifier(name)}"'


def _validate_identifier(name: str) -> str:
    text = str(name).strip().upper()
    if not text or not text.replace("_", "").isalnum():
        raise ValueError(f"Unsafe Oracle identifier: {name!r}")
    return text


def _is_missing_target_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return "ORA-00942" in text or "TABLE OR VIEW DOES NOT EXIST" in text


def _safe_dq_error(exc: Exception) -> str:
    text = str(exc).splitlines()[0]
    text = text.replace("password", "credential").replace("PASSWORD", "CREDENTIAL")
    return text[:240]


# ── Varsayılan Kural Şablonları ───────────────────────────────────────────────

DEFAULT_RULES = {
    "pk_not_null": lambda col: QualityRule(
        f"PK_NOT_NULL_{col}", col, "not_null", severity="ERROR"
    ),
    "positive_number": lambda col: QualityRule(
        f"POSITIVE_{col}", col, "range", {"min": 0}, severity="WARNING"
    ),
    "valid_date_range": lambda col: QualityRule(
        f"DATE_RANGE_{col}", col, "range",
        {"min": "2000-01-01", "max": "2030-12-31"}, severity="WARNING"
    ),
}


# ── Doğrulama Sonucu ─────────────────────────────────────────────────────────

class ValidationResult:
    """Bir doğrulama çalışmasının sonucu."""

    def __init__(self, table_owner: str, table_name: str):
        self.table_owner = table_owner
        self.table_name = table_name
        self.started_at = datetime.now()
        self.finished_at = None
        self.total_rows = 0
        self.violations: list[dict] = []
        self.rules_run: list[dict] = []   # Tüm kurallar (geçen + ihlal eden)
        self.passed = True

    def add_violation(self, rule: QualityRule, count: int, sample_values: list = None):
        violation = {
            "rule_name":   rule.rule_name,
            "column":      rule.column,
            "rule_type":   rule.rule_type,
            "severity":    rule.severity,
            "violation_count": count,
            "sample_values":   sample_values or [],
        }
        self.violations.append(violation)
        if rule.severity == "ERROR":
            self.passed = False

    def record_rule(self, rule: QualityRule, passed: bool, violation_count: int = 0) -> None:
        """Çalışan her kuralı (geçen veya ihlal eden) kaydeder."""
        self.rules_run.append({
            "rule_name":       rule.rule_name,
            "column":          rule.column,
            "rule_type":       rule.rule_type,
            "severity":        rule.severity,
            "passed":          passed,
            "status":          "passed" if passed else "failed",
            "violation_count": violation_count,
        })

    def record_unsupported(self, rule: QualityRule) -> None:
        """Fail closed: an unknown rule type must not be reported as passed."""
        label = _quality_rule_type_label(rule.rule_type)
        self.rules_run.append({
            "rule_name": rule.rule_name,
            "column": rule.column,
            "rule_type": rule.rule_type,
            "severity": rule.severity,
            "passed": False,
            "status": "unsupported",
            "violation_count": 0,
            "reason": f"Unsupported quality rule type: {label}.",
        })
        self.passed = False

    def finish(self):
        self.finished_at = datetime.now()

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"ValidationResult({self.table_owner}.{self.table_name}, "
            f"{status}, violations={len(self.violations)})"
        )

    def summary(self) -> dict:
        unsupported_rules = [
            {
                "rule_name": rule["rule_name"],
                "column": rule["column"],
                "rule_type": rule["rule_type"],
                "reason": rule.get("reason") or "Unsupported quality rule type: unknown.",
            }
            for rule in self.rules_run
            if rule.get("status") == "unsupported"
        ]
        passed = self.passed and not unsupported_rules
        return {
            "table":       f"{self.table_owner}.{self.table_name}",
            "total_rows":  self.total_rows,
            "passed":      passed,
            "status":      "passed" if passed else "failed",
            "errors":      len([v for v in self.violations if v["severity"] == "ERROR"]),
            "warnings":    len([v for v in self.violations if v["severity"] == "WARNING"]),
            "violations":  self.violations,
            "rules":       self.rules_run,
            "unsupported_rules": unsupported_rules,
            "duration_ms": int((self.finished_at - self.started_at).total_seconds() * 1000)
                           if self.finished_at else None,
        }


# ── Ana Yönetici ──────────────────────────────────────────────────────────────

class DataQualityManager:
    """Veri kalitesi kontrol motoru."""

    def __init__(self):
        self.conn = None
        self.audit_conn = None
        self.audit_owner = (AUDIT_ORACLE_USER or ORACLE_USER or "AUDIT_USER").upper().replace('"', "")

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def _get_audit_conn(self):
        ddl_user = (ORACLE_DDL_USER or "").upper()
        if ddl_user and ddl_user == self.audit_owner and ORACLE_DDL_PASSWORD:
            if self.audit_conn is None or not self.audit_conn.is_healthy():
                self.audit_conn = oracledb.connect(
                    user=ORACLE_DDL_USER,
                    password=ORACLE_DDL_PASSWORD,
                    dsn=ORACLE_DSN,
                )
            return self.audit_conn
        return self._get_conn()

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
        if self.audit_conn:
            self.audit_conn.close()
            self.audit_conn = None

    def __enter__(self) -> "DataQualityManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # ── Otomatik Kural Üretimi ────────────────────────────────────────────────
    def generate_rules(self, owner: str, table_name: str) -> list[QualityRule]:
        """
        Tablo yapısına göre otomatik kalite kuralları üretir.
        - NOT NULL kısıtlı kolonlar → not_null kuralı
        - NUMBER kolonlar → tip kontrolü
        - DATE kolonlar → aralık kontrolü
        """
        # Kullanıcı talebi üzerine otomatik kural üretimi devre dışı bırakıldı.
        # Artık sadece manuel olarak eklenen "Kural Setleri" dikkate alınacak.
        rules = []
        logger.info("📏 %s.%s için otomatik kural üretimi kapalı.", owner, table_name)
        return rules

    # ── Kural Çalıştırma ─────────────────────────────────────────────────────
    @staticmethod
    def _qi(name: str) -> str:
        """S-11: Identifier'ı tırnak içine al — kolon/tablo adında injection engeli."""
        return '"' + name.upper().replace('"', '') + '"'

    def _audit_table_name(self, table_name: str) -> str:
        return f"{self._qi(self.audit_owner)}.{self._qi('ERR_' + table_name)}"

    def _check_not_null(self, conn, owner: str, table: str, rule: QualityRule) -> int:
        cursor = conn.cursor()
        col = self._qi(rule.column)
        try:
            cursor.execute(f"""
                SELECT COUNT(*) FROM {self._qi(owner)}.{self._qi(table)}
                WHERE {col} IS NULL
            """)
            return cursor.fetchone()[0]
        finally:
            cursor.close()

    def _check_date_range(self, conn, owner: str, table: str, rule: QualityRule) -> int:
        min_year = rule.params.get("min_year", 2000)
        max_year = rule.params.get("max_year", 2030)
        col = self._qi(rule.column)
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                SELECT COUNT(*) FROM {self._qi(owner)}.{self._qi(table)}
                WHERE {col} IS NOT NULL
                  AND (EXTRACT(YEAR FROM {col}) < :min_yr
                    OR EXTRACT(YEAR FROM {col}) > :max_yr)
            """, {"min_yr": min_year, "max_yr": max_year})
            return cursor.fetchone()[0]
        finally:
            cursor.close()

    def _get_row_count(self, conn, owner: str, table: str) -> int:
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {self._qi(owner)}.{self._qi(table)}")
            return cursor.fetchone()[0]
        finally:
            cursor.close()

    def _record_unsupported_rule(
        self,
        result: ValidationResult,
        owner: str,
        table_name: str,
        rule: QualityRule,
    ) -> None:
        result.record_unsupported(rule)
        logger.warning(
            "Unsupported quality rule type %s for %s.%s rule %s.",
            _quality_rule_type_label(rule.rule_type),
            owner,
            table_name,
            rule.rule_name,
        )

    # ── Tablo Doğrulama ──────────────────────────────────────────────────────
    def validate_table(self, owner: str, table_name: str,
                       custom_rules: list[QualityRule] = None) -> ValidationResult:
        """
        Belirtilen tablo için veri kalitesi kontrollerini çalıştırır.
        custom_rules verilmezse otomatik kurallar üretilir.
        """
        result = ValidationResult(owner, table_name)
        conn = self._get_conn()

        try:
            result.total_rows = self._get_row_count(conn, owner, table_name)
            rules = custom_rules or self.generate_rules(owner, table_name)

            for rule in rules:
                if not _is_supported_quality_rule(rule):
                    self._record_unsupported_rule(result, owner, table_name, rule)
                    continue

                match rule.rule_type:
                    case "not_null":
                        violation_count = self._check_not_null(conn, owner, table_name, rule)
                    case "date_range":
                        violation_count = self._check_date_range(conn, owner, table_name, rule)
                    case _:
                        self._record_unsupported_rule(result, owner, table_name, rule)
                        continue

                # Her kuralı kaydediyoruz (geçen veya ihlal eden)
                result.record_rule(rule, passed=(violation_count == 0), violation_count=violation_count)

                if violation_count > 0:
                    result.add_violation(rule, violation_count)
                    level = logger.warning if rule.severity == "ERROR" else logger.info
                    level(
                        "❌ %s.%s — %s: %d ihlal (%s)",
                        owner, table_name, rule.rule_name, violation_count, rule.severity
                    )
                else:
                    logger.debug("✅ %s.%s — %s: OK", owner, table_name, rule.rule_name)

        except Exception as e:
            logger.error("Doğrulama hatası %s.%s: %s", owner, table_name, e)
            result.passed = False
        finally:
            result.finish()

        return result

    # ── Hata Tablosu Oluşturma ────────────────────────────────────────────────
    def create_error_table(self, owner: str, table_name: str):
        """
        Belirtilen tablo için ERR_<TABLE> hata tablosunu oluşturur.
        Kaynak tablonun tüm kolonlarını + hata bilgi kolonlarını içerir.
        """
        err_table = f"ERR_{table_name}"
        conn = self._get_audit_conn()
        cursor = conn.cursor()

        try:
            # Kaynak kolon listesi
            cursor.execute("""
                SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE
                FROM ALL_TAB_COLUMNS
                WHERE OWNER = :owner AND TABLE_NAME = :tname
                ORDER BY COLUMN_ID
            """, {"owner": owner.upper(), "tname": table_name.upper()})

            cols = []
            for col_name, dtype, dlen, dprec, dscale in cursor.fetchall():
                if dtype in ("VARCHAR2", "CHAR", "NVARCHAR2"):
                    cols.append(f"  {col_name} {dtype}({dlen})")
                elif dtype == "NUMBER" and dprec:
                    if dscale and int(dscale) > 0:
                        cols.append(f"  {col_name} NUMBER({int(dprec)},{int(dscale)})")
                    else:
                        cols.append(f"  {col_name} NUMBER({int(dprec)})")
                else:
                    cols.append(f"  {col_name} {dtype}")

            # Hata bilgi kolonları ekle
            cols.append("  ERR_RULE_NAME   VARCHAR2(128)")
            cols.append("  ERR_COLUMN      VARCHAR2(128)")
            cols.append("  ERR_SEVERITY    VARCHAR2(20)")
            cols.append("  ERR_MESSAGE     VARCHAR2(500)")
            cols.append("  ERR_DATE        TIMESTAMP DEFAULT SYSTIMESTAMP")

            ddl = f"CREATE TABLE {self._audit_table_name(table_name)} (\n"
            ddl += ",\n".join(cols)
            ddl += "\n)"

            # Var olan tabloyu atla
            try:
                cursor.execute(ddl)
                conn.commit()
                logger.info("✅ Hata tablosu oluşturuldu: %s.%s", self.audit_owner, err_table)
            except oracledb.DatabaseError as e:
                if "ORA-00955" in str(e):  # table already exists
                    logger.info("ℹ️  Hata tablosu zaten mevcut: %s.%s", self.audit_owner, err_table)
                else:
                    raise

        finally:
            cursor.close()

    # ── Toplu ERR_ Tablo Olusturma ──────────────────────────────────────────
    def create_all_error_tables(self, owner: str) -> dict:
        """
        Belirtilen sema altindaki tum tablolar icin ERR_ tablolarini toplu olusturur.
        Zaten mevcut olan ERR_ tablolari atlanir.
        Returns: {"created": [...], "skipped": [...], "errors": [...]}
        """
        conn = self._get_audit_conn()
        cursor = conn.cursor()
        result = {"created": [], "skipped": [], "errors": []}

        try:
            cursor.execute("""
                SELECT TABLE_NAME FROM ALL_TABLES
                WHERE OWNER = :owner
                  AND TABLE_NAME NOT LIKE 'ERR\\_%' ESCAPE '\\'
                  AND TABLE_NAME NOT LIKE 'BIN$%'
                ORDER BY TABLE_NAME
            """, {"owner": owner.upper()})

            tables = [row[0] for row in cursor.fetchall()]

            for tname in tables:
                try:
                    self.create_error_table(owner, tname)
                    # Basarili mi yoksa zaten var miydi kontrol et
                    cursor.execute("""
                        SELECT COUNT(*) FROM ALL_TABLES
                        WHERE OWNER = :owner AND TABLE_NAME = :ename
                    """, {"owner": self.audit_owner, "ename": f"ERR_{tname}"})
                    result["created"].append(tname)
                except oracledb.DatabaseError as e:
                    if "ORA-00955" in str(e):
                        result["skipped"].append(tname)
                    else:
                        result["errors"].append({"table": tname, "error": str(e)})
                except Exception as e:
                    result["errors"].append({"table": tname, "error": str(e)})

        finally:
            cursor.close()

        logger.info(
            "Toplu ERR_ olusturma: %d olusturuldu, %d atlandi, %d hata",
            len(result["created"]), len(result["skipped"]), len(result["errors"])
        )
        return result

    # ── Hata Kaydi Ekleme ────────────────────────────────────────────────────
    def insert_error_record(self, owner: str, table_name: str,
                            row_data: dict, rule_name: str,
                            column: str, severity: str,
                            message: str):
        """
        ERR_<TABLE> tablosuna bir ihlal kaydi ekler.
        row_data: kaynak satirin kolon->deger dict'i (uyusan kolonlar yazilir).
        """
        err_table = self._audit_table_name(table_name)
        conn = self._get_conn()
        cursor = conn.cursor()

        try:
            # ERR_ tablosundaki kolonlari al
            cursor.execute("""
                SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS
                WHERE OWNER = :owner AND TABLE_NAME = :ename
                ORDER BY COLUMN_ID
            """, {"owner": self.audit_owner, "ename": f"ERR_{table_name}"})
            err_cols = {row[0] for row in cursor.fetchall()}

            # Kaynak satir verileri + hata bilgileri
            insert_data = {}
            for k, v in row_data.items():
                if k.upper() in err_cols:
                    insert_data[k.upper()] = v

            insert_data["ERR_RULE_NAME"] = rule_name
            insert_data["ERR_COLUMN"] = column
            insert_data["ERR_SEVERITY"] = severity
            insert_data["ERR_MESSAGE"] = message[:500] if message else ""

            col_list = ", ".join(insert_data.keys())
            bind_list = ", ".join(f":{k}" for k in insert_data.keys())
            sql = f"INSERT INTO {err_table} ({col_list}) VALUES ({bind_list})"

            cursor.execute(sql, insert_data)
            conn.commit()

        finally:
            cursor.close()

    # ── Dogrulama + Otomatik Hata Kaydi ──────────────────────────────────────
    def validate_and_insert_errors(self, owner: str, table_name: str,
                                   custom_rules: list[QualityRule] = None,
                                   max_errors_per_rule: int = 100) -> ValidationResult:
        """
        validate_table gibi calisir, ek olarak ihlal eden satirlari
        ERR_<TABLE> tablosuna yazar. max_errors_per_rule ile kural basina
        kaydedilecek hata sayisi sinirlanir.
        """
        result = ValidationResult(owner, table_name)
        conn = self._get_conn()

        try:
            result.total_rows = self._get_row_count(conn, owner, table_name)
            rules = custom_rules or self.generate_rules(owner, table_name)

            # ERR_ tablosunun var oldugundan emin ol
            self.create_error_table(owner, table_name)

            for rule in rules:
                if not _is_supported_quality_rule(rule):
                    self._record_unsupported_rule(result, owner, table_name, rule)
                    continue

                match rule.rule_type:
                    case "not_null":
                        violation_count = self._check_not_null(conn, owner, table_name, rule)
                        if violation_count > 0:
                            # Ihlal eden satirlari cek ve ERR_'ye yaz
                            self._insert_null_violations(
                                conn, owner, table_name, rule, max_errors_per_rule
                            )
                    case "date_range":
                        violation_count = self._check_date_range(conn, owner, table_name, rule)
                        if violation_count > 0:
                            self._insert_date_violations(
                                conn, owner, table_name, rule, max_errors_per_rule
                            )
                    case _:
                        self._record_unsupported_rule(result, owner, table_name, rule)
                        continue

                if violation_count > 0:
                    result.add_violation(rule, violation_count)
                    level = logger.warning if rule.severity == "ERROR" else logger.info
                    level(
                        "ERR_ INSERT: %s.%s — %s: %d ihlal",
                        owner, table_name, rule.rule_name, violation_count
                    )

        except Exception as e:
            logger.error("validate_and_insert hatasi %s.%s: %s", owner, table_name, e)
            result.passed = False
        finally:
            result.finish()

        return result

    def _insert_null_violations(self, conn, owner: str, table: str,
                                rule: QualityRule, limit: int):
        """NOT NULL ihlali yapan satirlari ERR_ tablosuna yazar."""
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                SELECT * FROM {owner}.{table}
                WHERE {rule.column} IS NULL AND ROWNUM <= :lmt
            """, {"lmt": limit})
            cols = [d[0] for d in cursor.description]
            for row in cursor.fetchall():
                row_data = dict(zip(cols, row))
                self.insert_error_record(
                    owner, table, row_data, rule.rule_name,
                    rule.column, rule.severity,
                    f"{rule.column} kolonu NULL olamaz"
                )
        finally:
            cursor.close()

    def _insert_date_violations(self, conn, owner: str, table: str,
                                rule: QualityRule, limit: int):
        """Tarih araligi ihlali yapan satirlari ERR_ tablosuna yazar."""
        min_year = rule.params.get("min_year", 2000)
        max_year = rule.params.get("max_year", 2030)
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                SELECT * FROM {owner}.{table}
                WHERE {rule.column} IS NOT NULL
                  AND (EXTRACT(YEAR FROM {rule.column}) < :min_yr
                    OR EXTRACT(YEAR FROM {rule.column}) > :max_yr)
                  AND ROWNUM <= :lmt
            """, {"min_yr": min_year, "max_yr": max_year, "lmt": limit})
            cols = [d[0] for d in cursor.description]
            for row in cursor.fetchall():
                row_data = dict(zip(cols, row))
                self.insert_error_record(
                    owner, table, row_data, rule.rule_name,
                    rule.column, rule.severity,
                    f"{rule.column} degeri {min_year}-{max_year} araliginda olmali"
                )
        finally:
            cursor.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(description="Veri Kalitesi Yöneticisi")
    sub = parser.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="Tablo kalite kontrolü")
    v.add_argument("--owner", default="DWH")
    v.add_argument("--table", required=True)

    e = sub.add_parser("create-err", help="Hata tablosu oluştur")
    e.add_argument("--owner", default="DWH")
    e.add_argument("--table", required=True)

    args = parser.parse_args()
    dqm = DataQualityManager()

    try:
        if args.cmd == "validate":
            result = dqm.validate_table(args.owner, args.table)
            summary = result.summary()
            print(f"\n{'━' * 60}")
            print(f"  Tablo: {summary['table']}")
            print(f"  Toplam Satır: {summary['total_rows']:,}")
            print(f"  Sonuç: {'✅ GEÇTİ' if summary['passed'] else '❌ BAŞARISIZ'}")
            print(f"  Hatalar: {summary['errors']}  |  Uyarılar: {summary['warnings']}")
            print(f"  Süre: {summary['duration_ms']}ms")
            print(f"{'━' * 60}")
            if summary['violations']:
                print("\n  İhlaller:")
                for v in summary['violations']:
                    icon = "🔴" if v['severity'] == "ERROR" else "🟡"
                    print(f"    {icon} {v['rule_name']}: {v['violation_count']} satır")
            print()

        elif args.cmd == "create-err":
            dqm.create_error_table(args.owner, args.table)
            print(f"\n✅ {dqm.audit_owner}.ERR_{args.table} tablosu hazır.\n")

        else:
            parser.print_help()
    finally:
        dqm.close()
