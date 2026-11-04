"""Generic, connectionless Type 2 (SCD2) planner for EDW and Datamart targets.

The planner turns a validated request plus source column metadata into a
reviewable target contract: surrogate key, natural key, tracked attributes,
effective dating, current flag and audit columns. It never opens a connection
and never touches rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

IDENTIFIER_RE = re.compile(r"^[A-Z0-9_$#]{1,30}$")
# ODI scenario names are limited to 35 characters; "<mapping>_SCN" must fit.
MAPPING_NAME_MAX = 31
MAPPING_NAME_RE = re.compile(r"^[A-Z0-9_$#]{1,%d}$" % MAPPING_NAME_MAX)
_TYPE_SQL_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:\([0-9]+(?:,[0-9]+| (?:CHAR|BYTE))?\))?$")

TARGET_OWNERS = ("DWH", "DM")
# ODI model code per schema owner. Targets must be DWH or DM; sources may also
# be the HR and SH source models that already exist in the repository.
TARGET_MODEL_BY_OWNER = {"DWH": "TGT_DWH", "DM": "TGT_DM"}
SOURCE_MODEL_BY_OWNER = {
    "DWH": "TGT_DWH",
    "DM": "TGT_DM",
    "HR": "SRC_HR",
    "SH": "SRC_SH",
}

SCD2_COLUMNS = (
    ("EFFECTIVE_FROM", "DATE", False),
    ("EFFECTIVE_TO", "DATE", False),
    ("CURRENT_FLAG", "VARCHAR2(1)", False),
    ("INSERT_DATE", "TIMESTAMP", False),
    ("UPDATE_DATE", "TIMESTAMP", True),
    ("SOURCE_SYSTEM", "VARCHAR2(50)", False),
    ("ETL_BATCH_ID", "NUMBER", False),
)
RESERVED_COLUMNS = frozenset(name for name, _t, _n in SCD2_COLUMNS)
OPEN_ENDED_DATE_SQL = "TO_DATE('24000101','YYYYMMDD')"
SOURCE_SYSTEM_LITERAL = "ODI_SCD2"

_UNSUPPORTED_TYPE_PREFIXES = ("CLOB", "NCLOB", "BLOB", "BFILE", "LONG", "RAW", "XMLTYPE", "ROWID", "UROWID")
_PASSTHROUGH_BASES = {
    "NUMBER",
    "FLOAT",
    "DATE",
    "TIMESTAMP",
    "VARCHAR2",
    "NVARCHAR2",
    "CHAR",
    "NCHAR",
}


class Scd2PlanError(ValueError):
    """Raised with a stable machine-readable code for an invalid request."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _identifier(value: object, code: str, pattern: re.Pattern[str] = IDENTIFIER_RE) -> str:
    text = str(value or "").strip().upper()
    if not pattern.fullmatch(text):
        raise Scd2PlanError(code)
    if "$" in text:
        # ODI parameter transport rejects "$"; refuse early with a clear code.
        raise Scd2PlanError(code + "_unsupported_character")
    return text


def _mapping_name(value: object) -> str:
    text = str(value or "").strip().upper()
    if len(text) > MAPPING_NAME_MAX and re.fullmatch(r"[A-Z0-9_$#]+", text):
        raise Scd2PlanError("mapping_name_too_long")
    return _identifier(text, "invalid_mapping_name", MAPPING_NAME_RE)


