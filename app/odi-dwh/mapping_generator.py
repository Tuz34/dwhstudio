"""
Mapping Generator — Otonom ODI Mapping Orkestratoru
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Kullanicinin dogal dil prompt'unu alir ve uctan uca mapping olusturur:
  1. AI Agent ile prompt'u yapisal parametrelere donusturur
  2. Hedef Oracle tabloyu olusturur (source connector tip donusumu + ODS default kolonlar)
  3. ODI mapping olusturur (OdiOrchestrator)
  4. Surrogate Key nesneleri olusturur (SkeyManager) — opsiyonel
  5. Data Lineage kaydeder (LineageManager)
  6. DQ testleri uretir (TestGenerator)
  7. Mapping snapshot alir (MappingSnapshotManager)

Tum adimlar MAPPING_LOG'a loglanir ve hata durumunda batch rollback desteklenir.
"""

import json
import logging
import re
import uuid
from datetime import datetime

import oracledb

from config import (
    ODI_FOLDER,
    ODI_PROJECT,
    ORACLE_DDL_PASSWORD,
    ORACLE_DDL_USER,
    ORACLE_DSN,
    SOURCE_ORACLE_SCHEMAS,
    SOURCE_SYSTEM_NAME,
)
from exceptions import MappingGenerationError
from mapping_state_manager import redact_sensitive_text
from source_connector import get_source_connector

logger = logging.getLogger("mapping_generator")

def _sql_string_literal(value: str | None, fallback: str = "ORACLE_SOURCE") -> str:
    """Oracle SQL literal icin tek tirnak kacir."""
    text = (value or fallback).strip() or fallback
    return text.replace("'", "''")


_SOURCE_SYSTEM_LITERAL = _sql_string_literal(SOURCE_SYSTEM_NAME, "ORACLE_SOURCE")


# Mapping parametrelerini prompt'tan cikarirken beklenen JSON formati
MAPPING_EXTRACT_PROMPT = """You are an ODI Mapping Configuration Expert.
The user wants to create an ODI mapping. Parse their request and extract structured parameters.

RULES:
1. mapping_name: Auto-generate if not provided. Format: MAP_<SRC>_TO_<TGT> (max 30 chars, uppercase)
2. Source system is Oracle by default. Source schemas are HR and SH.
3. src_model: Logical source hint only. Prefer "ORACLE_SOURCE" for Oracle sources; actual ODI model code is resolved from ODI repository tables.
4. src_table: Source table in SCHEMA.TABLE format (e.g. "HR.EMPLOYEES", "SH.SALES").
5. tgt_model: Logical target layer only. Use "ORACLE_ODS" for ODS targets and "ORACLE_DWH" for DWH targets; actual ODI model code is resolved from ODI repository tables.
6. tgt_table: Target table (UPPERCASE). Prefix source schema into ODS name, e.g. HR.EMPLOYEES -> ODS_HR_EMPLOYEES.
7. tgt_owner: Target schema. Default: "DWH".
8. needs_skey: true if user mentions "surrogate key", "SKEY", "boyut tablosu", "dimension"
9. skey_natural_key: If needs_skey, the natural key expression (e.g., "SRC.PRODUCT_ID")
10. description: One-line Turkish description of what this mapping does.

OUTPUT ONLY VALID JSON (no markdown):
{
  "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
  "src_model": "ORACLE_SOURCE",
  "src_table": "HR.EMPLOYEES",
  "tgt_model": "ORACLE_ODS",
  "tgt_table": "ODS_HR_EMPLOYEES",
  "tgt_owner": "DWH",
  "needs_skey": false,
  "skey_natural_key": null,
  "description": "HR.EMPLOYEES tablosunu Oracle kaynaktan ODS katmanina aktarir"
}"""


