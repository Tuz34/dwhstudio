"""Read-only Oracle user/security baseline audit.

The script intentionally reports only usernames, grants, object names, and
sanitized configuration metadata. It never prints passwords, API keys, raw
connection strings, or row data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


EXPECTED_USERS = ["APP_USER", "AUDIT_USER", "ODI_EXEC", "ODS_OWNER", "DWH_OWNER"]
CURRENT_ENV_USERS = ["APP_USER", "AUDIT_USER", "REPO_ODI_REPO", "DWH", "HR", "SH"]
EXPECTED_AUDIT_TABLES = [
    "DATA_LINEAGE",
    "ETL_WATERMARK",
    "GRANT_LOG",
    "MAPPING_LOG",
    "MAPPING_SNAPSHOT",
    "SCHEMA_HASH",
]
AUDIT_SCHEMA_OWNER = "AUDIT_USER"
APP_ALLOWED_DIRECT_PRIVS = {"CREATE SESSION", "CREATE SYNONYM"}
APP_BROAD_ROLES = {"SELECT_CATALOG_ROLE", "DBA", "EXP_FULL_DATABASE", "IMP_FULL_DATABASE"}
DDL_LIKE_PRIV_PREFIXES = ("CREATE ", "ALTER ", "DROP ")
DANGEROUS_ANY_PRIVS = {
    "CREATE ANY TABLE",
    "ALTER ANY TABLE",
    "DROP ANY TABLE",
    "CREATE ANY PROCEDURE",
    "ALTER ANY PROCEDURE",
    "DROP ANY PROCEDURE",
    "GRANT ANY PRIVILEGE",
    "GRANT ANY OBJECT PRIVILEGE",
    "SELECT ANY DICTIONARY",
    "SELECT ANY TABLE",
    "EXECUTE ANY PROCEDURE",
    "CREATE USER",
    "ALTER USER",
    "DROP USER",
}

PRODUCTIZED_CAPABILITIES = [
    {
        "capability": "read_only_analysis",
        "actor_config_key": "oracle_user",
        "default_actor": "APP_USER",
        "allowed_privileges": ["CREATE SESSION", "object SELECT grants only"],
        "forbidden_privileges": ["SELECT ANY TABLE", "SELECT ANY DICTIONARY", "DBA", "SELECT_CATALOG_ROLE"],
        "requires_live_approval": False,
    },
    {
        "capability": "data_quality_checks",
        "actor_config_key": "oracle_user",
        "default_actor": "APP_USER",
        "allowed_privileges": ["CREATE SESSION", "object SELECT grants only", "object INSERT into approved error tables"],
        "forbidden_privileges": ["ANY privileges", "DBA"],
        "requires_live_approval": False,
    },
    {
        "capability": "schema_sync_ddl",
        "actor_config_key": "oracle_ddl_user",
        "default_actor": "DWH",
        "allowed_privileges": ["schema owner DDL on approved target schemas"],
        "forbidden_privileges": ["SYSDBA", "ANY privileges outside approved target schemas"],
        "requires_live_approval": True,
    },
    {
        "capability": "odi_metadata_sync",
        "actor_config_key": "odi_repo_user",
        "default_actor": "REPO_ODI_REPO",
        "allowed_privileges": ["ODI repository metadata access through ODI SDK"],
        "forbidden_privileges": ["DBA", "SYSDBA", "application data owner privileges"],
        "requires_live_approval": True,
    },
    {
        "capability": "scenario_load_plan_run",
        "actor_config_key": "odi_repo_user",
        "default_actor": "REPO_ODI_REPO",
        "allowed_privileges": ["ODI scenario/load-plan runtime execution through approved agent"],
        "forbidden_privileges": ["direct broad DB DDL", "unapproved target truncate/load"],
        "requires_live_approval": True,
    },
    {
        "capability": "security_audit_read",
        "actor_config_key": "audit_runtime_user",
        "default_actor": AUDIT_SCHEMA_OWNER,
        "allowed_privileges": ["read redacted audit evidence", "append local audit events"],
        "forbidden_privileges": ["raw credential read", "raw row export"],
        "requires_live_approval": False,
    },
]


def mask_dsn(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"//([^:/@]+):([^@]+)@", r"//***:***@", text)
    text = re.sub(r"([^/\s:]+)/(.*)@", r"\1/***@", text)
    return text


def _rows(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _read_hardcoded_credential_findings(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    path = root / "setup_oracle_users.py"
    if not path.exists():
        return findings
    text = path.read_text(encoding="utf-8", errors="ignore")
    identified_literals = re.findall(r"IDENTIFIED BY\s+[\"']([^\"']+)[\"']", text, re.IGNORECASE)
    has_literal_identified_by = any("{" not in item and not item.startswith(":") for item in identified_literals)
    has_legacy_user_password_tuple = re.search(
        r'\("(APP_USER|AUDIT_USER|ODI_EXEC|ODS_OWNER|DWH_OWNER)",\s*"[^"]+",\s*"[^"]+"',
        text,
    )
    if has_literal_identified_by or has_legacy_user_password_tuple:
        findings.append({
            "severity": "high",
            "area": "credential_hygiene",
            "message": "setup_oracle_users.py appears to contain hardcoded credential literals; rotate and move to env/secret input.",
        })
    return findings


def classify_security_report(report: dict[str, Any], *, workspace_root: Path | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    accounts = report.get("accounts") or []
    sys_privs = report.get("system_privileges") or []
    role_privs = report.get("role_privileges") or []
    audit_tables = report.get("audit_tables") or []

    present_users = {str(row.get("USERNAME", "")).upper() for row in accounts}
    missing_design_users = [user for user in EXPECTED_USERS if user not in present_users]
    if missing_design_users:
        findings.append({
            "severity": "medium",
            "area": "account_model",
            "message": "Expected design users are missing; environment may be using a simplified DWH schema model.",
            "users": missing_design_users,
        })

    non_open = [
        row.get("USERNAME")
        for row in accounts
        if row.get("ACCOUNT_STATUS") and str(row.get("ACCOUNT_STATUS")).upper() != "OPEN"
    ]
    if non_open:
        findings.append({
            "severity": "high",
            "area": "account_status",
            "message": "One or more expected users are not OPEN.",
            "users": non_open,
        })

    for row in sys_privs:
        grantee = str(row.get("GRANTEE", "")).upper()
        privilege = str(row.get("PRIVILEGE", "")).upper()
        if privilege in DANGEROUS_ANY_PRIVS:
            severity = "high" if privilege != "SELECT ANY DICTIONARY" else "medium"
            findings.append({
                "severity": severity,
                "area": "system_privilege",
                "user": grantee,
                "privilege": privilege,
                "message": "Broad system privilege should be reviewed against least privilege.",
            })
        if grantee == "APP_USER" and privilege.startswith(DDL_LIKE_PRIV_PREFIXES) and privilege not in APP_ALLOWED_DIRECT_PRIVS:
            findings.append({
                "severity": "high",
                "area": "app_user_privilege",
                "user": grantee,
                "privilege": privilege,
                "message": "APP_USER should not hold direct DDL privileges beyond the approved baseline.",
            })

    for row in role_privs:
        grantee = str(row.get("GRANTEE", "")).upper()
        role = str(row.get("GRANTED_ROLE", "")).upper()
        if grantee == "APP_USER" and role in APP_BROAD_ROLES:
            severity = "high" if role == "DBA" else "medium"
            findings.append({
                "severity": severity,
                "area": "app_user_role",
                "user": grantee,
                "role": role,
                "message": "APP_USER has a broad role; acceptable for dev/TST only if explicitly scoped.",
            })

    present_audit_tables = {str(row.get("TABLE_NAME", "")).upper() for row in audit_tables}
    missing_audit_tables = [table for table in EXPECTED_AUDIT_TABLES if table not in present_audit_tables]
    if missing_audit_tables:
        findings.append({
            "severity": "medium",
            "area": "audit_infra",
            "message": "Expected audit infrastructure tables are missing.",
            "tables": missing_audit_tables,
        })

    if workspace_root:
        findings.extend(_read_hardcoded_credential_findings(workspace_root))

    return findings


def _config_actor(report: dict[str, Any], key: str, default: str) -> str:
    config = report.get("config") or {}
    return str(config.get(key) or default).upper()


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().upper()


def _has_runtime_sysdba(report: dict[str, Any]) -> bool:
    config = report.get("config") or {}
    runtime_user = _normalize_token(
        config.get("oracle_user")
        or config.get("runtime_oracle_user")
        or config.get("app_user")
    )
    if runtime_user == "SYS":
        return True

    runtime_modes = (
        config.get("oracle_connection_mode"),
        config.get("oracle_auth_mode"),
        config.get("runtime_connection_mode"),
        config.get("runtime_auth_mode"),
    )
    if any(_normalize_token(mode) == "SYSDBA" for mode in runtime_modes):
        return True

    session_rows = report.get("session") or []
    if isinstance(session_rows, dict):
        session_rows = [session_rows]
    for session in session_rows:
        if not isinstance(session, dict):
            continue
        if _normalize_token(session.get("SESSION_USER")) == "SYS":
            return True
        for key in ("AUTHENTICATION_TYPE", "AUTHENTICATION_METHOD", "AUTH_MODE", "CONNECTION_MODE"):
            if _normalize_token(session.get(key)) == "SYSDBA":
                return True

    if not runtime_user:
        return False
    for row in report.get("system_privileges") or []:
        if (
            _normalize_token(row.get("GRANTEE")) == runtime_user
            and _normalize_token(row.get("PRIVILEGE")) == "SYSDBA"
        ):
            return True
    for row in report.get("role_privileges") or []:
        if (
            _normalize_token(row.get("GRANTEE")) == runtime_user
            and _normalize_token(row.get("GRANTED_ROLE")) == "SYSDBA"
        ):
            return True
    return False


def build_least_privilege_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Build a redacted, read-only least-privilege migration plan.

    This does not execute GRANT/REVOKE/DDL. It only interprets already collected
    metadata into capability boundaries and suggested migration steps.
    """
    capability_matrix: list[dict[str, Any]] = []
    for capability in PRODUCTIZED_CAPABILITIES:
        capability_matrix.append({
            "capability": capability["capability"],
            "actor": _config_actor(report, capability["actor_config_key"], capability["default_actor"]),
            "actor_config_key": capability["actor_config_key"],
            "allowed_privileges": capability["allowed_privileges"],
            "forbidden_privileges": capability["forbidden_privileges"],
            "requires_live_approval": capability["requires_live_approval"],
        })

    migration_plan: list[dict[str, Any]] = []
    for row in report.get("system_privileges") or []:
        grantee = str(row.get("GRANTEE", "")).upper()
        privilege = str(row.get("PRIVILEGE", "")).upper()
        if grantee == "APP_USER" and privilege in {"SELECT ANY TABLE", "SELECT ANY DICTIONARY"}:
            migration_plan.append({
                "action": "replace_broad_select_with_object_grants",
                "actor": grantee,
                "current_privilege": privilege,
                "target_state": "grant SELECT only on approved source/target metadata objects",
                "approval_required": True,
                "rollback": "restore previous privilege only if a tested approved workflow requires it",
            })
        elif privilege in DANGEROUS_ANY_PRIVS:
            migration_plan.append({
                "action": "review_broad_system_privilege",
                "actor": grantee,
                "current_privilege": privilege,
                "target_state": "replace with schema/object-scoped grants",
                "approval_required": True,
                "rollback": "restore prior grant from redacted pre-change evidence if needed",
            })

    for row in report.get("role_privileges") or []:
        grantee = str(row.get("GRANTEE", "")).upper()
        role = str(row.get("GRANTED_ROLE", "")).upper()
        if grantee == "APP_USER" and role in APP_BROAD_ROLES:
            migration_plan.append({
                "action": "remove_broad_catalog_role_from_app_user",
                "actor": grantee,
                "current_role": role,
                "target_state": "use explicit object grants or approved metadata views",
                "approval_required": True,
                "rollback": "restore role only with documented approval if read-only workflows fail",
            })
        elif role == "DBA":
            migration_plan.append({
                "action": "remove_dba_role_from_runtime_identity",
                "actor": grantee,
                "current_role": role,
                "target_state": "split runtime, DDL, and ODI repository identities",
                "approval_required": True,
                "rollback": "restore role only as temporary break-glass with expiry",
            })

    return {
        "capability_matrix": capability_matrix,
        "runtime_sysdba_required": _has_runtime_sysdba(report),
        "migration_plan": migration_plan,
        "migration_count": len(migration_plan),
        "safety_boundary": {
            "mode": "read_only_analysis",
            "will_execute_grant_revoke": False,
            "will_execute_db_ddl": False,
            "will_write_odi_repository": False,
            "requires_user_approval_for_live_changes": True,
        },
    }


