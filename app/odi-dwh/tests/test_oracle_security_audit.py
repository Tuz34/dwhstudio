import uuid
from pathlib import Path

import pytest

from oracle_security_audit import (
    build_least_privilege_plan,
    classify_security_report,
    compact_report,
    mask_dsn,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace_tmp(request):
    path = PROJECT_ROOT / ".tmp_oracle_security_audit_tests" / f"{request.node.name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def test_mask_dsn_redacts_embedded_password():
    assert mask_dsn("user/password@localhost:1521/orcl") == "user/***@localhost:1521/orcl"
    assert mask_dsn("//user:password@localhost:1521/orcl") == "//***:***@localhost:1521/orcl"


def test_classify_flags_missing_design_users_and_broad_app_role(workspace_tmp: Path):
    report = {
        "accounts": [
            {"USERNAME": "APP_USER", "ACCOUNT_STATUS": "OPEN"},
            {"USERNAME": "AUDIT_USER", "ACCOUNT_STATUS": "OPEN"},
            {"USERNAME": "DWH", "ACCOUNT_STATUS": "OPEN"},
        ],
        "system_privileges": [
            {"GRANTEE": "APP_USER", "PRIVILEGE": "CREATE SESSION"},
            {"GRANTEE": "APP_USER", "PRIVILEGE": "CREATE SYNONYM"},
        ],
        "role_privileges": [
            {"GRANTEE": "APP_USER", "GRANTED_ROLE": "SELECT_CATALOG_ROLE"},
        ],
        "audit_tables": [
            {"TABLE_NAME": "DATA_LINEAGE"},
            {"TABLE_NAME": "MAPPING_LOG"},
            {"TABLE_NAME": "MAPPING_SNAPSHOT"},
        ],
    }

    findings = classify_security_report(report, workspace_root=workspace_tmp)
    by_area = {item["area"]: item for item in findings}

    assert by_area["account_model"]["severity"] == "medium"
    assert "ODI_EXEC" in by_area["account_model"]["users"]
    assert by_area["app_user_role"]["role"] == "SELECT_CATALOG_ROLE"
    assert by_area["audit_infra"]["tables"] == ["ETL_WATERMARK", "GRANT_LOG", "SCHEMA_HASH"]
    assert "app_user_privilege" not in by_area


def test_classify_flags_app_user_direct_ddl_and_credential_hygiene(workspace_tmp: Path):
    (workspace_tmp / "setup_oracle_users.py").write_text(
        'users = [("APP_USER", "secret", "USERS")]\n'
        'cursor.execute("CREATE USER APP_USER IDENTIFIED BY \\"secret\\"")\n',
        encoding="utf-8",
    )
    report = {
        "accounts": [{"USERNAME": "APP_USER", "ACCOUNT_STATUS": "OPEN"}],
        "system_privileges": [{"GRANTEE": "APP_USER", "PRIVILEGE": "CREATE TABLE"}],
        "role_privileges": [],
        "audit_tables": [],
    }

    findings = classify_security_report(report, workspace_root=workspace_tmp)
    areas = {item["area"] for item in findings}

    assert "app_user_privilege" in areas
    assert "credential_hygiene" in areas


def test_classify_allows_env_placeholder_user_setup(workspace_tmp: Path):
    (workspace_tmp / "setup_oracle_users.py").write_text(
        'password = os.getenv("SETUP_APP_USER_PASSWORD")\n'
        'cursor.execute(f"CREATE USER APP_USER IDENTIFIED BY \\"{password}\\"")\n',
        encoding="utf-8",
    )
    report = {
        "accounts": [{"USERNAME": "APP_USER", "ACCOUNT_STATUS": "OPEN"}],
        "system_privileges": [],
        "role_privileges": [],
        "audit_tables": [],
    }

    findings = classify_security_report(report, workspace_root=workspace_tmp)
    areas = {item["area"] for item in findings}

    assert "credential_hygiene" not in areas


def test_setup_oracle_users_hardens_app_user_catalog_role():
    source = Path("setup_oracle_users.py").read_text(encoding="utf-8")

    assert '("SELECT ANY DICTIONARY", "APP_USER")' not in source
    assert '"SELECT ANY DICTIONARY"' in source
    assert '"SELECT_CATALOG_ROLE"' in source
    assert "revoke_if_user_exists(cursor, managed_users, privilege, \"APP_USER\")" in source


def test_classify_allows_canonical_audit_user_schema_prefix(workspace_tmp: Path):
    (workspace_tmp / "watermark_manager.py").write_text(
        "SELECT * FROM AUDIT_USER.ETL_WATERMARK\n",
        encoding="utf-8",
    )
    report = {
        "config": {"audit_oracle_user": "DWH"},
        "accounts": [{"USERNAME": "APP_USER", "ACCOUNT_STATUS": "OPEN"}],
        "system_privileges": [],
        "role_privileges": [],
        "audit_tables": [],
    }

    findings = classify_security_report(report, workspace_root=workspace_tmp)
    areas = {item["area"] for item in findings}

    assert "audit_owner_config" not in areas


def test_compact_report_counts_and_sorts_findings():
    report = {
        "config": {"environment": "dev"},
        "session": {"SESSION_USER": "APP_USER"},
        "accounts": [{}, {}],
        "system_privileges": [{}],
        "role_privileges": [{}, {}, {}],
        "audit_tables": [{}],
        "findings": [
            {"severity": "medium", "area": "b"},
            {"severity": "high", "area": "a"},
        ],
    }

    compact = compact_report(report)

    assert compact["account_count"] == 2
    assert compact["role_privilege_count"] == 3
    assert compact["findings"][0]["severity"] == "high"


def test_least_privilege_plan_defines_productized_capability_matrix():
    report = {
        "config": {
            "oracle_user": "APP_USER",
            "source_oracle_user": "APP_USER",
            "target_oracle_user": "DWH",
            "oracle_ddl_user": "DWH",
            "odi_repo_user": "REPO_ODI_REPO",
            "audit_runtime_user": "DWH",
        },
        "accounts": [
            {"USERNAME": "APP_USER", "ACCOUNT_STATUS": "OPEN"},
            {"USERNAME": "DWH", "ACCOUNT_STATUS": "OPEN"},
            {"USERNAME": "REPO_ODI_REPO", "ACCOUNT_STATUS": "OPEN"},
        ],
        "system_privileges": [
            {"GRANTEE": "APP_USER", "PRIVILEGE": "CREATE SESSION"},
            {"GRANTEE": "APP_USER", "PRIVILEGE": "SELECT ANY TABLE"},
            {"GRANTEE": "DWH", "PRIVILEGE": "CREATE TABLE"},
        ],
        "role_privileges": [
            {"GRANTEE": "APP_USER", "GRANTED_ROLE": "SELECT_CATALOG_ROLE"},
        ],
        "audit_tables": [{"TABLE_NAME": "MAPPING_LOG"}],
    }

    plan = build_least_privilege_plan(report)

    capabilities = {item["capability"]: item for item in plan["capability_matrix"]}
    assert capabilities["read_only_analysis"]["actor"] == "APP_USER"
    assert capabilities["schema_sync_ddl"]["actor"] == "DWH"
    assert capabilities["odi_metadata_sync"]["actor"] == "REPO_ODI_REPO"
    assert capabilities["scenario_load_plan_run"]["requires_live_approval"] is True
    assert capabilities["read_only_analysis"]["allowed_privileges"] == ["CREATE SESSION", "object SELECT grants only"]
    assert plan["runtime_sysdba_required"] is False

    migration_actions = {item["action"] for item in plan["migration_plan"]}
    assert "replace_broad_select_with_object_grants" in migration_actions
    assert "remove_broad_catalog_role_from_app_user" in migration_actions
    assert all("password" not in str(item).lower() for item in plan["migration_plan"])


def test_compact_report_includes_least_privilege_summary():
    report = {
        "config": {"oracle_user": "APP_USER", "oracle_ddl_user": "DWH", "odi_repo_user": "REPO_ODI_REPO"},
        "session": {"SESSION_USER": "APP_USER"},
        "accounts": [{"USERNAME": "APP_USER", "ACCOUNT_STATUS": "OPEN"}],
        "system_privileges": [{"GRANTEE": "APP_USER", "PRIVILEGE": "SELECT ANY TABLE"}],
        "role_privileges": [],
        "audit_tables": [],
        "findings": [],
    }

    compact = compact_report(report)

    assert "least_privilege" in compact
    assert compact["least_privilege"]["runtime_sysdba_required"] is False
    assert compact["least_privilege"]["migration_count"] >= 1


def test_runtime_sysdba_detection_ignores_policy_text_false_positive():
    report = {
        "config": {
            "oracle_user": "APP_USER",
            "oracle_connection_mode": "normal",
            "operator_note": "SYSDBA is forbidden for runtime users",
        },
        "system_privileges": [
            {"GRANTEE": "APP_USER", "PRIVILEGE": "CREATE SESSION"},
            {"GRANTEE": "DWH", "PRIVILEGE": "SELECT ANY TABLE"},
        ],
        "role_privileges": [
            {"GRANTEE": "APP_USER", "GRANTED_ROLE": "NOT_SYSDBA_RELATED_LABEL"},
        ],
    }

    compact = compact_report(report)

    assert compact["least_privilege"]["runtime_sysdba_required"] is False


def test_runtime_sysdba_detection_flags_explicit_runtime_admin_mode():
    sys_user_report = {
        "config": {"oracle_user": "SYS", "oracle_connection_mode": "normal"},
        "system_privileges": [],
        "role_privileges": [],
    }
    explicit_mode_report = {
        "config": {"oracle_user": "APP_USER", "oracle_connection_mode": "SYSDBA"},
        "system_privileges": [],
        "role_privileges": [],
    }

    assert compact_report(sys_user_report)["least_privilege"]["runtime_sysdba_required"] is True
    assert compact_report(explicit_mode_report)["least_privilege"]["runtime_sysdba_required"] is True
