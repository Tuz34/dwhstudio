"""
Mapping Generator — Autonomous ODI Mapping Orchestrator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Takes the user's natural-language prompt and creates an end-to-end mapping:
  1. Uses AI Agent to convert the prompt to structured parameters
  2. Creates the target Oracle table (source connector type conversion + ODS default columns)
  3. Creates the ODI mapping (OdiOrchestrator)
  4. Creates surrogate key objects (SkeyManager) — optional
  5. Records data lineage (LineageManager)
  6. Generates DQ tests (TestGenerator)
  7. Takes a mapping snapshot (MappingSnapshotManager)

All steps are logged to MAPPING_LOG, and batch rollback is supported on failure.
"""

import json
import logging
import re
import uuid
from datetime import datetime

import oracledb

from config import (
    ODI_APPROVED_IKMS,
    ODI_FOLDER,
    ODI_IKM_PRIORITY,
    ODI_PROJECT,
    ORACLE_DDL_PASSWORD,
    ORACLE_DDL_USER,
    ORACLE_DSN,
    SOURCE_ORACLE_SCHEMAS,
    SOURCE_SYSTEM_NAME,
)
from exceptions import MappingGenerationError
from ikm_policy import is_update_ikm, normalize_repository_preflight, resolve_approved_ikm, verified_source_identity
from mapping_state_manager import redact_sensitive_text
from source_connector import get_source_connector

logger = logging.getLogger("mapping_generator")

# Internal (target-schema) sources are read through the target connector.
INTERNAL_SOURCE_MODELS = ("ORACLE_ODS", "ORACLE_DWH")
DEFAULT_INTERNAL_SOURCE_OWNER = "DWH"
ALLOWED_INTERNAL_OWNERS = frozenset({"DWH", "ODS", "AUDIT_USER", "DWH_FLAT"})
INTEGRATION_TYPES = frozenset({"IU", "CONTROL_APPEND"})
MAX_UPDATE_KEY_COLUMNS = 8
# SKEY lookup tables are always loaded with this merge KM (see _create_odi_mapping).
SKEY_LOOKUP_IKM = "IKM Oracle Incremental Update (MERGE)"
_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_AUDIT_PRESERVE_ON_MATCH = ("INSERT_DATE", "SOURCE_SYSTEM", "CURRENT_FLAG", "EFFECTIVE_FROM", "EFFECTIVE_TO")
_AUDIT_REFRESH_ON_CHANGE = ("UPDATE_DATE", "ETL_BATCH_ID")
_AUDIT_COLUMN_ORDER = (
    "INSERT_DATE", "UPDATE_DATE", "CURRENT_FLAG", "EFFECTIVE_FROM", "EFFECTIVE_TO",
    "SOURCE_SYSTEM", "ETL_BATCH_ID",
)
_SESSION_BATCH_SQL = "'<%=odiRef.getSession(\"SESS_NO\")%>'"
_FORBIDDEN_EXPRESSION_RE = re.compile(r"ROWNUM|\bMAX\s*\(|\bROW_NUMBER\s*\(|\bTARGET\s*\.|/\*\+")


class TargetTableBlockedError(RuntimeError):
    """Existing target failed a read-only schema/key check; no DDL ran."""

    def __init__(self, code: str, detail: dict) -> None:
        super().__init__(f"{code}: existing target does not satisfy the requested plan")
        self.error_code = code
        self.detail = detail


class AuditPreservationRejected(ValueError):
    """Same-schema audit SQL failed static validation before any DDL."""


class UpdateKeyRequiredError(ValueError):
    """A merge/update KM would run without an update key (structured code update_key_required)."""

    error_code = "update_key_required"

    def __init__(self, detail: str) -> None:
        super().__init__(f"update_key_required: {detail}")


class RepositoryPreflightBlockedError(RuntimeError):
    error_code = "repository_preflight_failed"

    def __init__(self, detail: dict) -> None:
        super().__init__("repository_preflight_failed: repository identity could not be verified")
        self.detail = detail
        if detail.get("code") == "ikm_not_imported":
            self.error_code = "ikm_not_imported"


class SourceTableNotFound(LookupError):
    """Source columns were not visible through the selected source model."""

    def __init__(self, src_model: object) -> None:
        text = str(src_model or "").strip().upper()
        if not _IDENTIFIER_RE.fullmatch(text):
            text = "UNKNOWN"
        self.src_model = text
        super().__init__(f"Source table not found through source model {text}")


def _sql_string_literal(value: str | None, fallback: str = "ORACLE_SOURCE") -> str:
    """Escape single quotes in an Oracle SQL literal."""
    text = (value or fallback).strip() or fallback
    return text.replace("'", "''")


_SOURCE_SYSTEM_LITERAL = _sql_string_literal(SOURCE_SYSTEM_NAME, "ORACLE_SOURCE")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
# Reviewed classic acceptance reads internal owners through ORACLE_DWH.
# INTERNAL_SOURCE_MODELS has no per-owner map; both codes share that read path.
CLASSIC_INTERNAL_SOURCE_MODEL = "ORACLE_DWH"
_AUDIT_DEFAULT_TAIL_RE = re.compile(r"\s+DEFAULT\s+.*\Z", re.IGNORECASE | re.DOTALL)
_AUDIT_NOT_NULL_RE = re.compile(r"\s+NOT\s+NULL\s*\Z", re.IGNORECASE)
_AUDIT_TYPE_RE = re.compile(
    r"(?:"
    r"TIMESTAMP(?:\(\d+\))?(?: WITH LOCAL TIME ZONE| WITH TIME ZONE)?"
    r"|DATE"
    r"|NUMBER(?:\(\d+(?:,\d+)?\))?"
    r"|FLOAT(?:\(\d+\))?"
    r"|BINARY_DOUBLE|BINARY_FLOAT"
    r"|VARCHAR2\(\d+(?: BYTE| CHAR)?\)"
    r"|NVARCHAR2\(\d+\)"
    r"|CHAR\(\d+(?: BYTE| CHAR)?\)"
    r"|NCHAR\(\d+\)"
    r")\Z"
)
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'\Z")


def classic_internal_source_model(owner: object) -> str | None:
    """Return ORACLE_DWH for an internal owner, or None for any other owner."""
    text = str(owner or "").strip().upper()
    if text in ALLOWED_INTERNAL_OWNERS:
        return CLASSIC_INTERNAL_SOURCE_MODEL
    return None


def _canonical_audit_type(type_sql: object) -> str:
    """Oracle datatype of one planned target column, without DEFAULT or nullability."""
    text = " ".join(str(type_sql or "").strip().upper().split())
    text = _AUDIT_DEFAULT_TAIL_RE.sub("", text).strip()
    text = _AUDIT_NOT_NULL_RE.sub("", text).strip()
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*,\s*", ",", text)
    text = re.sub(r"\s*\)", ")", text)
    text = re.sub(r"\s+", " ", text)
    if not _AUDIT_TYPE_RE.fullmatch(text):
        raise AuditPreservationRejected("Audit preservation column type is not supported.")
    return text


def _audit_type_family(oracle_type: str) -> str:
    if oracle_type.startswith("TIMESTAMP"):
        return "timestamp"
    if oracle_type == "DATE":
        return "date"
    if oracle_type.startswith(("NUMBER", "FLOAT", "BINARY_")):
        return "number"
    return "character"


def _default_expression_family(expression: str) -> str | None:
    if expression == "CURRENT_TIMESTAMP":
        return "timestamp_tz"
    if expression.startswith(("TRUNC(", "TO_DATE(")):
        return "date"
    if _SQL_STRING_LITERAL_RE.fullmatch(expression):
        return "char"
    return None