class MappingGenerator:
    """Otonom mapping olusturma orkestratoru.

    Kullanim:
        with MappingGenerator() as mg:
            result = mg.generate_from_prompt("Orders tablosunu ODS'ye aktar")
            # veya
            result = mg.generate_from_params({...})
    """

    # Adim isimleri (UI ilerleme gostergesi icin)
    STEPS = [
        "prompt_parse",
        "validate_params",
        "create_target_table",
        "resolve_odi_repository",
        "register_datastores",
        "create_mapping",
        "create_skey",
        "populate_lineage",
        "generate_tests",
        "take_snapshot",
    ]

    # ── SQL Server → Oracle Tip Donusum Tablosu ──────────────────────────────
    # Her key buyuk harfle tutulur. Deger tuple: (oracle_type, needs_length)
    # needs_length: True ise kaynak uzunluk aktarilir, False ise sabit tip.
    MSSQL_TO_ORACLE: dict[str, tuple[str, bool]] = {
        # Tam sayi tipleri
        "INT":             ("NUMBER(10,0)",     False),
        "BIGINT":          ("NUMBER(19,0)",     False),
        "SMALLINT":        ("NUMBER(5,0)",      False),
        "TINYINT":         ("NUMBER(3,0)",      False),
        "BIT":             ("NUMBER(1,0)",      False),
        # Ondalikli tipler (precision/scale kaynak tablonun kendisinden gelir)
        "DECIMAL":         ("NUMBER",           True),
        "NUMERIC":         ("NUMBER",           True),
        "FLOAT":           ("FLOAT(126)",       False),
        "REAL":            ("FLOAT(63)",        False),
        "MONEY":           ("NUMBER(19,4)",     False),
        "SMALLMONEY":      ("NUMBER(10,4)",     False),
        # Metin tipleri
        "NVARCHAR":        ("VARCHAR2",         True),   # len*2 (UTF-16→UTF-8)
        "VARCHAR":         ("VARCHAR2",         True),
        "NCHAR":           ("VARCHAR2",         True),   # len*2
        "CHAR":            ("CHAR",             True),
        "TEXT":            ("CLOB",             False),
        "NTEXT":           ("NCLOB",            False),
        "XML":             ("CLOB",             False),
        # Tarih/Zaman tipleri
        "DATETIME":        ("TIMESTAMP(3)",     False),
        "DATETIME2":       ("TIMESTAMP(6)",     False),
        "SMALLDATETIME":   ("DATE",             False),
        "DATE":            ("DATE",             False),
        "TIME":            ("VARCHAR2(16)",     False),
        "DATETIMEOFFSET":  ("TIMESTAMP(6) WITH TIME ZONE", False),
        # Ikili tipler
        "VARBINARY":       ("BLOB",             False),
        "BINARY":          ("RAW",              True),
        "IMAGE":           ("BLOB",             False),
        # Diger
        "UNIQUEIDENTIFIER": ("VARCHAR2(36)",    False),
        "SQL_VARIANT":     ("VARCHAR2(4000)",   False),
        "HIERARCHYID":     ("VARCHAR2(4000)",   False),
        "GEOGRAPHY":       ("CLOB",             False),
        "GEOMETRY":        ("CLOB",             False),
    }

    # ODS katmani icin eklenen standart kolonlar
    ODS_DEFAULT_COLUMNS = [
        ("INSERT_DATE",    "TIMESTAMP DEFAULT SYSTIMESTAMP"),
        ("UPDATE_DATE",    "TIMESTAMP"),
        ("SOURCE_SYSTEM",  f"VARCHAR2(50) DEFAULT '{_SOURCE_SYSTEM_LITERAL}'"),
        ("ETL_BATCH_ID",   "NUMBER"),
    ]

    # DWH boyut tablolari icin SCD2 ek kolonlari
    DWH_SCD2_COLUMNS = [
        ("INSERT_DATE",     "TIMESTAMP DEFAULT SYSTIMESTAMP"),
        ("UPDATE_DATE",     "TIMESTAMP"),
        ("CURRENT_FLAG",    "VARCHAR2(1) DEFAULT 'Y'"),
        ("EFFECTIVE_FROM",  "DATE DEFAULT TRUNC(SYSDATE)"),
        ("EFFECTIVE_TO",    "DATE DEFAULT TO_DATE('9999-12-31','YYYY-MM-DD')"),
        ("SOURCE_SYSTEM",   f"VARCHAR2(50) DEFAULT '{_SOURCE_SYSTEM_LITERAL}'"),
        ("ETL_BATCH_ID",    "NUMBER"),
    ]

    def __init__(self):
        self._batch_id = str(uuid.uuid4())[:8]
        self._log_entries = []
        self._odi_repo_reader = None

    def __enter__(self) -> "MappingGenerator":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    # ── Yardimci Metodlar ──────────────────────────────────────────────────────

    def _add_log(self, step: str, status: str, detail: str = ""):
        """Islem adimini dahili log listesine ekler."""
        detail = redact_sensitive_text(detail, max_len=500)
        entry = {
            "step": step,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now().isoformat(),
        }
        self._log_entries.append(entry)
        log_fn = logger.info if status == "SUCCESS" else logger.warning
        log_fn("MappingGenerator [%s] %s: %s %s", self._batch_id, step, status, detail)

    def _validate_identifier(self, value: str, field_name: str) -> str:
        """SQL/ODI identifier dogrulamasi. Gecersiz karakterleri reddeder.

        Args:
            value: Dogrulanacak deger.
            field_name: Hata mesajinda kullanilacak alan adi.

        Returns:
            Buyuk harfe donusturulmus, gecerli identifier.

        Raises:
            ValueError: Deger bos veya gecersiz karakterler iceriyorsa.
        """
        if not value or not value.strip():
            raise ValueError(f"{field_name} bos olamaz.")
        clean = value.strip().upper()
        if not re.match(r'^[A-Z][A-Z0-9_]{0,127}$', clean):
            raise ValueError(
                f"{field_name} gecersiz: '{clean}'. "
                "Sadece harf, rakam, alt cizgi; harf ile baslamali; max 128 karakter."
            )
        return clean

    def _get_odi_repo_reader(self):
        """Lazy-load the read-only ODI repository metadata reader."""
        if self._odi_repo_reader is None:
            from odi_repository import OdiRepositoryReader
            self._odi_repo_reader = OdiRepositoryReader()
        return self._odi_repo_reader

    @staticmethod
    def _short_table_name(table_name: str | None) -> str:
        text = (table_name or "").strip().upper()
        return text.split(".", 1)[-1] if "." in text else text

    @staticmethod
    def _schema_hint_from_model(model_name: str | None) -> str | None:
        text = (model_name or "").strip().upper()
        for prefix in ("ORACLE_", "SRC_", "TGT_", "MDL_SRC_ORACLE_", "MDL_TGT_ORACLE_"):
            if text.startswith(prefix) and len(text) > len(prefix):
                return text[len(prefix):]
        return None

    def _source_schema_hint(self, params: dict) -> str | None:
        src_table = params.get("src_table")
        if src_table and "." in src_table:
            return src_table.split(".", 1)[0].upper()
        return self._schema_hint_from_model(params.get("src_model"))

    def _target_schema_hint(self, params: dict) -> str | None:
        return (
            (params.get("tgt_owner") or "").strip().upper()
            or self._schema_hint_from_model(params.get("tgt_model"))
            or "DWH"
        )

    @staticmethod
    def _repo_public_fields(row: dict | None) -> dict | None:
        if not row:
            return None
        keys = [
            "model_code", "model_name", "technology", "logical_schema",
            "datastore_name", "resource_name", "score", "ambiguous",
            "candidate_count",
        ]
        return {key: row.get(key) for key in keys if key in row}

    def _resolve_odi_repository_bindings(self, params: dict) -> dict:
        """Resolve ODI model/datastore names from repository metadata tables."""
        reader = self._get_odi_repo_reader()
        src_hint = self._source_schema_hint(params)
        tgt_hint = self._target_schema_hint(params)

        source_ds = reader.resolve_datastore(
            params.get("src_table"),
            schema_hint=src_hint,
            role="source",
            preferred_model=params.get("src_model"),
        )
        source_model = source_ds or reader.resolve_model(
            schema_hint=src_hint,
            role="source",
            preferred_model=params.get("src_model"),
        )

        target_ds = reader.resolve_datastore(
            params.get("tgt_table"),
            schema_hint=tgt_hint,
            role="target",
            preferred_model=params.get("tgt_model"),
        )
        target_model = target_ds or reader.resolve_model(
            schema_hint=tgt_hint,
            role="target",
            preferred_model=params.get("tgt_model"),
        )

        mapping_row = self._find_repository_mapping(params.get("mapping_name"))

        if source_model:
            params["_odi_src_model_code"] = source_model.get("model_code")
        if source_ds:
            params["_odi_src_datastore_name"] = source_ds.get("datastore_name")
            params["_odi_src_datastore_exists"] = True
        else:
            params["_odi_src_datastore_name"] = self._short_table_name(params.get("src_table"))
            params["_odi_src_datastore_exists"] = False

        if target_model:
            params["_odi_tgt_model_code"] = target_model.get("model_code")
        if target_ds:
            params["_odi_tgt_datastore_name"] = target_ds.get("datastore_name")
            params["_odi_tgt_datastore_exists"] = True
        else:
            params["_odi_tgt_datastore_name"] = self._short_table_name(params.get("tgt_table"))
            params["_odi_tgt_datastore_exists"] = False

        status = "passed" if source_model and target_model else "attention_required"
        bindings = {
            "status": status,
            "repository_connection_attempted": True,
            "mapping_exists": mapping_row is not None,
            "mapping": mapping_row,
            "source_datastore_exists": source_ds is not None,
            "target_datastore_exists": target_ds is not None,
            "source": self._repo_public_fields(source_ds or source_model),
            "target": self._repo_public_fields(target_ds or target_model),
        }
        params["_odi_repository"] = bindings
        src_label = bindings["source"].get("model_code") if bindings.get("source") else "unresolved"
        tgt_label = bindings["target"].get("model_code") if bindings.get("target") else "unresolved"
        self._add_log(
            "resolve_odi_repository",
            "SUCCESS" if status == "passed" else "WARNING",
            f"source_model={src_label}, target_model={tgt_label}",
        )
        return bindings

    def _find_repository_mapping(self, mapping_name: str | None) -> dict | None:
        reader = self._get_odi_repo_reader()
        return (
            reader.mapping_exists(mapping_name, project=ODI_PROJECT, folder=ODI_FOLDER)
            or reader.mapping_exists(mapping_name, folder=ODI_FOLDER)
            or reader.mapping_exists(mapping_name)
        )

    def _ensure_odi_repository_bindings(self, params: dict) -> dict:
        if params.get("_odi_repository"):
            return params["_odi_repository"]
        return self._resolve_odi_repository_bindings(params)

    def _resolved_odi_source_model_code(self, params: dict) -> str:
        self._ensure_odi_repository_bindings(params)
        model_code = params.get("_odi_src_model_code")
        if model_code:
            return model_code
        raise RuntimeError("ODI source model could not be resolved from repository tables.")

    def _resolved_odi_target_model_code(self, params: dict) -> str:
        self._ensure_odi_repository_bindings(params)
        model_code = params.get("_odi_tgt_model_code")
        if model_code:
            return model_code
        raise RuntimeError("ODI target model could not be resolved from repository tables.")

    def _check_mapping_exists(self, mapping_name: str) -> tuple[bool, str | None]:
        """ODI work repository'de mapping varligini read-only kontrol eder."""
        try:
            mapping = self._find_repository_mapping(mapping_name)
            if mapping:
                return True, mapping.get("mapping_name") or mapping_name
            return False, None
        except Exception as e:
            logger.warning("Mapping repo tablo varlik kontrolu hatasi: %s", e)

        try:
            from odi_orchestrator import OdiOrchestrator

            probe = OdiOrchestrator().read_only_probe(
                approved=True,
                mapping_name=mapping_name,
            )
            mapping_check = next(
                (check for check in probe.get("checks", []) if check.get("name") == "mapping_found"),
                None,
            )
            if mapping_check and mapping_check.get("status") == "pass":
                return True, mapping_name
            return False, None
        except Exception as e:
            logger.warning("Mapping SDK varlik kontrolu hatasi: %s", e)
            return False, None

    # ── Kaynak Tablo Sema Okuma ──────────────────────────────────────────────

    def _source_schema_df_to_legacy_columns(self, df, connector) -> list[dict]:
        """Connector schema contract'ini eski mapping kolon formatina cevirir."""
        columns: list[dict] = []
        for _, row in df.iterrows():
            data_type = str(row.get("DATA_TYPE") or "VARCHAR2").upper()
            full_type = str(row.get("FULL_DATA_TYPE") or data_type).upper()
            max_len = row.get("DATA_LENGTH")
            precision = row.get("DATA_PRECISION")
            scale = row.get("DATA_SCALE")
            nullable = row.get("NULLABLE") or "YES"
            oracle_type = connector.type_to_oracle(full_type, max_len, precision, scale)
            columns.append({
                "COLUMN_NAME": str(row.get("COLUMN_NAME") or "").upper(),
                "DATA_TYPE": data_type,
                "FULL_DATA_TYPE": full_type,
                "ORACLE_TYPE": oracle_type,
                "MAX_LEN": max_len,
                "IS_NULLABLE": nullable,
                "NUMERIC_PRECISION": precision,
                "NUMERIC_SCALE": scale,
                "COMMENTS": row.get("COMMENTS"),
            })
        return columns

    def _get_source_columns_mssql(self, src_table: str) -> list[dict]:
        """SQL Server'dan kaynak tablo kolonlarini getirir.

        Args:
            src_table: Tablo adi (orn. "Person.Person" veya "Orders").

        Returns:
            Her kolon icin COLUMN_NAME, DATA_TYPE, MAX_LEN, IS_NULLABLE,
            NUMERIC_PRECISION, NUMERIC_SCALE alanlari iceren liste.
        """
        connector = get_source_connector("mssql")
        schema_df = connector.get_table_schema(src_table)
        columns = self._source_schema_df_to_legacy_columns(schema_df, connector)
        full_name = str(src_table).upper()
        return columns, full_name

    def _get_source_columns_oracle(self, src_table: str) -> list[dict]:
        """Oracle'dan kaynak tablo kolonlarini getirir."""
        connector = get_source_connector("oracle")
        schema_df = connector.get_table_schema(src_table)
        return self._source_schema_df_to_legacy_columns(schema_df, connector)

    @staticmethod
    def _positive_int(value) -> int | None:
        """Return a positive int or None for null/NaN/non-numeric metadata."""
        if value is None:
            return None
        try:
            if value != value:
                return None
            number = int(value)
            return number if number > 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _odi_register_column_defs(self, columns: list[dict]) -> list[dict]:
        """Convert connector column metadata to register_datastore.groovy input."""
        if columns is None:
            raise ValueError("columns listesi None")

        col_defs = []
        for col in columns:
            if col is None:
                continue
            col_name = str(col.get("COLUMN_NAME", col.get("column_name", ""))).strip().upper()
            if not col_name:
                continue
            raw_dt = str(col.get("DATA_TYPE", col.get("data_type", "VARCHAR2"))).strip().upper()
            dt = re.split(r'[\(\s]', raw_dt)[0].strip()
            max_len = self._positive_int(
                col.get("MAX_LEN") or col.get("CHARACTER_MAXIMUM_LENGTH") or col.get("max_len")
            )
            prec = self._positive_int(col.get("NUMERIC_PRECISION") or col.get("precision"))
            scale = self._positive_int(col.get("NUMERIC_SCALE") or col.get("scale"))

            col_def = {"name": col_name, "type": dt}
            if max_len:
                col_def["length"] = max_len
            elif prec:
                col_def["length"] = prec
            if scale:
                col_def["scale"] = scale
            col_defs.append(col_def)
        return col_defs

    def _convert_mssql_type(self, data_type: str, max_len: int | None,
                             precision: int | None, scale: int | None) -> str:
        """SQL Server veri tipini Oracle eşdeğerine dönüştürür.

        Args:
            data_type: SQL Server veri tipi (orn. "nvarchar", "int").
            max_len: CHARACTER_MAXIMUM_LENGTH (metin tipleri icin).
            precision: NUMERIC_PRECISION (sayi tipleri icin).
            scale: NUMERIC_SCALE (sayi tipleri icin).

        Returns:
            Oracle DDL kolon tip string'i (orn. "VARCHAR2(200)", "NUMBER(10,0)").
        """
        dt_upper = data_type.strip().upper()
        entry = self.MSSQL_TO_ORACLE.get(dt_upper)

        if entry is None:
            # Bilinmeyen tip — VARCHAR2(4000) fallback
            logger.warning("Bilinmeyen SQL Server tipi: %s → VARCHAR2(4000)", data_type)
            return "VARCHAR2(4000)"

        oracle_type, needs_length = entry

        if not needs_length:
            return oracle_type

        # Uzunluk gerektiren tipler
        if dt_upper in ("DECIMAL", "NUMERIC"):
            p = int(precision) if precision else 38
            s = int(scale) if scale is not None else 0
            return f"NUMBER({p},{s})"

        if dt_upper in ("NVARCHAR", "NCHAR"):
            # NVARCHAR(MAX) → CLOB
            if max_len is None or max_len < 0:
                return "CLOB"
            ora_len = min(int(max_len) * 2, 4000)
            return f"VARCHAR2({ora_len})"

        if dt_upper == "VARCHAR":
            if max_len is None or max_len < 0:
                return "CLOB"
            ora_len = min(int(max_len), 4000)
            return f"VARCHAR2({ora_len})"

        if dt_upper == "CHAR":
            length = min(int(max_len or 1), 2000)
            return f"CHAR({length})"

        if dt_upper == "BINARY":
            length = min(int(max_len or 1), 2000)
            return f"RAW({length})"

        return oracle_type

    def _build_target_column_plan(self, params: dict) -> dict:
        """Build the expected target table columns without changing the database."""
        src_model = params["src_model"]
        src_table = params["src_table"]
        tgt_model = params["tgt_model"]
        _src_upper = src_model.upper()
        _is_mssql = ("SQL_SERVER" in _src_upper or "MSSQL" in _src_upper
                     or "ADVENTUREWORK" in _src_upper)

        try:
            if _is_mssql:
                src_columns, full_src_name = self._get_source_columns_mssql(src_table)
                is_mssql_source = True
                params["src_table"] = full_src_name
            else:
                src_columns = self._get_source_columns_oracle(src_table)
                is_mssql_source = False
        except Exception as e:
            self._add_log("target_preflight", "FAILED", f"Kaynak sema alinamadi: {e}")
            raise RuntimeError(f"Kaynak tablo semasi alinamadi ({src_model}.{src_table}): {e}") from e

        if src_columns is None:
            self._add_log("target_preflight", "FAILED", "src_columns None dondu")
            raise RuntimeError("Kaynak kolon listesi alinamadi (None)")

        if not src_columns:
            self._add_log("target_preflight", "FAILED",
                           f"Kaynak tabloda kolon bulunamadi: {params.get('src_table')}")
            raise RuntimeError(f"Kaynak tabloda kolon bulunamadi: {src_model}.{params.get('src_table')}")

        col_defs: list[str] = []
        desired_col_defs: list[tuple[str, str]] = []
        existing_col_names: set[str] = set()

        for col in src_columns:
            if col is None:
                continue
            col_name = str(col.get("COLUMN_NAME", "")).strip().upper()
            if not col_name:
                continue
            existing_col_names.add(col_name)

            ora_type = col.get("ORACLE_TYPE")
            if ora_type:
                pass
            elif is_mssql_source:
                ora_type = self._convert_mssql_type(
                    data_type=str(col.get("DATA_TYPE", "VARCHAR")),
                    max_len=col.get("MAX_LEN"),
                    precision=col.get("NUMERIC_PRECISION"),
                    scale=col.get("NUMERIC_SCALE"),
                )
            else:
                dt = str(col.get("DATA_TYPE", "VARCHAR2"))
                ml = col.get("MAX_LEN")
                prec = col.get("NUMERIC_PRECISION")
                sc = col.get("NUMERIC_SCALE")
                if dt.upper() in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
                    ora_type = f"{dt}({ml})" if ml else dt
                elif dt.upper() == "NUMBER" and prec:
                    ora_type = f"NUMBER({int(prec)},{int(sc or 0)})"
                else:
                    ora_type = dt

            nullable = col.get("IS_NULLABLE", "YES")
            not_null = "" if nullable in ("YES", "Y", True) else " NOT NULL"
            col_defs.append(f'    "{col_name}"  {ora_type}{not_null}')
            desired_col_defs.append((col_name, ora_type))

        default_cols = self.DWH_SCD2_COLUMNS if "DWH" in tgt_model.upper() else self.ODS_DEFAULT_COLUMNS
        defaults_added = []
        for def_name, def_type in default_cols:
            if def_name not in existing_col_names:
                col_defs.append(f'    "{def_name}"  {def_type}')
                desired_col_defs.append((def_name, def_type))
                defaults_added.append(def_name)

        return {
            "src_columns": src_columns,
            "col_defs": col_defs,
            "desired_col_defs": desired_col_defs,
            "defaults_added": defaults_added,
        }

    def target_table_preflight(self, params: dict) -> dict:
        """Read-only target table preflight for mapping creation."""
        validated = self.validate_params(params)
        plan = self._build_target_column_plan(validated)
        tgt_owner = validated["tgt_owner"]
        tgt_table = validated["tgt_table"]
        expected_columns = [name for name, _type_sql in plan["desired_col_defs"]]

        from db import get_connection
        conn = get_connection(use_ddl=True)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER=:o AND TABLE_NAME=:t",
                {"o": tgt_owner.upper(), "t": tgt_table.upper()},
            )
            exists = cursor.fetchone()[0] > 0

            target_columns: list[str] = []
            missing_columns: list[str] = []
            extra_columns: list[str] = []
            has_rows = None
            row_state = "not_created"
            row_probe_error = None

            if exists:
                cursor.execute("""
                    SELECT COLUMN_NAME
                    FROM ALL_TAB_COLUMNS
                    WHERE OWNER = :owner AND TABLE_NAME = :tname
                    ORDER BY COLUMN_ID
                """, {"owner": tgt_owner.upper(), "tname": tgt_table.upper()})
                target_columns = [str(row[0]).upper() for row in cursor.fetchall()]
                target_set = set(target_columns)
                expected_set = set(expected_columns)
                missing_columns = [name for name in expected_columns if name not in target_set]
                extra_columns = [name for name in target_columns if name not in expected_set]

                try:
                    cursor.execute(f"SELECT 1 FROM {tgt_owner}.{tgt_table} WHERE ROWNUM = 1")
                    has_rows = cursor.fetchone() is not None
                    row_state = "has_rows" if has_rows else "empty"
                except Exception as exc:
                    row_state = "unknown"
                    row_probe_error = str(exc)

            metadata_aligned = exists and not missing_columns
            metadata_exact = metadata_aligned and not extra_columns
            if not exists:
                recommended_action = "create"
            elif metadata_exact:
                recommended_action = "reuse"
            elif missing_columns:
                recommended_action = "reconcile"
            else:
                recommended_action = "reuse_with_extra_columns"

            repository = {
                "status": "not_checked",
                "mapping_exists": None,
                "source_datastore_exists": None,
                "target_datastore_exists": None,
            }
            try:
                repository = self._resolve_odi_repository_bindings(validated)
            except Exception as exc:
                repository = {
                    "status": "unavailable",
                    "mapping_exists": None,
                    "source_datastore_exists": None,
                    "target_datastore_exists": None,
                    "repository_connection_attempted": True,
                    "error": str(exc).split("\n")[0],
                }

            return {
                "status": "OK",
                "target": f"{tgt_owner}.{tgt_table}",
                "exists": exists,
                "row_state": row_state,
                "has_rows": has_rows,
                "row_probe_error": row_probe_error,
                "expected_columns_count": len(expected_columns),
                "target_columns_count": len(target_columns),
                "missing_columns": missing_columns,
                "extra_columns": extra_columns,
                "metadata_aligned": metadata_aligned,
                "metadata_exact": metadata_exact,
                "recommended_action": recommended_action,
                "repository": repository,
            }
        finally:
            cursor.close()
            conn.close()

    # ── Hedef Tablo Olusturma ─────────────────────────────────────────────────


    def _create_target_table(self, params: dict) -> dict:
        """Build a reviewed column plan, then apply it through the persistent target service."""
        from target_table_service import execute_target_table_plan

        logger.info("Creating target table from validated mapping parameters")
        try:
            plan = self._build_target_column_plan(params)
            return execute_target_table_plan(self, params, plan, ORACLE_DDL_USER or "APP_USER")
        except MappingGenerationError:
            raise
        except Exception as exc:
            self._add_log("create_target_table", "FAILED", type(exc).__name__)
            raise MappingGenerationError("Target table operation failed") from exc

    # ── AI Prompt Parsing ──────────────────────────────────────────────────────

    def parse_prompt(self, user_prompt: str) -> dict:
        """Dogal dil prompt'unu yapisal mapping parametrelerine donusturur.

        Args:
            user_prompt: Kullanicinin dogal dil istegi
                (orn. "Orders tablosunu SQL Server'dan ODS'ye aktar").

        Returns:
            Sozluk: mapping_name, src_model, src_table, tgt_model, tgt_table,
            tgt_owner, needs_skey, skey_natural_key, description alanlari.

        Raises:
            ValueError: AI ciktisi parse edilemezse veya gerekli alanlar eksikse.
        """
        from ai_agent import call_llm, extract_json

        messages = [
            {"role": "system", "content": MAPPING_EXTRACT_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        raw = call_llm(messages)

        parsed = extract_json(raw)

        # Zorunlu alanlari kontrol et
        required = ["mapping_name", "src_table", "tgt_table", "src_model", "tgt_model"]
        missing = [f for f in required if not parsed.get(f)]
        if missing:
            raise ValueError(f"AI eksik alan uretti: {', '.join(missing)}. Ham cikti: {raw[:200]}")

        self._add_log("prompt_parse", "SUCCESS", f"mapping={parsed.get('mapping_name')}")
        return parsed

    # ── Parametre Dogrulama ────────────────────────────────────────────────────

    def validate_params(self, params: dict) -> dict:
        """Mapping parametrelerini dogrular ve normalize eder.

        Raises:
            ValueError: Gecersiz veya eksik parametre varsa.
        """
        if params is None:
            logger.error("validate_params'a None degeri geldi!")
            raise ValueError("Parametreler bos (None) olamaz.")

        validated = {}

        validated["mapping_name"] = self._validate_identifier(
            params.get("mapping_name", ""), "mapping_name"
        )
        validated["src_model"] = params.get("src_model", "ORACLE_SOURCE").strip().upper()
        validated["drop_target_if_exists"] = params.get("drop_target_if_exists") is True
        validated["replace_existing_mapping"] = params.get("replace_existing_mapping") is True

        # src_table: SQL Server tablolari "Schema.Table" formatinda olabilir
        src_schema = (params.get("src_schema") or "").strip()
        raw_src_value = (params.get("src_table") or "").strip()
        if src_schema and raw_src_value and "." not in raw_src_value:
            raw_src_value = f"{src_schema}.{raw_src_value}"
        raw_src = raw_src_value.upper()
        if not raw_src:
            raise ValueError("src_table bos olamaz.")
            
        # AI bazen tablo adlarini DWH.ODS_ seklinde veriyor, sema adini es gec:
        src_table_no_schema = raw_src.split('.')[-1]
            
        # AI bazen ODS_ veya DWH_ tablolarini SQL_SERVER saniyor. Guvenlik agi:
        if src_table_no_schema.startswith("ODS_"):
            validated["src_model"] = "ORACLE_ODS"
        elif src_table_no_schema.startswith("DWH_"):
            validated["src_model"] = "ORACLE_DWH"

        source_schema_models = {f"ORACLE_{s}" for s in SOURCE_ORACLE_SCHEMAS}
        allowed_src_models = {
            "ORACLE_SOURCE", "ORACLE_ODS", "ORACLE_DWH", "DWH",
            "SQL_SERVER", "MSSQL", "ADVENTUREWORK_PRODUCTION",
            "SRC_HR", "SRC_SH", "TGT_DWH", "MDL_SRC_ORACLE_HR",
            "MDL_SRC_ORACLE_SH", "MDL_TGT_ORACLE_DWH",
        } | source_schema_models
        if validated["src_model"] not in allowed_src_models:
            raise ValueError(
                f"Gecersiz src_model: {validated['src_model']}. "
                f"Izin verilenler: {sorted(allowed_src_models)}"
            )

        # Kaynak modeli "SCHEMA.TABLE" formatini destekler mi?
        # - Disa donuk kaynaklar (MSSQL ve Oracle source: ORACLE_HR/ORACLE_SH/ORACLE_SOURCE)
        #   schema-qualified gelir; schema KORUNMALI (HR.EMPLOYEES -> HR.EMPLOYEES).
        # - Oracle internal kaynak-hedef (ORACLE_ODS / ORACLE_DWH) tek tablo adi kullanir.
        _internal_oracle = validated["src_model"] in ("ORACLE_ODS", "ORACLE_DWH")
        if not _internal_oracle:
            if not re.match(r'^[A-Z][A-Z0-9_]*(\.[A-Z][A-Z0-9_]*)?$', raw_src):
                raise ValueError(
                    f"src_table gecersiz: '{raw_src}'. "
                    "Format: TABLE veya SCHEMA.TABLE (harf, rakam, alt cizgi)."
                )
            validated["src_table"] = raw_src
        else:
            validated["src_table"] = self._validate_identifier(src_table_no_schema, "src_table")
            
        raw_tgt = (params.get("tgt_table") or "").strip().upper()
        tgt_table_no_schema = raw_tgt.split('.')[-1]
        validated["tgt_model"] = params.get("tgt_model", "ORACLE_ODS").strip().upper()
        if tgt_table_no_schema.startswith("ODS_"):
            validated["tgt_model"] = "ORACLE_ODS"
        elif tgt_table_no_schema.startswith("DWH_") or tgt_table_no_schema.startswith("SKEY_"):
            validated["tgt_model"] = "ORACLE_DWH"

        allowed_tgt_models = {"DWH", "ORACLE_ODS", "ORACLE_DWH", "ODS", "TGT_DWH", "MDL_TGT_ORACLE_DWH"}
        if validated["tgt_model"] not in allowed_tgt_models:
            raise ValueError(
                f"Gecersiz tgt_model: {validated['tgt_model']}. "
                f"Izin verilenler: {sorted(allowed_tgt_models)}"
            )
        
        # AI SKEY_MAP_ tablosunu hedef secebiliyor, bu yanlistir (skey_manager otomatik yapar). Hedef SKEY_ boyut tablosu olmali.
        if tgt_table_no_schema.startswith("SKEY_MAP_"):
            tgt_table_no_schema = tgt_table_no_schema.replace("SKEY_MAP_", "SKEY_")
            
        validated["tgt_table"] = self._validate_identifier(tgt_table_no_schema, "tgt_table")
        validated["tgt_owner"] = params.get("tgt_owner", "DWH").strip().upper()

        # Owner whitelist kontrolu
        allowed = {"DWH", "ODS", "AUDIT_USER", "DWH_FLAT"}
        if validated["tgt_owner"] not in allowed:
            raise ValueError(f"Gecersiz tgt_owner: {validated['tgt_owner']}. Izin verilenler: {allowed}")

        validated["needs_skey"] = params.get("needs_skey") is True
        validated["skey_natural_key"] = params.get("skey_natural_key") or None
        validated["description"] = params.get("description", "")

        if validated["needs_skey"] and not validated["skey_natural_key"]:
            raise ValueError("SKEY istendi ama skey_natural_key belirtilmedi.")

        self._add_log("validate_params", "SUCCESS",
                       f"{validated['src_table']} -> {validated['tgt_table']}")
        return validated

    # ── ODI Datastore Kayit ──────────────────────────────────────────────────────

    def _register_odi_datastores(self, params: dict) -> dict:
        """Kaynak ve hedef tablolari ODI modeline datastore olarak kaydeder.

        create_mapping.groovy, datastorelari dsFinder.findByName() ile arar.
        Eger datastore ODI'de kayitli degilse mapping olusturulamaz.
        Bu adim register_datastore.groovy kullanarak eksik datastore'lari kaydeder.

        Args:
            params: Dogrulanmis parametre sozlugu.

        Returns:
            Sozluk: src_result, tgt_result.
        """
        from odi_orchestrator import OdiOrchestrator
        orch = OdiOrchestrator()
        self._ensure_odi_repository_bindings(params)

        results = {"src_result": None, "tgt_result": None}

        # ── Kaynak datastore kaydi ──
        src_model = params["src_model"]
        src_table = params["src_table"]
        # SQL Server tablosu: Schema.Table formatindan tablo adini cikar
        if "." in src_table:
            parts = src_table.split(".", 1)
            src_ds_name = parts[1]
        else:
            src_ds_name = src_table

        # Kaynak kolon bilgilerini cek
        _src_upper_reg = src_model.upper()
        _is_mssql_reg = ("SQL_SERVER" in _src_upper_reg or "MSSQL" in _src_upper_reg
                         or "ADVENTUREWORK" in _src_upper_reg)
        try:
            if _is_mssql_reg:
                src_columns, _ = self._get_source_columns_mssql(src_table)
            else:
                src_columns = self._get_source_columns_oracle(src_table)

            src_col_defs = self._odi_register_column_defs(src_columns)

            # ODI model/datastore adini repository tablolarindan belirle.
            odi_src_model_code = self._resolved_odi_source_model_code(params)
            src_ds_name = params.get("_odi_src_datastore_name") or src_ds_name

            if params.get("_odi_src_datastore_exists"):
                results["src_result"] = {
                    "skipped": True,
                    "reason": "already_exists",
                    "model_code": odi_src_model_code,
                    "datastore": src_ds_name,
                }
                self._add_log("register_datastores", "SKIPPED",
                              f"Kaynak datastore mevcut: {odi_src_model_code}.{src_ds_name}")
            else:
                src_result = orch.register_datastore(
                    model_code=odi_src_model_code,
                    table_name=src_ds_name,
                    columns=src_col_defs,
                    resource_name=src_ds_name,
                )
                results["src_result"] = src_result

                if "Hata:" in src_result and "zaten mevcut" not in src_result.lower():
                    logger.warning("Kaynak datastore kaydi uyarisi: %s", src_result)
                else:
                    self._add_log("register_datastores", "SUCCESS",
                                   f"Kaynak: {odi_src_model_code}.{src_ds_name}")
        except Exception as e:
            logger.warning("Kaynak datastore kaydi basarisiz (devam): %s", e)
            results["src_result"] = f"WARNING: {e}"

        # ── Hedef datastore kaydi ──
        tgt_model = params["tgt_model"]
        tgt_table = params["tgt_table"]
        tgt_owner = params["tgt_owner"]

        try:
            # Hedef Oracle kolonlarini oku
            tgt_columns = self._get_source_columns_oracle(f"{tgt_owner}.{tgt_table}")
            tgt_col_defs = self._odi_register_column_defs(tgt_columns)

            # ODI model/datastore adini repository tablolarindan belirle.
            odi_tgt_model_code = self._resolved_odi_target_model_code(params)
            tgt_ds_name = params.get("_odi_tgt_datastore_name") or tgt_table

            if params.get("_odi_tgt_datastore_exists"):
                results["tgt_result"] = {
                    "skipped": True,
                    "reason": "already_exists",
                    "model_code": odi_tgt_model_code,
                    "datastore": tgt_ds_name,
                }
                self._add_log("register_datastores", "SKIPPED",
                              f"Hedef datastore mevcut: {odi_tgt_model_code}.{tgt_ds_name}")
            else:
                tgt_result = orch.register_datastore(
                    model_code=odi_tgt_model_code,
                    table_name=tgt_ds_name,
                    columns=tgt_col_defs,
                    resource_name=tgt_ds_name,
                )
                results["tgt_result"] = tgt_result

                if "Hata:" in tgt_result and "zaten mevcut" not in tgt_result.lower():
                    logger.warning("Hedef datastore kaydi uyarisi: %s", tgt_result)
                else:
                    self._add_log("register_datastores", "SUCCESS",
                                   f"Hedef: {odi_tgt_model_code}.{tgt_ds_name}")
        except Exception as e:
            logger.warning("Hedef datastore kaydi basarisiz (devam): %s", e)
            results["tgt_result"] = f"WARNING: {e}"

        return results

    # ── ODI Mapping Olusturma ──────────────────────────────────────────────────

    def _create_odi_mapping(self, params: dict) -> dict:
        """OdiOrchestrator uzerinden ODI mapping olusturur.

        Args:
            params: Dogrulanmis parametre sozlugu.

        Returns:
            Sozluk: odi_result (komut ciktisi) ve sequence_name.
        """
        from odi_orchestrator import OdiOrchestrator

        seq_name = f"SEQ_{params['tgt_table']}" if params["needs_skey"] else "NONE"

        self._ensure_odi_repository_bindings(params)
        if params.get("_odi_repository", {}).get("mapping_exists") and not params.get("replace_existing_mapping"):
            raise RuntimeError(
                "ODI mapping already exists. Use update/repair or explicitly approve replacement."
            )
        odi_src_model = self._resolved_odi_source_model_code(params)
        odi_tgt_model = self._resolved_odi_target_model_code(params)
        src_table = params.get("_odi_src_datastore_name") or self._short_table_name(params["src_table"])
        tgt_table = params.get("_odi_tgt_datastore_name") or params["tgt_table"]

        orch = OdiOrchestrator()
        
        # SKEY Tablolarinda NK ve SK kolonlari icin ozel expressions 
        expressions = {}
        ikm_override = None

        if params.get("needs_skey") and params.get("skey_natural_key"):
            nk_src = params["skey_natural_key"].strip()
            if not nk_src.upper().startswith("SRC."):
                nk_src = f"SRC.{nk_src}"
            
            # 1. Hedef SKEY sözlük tablosuysa: ODI Best Practice = SEQ.NEXTVAL + IKM Merge + UpdateKey(NATURAL_KEY)
            if params["tgt_table"].startswith("SKEY_") or params["tgt_table"].startswith("SKEY_MAP_"):
                # Sequence adını oluştur: SKEY_SALES_CUSTOMER → SEQ_SALES_CUSTOMER
                seq_suffix = params["tgt_table"].replace("SKEY_MAP_", "").replace("SKEY_", "")
                expressions["SKEY"] = f"{params['tgt_owner']}.SEQ_{seq_suffix}.NEXTVAL"
                expressions["NATURAL_KEY"] = f"TO_CHAR({nk_src})"
                # NOT EXISTS filter YOK — IKM Merge zaten UpdateKey(NATURAL_KEY) üzerinden MERGE INTO yazar
                # (ORA-00001 tamamen önlenir, duplicate kaydı insert etmez, var olanı günceller)
                ikm_override = "IKM Oracle Incremental Update (MERGE)"
                logger.info(
                    "SKEY sozlugu mapping (Best Practice): SEQ.NEXTVAL + IKM=%s, filter yok.", ikm_override
                )
                
            # 2. Gerçek boyut/fact tablosuysa: CASE WHEN EXISTS (SKEY lookup)
            else:
                from skey_manager import SkeyManager
                with SkeyManager() as sm:
                    skey_expr = sm.get_skey_expression(
                        owner=params["tgt_owner"],
                        table=params["tgt_table"],
                        nk_expr=f"TO_CHAR({nk_src})"
                    )
                expressions["SKEY"] = skey_expr
                expressions[f"SK_{params['tgt_table']}"] = skey_expr
                expressions[f"{params['tgt_table']}_SK"] = skey_expr
                expressions["NATURAL_KEY"] = nk_src
                expressions["NK"] = nk_src
                expressions[f"NK_{params['tgt_table']}"] = nk_src
                expressions[f"{params['tgt_table']}_NK"] = nk_src

        result = orch.generate_mapping(
            mapping_name=params["mapping_name"],
            src_model=odi_src_model,
            src_table=src_table,
            tgt_model=odi_tgt_model,
            tgt_table=tgt_table,
            sequence_name=seq_name,
            expressions=expressions,
            replace_existing_mapping=params.get("replace_existing_mapping") is True,
            filter_condition=None,          # NOT EXISTS YOK — IKM Merge halleder
            ikm_name=ikm_override,           # SKEY sözlükleri için override IKM
        )

        # OdiOrchestrator stub modda "KOMUT HAZIRLANDI: ..." doner
        if "Hata:" in result:
            self._add_log("create_mapping", "FAILED", result)
            raise RuntimeError(f"ODI mapping olusturulamadi: {result}")

        self._add_log("create_mapping", "SUCCESS", result[:100])
        return {
            "odi_result": result,
            "sequence_name": seq_name,
            "source_model": odi_src_model,
            "source_datastore": src_table,
            "target_model": odi_tgt_model,
            "target_datastore": tgt_table,
        }

    # ── SKEY Olusturma ─────────────────────────────────────────────────────────

    def _create_skey(self, params: dict) -> dict:
        """Surrogate Key nesnelerini olusturur (tablo + sequence + expression).

        Args:
            params: Dogrulanmis parametre sozlugu (needs_skey=True olmali).

        Returns:
            Sozluk: skey_result (create sonucu) ve skey_expression.
        """
        from skey_manager import SkeyManager

        with SkeyManager() as sm:
            skey_result = sm.create_skey_objects(
                owner=params["tgt_owner"],
                table=params["tgt_table"],
            )
            skey_expr = sm.get_skey_expression(
                owner=params["tgt_owner"],
                table=params["tgt_table"],
                nk_expr=params["skey_natural_key"],
            )

        if skey_result.get("errors"):
            self._add_log("create_skey", "PARTIAL",
                           f"Errors: {skey_result['errors']}")
        else:
            self._add_log("create_skey", "SUCCESS",
                           f"Created: {skey_result.get('created', [])}, "
                           f"Skipped: {skey_result.get('skipped', [])}")

        return {
            "skey_result": skey_result,
            "skey_expression": skey_expr,
        }

    # ── Lineage Kaydi ──────────────────────────────────────────────────────────

    def _populate_lineage(self, params: dict) -> dict:
        """Mapping icin DATA_LINEAGE kaydini olusturur.

        Args:
            params: Dogrulanmis parametre sozlugu.

        Returns:
            Sozluk: lineage_result.
        """
        from lineage_manager import LineageManager

        with LineageManager() as lm:
            try:
                lineage_result = lm.populate_from_odi()
                self._add_log("populate_lineage", "SUCCESS",
                               f"Inserted: {lineage_result.get('inserted', 0)}")
                return {"lineage_result": lineage_result}
            except Exception as e:
                # Lineage populate hatasi kritik degil, devam et
                self._add_log("populate_lineage", "SKIPPED", str(e))
                return {"lineage_result": {"error": str(e), "inserted": 0}}

    # ── DQ Test Uretimi ────────────────────────────────────────────────────────

    def _generate_tests(self, params: dict) -> dict:
        """Hedef tablo icin DQ testleri uretir ve RAG'a kaydeder.

        Args:
            params: Dogrulanmis parametre sozlugu.

        Returns:
            Sozluk: tests listesi.
        """
        from test_generator import TestGenerator

        tg = TestGenerator()
        try:
            tests = tg.generate_and_store_in_rag(
                owner=params["tgt_owner"],
                table_name=params["tgt_table"],
            )
            self._add_log("generate_tests", "SUCCESS", f"{len(tests)} test uretildi")
            return {"tests": tests}
        except Exception as e:
            # Test uretimi kritik degil
            self._add_log("generate_tests", "SKIPPED", str(e))
            return {"tests": [], "error": str(e)}
        finally:
            tg.close()

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def _take_snapshot(self, params: dict) -> dict:
        """Mapping'in ilk snapshot'ini alir.

        Args:
            params: Dogrulanmis parametre sozlugu.

        Returns:
            Sozluk: snap_id.
        """
        from mapping_snapshot_manager import MappingSnapshotManager

        snapshot_data = [
            {
                "mapping_name": params["mapping_name"],
                "src_model": params["src_model"],
                "src_table": params["src_table"],
                "tgt_model": params["tgt_model"],
                "tgt_table": params["tgt_table"],
                "tgt_owner": params["tgt_owner"],
                "needs_skey": params["needs_skey"],
                "created_by": "MappingGenerator",
                "batch_id": self._batch_id,
            }
        ]

        try:
            with MappingSnapshotManager() as msm:
                msm.ensure_table()
                snap_id = msm.take_snapshot(
                    mapping_name=params["mapping_name"],
                    mapping_data=snapshot_data,
                    label=f"Auto-generated by MappingGenerator (batch {self._batch_id})",
                )
                self._add_log("take_snapshot", "SUCCESS", f"snap_id={snap_id}")
                return {"snap_id": snap_id}
        except Exception as e:
            self._add_log("take_snapshot", "SKIPPED", str(e))
            return {"snap_id": None, "error": str(e)}

    # ── MAPPING_LOG Kaydi ──────────────────────────────────────────────────────

    def _log_to_mapping_log(self, params: dict, status: str = "SUCCESS",
                             error_message: str = None):
        """Sonucu AUDIT_USER.MAPPING_LOG'a kaydeder.

        Args:
            params: Dogrulanmis parametre sozlugu.
            status: "SUCCESS" veya "FAILED".
            error_message: Hata mesaji (opsiyonel).
        """
        from skey_manager import SkeyManager

        try:
            audit_metadata = {
                "mapping_name": params.get("mapping_name", "UNKNOWN"),
                "tgt_owner": params.get("tgt_owner", "DWH"),
            }
            with SkeyManager() as sm:
                sm._log_operation(
                    conn=sm._get_conn(),
                    batch_id=self._batch_id,
                    operation="GENERATE_MAPPING",
                    object_owner=params.get("tgt_owner", "DWH"),
                    object_name=params.get("mapping_name", "UNKNOWN"),
                    ddl_text=json.dumps(audit_metadata, ensure_ascii=False),
                    status=status,
                    error_message="Mapping operation failed." if error_message else None,
                )
        except Exception as e:
            logger.error("MAPPING_LOG write failed: %s", type(e).__name__)

    # ── Ana Orkestrasyon ───────────────────────────────────────────────────────

    def generate_from_prompt(self, user_prompt: str) -> dict:
        """Dogal dil prompt'undan uctan uca mapping olusturur.

        Bu metod tum adimlari sirayla calistirir:
          1. AI ile prompt parse
          2. Parametre dogrulama
          3. Hedef tablo olusturma (tip donusumu + ODS default kolonlar)
          4. ODI mapping olusturma
          5. SKEY olusturma (opsiyonel)
          6. Lineage kaydi
          7. DQ test uretimi
          8. Snapshot

        Args:
            user_prompt: Kullanicinin dogal dil istegi.

        Returns:
            Sozluk: batch_id, params, steps (her adimin sonucu),
            overall_status ve log.
        """
        # Adim 1: Prompt parse
        params = self.parse_prompt(user_prompt)
        return self.generate_from_params(params)

    def generate_from_params(self, params: dict) -> dict:
        """Yapisal parametrelerden uctan uca mapping olusturur.

        Args:
            params: Mapping parametreleri (parse_prompt ciktisi veya
                kullanici tarafindan saglanmis).

        Returns:
            Sozluk: batch_id, params, steps (her adimin sonucu),
            overall_status ve log.
        """
        result = {
            "batch_id": self._batch_id,
            "params": None,
            "steps": {},
            "overall_status": "SUCCESS",
            "log": [],
        }

        try:
            # Adim 2: Dogrulama
            validated = self.validate_params(params)
            result["params"] = validated

            # Adim 3: Hedef tablo olustur (SQL Server → Oracle tip donusumu + ODS default kolonlar)
            tgt_result = self._create_target_table(validated)
            result["steps"]["create_target_table"] = tgt_result

            repo_result = self._resolve_odi_repository_bindings(validated)
            result["steps"]["resolve_odi_repository"] = repo_result

            # Adim 4: ODI Datastore kaydi (kaynak + hedef)
            ds_result = self._register_odi_datastores(validated)
            result["steps"]["register_datastores"] = ds_result

            # Adim 5: ODI Mapping
            odi_result = self._create_odi_mapping(validated)
            result["steps"]["create_mapping"] = odi_result

            # Adim 6: SKEY (opsiyonel)
            if validated["needs_skey"]:
                skey_result = self._create_skey(validated)
                result["steps"]["create_skey"] = skey_result
            else:
                self._add_log("create_skey", "SKIPPED", "SKEY istenmedi")
                result["steps"]["create_skey"] = {"skipped": True}

            # Adim 6: Lineage
            lineage_result = self._populate_lineage(validated)
            result["steps"]["populate_lineage"] = lineage_result

            # Adim 7: DQ Testleri
            test_result = self._generate_tests(validated)
            result["steps"]["generate_tests"] = test_result

            # Adim 8: Snapshot
            snap_result = self._take_snapshot(validated)
            result["steps"]["take_snapshot"] = snap_result

            # MAPPING_LOG'a basarili kayit
            self._log_to_mapping_log(validated, "SUCCESS")

        except (ValueError, RuntimeError) as e:
            result["overall_status"] = "FAILED"
            result["error"] = str(e)
            self._add_log("pipeline", "FAILED", str(e))

            # MAPPING_LOG'a hatali kayit
            if result.get("params"):
                self._log_to_mapping_log(result["params"], "FAILED", str(e))

        except Exception as e:
            result["overall_status"] = "FAILED"
            result["error"] = f"Beklenmeyen hata: {str(e)}"
            self._add_log("pipeline", "FAILED", str(e))
            logger.error("MappingGenerator beklenmeyen hata: %s", e, exc_info=True)

        result["log"] = self._log_entries
        return result

    # ── Mapping Guncelleme ───────────────────────────────────────────────────

    def update_mapping(self, mapping_name: str, changes: dict) -> dict:
        """Mevcut bir ODI mapping'i gunceller.

        Guncelleme adimlari:
          1. Mevcut mapping'in snapshot'ini al (onceki durum)
          2. ODI mapping guncelle (OdiOrchestrator)
          3. SKEY guncelle (gerekirse)
          4. Lineage yenile
          5. DQ testleri yeniden uret
          6. Yeni snapshot al (sonraki durum)

        Args:
            mapping_name: Guncellenecek mapping adi.
            changes: Degisiklik sozlugu. Desteklenen anahtarlar:
                - expressions: dict — Kolon expression guncelleme (orn: {"NAMEE": "Product.Name"})
                - filter_action: str — Filter islem tipi (add/modify/remove)
                - filter_condition: str — Filter kosulu
                - filter_name: str — Mevcut filter adi
                - ikm_name: str — IKM degistirme
                - lkm_name: str — LKM degistirme
                - new_mapping_name: Mapping rename
                - tgt_table, tgt_owner: Pre-validation icin hedef tablo bilgisi
                - needs_skey, skey_natural_key: SKEY degisikligi

        Returns:
            Sozluk: batch_id, mapping_name, changes_applied, steps, log.
        """
        result = {
            "batch_id": self._batch_id,
            "mapping_name": mapping_name,
            "operation": "UPDATE",
            "changes_applied": changes,
            "steps": {},
            "overall_status": "SUCCESS",
            "log": [],
        }

        try:
            mapping_name = self._validate_identifier(mapping_name, "mapping_name")

            # Mapping var mi kontrol et, yoksa en benzerini bul
            exists, suggested = self._check_mapping_exists(mapping_name)
            if not exists:
                if suggested:
                    err_msg = f"Mapping found: {mapping_name} bulunamadı. En benzer mapping bulundu: {suggested}. Lütfen bunu kullanın."
                    self._add_log("check_mapping", "FAILED", err_msg)
                    raise ValueError(err_msg)
                else:
                    err_msg = f"Mapping not found: {mapping_name} bulunamadı ve benzer bir eşleşme de bulunamadı."
                    self._add_log("check_mapping", "FAILED", err_msg)
                    raise ValueError(err_msg)

            # Adim 1: Onceki snapshot (degisiklik oncesi durum)
            pre_snap = self._take_snapshot_for_update(mapping_name, "PRE_UPDATE")
            result["steps"]["pre_snapshot"] = pre_snap

            # Adim 2: ODI mapping guncelle
            update_params = {"mapping_name": mapping_name}
            update_params.update(changes)

            # Degisikliklerde yeni tablo adlari varsa dogrula
            for field in ["src_table", "tgt_table"]:
                if field in changes and changes[field]:
                    # Schema temizligi yap: "SCHEMA.TABLE" -> "TABLE"
                    val = str(changes[field]).strip().upper()
                    if "." in val:
                        val = val.split('.')[-1]
                    changes[field] = self._validate_identifier(val, field)

            if "tgt_owner" in changes:
                allowed = {"DWH", "ODS", "AUDIT_USER", "DWH_FLAT"}
                if changes["tgt_owner"].upper() not in allowed:
                    raise ValueError(f"Gecersiz tgt_owner: {changes['tgt_owner']}")
                changes["tgt_owner"] = changes["tgt_owner"].upper()

            odi_result = self._update_odi_mapping(mapping_name, changes)
            result["steps"]["update_mapping"] = odi_result

            # Adim 3: SKEY guncelle (gerekirse)
            if changes.get("needs_skey"):
                tgt_owner = changes.get("tgt_owner", "DWH")
                tgt_table = changes.get("tgt_table", mapping_name.replace("MAP_", ""))
                skey_params = {
                    "tgt_owner": tgt_owner,
                    "tgt_table": tgt_table,
                    "needs_skey": True,
                    "skey_natural_key": changes.get("skey_natural_key", ""),
                }
                if skey_params["skey_natural_key"]:
                    skey_result = self._create_skey(skey_params)
                    result["steps"]["update_skey"] = skey_result
                else:
                    self._add_log("update_skey", "SKIPPED", "skey_natural_key belirtilmedi")
            else:
                self._add_log("update_skey", "SKIPPED", "SKEY degisikligi yok")

            # Adim 4: Lineage yenile
            lineage_result = self._populate_lineage(update_params)
            result["steps"]["refresh_lineage"] = lineage_result

            # Adim 5: DQ testleri yeniden uret
            tgt_owner = changes.get("tgt_owner", "DWH")
            tgt_table = changes.get("tgt_table")
            if tgt_table:
                test_result = self._generate_tests({"tgt_owner": tgt_owner, "tgt_table": tgt_table})
                result["steps"]["regenerate_tests"] = test_result
            else:
                self._add_log("regenerate_tests", "SKIPPED", "Hedef tablo degismedi")

            # Adim 6: Sonraki snapshot (degisiklik sonrasi durum)
            final_name = changes.get("new_mapping_name", mapping_name)
            post_snap = self._take_snapshot_for_update(final_name, "POST_UPDATE")
            result["steps"]["post_snapshot"] = post_snap
            result["mapping_name"] = final_name

            # MAPPING_LOG
            self._log_to_mapping_log(
                {"mapping_name": final_name, "tgt_owner": changes.get("tgt_owner", "DWH")},
                "SUCCESS"
            )

        except (ValueError, RuntimeError) as e:
            result["overall_status"] = "FAILED"
            result["error"] = str(e)
            self._add_log("update_pipeline", "FAILED", str(e))
        except Exception as e:
            result["overall_status"] = "FAILED"
            result["error"] = f"Beklenmeyen hata: {str(e)}"
            self._add_log("update_pipeline", "FAILED", str(e))
            logger.error("MappingGenerator update hatasi: %s", e, exc_info=True)

        result["log"] = self._log_entries
        return result

    def _update_odi_mapping(self, mapping_name: str, changes: dict) -> dict:
        """Cerrah (Surgeon) update: OdiOrchestrator uzerinden mevcut mapping'i gunceller.

        Operasyon tipi changes dict'teki anahtarlardan otomatik tespit edilir:
            - expressions key → update_expression
            - filter_action key → update_filter
            - ikm_name/lkm_name key → update_km
            - new_mapping_name key → rename

        Args:
            mapping_name: Guncellenecek mapping adi.
            changes: Degisiklik sozlugu. Desteklenen anahtarlar:
                - expressions: dict (orn: {"NAMEE": "Product.Name"})
                - filter_action: str (add/modify/remove)
                - filter_condition: str
                - filter_name: str
                - ikm_name: str
                - lkm_name: str
                - truncate_target_table: bool
                - new_mapping_name: str
                - rename_source_table_name: str
                - rename_target_table_name: str
                - rename_column_side: str
                - rename_column_old_name: str
                - rename_column_new_name: str

        Returns:
            Sozluk: odi_result.
        """
        from odi_orchestrator import OdiOrchestrator

        orch = OdiOrchestrator()

        # Operasyon tipi otomatik tespit
        if "expressions" in changes and changes["expressions"]:
            operation = "update_expression"
        elif "filter_action" in changes:
            operation = "update_filter"
        elif "ikm_name" in changes or "lkm_name" in changes or "truncate_target_table" in changes:
            operation = "update_km"
        elif "new_mapping_name" in changes:
            operation = "rename"
        else:
            operation = "rename"

        # Pre-validation: Expression update icin Oracle DB'de dogrula
        if operation == "update_expression":
            tgt_table = changes.get("tgt_table")
            tgt_owner = changes.get("tgt_owner", "DWH")
            if tgt_table:
                validation = orch.validate_expression(tgt_table, tgt_owner, changes["expressions"])
                if not validation.get("valid", True):
                    error_detail = "; ".join(f"{k}: {v}" for k, v in validation.get("errors", {}).items())
                    self._add_log("pre_validate", "FAILED", error_detail)
                    raise ValueError(f"Expression validation failed: {error_detail}")
                self._add_log("pre_validate", "SUCCESS", "Expressions validated against Oracle DB")

        result = orch.update_mapping(
            mapping_name=mapping_name,
            operation=operation,
            new_mapping_name=changes.get("new_mapping_name"),
            expressions=changes.get("expressions"),
            filter_action=changes.get("filter_action"),
            filter_condition=changes.get("filter_condition"),
            filter_name=changes.get("filter_name"),
            ikm_name=changes.get("ikm_name"),
            lkm_name=changes.get("lkm_name"),
            truncate_target_table=changes.get("truncate_target_table"),
            rename_source_table_name=changes.get("rename_source_table_name"),
            rename_target_table_name=changes.get("rename_target_table_name"),
            rename_column_side=changes.get("rename_column_side"),
            rename_column_old_name=changes.get("rename_column_old_name"),
            rename_column_new_name=changes.get("rename_column_new_name"),
        )

        if "Hata:" in result or "FATAL ERROR" in result:
            self._add_log("update_mapping", "FAILED", result)
            raise RuntimeError(f"ODI mapping guncelleme basarisiz: {result}")

        self._add_log("update_mapping", "SUCCESS", result[:100])
        return {"odi_result": result}

    def _take_snapshot_for_update(self, mapping_name: str, label_prefix: str) -> dict:
        """Guncelleme oncesi/sonrasi snapshot alir.

        Args:
            mapping_name: Mapping adi.
            label_prefix: Snapshot etiketi on eki (PRE_UPDATE/POST_UPDATE).

        Returns:
            Sozluk: snap_id.
        """
        from mapping_snapshot_manager import MappingSnapshotManager

        try:
            with MappingSnapshotManager() as msm:
                msm.ensure_table()
                snap_id = msm.take_snapshot(
                    mapping_name=mapping_name,
                    mapping_data=[{"operation": label_prefix, "batch_id": self._batch_id}],
                    label=f"{label_prefix} (batch {self._batch_id})",
                )
                self._add_log(f"snapshot_{label_prefix.lower()}", "SUCCESS", f"snap_id={snap_id}")
                return {"snap_id": snap_id}
        except Exception as e:
            self._add_log(f"snapshot_{label_prefix.lower()}", "SKIPPED", str(e))
            return {"snap_id": None, "error": str(e)}

    # ── Mapping Silme ──────────────────────────────────────────────────────────

    def delete_mapping(self, mapping_name: str, tgt_owner: str = "DWH") -> dict:
        """Bir mapping'i ve iliskili nesneleri siler (rollback).

        Adimlar:
          1. Son snapshot al (silme oncesi durum)
          2. Rollback manager ile iliskili DDL'leri geri al
          3. MAPPING_LOG'a DELETE islemi kaydet

        Args:
            mapping_name: Silinecek mapping adi.
            tgt_owner: Hedef schema. Varsayilan "DWH".

        Returns:
            Sozluk: batch_id, mapping_name, steps, log.
        """
        result = {
            "batch_id": self._batch_id,
            "mapping_name": mapping_name,
            "operation": "DELETE",
            "steps": {},
            "overall_status": "SUCCESS",
            "log": [],
        }

        try:
            mapping_name = self._validate_identifier(mapping_name, "mapping_name")

            # Adim 1: Silme oncesi snapshot
            pre_snap = self._take_snapshot_for_update(mapping_name, "PRE_DELETE")
            result["steps"]["pre_snapshot"] = pre_snap

            # Adim 2: OdiOrchestrator ile fiziki ODI Mapping nesnesini sil
            from odi_orchestrator import OdiOrchestrator
            orch = OdiOrchestrator()
            odi_del_result = orch.delete_mapping(mapping_name)
            if "Hata:" in odi_del_result:
                self._add_log("odi_delete", "FAILED", odi_del_result)
                raise RuntimeError(f"ODI tarafindan silinemedi: {odi_del_result}")
            else:
                self._add_log("odi_delete", "SUCCESS", odi_del_result[:100])
                result["steps"]["odi_delete"] = odi_del_result

            # Adim 3: Rollback (varsa iliskili DDL nesneleri geri al)
            from rollback_manager import RollbackManager
            with RollbackManager() as rm:
                try:
                    # Gecmis batch_id ile tablo/DDL sileceksek MAPPING_LOG'dan en son id'yi de bulabilir.
                    # Ancak simdilik yeni batch uzerinden islem calisiyor
                    rollback_result = rm.rollback_batch(self._batch_id)
                    self._add_log("rollback_objects", "SUCCESS",
                                   f"Rolled back: {rollback_result}")
                    result["steps"]["rollback"] = rollback_result
                except Exception as e:
                    self._add_log("rollback_objects", "SKIPPED",
                                   f"Rollback icin kayit bulunamadi: {e}")
                    result["steps"]["rollback"] = {"skipped": True, "reason": str(e)}

            # Adim 3: MAPPING_LOG'a DELETE kaydi
            self._log_to_mapping_log(
                {"mapping_name": mapping_name, "tgt_owner": tgt_owner},
                "SUCCESS"
            )
            self._add_log("delete_log", "SUCCESS", f"{mapping_name} silindi")

        except (ValueError, RuntimeError) as e:
            result["overall_status"] = "FAILED"
            result["error"] = str(e)
            self._add_log("delete_pipeline", "FAILED", str(e))
        except Exception as e:
            result["overall_status"] = "FAILED"
            result["error"] = f"Beklenmeyen hata: {str(e)}"
            logger.error("MappingGenerator delete hatasi: %s", e, exc_info=True)

        result["log"] = self._log_entries
        return result

    # ── Prompt Analizi ─────────────────────────────────────────────────────────

    def analyze_prompt(self, user_prompt: str) -> dict:
        """Prompt'u parse eder ama mapping olusturmaz (on izleme).

        Args:
            user_prompt: Kullanicinin dogal dil istegi.

        Returns:
            Sozluk: params (parse sonucu), validation (dogrulama sonucu),
            preview (on izleme bilgileri).
        """
        result = {"prompt": user_prompt, "params": None, "validation": None, "preview": None}

        try:
            params = self.parse_prompt(user_prompt)
            result["params"] = params

            validated = self.validate_params(params)
            result["validation"] = {"status": "VALID", "params": validated}

            # On izleme bilgileri
            preview = {
                "mapping_name": validated["mapping_name"],
                "flow": f"{validated['src_model']}.{validated['src_table']} --> "
                        f"{validated['tgt_owner']}.{validated['tgt_table']}",
                "will_create_skey": validated["needs_skey"],
                "steps_to_run": [
                    f"Hedef tablo olustur: {validated['tgt_owner']}.{validated['tgt_table']}",
                    "ODI Mapping olustur",
                ],
            }
            if validated["needs_skey"]:
                preview["steps_to_run"].append(
                    f"SKEY nesneleri olustur (SKEY_MAP_{validated['tgt_table']}, "
                    f"SEQ_{validated['tgt_table']})"
                )
                preview["steps_to_run"].append(
                    f"SKEY Expression: CASE WHEN ... {validated['skey_natural_key']} ..."
                )
            preview["steps_to_run"].extend([
                "Lineage kaydi (DATA_LINEAGE)",
                "DQ testleri uret + RAG kaydi",
                "Mapping snapshot al",
            ])
            result["preview"] = preview

        except (ValueError, RuntimeError) as e:
            result["validation"] = {"status": "INVALID", "error": str(e)}

        return result