def collect_live_report() -> dict[str, Any]:
    import oracledb
    import config

    conn = oracledb.connect(user=config.ORACLE_USER, password=config.ORACLE_PASSWORD, dsn=config.ORACLE_DSN)
    try:
        cur = conn.cursor()
        report: dict[str, Any] = {
            "config": {
                "environment": config.ENVIRONMENT,
                "oracle_user": config.ORACLE_USER,
                "oracle_connection_mode": "SYSDBA" if str(config.ORACLE_USER or "").strip().lower() == "sys" else "normal",
                "oracle_ddl_user": config.ORACLE_DDL_USER,
                "source_oracle_user": config.SOURCE_ORACLE_USER,
                "target_oracle_user": config.TARGET_ORACLE_USER,
                "odi_repo_user": config.ODI_REPO_USER,
                "audit_schema_owner": AUDIT_SCHEMA_OWNER,
                "audit_runtime_user": config.AUDIT_ORACLE_USER,
                "source_oracle_schemas": config.SOURCE_ORACLE_SCHEMAS,
                "oracle_dsn_masked": mask_dsn(config.ORACLE_DSN),
            },
            "session": _rows(cur, """
                SELECT SYS_CONTEXT('USERENV','SESSION_USER') SESSION_USER,
                       SYS_CONTEXT('USERENV','CURRENT_SCHEMA') CURRENT_SCHEMA,
                       SYS_CONTEXT('USERENV','DB_NAME') DB_NAME,
                       SYS_CONTEXT('USERENV','SERVICE_NAME') SERVICE_NAME
                FROM DUAL
            """)[0],
            "accounts": _rows(cur, """
                SELECT USERNAME, ACCOUNT_STATUS, DEFAULT_TABLESPACE, TEMPORARY_TABLESPACE
                FROM DBA_USERS
                WHERE USERNAME IN ('APP_USER','AUDIT_USER','ODI_EXEC','ODS_OWNER','DWH_OWNER','DWH','REPO_ODI_REPO','HR','SH')
                ORDER BY USERNAME
            """),
            "system_privileges": _rows(cur, """
                SELECT GRANTEE, PRIVILEGE, ADMIN_OPTION
                FROM DBA_SYS_PRIVS
                WHERE GRANTEE IN ('APP_USER','AUDIT_USER','ODI_EXEC','ODS_OWNER','DWH_OWNER','DWH','REPO_ODI_REPO','HR','SH')
                ORDER BY GRANTEE, PRIVILEGE
            """),
            "role_privileges": _rows(cur, """
                SELECT GRANTEE, GRANTED_ROLE, ADMIN_OPTION, DEFAULT_ROLE
                FROM DBA_ROLE_PRIVS
                WHERE GRANTEE IN ('APP_USER','AUDIT_USER','ODI_EXEC','ODS_OWNER','DWH_OWNER','DWH','REPO_ODI_REPO','HR','SH')
                ORDER BY GRANTEE, GRANTED_ROLE
            """),
            "audit_tables": _rows(cur, """
                SELECT TABLE_NAME
                FROM ALL_TABLES
                WHERE OWNER='AUDIT_USER'
                  AND TABLE_NAME IN ('SCHEMA_HASH','ETL_WATERMARK','GRANT_LOG','DATA_LINEAGE','MAPPING_SNAPSHOT','MAPPING_LOG')
                ORDER BY TABLE_NAME
            """),
        }
    finally:
        conn.close()

    report["findings"] = classify_security_report(report, workspace_root=Path(__file__).resolve().parent)
    return report


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    severity_order = {"high": 0, "medium": 1, "low": 2}
    findings = sorted(report.get("findings", []), key=lambda item: severity_order.get(item.get("severity"), 9))
    least_privilege = build_least_privilege_plan(report)
    return {
        "config": report.get("config", {}),
        "session": report.get("session", {}),
        "account_count": len(report.get("accounts", [])),
        "system_privilege_count": len(report.get("system_privileges", [])),
        "role_privilege_count": len(report.get("role_privileges", [])),
        "audit_table_count": len(report.get("audit_tables", [])),
        "findings": findings,
        "least_privilege": {
            "capability_matrix": least_privilege["capability_matrix"],
            "runtime_sysdba_required": least_privilege["runtime_sysdba_required"],
            "migration_count": least_privilege["migration_count"],
            "migration_plan": least_privilege["migration_plan"],
            "safety_boundary": least_privilege["safety_boundary"],
        },
    }


def main() -> None:
    report = compact_report(collect_live_report())
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