def _typed_audit_default(expression: str, type_sql: object) -> str:
    """Render one audit default so every CASE branch has the column datatype.

    CURRENT_TIMESTAMP is TIMESTAMP WITH TIME ZONE. TO_TIMESTAMP makes a
    TIMESTAMP. A quoted session number is CHAR; TO_NUMBER makes a NUMBER.
    A character literal is CHAR. CAST covers every other supported type.
    DATE defaults that are already DATE stay as they are.
    TO_NUMBER('AMBIGUOUS') stays in the subquery WHERE, so the selected
    value remains the column itself for TIMESTAMP, VARCHAR2, and NUMBER.
    """
    oracle_type = _canonical_audit_type(type_sql)
    family = _audit_type_family(oracle_type)
    current = _default_expression_family(expression)
    if family == "date" and current == "date":
        return expression
    if family == "number":
        # Only the ODI session token becomes a number. Other defaults are not numeric.
        if expression != _SESSION_BATCH_SQL:
            raise AuditPreservationRejected(
                "Audit preservation numeric default is not a session number."
            )
        converted = f"TO_NUMBER({expression})"
        if oracle_type == "NUMBER":
            return converted
        return f"CAST({converted} AS {oracle_type})"
    if family == "timestamp":
        # CURRENT_TIMESTAMP is TIMESTAMP WITH TIME ZONE. TO_TIMESTAMP returns TIMESTAMP.
        rendered = (
            f"TO_TIMESTAMP(TO_CHAR({expression},'YYYY-MM-DD HH24:MI:SS.FF6'),"
            f"'YYYY-MM-DD HH24:MI:SS.FF6')"
        )
        if oracle_type == "TIMESTAMP":
            return rendered
        return f"CAST({rendered} AS {oracle_type})"
    return f"CAST({expression} AS {oracle_type})"