def _identifier_list(value: object, code: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise Scd2PlanError(code)
    names = [_identifier(item, code) for item in value]
    if len(set(names)) != len(names):
        raise Scd2PlanError(code + "_duplicate")
    return names


def identifier_list(value: object, code: str) -> list[str]:
    """Validate a list of unique identifiers (public form of the helper)."""
    return _identifier_list(value, code)


def normalize_source_columns(
    columns: Iterable[Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Normalize catalog-shaped metadata into name/data_type/nullable dicts."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in columns:
        if not isinstance(item, Mapping):
            raise Scd2PlanError("invalid_source_columns")
        name = str(item.get("name") or "").strip().upper()
        data_type = re.sub(r"\s+", "", str(item.get("data_type") or "").upper())
        data_type = re.sub(r"(\d)(BYTE|CHAR)\)", r"\1 \2)", data_type)
        if data_type.startswith("TIMESTAMP(") and ")" in data_type:
            data_type = "TIMESTAMP"
        if not IDENTIFIER_RE.fullmatch(name) or name in seen or not data_type:
            raise Scd2PlanError("invalid_source_columns")
        seen.add(name)
        normalized.append(
            {"name": name, "data_type": data_type, "nullable": bool(item.get("nullable", True))}
        )
    if not normalized:
        raise Scd2PlanError("source_columns_required")
    return normalized


def _supported_type(data_type: str) -> bool:
    if data_type.startswith(_UNSUPPORTED_TYPE_PREFIXES):
        return False
    if not _TYPE_SQL_RE.fullmatch(data_type):
        return False
    return data_type.split("(", 1)[0] in _PASSTHROUGH_BASES


def oracle_ddl_type(data_type: str) -> str:
    """Make an unqualified Oracle character size independent of session defaults."""
    return re.sub(r"^(VARCHAR2|CHAR)\(([0-9]+)\)$", r"\1(\2 BYTE)", data_type)


def surrogate_key_name(target_table: str, taken: Iterable[str]) -> str:
    stem = target_table
    for prefix in ("DIM_", "D_"):
        if stem.startswith(prefix) and len(stem) > len(prefix):
            stem = stem[len(prefix):]
            break
    name = f"{stem[:27]}_SK"
    if name in set(taken):
        raise Scd2PlanError("surrogate_key_collision")
    return name


def validate_request(request: Mapping[str, object]) -> dict[str, Any]:
    """Validate the caller request shape; no metadata is needed here."""
    if not isinstance(request, Mapping):
        raise Scd2PlanError("invalid_request")
    source_owner = _identifier(request.get("source_owner"), "invalid_source_owner")
    if source_owner not in SOURCE_MODEL_BY_OWNER:
        raise Scd2PlanError("source_owner_not_allowed")
    target_owner = _identifier(request.get("target_owner"), "invalid_target_owner")
    if target_owner not in TARGET_OWNERS:
        raise Scd2PlanError("target_owner_not_allowed")
    source_table = _identifier(request.get("source_table"), "invalid_source_table")
    target_table = _identifier(request.get("target_table"), "invalid_target_table")
    mapping_name = _mapping_name(request.get("mapping_name"))
    natural_key = _identifier_list(request.get("natural_key"), "invalid_natural_key")
    tracked_raw = request.get("tracked_columns")
    if isinstance(tracked_raw, str):
        if tracked_raw.strip().lower() != "all":
            raise Scd2PlanError("invalid_tracked_columns")
        tracked: list[str] | str = "all"
    else:
        tracked = _identifier_list(tracked_raw, "invalid_tracked_columns")
    grant_reader = request.get("grant_select_to_app_user", False)
    if not isinstance(grant_reader, bool):
        raise Scd2PlanError("invalid_grant_option")
    overwrite_raw = request.get("overwrite_columns")
    overwrite = _identifier_list(overwrite_raw, "invalid_overwrite_columns") if overwrite_raw else []
    sequence_raw = request.get("sequence_name")
    sequence = (
        _identifier(sequence_raw, "invalid_sequence_name")
        if sequence_raw not in (None, "")
        else _identifier(f"SEQ_{target_table}"[:30], "invalid_sequence_name")
    )
    if source_owner == target_owner and source_table == target_table:
        raise Scd2PlanError("source_equals_target")
    return {
        "source_owner": source_owner,
        "source_table": source_table,
        "target_owner": target_owner,
        "target_table": target_table,
        "mapping_name": mapping_name,
        "natural_key": natural_key,
        "tracked": tracked,
        "overwrite": overwrite,
        "grant_reader": grant_reader,
        "sequence_name": sequence,
    }


def _target_column(name: str, data_type: str, nullable: bool, role: str) -> dict[str, Any]:
    return {"name": name, "data_type": data_type, "nullable": nullable, "scd_role": role}


def default_exec_user() -> str:
    """Execution user from config (ODI_EXEC_USER, default ODI_EXEC)."""
    from config import ODI_EXEC_USER

    return ODI_EXEC_USER


def grants_enabled() -> bool:
    """Whether the executor grants DML to the ODI execution user (ODI_EXEC_GRANTS_ENABLED, default true)."""
    from config import ODI_EXEC_GRANTS_ENABLED

    return bool(ODI_EXEC_GRANTS_ENABLED)


def build_scd2_plan(
    request: Mapping[str, object],
    source_columns: Iterable[Mapping[str, object]],
    exec_user: str | None = None,
) -> dict[str, Any]:
    """Build the connectionless Type 2 target contract.

    Raises ``Scd2PlanError`` for invalid input. The returned dictionary holds
    only metadata: no rows, no key values, no credentials.
    """
    req = validate_request(request)
    grantee = _identifier(exec_user or default_exec_user(), "invalid_exec_user")
    source = normalize_source_columns(source_columns)
    by_name = {column["name"]: column for column in source}

    missing_nk = [name for name in req["natural_key"] if name not in by_name]
    if missing_nk:
        raise Scd2PlanError("natural_key_not_in_source", ",".join(missing_nk))

    overwrite = list(req["overwrite"])
    missing_ow = [name for name in overwrite if name not in by_name]
    if missing_ow:
        raise Scd2PlanError("overwrite_not_in_source", ",".join(missing_ow))
    if any(name in req["natural_key"] for name in overwrite):
        raise Scd2PlanError("overwrite_overlaps_natural_key")
    if req["tracked"] == "all":
        tracked = [
            c["name"]
            for c in source
            if c["name"] not in req["natural_key"] and c["name"] not in overwrite
        ]
    else:
        tracked = list(req["tracked"])
        missing = [name for name in tracked if name not in by_name]
        if missing:
            raise Scd2PlanError("tracked_not_in_source", ",".join(missing))
        overlap = [name for name in tracked if name in req["natural_key"]]
        if overlap:
            raise Scd2PlanError("tracked_overlaps_natural_key", ",".join(overlap))
        if any(name in overwrite for name in tracked):
            raise Scd2PlanError("tracked_overlaps_overwrite")
    if not tracked:
        raise Scd2PlanError("no_tracked_columns")

    business = [*req["natural_key"], *tracked, *overwrite]
    reserved_hit = [name for name in business if name in RESERVED_COLUMNS]
    if reserved_hit:
        raise Scd2PlanError("reserved_column_name", ",".join(reserved_hit))
    unsupported = [name for name in business if not _supported_type(by_name[name]["data_type"])]
    if unsupported:
        raise Scd2PlanError("unsupported_column_type", ",".join(unsupported))

    sk_name = surrogate_key_name(req["target_table"], business)
    columns: list[dict[str, Any]] = [_target_column(sk_name, "NUMBER", False, "SURROGATE_KEY")]
    columns.extend(
        _target_column(name, by_name[name]["data_type"], False, "NATURAL_KEY")
        for name in req["natural_key"]
    )
    columns.extend(
        _target_column(name, by_name[name]["data_type"], by_name[name]["nullable"], "ADD_ROW_ON_CHANGE")
        for name in tracked
    )
    columns.extend(
        _target_column(name, by_name[name]["data_type"], by_name[name]["nullable"], "OVERWRITE_ON_CHANGE")
        for name in overwrite
    )
    scd_roles = {
        "EFFECTIVE_FROM": "START_TIMESTAMP",
        "EFFECTIVE_TO": "END_TIMESTAMP",
        "CURRENT_FLAG": "CURRENT_RECORD_FLAG",
    }
    columns.extend(
        _target_column(name, data_type, nullable, scd_roles.get(name, "NONE"))
        for name, data_type, nullable in SCD2_COLUMNS
    )
    if len(columns) > 1000:
        raise Scd2PlanError("too_many_columns")

    return {
        "mode": "scd2_static_plan",
        "status": "planned",
        "source": {"owner": req["source_owner"], "table": req["source_table"]},
        "target": {"owner": req["target_owner"], "table": req["target_table"]},
        "mapping_name": req["mapping_name"],
        "scenario_name": f"{req['mapping_name']}_SCN",
        "scenario_version": "001",
        "models": {
            "source": SOURCE_MODEL_BY_OWNER[req["source_owner"]],
            "target": TARGET_MODEL_BY_OWNER[req["target_owner"]],
        },
        "surrogate_key": {
            "column": sk_name,
            "data_type": "NUMBER",
            "sequence": req["sequence_name"],
            "generation": "sequence_per_row_version",
        },
        "natural_key": list(req["natural_key"]),
        "tracked_attributes": tracked,
        "overwrite_attributes": overwrite,
        "target_columns": columns,
        "primary_key": {"name": _pk_name(req["target_table"]), "columns": [sk_name]},
        "alternate_key": {"name": _ak_name(req["target_table"]), "columns": list(req["natural_key"])},
        "options": {"grant_select_to_app_user": req["grant_reader"], "exec_user": grantee},
        "knowledge_modules": {
            "ikm": "IKM Oracle Slowly Changing Dimension",
            "lkm": "LKM SQL to Oracle (Built-In)",
            "integration_type": "SCD",
        },
        "dq_gates": [
            "effective_date_order",
            "approved_current_flag_values",
            "single_current_row_per_natural_key",
        ],
        "requires_user_approval": True,
        "connection_attempted": False,
        "write_operations_attempted": [],
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
    }


def _pk_name(target_table: str) -> str:
    return f"PK_{target_table}"[:30]


def _ak_name(target_table: str) -> str:
    return f"AK_{target_table}"[:30]


def render_create_table(plan: Mapping[str, Any]) -> str:
    """Render the CREATE TABLE statement for the planned target."""
    target = plan["target"]
    definitions = []
    for column in plan["target_columns"]:
        data_type = str(column["data_type"])
        if not _TYPE_SQL_RE.fullmatch(data_type):
            raise Scd2PlanError("unsupported_column_type", str(column["name"]))
        null_sql = "" if column["nullable"] else " NOT NULL"
        definitions.append(f'  "{column["name"]}" {oracle_ddl_type(data_type)}{null_sql}')
    pk = plan["primary_key"]
    definitions.append(
        f'  CONSTRAINT "{pk["name"]}" PRIMARY KEY ("{pk["columns"][0]}")'
    )
    return (
        f'CREATE TABLE "{target["owner"]}"."{target["table"]}" (\n'
        + ",\n".join(definitions)
        + "\n)"
    )


def render_create_sequence(plan: Mapping[str, Any]) -> str:
    owner = plan["target"]["owner"]
    sequence = plan["surrogate_key"]["sequence"]
    return f'CREATE SEQUENCE "{owner}"."{sequence}" START WITH 1 INCREMENT BY 1 NOCYCLE'


def _all_grants(plan: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    table = plan["target"]["table"]
    sequence = plan["surrogate_key"]["sequence"]
    exec_user = plan["options"]["exec_user"]
    grants = [(table, exec_user, privilege) for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")]
    grants.append((sequence, exec_user, "SELECT"))
    if plan.get("options", {}).get("grant_select_to_app_user") is True:
        grants.append((table, "APP_USER", "SELECT"))
    return grants


def grant_skip_flags(plan: Mapping[str, Any]) -> dict[str, bool]:
    """Boolean flags explaining why grants are skipped (only true flags are returned)."""
    if not grants_enabled():
        return {"grants_skipped_by_config": True}
    owner = str(plan["target"]["owner"]).upper()
    if any(grantee.upper() == owner for _name, grantee, _privilege in _all_grants(plan)):
        return {"grants_skipped_owner_is_grantee": True}
    return {}


def required_grants(plan: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Required (object name, grantee, privilege) triples.

    The execution user (``ODI_EXEC_USER``, default ODI_EXEC) only by default. A SELECT for APP_USER (needed by the app's own
    count-only postflight read) is opt-in via ``grant_select_to_app_user``. Returns an empty list when grants are
    disabled by config (``ODI_EXEC_GRANTS_ENABLED=false``); grants whose grantee is the target owner are dropped
    because an owner cannot grant to itself (ORA-01749).
    """
    if not grants_enabled():
        return []
    owner = str(plan["target"]["owner"]).upper()
    return [g for g in _all_grants(plan) if g[1].upper() != owner]


def render_grant_statement(plan: Mapping[str, Any], grant: tuple[str, str, str]) -> str:
    name, grantee, privilege = grant
    return f'GRANT {privilege} ON "{plan["target"]["owner"]}"."{name}" TO {grantee}'


def render_grant_statements(plan: Mapping[str, Any]) -> list[str]:
    return [render_grant_statement(plan, grant) for grant in required_grants(plan)]


def required_grantees(plan: Mapping[str, Any]) -> list[str]:
    """Distinct grantee users that must exist before any DDL runs."""
    return sorted({grantee for _name, grantee, _privilege in required_grants(plan)})


def expected_target_metadata(plan: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    return [
        (str(c["name"]), str(c["data_type"]), bool(c["nullable"])) for c in plan["target_columns"]
    ]


def odi_expressions(plan: Mapping[str, Any]) -> dict[str, str]:
    """Fixed (non-source) target expressions, shown in the plan for review."""
    owner = plan["target"]["owner"]
    return {
        plan["surrogate_key"]["column"]: f"{owner}.{plan['surrogate_key']['sequence']}.NEXTVAL",
        "EFFECTIVE_FROM": "CURRENT_DATE",
        "EFFECTIVE_TO": OPEN_ENDED_DATE_SQL,
        "CURRENT_FLAG": "1",
        "INSERT_DATE": "CURRENT_TIMESTAMP",
        "UPDATE_DATE": "CURRENT_TIMESTAMP",
        "SOURCE_SYSTEM": f"'{SOURCE_SYSTEM_LITERAL}'",
        "ETL_BATCH_ID": '<%=odiRef.getSession("SESS_NO")%>',
    }


def groovy_params(plan: Mapping[str, Any]) -> dict[str, str]:
    """System-property parameters for the generic SCD2 Groovy actions.

    Values are drawn only from the validated plan; the caller never chooses
    the script, only identifiers that already passed validation.
    """
    columns = plan["target_columns"]
    return {
        "odi.scd2.source_model": plan["models"]["source"],
        "odi.scd2.source_table": plan["source"]["table"],
        "odi.scd2.target_model": plan["models"]["target"],
        "odi.scd2.target_owner": plan["target"]["owner"],
        "odi.scd2.target_table": plan["target"]["table"],
        "odi.scd2.mapping": plan["mapping_name"],
        "odi.scd2.sequence": plan["surrogate_key"]["sequence"],
        "odi.scd2.sk": plan["surrogate_key"]["column"],
        "odi.scd2.natural_key": ",".join(plan["natural_key"]),
        "odi.scd2.tracked": ",".join(plan["tracked_attributes"]),
        "odi.scd2.overwrite": ",".join(plan["overwrite_attributes"]) or "none",
        "odi.scd2.column_types": ",".join(
            f"{c['name']}:{_param_type(oracle_ddl_type(str(c['data_type'])))}" for c in columns
        ),
        "odi.scd2.column_nullable": ",".join(
            f"{c['name']}:{'Y' if c['nullable'] else 'N'}" for c in columns
        ),
    }


def _param_type(data_type: str) -> str:
    """Encode a SQL type for the parameter transport (no parentheses issues)."""
    return data_type.replace("(", "_").replace(")", "").replace(",", "_").replace(" ", "_")


def probe_params(
    source_owner: str,
    source_table: str,
    target_owner: str,
    target_table: str,
    mapping_name: str,
) -> dict[str, str]:
    """Validated parameters for the read-only ODI readiness probe."""
    src_owner = _identifier(source_owner, "invalid_source_owner")
    tgt_owner = _identifier(target_owner, "invalid_target_owner")
    if src_owner not in SOURCE_MODEL_BY_OWNER:
        raise Scd2PlanError("source_owner_not_allowed")
    if tgt_owner not in TARGET_OWNERS:
        raise Scd2PlanError("target_owner_not_allowed")
    mapping = _mapping_name(mapping_name)
    return {
        "odi.scd2.source_model": SOURCE_MODEL_BY_OWNER[src_owner],
        "odi.scd2.source_table": _identifier(source_table, "invalid_source_table"),
        "odi.scd2.target_model": TARGET_MODEL_BY_OWNER[tgt_owner],
        "odi.scd2.target_table": _identifier(target_table, "invalid_target_table"),
        "odi.scd2.mapping": mapping,
    }