def _identifier_or_none(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if _IDENTIFIER_RE.fullmatch(text):
        return text
    return None


def _oracle_physical_model(model: object) -> bool:
    text = str(model or "").strip().upper()
    if any(token in text for token in ("MSSQL", "SQL_SERVER", "ADVENTUREWORK")):
        return False
    return text.startswith("ORACLE_") or text in {"DWH", "ODS", "TGT_DWH"}


def _audit_lookup_alias(reserved: set[str]) -> str:
    """Return a subquery alias outside the reserved table, column, and component tokens."""
    forbidden = {str(name).upper() for name in reserved if name}
    forbidden.update({"SRC", "JOIN"})
    candidate = "ODI_AUDIT_TARGET_ROW"
    sequence = 2
    while candidate in forbidden:
        candidate = f"ODI_AUDIT_TARGET_ROW_{sequence}"
        sequence += 1
        if sequence > 100:
            raise AuditPreservationRejected("Audit preservation lookup alias collides.")
    return candidate


def _null_safe_equal(alias: str, column: str) -> str:
    quoted = f'"{column}"'
    return (
        f'({alias}.{quoted}=SRC.{quoted} OR '
        f'({alias}.{quoted} IS NULL AND SRC.{quoted} IS NULL))'
    )


def _audit_case(
    table_sql: str, alias: str, key_where: str, column: str, default_sql: str,
    change_where: str | None,
) -> str:
    count_keys = f'(SELECT COUNT(*) FROM {table_sql} {alias} WHERE {key_where})'
    lookup = f'(SELECT {alias}."{column}" FROM {table_sql} {alias} WHERE {key_where})'
    # A multi-row scalar is ORA-01427 on Oracle. TO_NUMBER forces the same branch to
    # fail closed where a scalar subquery would otherwise return an arbitrary row.
    fail = (
        f'(SELECT {alias}."{column}" FROM {table_sql} {alias} WHERE {key_where} '
        "AND TO_NUMBER('AMBIGUOUS') IS NOT NULL)"
    )
    lookup_fail = fail
    if change_where is None:
        return (
            f'CASE WHEN {count_keys}>1 THEN {lookup_fail} WHEN {count_keys}=0 THEN {default_sql} '
            f'ELSE {lookup} END'
        )
    count_changed = f'(SELECT COUNT(*) FROM {table_sql} {alias} WHERE {change_where})'
    return (
        f'CASE WHEN {count_keys}>1 THEN {lookup_fail} WHEN {count_keys}=0 THEN {default_sql} '
        f'WHEN {count_changed}=1 THEN {default_sql} ELSE {lookup} END'
    )


def build_same_schema_audit_expressions(
    *,
    integration_type: object,
    src_owner: object,
    tgt_owner: object,
    src_model: object,
    tgt_model: object,
    tgt_table: object,
    update_key_columns: list[str] | None,
    source_columns: list[str],
    target_columns: list[str],
    source_system_literal: str,
    join_table: object = None,
    source_table: object = None,
    target_column_types: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build null-safe audit CASE expressions for one qualified same-schema IU mapping.

    The returned expression text is for the mapping builder. Callers report counts only.
    A different owner, a non-Oracle model, CONTROL_APPEND, a join, or a missing key stays
    unqualified and keeps the existing default expressions.
    """
    keys: list[str] = []
    for item in update_key_columns or []:
        name = _identifier_or_none(item)
        if name and name not in keys:
            keys.append(name)
    owner = _identifier_or_none(tgt_owner)
    source_owner = _identifier_or_none(src_owner)
    table = _identifier_or_none(tgt_table)
    qualified = bool(
        str(integration_type or "").strip().upper() == "IU"
        and keys
        and owner
        and table
        and source_owner == owner
        and _oracle_physical_model(src_model)
        and _oracle_physical_model(tgt_model)
        and not str(join_table or "").strip()
    )
    public = {
        "qualified": qualified,
        "expressions": {},
        "expression_count": 0,
        "business_column_count": 0,
        "key_count": len(keys) if qualified else 0,
        "lookup_alias": None,
    }
    if not qualified or owner is None or table is None:
        return public
    source_names: list[str] = []
    for item in source_columns:
        name = _identifier_or_none(item)
        if name is None:
            raise AuditPreservationRejected("Audit preservation source column is not an identifier.")
        if name not in source_names:
            source_names.append(name)
    target_names: list[str] = []
    for item in target_columns:
        name = _identifier_or_none(item)
        if name is None:
            raise AuditPreservationRejected("Audit preservation target column is not an identifier.")
        if name not in target_names:
            target_names.append(name)
    missing_keys = [name for name in keys if name not in target_names]
    if missing_keys:
        raise AuditPreservationRejected("Audit preservation update key is not on the target.")
    audit_names = set(_AUDIT_PRESERVE_ON_MATCH) | set(_AUDIT_REFRESH_ON_CHANGE)
    business = [
        name for name in source_names
        if name in target_names and name not in keys and name not in audit_names
    ]
    defaults = {
        "INSERT_DATE": "CURRENT_TIMESTAMP",
        "UPDATE_DATE": "CURRENT_TIMESTAMP",
        "CURRENT_FLAG": "'Y'",
        "EFFECTIVE_FROM": "TRUNC(CURRENT_TIMESTAMP)",
        "EFFECTIVE_TO": "TO_DATE('9999-12-31','YYYY-MM-DD')",
        "SOURCE_SYSTEM": f"'{source_system_literal}'",
        "ETL_BATCH_ID": _SESSION_BATCH_SQL,
    }
    source_table_name = None
    if str(source_table or "").strip():
        source_table_name = _identifier_or_none(source_table)
        if source_table_name is None:
            raise AuditPreservationRejected("Audit preservation source table is not an identifier.")
    alias = _audit_lookup_alias({
        table, source_table_name, *keys, *source_names, *target_names,
    })
    key_where = " AND ".join(_null_safe_equal(alias, name) for name in keys)
    change_where = None
    if business:
        differ = " OR ".join(
            f'CASE WHEN {_null_safe_equal(alias, name)} THEN 0 ELSE 1 END=1' for name in business
        )
        change_where = f"{key_where} AND ({differ})"
    table_sql = f'"{owner}"."{table}"'
    expressions: dict[str, str] = {}
    for column in _AUDIT_COLUMN_ORDER:
        if column not in target_names or column in keys:
            continue
        default_sql = defaults[column]
        if target_column_types is not None:
            if column not in target_column_types:
                raise AuditPreservationRejected("Audit preservation column type is missing.")
            default_sql = _typed_audit_default(default_sql, target_column_types[column])
        use_change = change_where if column in _AUDIT_REFRESH_ON_CHANGE else None
        expressions[column] = _audit_case(
            table_sql, alias, key_where, column, default_sql, use_change,
        )
    from odi_orchestrator import _EXPR_VAL_RE

    if expressions:
        transport = ";".join(f"{column}={expression}" for column, expression in expressions.items())
        if _FORBIDDEN_EXPRESSION_RE.search(transport) or not _EXPR_VAL_RE.fullmatch(transport):
            raise AuditPreservationRejected("Audit preservation expressions are not transport-safe.")
    public["expressions"] = expressions
    public["expression_count"] = len(expressions)
    public["business_column_count"] = len(business)
    public["lookup_alias"] = alias
    return public
_ERROR_TYPE_ASSIGN_RE = re.compile(r"\berror_type=([A-Za-z][A-Za-z0-9_$]{0,127})\b")


def _error_type_class(value: object) -> str | None:
    if isinstance(value, str) and _ERROR_TYPE_RE.fullmatch(value):
        return value
    return None


def _json_object(line: str) -> dict | None:
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def mapping_replace_state_from_output(output: str) -> dict[str, object]:
    """Read replaced, kept_existing_on_failure, and the exception class only."""
    replaced = False
    kept_existing_on_failure = False
    error_type: str | None = None
    for line in (output or "").splitlines():
        payload = _json_object(line)
        if payload is None:
            continue
        if not any(key in payload for key in ("replaced", "kept_existing_on_failure", "error_type")):
            continue
        replaced = payload.get("replaced") is True
        kept_existing_on_failure = payload.get("kept_existing_on_failure") is True
        error_type = _error_type_class(payload.get("error_type"))
    if error_type is None:
        match = _ERROR_TYPE_ASSIGN_RE.search(output or "")
        if match:
            error_type = match.group(1)
    return {
        "replaced": replaced,
        "kept_existing_on_failure": kept_existing_on_failure,
        "error_type": error_type,
    }


def create_mapping_output_failed(output: str) -> bool:
    """True when Groovy reported a failed create, including a structured error line."""
    text = output or ""
    if "Hata:" in text or "FATAL ERROR" in text:
        return True
    for line in text.splitlines():
        payload = _json_object(line)
        if payload is not None and str(payload.get("status", "")).lower() in {"error", "fail", "failed"}:
            return True
    return False


class MappingCreationFailure(RuntimeError):
    """ODI mapping creation failed. error_type is the exception class name only."""

    def __init__(self, message: str, state: dict[str, object]) -> None:
        super().__init__(message)
        self.replaced = state.get("replaced") is True
        self.kept_existing_on_failure = state.get("kept_existing_on_failure") is True
        self.error_type = _error_type_class(state.get("error_type"))


# Expected JSON format when extracting mapping parameters from a prompt
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
8. needs_skey: true if user mentions "surrogate key", "SKEY", "boyut tablosu", "dimension table", "dimension"
9. skey_natural_key: If needs_skey, the natural key expression (e.g., "SRC.PRODUCT_ID")
10. description: One-line English description of what this mapping does.

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
  "description": "Transfers HR.EMPLOYEES from the Oracle source to the ODS layer"
}"""


class MappingGenerator:
    """Autonomous mapping creation orchestrator.

    Usage:
        with MappingGenerator() as mg:
            result = mg.generate_from_prompt("Transfer the Orders table to ODS")
            # or
            result = mg.generate_from_params({...})
    """

    # Step names (for the UI progress indicator)
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

    # ── SQL Server → Oracle Type Conversion Table ──────────────────────────────
    # Each key is uppercase. Value tuple: (oracle_type, needs_length)
    # needs_length: use source length if True; use a fixed type if False.
    MSSQL_TO_ORACLE: dict[str, tuple[str, bool]] = {
        # Integer types
        "INT":             ("NUMBER(10,0)",     False),
        "BIGINT":          ("NUMBER(19,0)",     False),
        "SMALLINT":        ("NUMBER(5,0)",      False),
        "TINYINT":         ("NUMBER(3,0)",      False),
        "BIT":             ("NUMBER(1,0)",      False),
        # Decimal types (precision/scale come from the source table)
        "DECIMAL":         ("NUMBER",           True),
        "NUMERIC":         ("NUMBER",           True),
        "FLOAT":           ("FLOAT(126)",       False),
        "REAL":            ("FLOAT(63)",        False),
        "MONEY":           ("NUMBER(19,4)",     False),
        "SMALLMONEY":      ("NUMBER(10,4)",     False),
        # Text types
        "NVARCHAR":        ("VARCHAR2",         True),   # len*2 (UTF-16→UTF-8)
        "VARCHAR":         ("VARCHAR2",         True),
        "NCHAR":           ("VARCHAR2",         True),   # len*2
        "CHAR":            ("CHAR",             True),
        "TEXT":            ("CLOB",             False),
        "NTEXT":           ("NCLOB",            False),
        "XML":             ("CLOB",             False),
        # Date/time types
        "DATETIME":        ("TIMESTAMP(3)",     False),
        "DATETIME2":       ("TIMESTAMP(6)",     False),
        "SMALLDATETIME":   ("DATE",             False),
        "DATE":            ("DATE",             False),
        "TIME":            ("VARCHAR2(16)",     False),
        "DATETIMEOFFSET":  ("TIMESTAMP(6) WITH TIME ZONE", False),
        # Binary types
        "VARBINARY":       ("BLOB",             False),
        "BINARY":          ("RAW",              True),
        "IMAGE":           ("BLOB",             False),
        # Other
        "UNIQUEIDENTIFIER": ("VARCHAR2(36)",    False),
        "SQL_VARIANT":     ("VARCHAR2(4000)",   False),
        "HIERARCHYID":     ("VARCHAR2(4000)",   False),
        "GEOGRAPHY":       ("CLOB",             False),
        "GEOMETRY":        ("CLOB",             False),
    }

    # Standard columns added for the ODS layer
    ODS_DEFAULT_COLUMNS = [
        ("INSERT_DATE",    "TIMESTAMP DEFAULT SYSTIMESTAMP"),
        ("UPDATE_DATE",    "TIMESTAMP"),
        ("SOURCE_SYSTEM",  f"VARCHAR2(50) DEFAULT '{_SOURCE_SYSTEM_LITERAL}'"),
        ("ETL_BATCH_ID",   "NUMBER"),
    ]

    # Additional SCD2 columns for DWH dimension tables
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
        self._audit_preservation_expressions: dict[str, str] = {}
        self._audit_lookup_alias: str | None = None
        self._audit_preservation_public = {
            "qualified": False, "expression_count": 0, "business_column_count": 0, "key_count": 0,
        }

    def __enter__(self) -> "MappingGenerator":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    # ── Helper Methods ──────────────────────────────────────────────────────

    def _add_log(self, step: str, status: str, detail: str = ""):
        """Add an operation step to the internal log list."""
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
        """Validate SQL/ODI identifiers. Reject invalid characters.

        Args:
            value: Value to validate.
            field_name: Field name to use in the error message.

        Returns:
            Valid uppercase identifier.

        Raises:
            ValueError: If the value is empty or contains invalid characters.
        """
        if not value or not value.strip():
            raise ValueError(f"{field_name} cannot be empty.")
        clean = value.strip().upper()
        if not re.match(r'^[A-Z][A-Z0-9_]{0,127}$', clean):
            raise ValueError(
                f"{field_name} is invalid: '{clean}'. "
                "Letters, digits, and underscores only; must start with a letter; max 128 characters."
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
        if params.get("src_owner"):
            return params["src_owner"]
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
            "physical_schema", "context", "datastore_id", "datastore_exists",
        ]
        return {key: row.get(key) for key in keys if key in row}

    def _resolve_odi_repository_bindings(self, params: dict) -> dict:
        """Resolve ODI model/datastore names from repository metadata tables."""
        identity = None
        if params.get("src_model") in INTERNAL_SOURCE_MODELS:
            try:
                identity = verified_source_identity(
                    params.get("_odi_source_identity"), params["src_owner"], params["src_table"], params["context"],
                )
            except ValueError as exc:
                raise RepositoryPreflightBlockedError({"code": "source_identity_unverified"}) from exc
        reader = self._get_odi_repo_reader()
        src_hint = self._source_schema_hint(params)
        tgt_hint = self._target_schema_hint(params)

        source_ds = (identity if identity["datastore_exists"] else None) if identity else reader.resolve_datastore(
            params.get("src_table"),
            schema_hint=src_hint,
            role="source",
            preferred_model=params.get("src_model"),
        )
        source_model = identity or source_ds or reader.resolve_model(
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

    def _preflight_odi_repository(self, params: dict) -> dict:
        from config import ODI_SOURCE_MODEL_BINDINGS
        from odi_orchestrator import OdiOrchestrator
        from source_model_policy import SourceModelConfigError, trusted_source_model_code

        internal = params["src_model"] in INTERNAL_SOURCE_MODELS
        owner = params["src_owner"] if internal else None
        table = params["src_table"] if internal else None
        params.pop("_odi_source_identity", None)
        try:
            expected = trusted_source_model_code(params["context"], owner, ODI_SOURCE_MODEL_BINDINGS)
        except SourceModelConfigError:
            raise RepositoryPreflightBlockedError({
                "tool": "odi.mapping_preflight",
                "status": "blocked",
                "side_effects": False,
                "repository_connection_attempted": False,
                "code": "source_model_config_invalid",
            }) from None
        try:
            result = OdiOrchestrator().preflight_mapping(
                approved=True, ikm_name=params["ikm_name"], src_owner=owner, src_table=table, context=params["context"],
            )
        except Exception as exc:
            raise RepositoryPreflightBlockedError({"code": "repository_lookup_failed"}) from exc
        result = normalize_repository_preflight(
            result, params["ikm_name"], owner, table, params["context"], expected_model_code=expected,
        )
        if result["status"] != "passed":
            raise RepositoryPreflightBlockedError(result)
        params["_odi_ikm_id"] = result["ikm_id"]
        if internal:
            params["_odi_source_identity"] = result["source"]
        return result

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
        """Check mapping existence in the ODI work repository in read-only mode."""
        try:
            mapping = self._find_repository_mapping(mapping_name)
            if mapping:
                return True, mapping.get("mapping_name") or mapping_name
            return False, None
        except Exception as e:
            logger.warning("Mapping repository table existence check failed: %s", e)

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
            logger.warning("Mapping SDK existence check failed: %s", e)
            return False, None

    # ── Read Source Table Schema ──────────────────────────────────────────────

    def _source_schema_df_to_legacy_columns(self, df, connector) -> list[dict]:
        """Convert the connector schema contract to the legacy mapping column format."""
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
        """Fetch source table columns from SQL Server.

        Args:
            src_table: Table name (e.g. "Person.Person" or "Orders").

        Returns:
            For each column, return COLUMN_NAME, DATA_TYPE, MAX_LEN, IS_NULLABLE,
            List containing NUMERIC_PRECISION and NUMERIC_SCALE fields.
        """
        connector = get_source_connector("mssql")
        schema_df = connector.get_table_schema(src_table)
        columns = self._source_schema_df_to_legacy_columns(schema_df, connector)
        full_name = str(src_table).upper()
        return columns, full_name

    def _get_source_columns_oracle(self, src_table: str) -> list[dict]:
        """Fetch source table columns from Oracle."""
        connector = get_source_connector("oracle")
        schema_df = connector.get_table_schema(src_table)
        return self._source_schema_df_to_legacy_columns(schema_df, connector)

    def _source_columns_for(self, params: dict) -> list[dict]:
        """Read source columns. Internal ODS/DWH sources go through the target connector.

        ORACLE_ODS / ORACLE_DWH tables live in the target schema, so they are read with
        an explicit owner instead of the source connector's default schema.
        """
        if params.get("src_model") in INTERNAL_SOURCE_MODELS:
            from target_table_service import read_table_schema

            owner = params.get("src_owner") or DEFAULT_INTERNAL_SOURCE_OWNER
            schema_df = read_table_schema(owner, params["src_table"])
            return self._source_schema_df_to_legacy_columns(
                schema_df, get_source_connector("oracle")
            )
        return self._get_source_columns_oracle(params["src_table"])

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
            raise ValueError("columns list is None")

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
            if dt == "NUMBER":
                # Oracle DATA_LENGTH is storage bytes, not declared precision.
                if prec:
                    col_def["length"] = prec
            elif max_len:
                col_def["length"] = max_len
            elif prec:
                col_def["length"] = prec
            if scale:
                col_def["scale"] = scale
            col_defs.append(col_def)
        return col_defs

    def _convert_mssql_type(self, data_type: str, max_len: int | None,
                             precision: int | None, scale: int | None) -> str:
        """Convert a SQL Server data type to its Oracle equivalent.

        Args:
            data_type: SQL Server data type (e.g. "nvarchar", "int").
            max_len: CHARACTER_MAXIMUM_LENGTH (for text types).
            precision: NUMERIC_PRECISION (for numeric types).
            scale: NUMERIC_SCALE (for numeric types).

        Returns:
            Oracle DDL column type string (e.g. "VARCHAR2(200)", "NUMBER(10,0)").
        """
        dt_upper = data_type.strip().upper()
        entry = self.MSSQL_TO_ORACLE.get(dt_upper)

        if entry is None:
            # Unknown type — fall back to VARCHAR2(4000)
            logger.warning("Unknown SQL Server type: %s → VARCHAR2(4000)", data_type)
            return "VARCHAR2(4000)"

        oracle_type, needs_length = entry

        if not needs_length:
            return oracle_type

        # Types requiring a length
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
                src_columns = self._source_columns_for(params)
                is_mssql_source = False
        except Exception as e:
            self._add_log("target_preflight", "FAILED", f"Could not retrieve source schema: {e}")
            raise RuntimeError(f"Could not retrieve source table schema ({src_model}.{src_table}): {e}") from e

        if src_columns is None:
            self._add_log("target_preflight", "FAILED", "src_columns returned None")
            raise RuntimeError("Could not retrieve source column list (None)")

        if not src_columns:
            self._add_log(
                "target_preflight", "FAILED",
                f"No column found in source table: {params.get('src_table')}",
            )
            raise SourceTableNotFound(src_model)

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

        key_columns = list(params.get("update_key_columns") or [])
        unique_key = primary_key = None
        if key_columns:
            known = {name for name, _type_sql in desired_col_defs}
            missing = [name for name in key_columns if name not in known]
            if missing:
                raise ValueError(
                    "update_key_columns not found in target columns: " + ", ".join(missing)
                )
            if params.get("update_key_kind") == "primary":
                primary_key = key_columns
            else:
                unique_key = key_columns

        return {
            "src_columns": src_columns,
            "col_defs": col_defs,
            "desired_col_defs": desired_col_defs,
            "defaults_added": defaults_added,
            "unique_key": unique_key,
            "primary_key": primary_key,
        }

    def _prepare_audit_preservation(self, params: dict, plan: dict) -> dict[str, object]:
        """Validate same-schema audit SQL before the target DDL step."""
        source_columns = [
            str(column.get("COLUMN_NAME") or "")
            for column in plan.get("src_columns") or []
            if isinstance(column, dict)
        ]
        target_pairs = list(plan.get("desired_col_defs") or [])
        target_columns = [name for name, _type_sql in target_pairs]
        target_column_types = {name: type_sql for name, type_sql in target_pairs}
        profile = build_same_schema_audit_expressions(
            integration_type=params.get("integration_type"),
            src_owner=params.get("src_owner"),
            tgt_owner=params.get("tgt_owner"),
            src_model=params.get("src_model"),
            tgt_model=params.get("tgt_model"),
            tgt_table=params.get("tgt_table"),
            update_key_columns=list(params.get("update_key_columns") or []),
            source_columns=source_columns,
            target_columns=target_columns,
            source_system_literal=_SOURCE_SYSTEM_LITERAL,
            join_table=params.get("join_table"),
            source_table=params.get("src_table"),
            target_column_types=target_column_types,
        )
        expressions = dict(profile["expressions"])
        self._audit_preservation_expressions = expressions
        self._audit_lookup_alias = profile.get("lookup_alias") if expressions else None
        self._audit_preservation_public = {
            "qualified": profile["qualified"] is True,
            "expression_count": profile["expression_count"],
            "business_column_count": profile["business_column_count"],
            "key_count": profile["key_count"],
        }
        state = "SUCCESS" if profile["qualified"] is True else "SKIPPED"
        self._add_log(
            "audit_preservation", state,
            "qualified={qualified} expressions={expression_count} "
            "business_columns={business_column_count} keys={key_count}".format(**self._audit_preservation_public),
        )
        return profile

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

    # ── Create Target Table ─────────────────────────────────────────────────


    def _create_target_table(self, params: dict) -> dict:
        """Build a reviewed column plan, then apply it through the persistent target service."""
        from target_table_service import execute_target_table_plan

        logger.info("Creating target table from validated mapping parameters")
        try:
            plan = self._build_target_column_plan(params)
            self._prepare_audit_preservation(params, plan)
            outcome = execute_target_table_plan(self, params, plan, ORACLE_DDL_USER or "APP_USER")
            if outcome.get("status") == "BLOCKED":
                raise TargetTableBlockedError(str(outcome["code"]), outcome)
            return outcome
        except (MappingGenerationError, TargetTableBlockedError, AuditPreservationRejected):
            raise
        except Exception as exc:
            self._add_log("create_target_table", "FAILED", type(exc).__name__)
            raise MappingGenerationError("Target table operation failed") from exc

    # ── AI Prompt Parsing ──────────────────────────────────────────────────────

    def parse_prompt(self, user_prompt: str) -> dict:
        """Convert a natural-language prompt to structured mapping parameters.

        Args:
            user_prompt: User's natural-language request
                (e.g. "Transfer the Orders table from SQL Server to ODS").

        Returns:
            Dictionary: mapping_name, src_model, src_table, tgt_model, tgt_table,
            tgt_owner, needs_skey, skey_natural_key, description fields.

        Raises:
            ValueError: If the AI output cannot be parsed or required fields are missing.
        """
        from ai_agent import call_llm, extract_json

        messages = [
            {"role": "system", "content": MAPPING_EXTRACT_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        raw = call_llm(messages)

        parsed = extract_json(raw)

        # Check required fields
        required = ["mapping_name", "src_table", "tgt_table", "src_model", "tgt_model"]
        missing = [f for f in required if not parsed.get(f)]
        if missing:
            raise ValueError(f"AI omitted required fields: {', '.join(missing)}. Raw output: {raw[:200]}")

        self._add_log("prompt_parse", "SUCCESS", f"mapping={parsed.get('mapping_name')}")
        return parsed

    # ── Parameter Validation ────────────────────────────────────────────────────

    def validate_params(self, params: dict) -> dict:
        """Validate and normalize mapping parameters.

        Raises:
            ValueError: If parameters are invalid or missing.
        """
        if params is None:
            logger.error("validate_params received None!")
            raise ValueError("Parameters cannot be empty (None).")

        validated = {}

        validated["mapping_name"] = self._validate_identifier(
            params.get("mapping_name", ""), "mapping_name"
        )
        validated["src_model"] = params.get("src_model", "ORACLE_SOURCE").strip().upper()
        validated["drop_target_if_exists"] = params.get("drop_target_if_exists") is True
        validated["replace_existing_mapping"] = params.get("replace_existing_mapping") is True

        # src_table: SQL Server tables may use "Schema.Table" format
        src_schema = (params.get("src_schema") or "").strip()
        raw_src_value = (params.get("src_table") or "").strip()
        if src_schema and raw_src_value and "." not in raw_src_value:
            raw_src_value = f"{src_schema}.{raw_src_value}"
        raw_src = raw_src_value.upper()
        if not raw_src:
            raise ValueError("src_table cannot be empty.")
            
        # AI sometimes returns table names as DWH.ODS_; omit the schema name:
        src_table_no_schema = raw_src.split('.')[-1]
            
        # AI sometimes assumes ODS_ or DWH_ tables are SQL_SERVER. Safeguard:
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
                f"Invalid src_model: {validated['src_model']}. "
                f"Allowed values: {sorted(allowed_src_models)}"
            )

        # Does the source model support the "SCHEMA.TABLE" format?
        # - External sources (MSSQL and Oracle source: ORACLE_HR/ORACLE_SH/ORACLE_SOURCE)
        #   arrive schema-qualified; the schema MUST BE PRESERVED (HR.EMPLOYEES -> HR.EMPLOYEES).
        # - Internal Oracle source/target (ORACLE_ODS / ORACLE_DWH) uses a single table name.
        _internal_oracle = validated["src_model"] in INTERNAL_SOURCE_MODELS
        validated["src_owner"] = None
        if _internal_oracle:
            owner_parts = raw_src.split(".")
            src_owner = owner_parts[-2] if len(owner_parts) > 1 else DEFAULT_INTERNAL_SOURCE_OWNER
            if src_owner not in ALLOWED_INTERNAL_OWNERS:
                raise ValueError(
                    f"Invalid src_owner: {src_owner}. Allowed values: {sorted(ALLOWED_INTERNAL_OWNERS)}"
                )
            validated["src_owner"] = src_owner
        if not _internal_oracle:
            if not re.match(r'^[A-Z][A-Z0-9_]*(\.[A-Z][A-Z0-9_]*)?$', raw_src):
                raise ValueError(
                    f"Invalid src_table: '{raw_src}'. "
                    "Format: TABLE or SCHEMA.TABLE (letters, digits, underscores)."
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
                f"Invalid tgt_model: {validated['tgt_model']}. "
                f"Allowed values: {sorted(allowed_tgt_models)}"
            )
        
        # AI may select the SKEY_MAP_ table as target; this is wrong (skey_manager handles it automatically). The target must be the SKEY_ dimension table.
        if tgt_table_no_schema.startswith("SKEY_MAP_"):
            tgt_table_no_schema = tgt_table_no_schema.replace("SKEY_MAP_", "SKEY_")
            
        validated["tgt_table"] = self._validate_identifier(tgt_table_no_schema, "tgt_table")
        validated["tgt_owner"] = params.get("tgt_owner", "DWH").strip().upper()

        # Check owner allowlist
        allowed = {"DWH", "ODS", "AUDIT_USER", "DWH_FLAT"}
        if validated["tgt_owner"] not in allowed:
            raise ValueError(f"Invalid tgt_owner: {validated['tgt_owner']}. Allowed values: {allowed}")

        validated["needs_skey"] = params.get("needs_skey") is True
        validated["skey_natural_key"] = params.get("skey_natural_key") or None
        validated["description"] = params.get("description", "")
        validated["context"] = self._validate_identifier(params.get("context") or "GLOBAL", "context")
        self._validate_load_options(params, validated)

        if validated["needs_skey"] and not validated["skey_natural_key"]:
            raise ValueError("SKEY was requested but skey_natural_key was not specified.")

        self._add_log("validate_params", "SUCCESS",
                       f"{validated['src_table']} -> {validated['tgt_table']}")
        return validated

    def _validate_load_options(self, params: dict, validated: dict) -> None:
        """Validate the additive update-key, integration-type and IKM request fields."""
        raw_keys = params.get("update_key_columns")
        if raw_keys is None or raw_keys == "" or raw_keys == []:
            items: list = []
        elif isinstance(raw_keys, str):
            items = raw_keys.split(",")
        elif isinstance(raw_keys, (list, tuple)):
            items = list(raw_keys)
        else:
            raise ValueError("update_key_columns must be a list of column names.")
        key_columns: list[str] = []
        for item in items:
            if not isinstance(item, str):
                raise ValueError("update_key_columns must contain column names.")
            name = self._validate_identifier(item, "update_key_columns")
            if name not in key_columns:
                key_columns.append(name)
        if len(key_columns) > MAX_UPDATE_KEY_COLUMNS:
            raise ValueError(f"update_key_columns allows at most {MAX_UPDATE_KEY_COLUMNS} columns.")
        validated["update_key_columns"] = key_columns

        kind = str(params.get("update_key_kind") or "unique").strip().lower()
        if kind not in ("unique", "primary"):
            raise ValueError("update_key_kind must be 'unique' or 'primary'.")
        validated["update_key_kind"] = kind

        integration_type = str(params.get("integration_type") or "IU").strip().upper()
        if integration_type not in INTEGRATION_TYPES:
            raise ValueError(f"integration_type must be one of {sorted(INTEGRATION_TYPES)}.")
        validated["integration_type"] = integration_type

        # Derive the key requirement from the KM that will really run, before any DDL.
        is_skey_lookup = (
            validated.get("needs_skey") is True
            and bool(validated.get("skey_natural_key"))
            and str(validated.get("tgt_table") or "").startswith("SKEY_")
        )
        if is_skey_lookup:
            # SKEY lookup mappings are always forced onto the merge KM with an update key.
            if integration_type != "IU":
                raise ValueError("An SKEY lookup mapping needs integration_type IU.")
        raw_ikm = params.get("ikm_name")
        if raw_ikm not in (None, ""):
            resolve_approved_ikm(raw_ikm, integration_type, ODI_APPROVED_IKMS, ODI_IKM_PRIORITY)
        effective = resolve_approved_ikm(
            SKEY_LOOKUP_IKM if is_skey_lookup else raw_ikm,
            integration_type, ODI_APPROVED_IKMS, ODI_IKM_PRIORITY,
        )
        validated["ikm_name"] = effective
        if (
            integration_type == "IU"
            and is_update_ikm(effective)
            and not key_columns
        ):
            raise UpdateKeyRequiredError(
                "the effective IKM requires an update key; pass update_key_columns "
                "(for an SKEY lookup target, the natural key column)."
            )

    # ── ODI Datastore Registration ──────────────────────────────────────────────────────

    def _register_odi_datastores(self, params: dict) -> dict:
        """Register source and target tables as datastores in the ODI model.

        create_mapping.groovy looks up datastores with dsFinder.findByName().
        A mapping cannot be created if the datastore is not registered in ODI.
        This step registers missing datastores using register_datastore.groovy.

        Args:
            params: Validated parameters dictionary.

        Returns:
            Dictionary: src_result, tgt_result.
        """
        from odi_orchestrator import OdiOrchestrator
        orch = OdiOrchestrator()
        self._ensure_odi_repository_bindings(params)

        results = {"src_result": None, "tgt_result": None}

        # ── Source datastore registration ──
        src_model = params["src_model"]
        src_table = params["src_table"]
        # SQL Server table: extract the table name from Schema.Table format
        if "." in src_table:
            parts = src_table.split(".", 1)
            src_ds_name = parts[1]
        else:
            src_ds_name = src_table

        # Fetch source column details
        _src_upper_reg = src_model.upper()
        _is_mssql_reg = ("SQL_SERVER" in _src_upper_reg or "MSSQL" in _src_upper_reg
                         or "ADVENTUREWORK" in _src_upper_reg)
        try:
            if _is_mssql_reg:
                src_columns, _ = self._get_source_columns_mssql(src_table)
            else:
                src_columns = self._source_columns_for(params)

            src_col_defs = self._odi_register_column_defs(src_columns)

            # Resolve the ODI model/datastore name from repository tables.
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
                              f"Source datastore exists: {odi_src_model_code}.{src_ds_name}")
            else:
                src_result = orch.register_datastore(
                    model_code=odi_src_model_code,
                    table_name=src_ds_name,
                    columns=src_col_defs,
                    resource_name=src_ds_name,
                )
                results["src_result"] = src_result

                if "hata:" in src_result.lower():
                    logger.warning("Source datastore registration warning: %s", src_result)
                else:
                    self._add_log("register_datastores", "SUCCESS",
                                   f"Source: {odi_src_model_code}.{src_ds_name}")
        except Exception as e:
            logger.warning("Source datastore registration failed (continuing): %s", e)
            results["src_result"] = f"WARNING: {e}"

        # ── Target datastore registration ──
        tgt_model = params["tgt_model"]
        tgt_table = params["tgt_table"]
        tgt_owner = params["tgt_owner"]
        key_columns = list(params.get("update_key_columns") or [])
        key_kind = params.get("update_key_kind") or "unique"
        key_kwargs: dict = {}
        key_failure: str | None = None
        if key_columns:
            from target_table_service import key_constraint_name

            key_kwargs = {
                "key_columns": key_columns,
                "key_name": key_constraint_name(tgt_table, key_columns, key_kind),
                "key_type": "PRIMARY_KEY" if key_kind == "primary" else "ALTERNATE_KEY",
            }

        try:
            # Read target Oracle columns
            from target_table_service import read_table_schema

            target_schema = read_table_schema(tgt_owner, tgt_table)
            tgt_columns = self._source_schema_df_to_legacy_columns(
                target_schema, get_source_connector("oracle")
            )
            tgt_col_defs = self._odi_register_column_defs(tgt_columns)

            # Resolve the ODI model/datastore name from repository tables.
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
                              f"Target datastore exists: {odi_tgt_model_code}.{tgt_ds_name}")
                if key_kwargs:
                    # Existing datastore: add the missing key only; never recreate it.
                    key_result = orch.register_datastore(
                        model_code=odi_tgt_model_code,
                        table_name=tgt_ds_name,
                        columns=tgt_col_defs,
                        resource_name=tgt_ds_name,
                        key_only=True,
                        **key_kwargs,
                    )
                    results["tgt_result"]["key_result"] = key_result
                    if not self._register_key_passed(key_result):
                        key_failure = "ODI update key registration failed for the target datastore."
            else:
                tgt_result = orch.register_datastore(
                    model_code=odi_tgt_model_code,
                    table_name=tgt_ds_name,
                    columns=tgt_col_defs,
                    resource_name=tgt_ds_name,
                    **key_kwargs,
                )
                results["tgt_result"] = tgt_result
                if key_kwargs and not self._register_key_passed(tgt_result):
                    key_failure = "ODI update key registration failed for the target datastore."

                if "hata:" in tgt_result.lower():
                    logger.warning("Target datastore registration warning: %s", tgt_result)
                else:
                    self._add_log("register_datastores", "SUCCESS",
                                   f"Target: {odi_tgt_model_code}.{tgt_ds_name}")
        except Exception as e:
            logger.warning("Target datastore registration failed (continuing): %s", e)
            results["tgt_result"] = f"WARNING: {e}"
            if key_kwargs:
                key_failure = "ODI update key registration failed for the target datastore."

        if key_failure:
            # A requested update key is a contract: do not continue to mapping creation without it.
            self._add_log("register_datastores", "FAILED", key_failure)
            raise RuntimeError(key_failure)

        return results

    @staticmethod
    def _register_key_passed(output: object) -> bool:
        """True when register_datastore reported a structured passed status with a key."""
        for line in str(output or "").splitlines():
            payload = _json_object(line)
            if payload is not None and str(payload.get("status", "")).lower() == "passed":
                return isinstance(payload.get("key"), dict)
        return False

    # ── Create ODI Mapping ──────────────────────────────────────────────────

    def _create_odi_mapping(self, params: dict) -> dict:
        """Create an ODI mapping through OdiOrchestrator.

        Args:
            params: Validated parameters dictionary.

        Returns:
            Dictionary: odi_result (command output) and sequence_name.
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
        
        # Special expressions for NK and SK columns in SKEY tables
        expressions = {}

        if params.get("needs_skey") and params.get("skey_natural_key"):
            nk_src = params["skey_natural_key"].strip()
            if not nk_src.upper().startswith("SRC."):
                nk_src = f"SRC.{nk_src}"
            
            # 1. If the target is an SKEY lookup table: ODI Best Practice = SEQ.NEXTVAL + IKM Merge + UpdateKey(NATURAL_KEY)
            if params["tgt_table"].startswith("SKEY_") or params["tgt_table"].startswith("SKEY_MAP_"):
                # Create sequence name: SKEY_SALES_CUSTOMER → SEQ_SALES_CUSTOMER
                seq_suffix = params["tgt_table"].replace("SKEY_MAP_", "").replace("SKEY_", "")
                expressions["SKEY"] = f"{params['tgt_owner']}.SEQ_{seq_suffix}.NEXTVAL"
                expressions["NATURAL_KEY"] = f"TO_CHAR({nk_src})"
                # No NOT EXISTS filter — IKM Merge already writes MERGE INTO through UpdateKey(NATURAL_KEY)
                # (prevents ORA-00001 entirely, avoids inserting duplicate records, updates existing records)
                logger.info(
                    "SKEY lookup mapping: SEQ.NEXTVAL + IKM=%s, no filter.", params["ikm_name"]
                )
                
            # 2. For an actual dimension/fact table: CASE WHEN EXISTS (SKEY lookup)
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

        audit_columns: list[str] = []
        for column, expression in self._audit_preservation_expressions.items():
            if column not in expressions:
                expressions[column] = expression
                audit_columns.append(column)

        integration_type = params.get("integration_type") or "IU"
        update_key_columns = (
            list(params.get("update_key_columns") or []) if integration_type == "IU" else []
        )

        result = orch.generate_mapping(
            mapping_name=params["mapping_name"],
            src_model=odi_src_model,
            src_table=src_table,
            tgt_model=odi_tgt_model,
            tgt_table=tgt_table,
            sequence_name=seq_name,
            expressions=expressions,
            replace_existing_mapping=params.get("replace_existing_mapping") is True,
            filter_condition=None,          # No NOT EXISTS — IKM Merge handles it
            ikm_name=params.get("ikm_name"),
            required_ikm_id=params.get("_odi_ikm_id"),
            source_identity=params.get("_odi_source_identity"),
            integration_type=integration_type,
            required_update_key_columns=update_key_columns or None,
            required_update_key_type=(
                "PRIMARY_KEY" if params.get("update_key_kind") == "primary" else "ALTERNATE_KEY"
            ) if update_key_columns else None,
            audit_preservation_columns=audit_columns or None,
            audit_lookup_alias=self._audit_lookup_alias if audit_columns else None,
        )

        # OdiOrchestrator returns "KOMUT HAZIRLANDI: ..." in stub mode.
        # Structured Groovy output reports whether the previous mapping was replaced
        # or kept because validation failed before the delete was committed.
        state = mapping_replace_state_from_output(result)
        if create_mapping_output_failed(result):
            failure_class = state["error_type"] or "MappingCreationFailure"
            self._add_log("create_mapping", "FAILED", failure_class)
            raise MappingCreationFailure(
                f"Could not create ODI mapping. error_type={failure_class}",
                state,
            )

        self._add_log("create_mapping", "SUCCESS", result[:100])
        return {
            "odi_result": result,
            "sequence_name": seq_name,
            "source_model": odi_src_model,
            "source_datastore": src_table,
            "target_model": odi_tgt_model,
            "target_datastore": tgt_table,
            "replaced": state["replaced"] is True,
            "kept_existing_on_failure": False,
            "error_type": None,
            "audit_preservation": dict(self._audit_preservation_public),
        }

    # ── Create SKEY ─────────────────────────────────────────────────────────

    def _create_skey(self, params: dict) -> dict:
        """Create surrogate key objects (table + sequence + expression).

        Args:
            params: Validated parameters dictionary (needs_skey must be True).

        Returns:
            Dictionary: skey_result (creation result) and skey_expression.
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

    # ── Lineage Record ──────────────────────────────────────────────────────────

    def _populate_lineage(self, params: dict) -> dict:
        """Create the DATA_LINEAGE record for the mapping.

        Args:
            params: Validated parameters dictionary.

        Returns:
            Dictionary: lineage_result.
        """
        from lineage_manager import LineageManager

        with LineageManager() as lm:
            try:
                lineage_result = lm.populate_from_odi()
                self._add_log("populate_lineage", "SUCCESS",
                               f"Inserted: {lineage_result.get('inserted', 0)}")
                return {"lineage_result": lineage_result}
            except Exception as e:
                # Lineage population failure is noncritical; continue
                self._add_log("populate_lineage", "SKIPPED", str(e))
                return {"lineage_result": {"error": str(e), "inserted": 0}}

    # ── DQ Test Generation ────────────────────────────────────────────────────────

    def _generate_tests(self, params: dict) -> dict:
        """Generate DQ tests for the target table and register them in RAG.

        Args:
            params: Validated parameters dictionary.

        Returns:
            Dictionary: tests list.
        """
        from test_generator import TestGenerator

        tg = TestGenerator()
        try:
            tests = tg.generate_and_store_in_rag(
                owner=params["tgt_owner"],
                table_name=params["tgt_table"],
            )
            self._add_log("generate_tests", "SUCCESS", f"{len(tests)} tests generated")
            return {"tests": tests}
        except Exception as e:
            # Test generation is noncritical
            self._add_log("generate_tests", "SKIPPED", str(e))
            return {"tests": [], "error": str(e)}
        finally:
            tg.close()

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def _take_snapshot(self, params: dict) -> dict:
        """Take the initial snapshot of the mapping.

        Args:
            params: Validated parameters dictionary.

        Returns:
            Dictionary: snap_id.
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

    # ── MAPPING_LOG Record ──────────────────────────────────────────────────────

    def _log_to_mapping_log(self, params: dict, status: str = "SUCCESS",
                             error_message: str = None):
        """Record the result in AUDIT_USER.MAPPING_LOG.

        Args:
            params: Validated parameters dictionary.
            status: "SUCCESS" or "FAILED".
            error_message: Error message (optional).
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

    # ── Main orchestration ───────────────────────────────────────────────────────

    def generate_from_prompt(self, user_prompt: str) -> dict:
        """Create an end-to-end mapping from a natural-language prompt.

        This method runs all steps in order:
          1. Parse prompt with AI
          2. Validate parameters
          3. Create target table (type conversion + ODS default columns)
          4. Create ODI mapping
          5. Create SKEY (optional)
          6. Record lineage
          7. Generate DQ tests
          8. Snapshot

        Args:
            user_prompt: User's natural-language request.

        Returns:
            Dictionary: batch_id, params, steps (result of each step),
            overall_status and log.
        """
        # Step 1: Parse prompt
        params = self.parse_prompt(user_prompt)
        return self.generate_from_params(params)

    def generate_from_params(self, params: dict) -> dict:
        """Create an end-to-end mapping from structured parameters.

        Args:
            params: Mapping parameters (parse_prompt output or
                provided by the user).

        Returns:
            Dictionary: batch_id, params, steps (result of each step),
            overall_status and log.
        """
        result = {
            "batch_id": self._batch_id,
            "params": None,
            "steps": {},
            "overall_status": "SUCCESS",
            "log": [],
        }
        persistent_steps_started = False

        try:
            # Step 2: Validation
            validated = self.validate_params(params)
            result["params"] = validated

            result["steps"]["repository_preflight"] = self._preflight_odi_repository(validated)
            repo_result = self._resolve_odi_repository_bindings(validated)
            result["steps"]["resolve_odi_repository"] = repo_result

            # Step 3: Create target table (SQL Server → Oracle type conversion + ODS default columns)
            persistent_steps_started = True
            tgt_result = self._create_target_table(validated)
            result["steps"]["create_target_table"] = tgt_result

            # Step 4: Register ODI datastores (source + target)
            ds_result = self._register_odi_datastores(validated)
            result["steps"]["register_datastores"] = ds_result

            # Step 5: ODI Mapping
            odi_result = self._create_odi_mapping(validated)
            result["steps"]["create_mapping"] = odi_result

            # Step 6: SKEY (optional)
            if validated["needs_skey"]:
                skey_result = self._create_skey(validated)
                result["steps"]["create_skey"] = skey_result
            else:
                self._add_log("create_skey", "SKIPPED", "SKEY not requested")
                result["steps"]["create_skey"] = {"skipped": True}

            # Step 6: Lineage
            lineage_result = self._populate_lineage(validated)
            result["steps"]["populate_lineage"] = lineage_result

            # Step 7: DQ Tests
            test_result = self._generate_tests(validated)
            result["steps"]["generate_tests"] = test_result

            # Step 8: Snapshot
            snap_result = self._take_snapshot(validated)
            result["steps"]["take_snapshot"] = snap_result

            # Successful MAPPING_LOG record
            self._log_to_mapping_log(validated, "SUCCESS")

        except (ValueError, RuntimeError) as e:
            result["overall_status"] = "FAILED"
            result["error"] = str(e)
            if getattr(e, "error_code", None):
                result["error_code"] = e.error_code
            if isinstance(e, TargetTableBlockedError):
                result["steps"]["create_target_table"] = e.detail
            if isinstance(e, RepositoryPreflightBlockedError):
                result["steps"]["repository_preflight"] = {"status": "BLOCKED", **e.detail}
            if isinstance(e, MappingCreationFailure):
                result["replaced"] = e.replaced
                result["kept_existing_on_failure"] = e.kept_existing_on_failure
                if e.error_type:
                    result["error_type"] = e.error_type
            failure_class = type(e).__name__
            if isinstance(e, MappingCreationFailure) and e.error_type:
                failure_class = e.error_type
            self._add_log("pipeline", "FAILED", failure_class)

            # Failed MAPPING_LOG record
            if persistent_steps_started and result.get("params"):
                self._log_to_mapping_log(result["params"], "FAILED", str(e))

        except Exception as e:
            result["overall_status"] = "FAILED"
            result["error"] = f"Unexpected error: {str(e)}"
            self._add_log("pipeline", "FAILED", str(e))
            logger.error("MappingGenerator unexpected error: %s", e, exc_info=True)

        result["log"] = self._log_entries
        return result

    # ── Update Mapping ───────────────────────────────────────────────────

    def update_mapping(self, mapping_name: str, changes: dict) -> dict:
        """Update an existing ODI mapping.

        Update steps:
          1. Take a snapshot of the existing mapping (previous state)
          2. Update ODI mapping (OdiOrchestrator)
          3. Update SKEY (if needed)
          4. Refresh lineage
          5. Regenerate DQ tests
          6. Take a new snapshot (subsequent state)

        Args:
            mapping_name: Name of the mapping to update.
            changes: Changes dictionary. Supported keys:
                - expressions: dict — Column expression update (e.g.: {"NAMEE": "Product.Name"})
                - filter_action: str — Filter operation type (add/modify/remove)
                - filter_condition: str — Filter condition
                - filter_name: str — Existing filter name
                - ikm_name: str — Change IKM
                - lkm_name: str — Change LKM
                - new_mapping_name: Mapping rename
                - tgt_table, tgt_owner: Target table details for pre-validation
                - needs_skey, skey_natural_key: SKEY change

        Returns:
            Dictionary: batch_id, mapping_name, changes_applied, steps, log.
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

            # Check whether the mapping exists; otherwise find the closest match
            exists, suggested = self._check_mapping_exists(mapping_name)
            if not exists:
                if suggested:
                    err_msg = f"Mapping found: {mapping_name} was not found. Closest mapping: {suggested}. Please use it."
                    self._add_log("check_mapping", "FAILED", err_msg)
                    raise ValueError(err_msg)
                else:
                    err_msg = f"Mapping not found: {mapping_name} was not found and no similar match was found."
                    self._add_log("check_mapping", "FAILED", err_msg)
                    raise ValueError(err_msg)

            # Step 1: Previous snapshot (state before changes)
            pre_snap = self._take_snapshot_for_update(mapping_name, "PRE_UPDATE")
            result["steps"]["pre_snapshot"] = pre_snap

            # Step 2: Update ODI mapping
            update_params = {"mapping_name": mapping_name}
            update_params.update(changes)

            # Validate any new table names in the changes
            for field in ["src_table", "tgt_table"]:
                if field in changes and changes[field]:
                    # Clean schema: "SCHEMA.TABLE" -> "TABLE"
                    val = str(changes[field]).strip().upper()
                    if "." in val:
                        val = val.split('.')[-1]
                    changes[field] = self._validate_identifier(val, field)

            if "tgt_owner" in changes:
                allowed = {"DWH", "ODS", "AUDIT_USER", "DWH_FLAT"}
                if changes["tgt_owner"].upper() not in allowed:
                    raise ValueError(f"Invalid tgt_owner: {changes['tgt_owner']}")
                changes["tgt_owner"] = changes["tgt_owner"].upper()

            odi_result = self._update_odi_mapping(mapping_name, changes)
            result["steps"]["update_mapping"] = odi_result

            # Step 3: Update SKEY (if needed)
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
                    self._add_log("update_skey", "SKIPPED", "skey_natural_key was not specified")
            else:
                self._add_log("update_skey", "SKIPPED", "No SKEY change")

            # Step 4: Refresh lineage
            lineage_result = self._populate_lineage(update_params)
            result["steps"]["refresh_lineage"] = lineage_result

            # Step 5: Regenerate DQ tests
            tgt_owner = changes.get("tgt_owner", "DWH")
            tgt_table = changes.get("tgt_table")
            if tgt_table:
                test_result = self._generate_tests({"tgt_owner": tgt_owner, "tgt_table": tgt_table})
                result["steps"]["regenerate_tests"] = test_result
            else:
                self._add_log("regenerate_tests", "SKIPPED", "Target table did not change")

            # Step 6: Subsequent snapshot (state after changes)
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
            result["error"] = f"Unexpected error: {str(e)}"
            self._add_log("update_pipeline", "FAILED", str(e))
            logger.error("MappingGenerator update failed: %s", e, exc_info=True)

        result["log"] = self._log_entries
        return result

    def _update_odi_mapping(self, mapping_name: str, changes: dict) -> dict:
        """Surgical update: modify the existing mapping through OdiOrchestrator.

        The operation type is detected automatically from the keys in the changes dictionary:
            - expressions key → update_expression
            - filter_action key → update_filter
            - ikm_name/lkm_name key → update_km
            - new_mapping_name key → rename

        Args:
            mapping_name: Name of the mapping to update.
            changes: Changes dictionary. Supported keys:
                - expressions: dict (e.g. {"NAMEE": "Product.Name"})
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
            Dictionary: odi_result.
        """
        from odi_orchestrator import OdiOrchestrator

        orch = OdiOrchestrator()

        # Detect operation type automatically
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

        # Pre-validation: validate expression updates in the Oracle DB
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
            raise RuntimeError(f"ODI mapping update failed: {result}")

        self._add_log("update_mapping", "SUCCESS", result[:100])
        return {"odi_result": result}

    def _take_snapshot_for_update(self, mapping_name: str, label_prefix: str) -> dict:
        """Take a snapshot before/after an update.

        Args:
            mapping_name: Mapping name.
            label_prefix: Snapshot label prefix (PRE_UPDATE/POST_UPDATE).

        Returns:
            Dictionary: snap_id.
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

    # ── Delete Mapping ──────────────────────────────────────────────────────────

    def delete_mapping(self, mapping_name: str, tgt_owner: str = "DWH") -> dict:
        """Delete a mapping and its associated objects (rollback).

        Steps:
          1. Take a final snapshot (state before deletion)
          2. Restore associated DDL with the rollback manager
          3. Record the DELETE operation in MAPPING_LOG

        Args:
            mapping_name: Mapping name to delete.
            tgt_owner: Target schema. Default: "DWH".

        Returns:
            Dictionary: batch_id, mapping_name, steps, log.
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

            # Step 1: Snapshot before deletion
            pre_snap = self._take_snapshot_for_update(mapping_name, "PRE_DELETE")
            result["steps"]["pre_snapshot"] = pre_snap

            # Step 2: Delete the physical ODI Mapping object with OdiOrchestrator
            from odi_orchestrator import OdiOrchestrator
            orch = OdiOrchestrator()
            odi_del_result = orch.delete_mapping(mapping_name)
            if "Hata:" in odi_del_result:
                self._add_log("odi_delete", "FAILED", odi_del_result)
                raise RuntimeError(f"ODI could not delete it: {odi_del_result}")
            else:
                self._add_log("odi_delete", "SUCCESS", odi_del_result[:100])
                result["steps"]["odi_delete"] = odi_del_result

            # Step 3: Rollback (restore associated DDL objects, if any)
            from rollback_manager import RollbackManager
            with RollbackManager() as rm:
                try:
                    # If deleting a table/DDL using a previous batch_id, the latest ID can also be retrieved from MAPPING_LOG.
                    # For now, the operation runs through a new batch
                    rollback_result = rm.rollback_batch(self._batch_id)
                    self._add_log("rollback_objects", "SUCCESS",
                                   f"Rolled back: {rollback_result}")
                    result["steps"]["rollback"] = rollback_result
                except Exception as e:
                    self._add_log("rollback_objects", "SKIPPED",
                                   f"No rollback record found: {e}")
                    result["steps"]["rollback"] = {"skipped": True, "reason": str(e)}

            # Step 3: DELETE record in MAPPING_LOG
            self._log_to_mapping_log(
                {"mapping_name": mapping_name, "tgt_owner": tgt_owner},
                "SUCCESS"
            )
            self._add_log("delete_log", "SUCCESS", f"{mapping_name} deleted")

        except (ValueError, RuntimeError) as e:
            result["overall_status"] = "FAILED"
            result["error"] = str(e)
            self._add_log("delete_pipeline", "FAILED", str(e))
        except Exception as e:
            result["overall_status"] = "FAILED"
            result["error"] = f"Unexpected error: {str(e)}"
            logger.error("MappingGenerator delete failed: %s", e, exc_info=True)

        result["log"] = self._log_entries
        return result

    # ── Prompt Analysis ─────────────────────────────────────────────────────────

    def analyze_prompt(self, user_prompt: str) -> dict:
        """Parse the prompt without creating a mapping (preview).

        Args:
            user_prompt: User's natural-language request.

        Returns:
            Dictionary: params (parse result), validation (validation result),
            preview (preview details).
        """
        result = {"prompt": user_prompt, "params": None, "validation": None, "preview": None}

        try:
            params = self.parse_prompt(user_prompt)
            result["params"] = params

            validated = self.validate_params(params)
            result["validation"] = {"status": "VALID", "params": validated}

            # Preview details
            preview = {
                "mapping_name": validated["mapping_name"],
                "flow": f"{validated['src_model']}.{validated['src_table']} --> "
                        f"{validated['tgt_owner']}.{validated['tgt_table']}",
                "will_create_skey": validated["needs_skey"],
                "steps_to_run": [
                    f"Create target table: {validated['tgt_owner']}.{validated['tgt_table']}",
                    "Create ODI mapping",
                ],
            }
            if validated["needs_skey"]:
                preview["steps_to_run"].append(
                    f"Create SKEY objects (SKEY_MAP_{validated['tgt_table']}, "
                    f"SEQ_{validated['tgt_table']})"
                )
                preview["steps_to_run"].append(
                    f"SKEY Expression: CASE WHEN ... {validated['skey_natural_key']} ..."
                )
            preview["steps_to_run"].extend([
                "Record lineage (DATA_LINEAGE)",
                "Generate DQ tests + register in RAG",
                "Take mapping snapshot",
            ])
            result["preview"] = preview

        except (ValueError, RuntimeError) as e:
            result["validation"] = {"status": "INVALID", "error": str(e)}

        return result
