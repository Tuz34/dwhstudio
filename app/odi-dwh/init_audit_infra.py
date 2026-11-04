"""Initialize missing AUDIT_USER audit tables with AUDIT_USER credentials only.

This script is intentionally narrow:
- no SYSDBA mode,
- no DWH broad privileges,
- no destructive DDL,
- no secret printing.

If the AUDIT_USER password is not supplied through environment variables, the
script exits without attempting a database connection or DDL.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


AUDIT_SCHEMA_OWNER = "AUDIT_USER"
AUDIT_PASSWORD_ENVS = ("SETUP_AUDIT_USER_PASSWORD", "AUDIT_USER_PASSWORD")

TABLE_DDL: dict[str, str] = {
    "SCHEMA_HASH": """
        CREATE TABLE SCHEMA_HASH (
            HASH_ID         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            TABLE_OWNER     VARCHAR2(128) NOT NULL,
            TABLE_NAME      VARCHAR2(128) NOT NULL,
            COLUMN_HASH     VARCHAR2(64)  NOT NULL,
            CHECK_DATE      TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            PREVIOUS_HASH   VARCHAR2(64),
            CHANGE_DETECTED CHAR(1) DEFAULT 'N' CHECK (CHANGE_DETECTED IN ('Y','N'))
        )
    """,
    "ETL_WATERMARK": """
        CREATE TABLE ETL_WATERMARK (
            WATERMARK_ID        NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            TABLE_OWNER         VARCHAR2(128) NOT NULL,
            TABLE_NAME          VARCHAR2(128) NOT NULL,
            LAST_EXTRACT_DATE   TIMESTAMP,
            LAST_EXTRACT_VALUE  VARCHAR2(256),
            ROW_COUNT           NUMBER,
            UPDATED_AT          TIMESTAMP DEFAULT SYSTIMESTAMP
        )
    """,
    "GRANT_LOG": """
        CREATE TABLE GRANT_LOG (
            LOG_ID        NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            GRANTEE       VARCHAR2(128),
            PRIVILEGE     VARCHAR2(128),
            TABLE_OWNER   VARCHAR2(128),
            TABLE_NAME    VARCHAR2(128),
            GRANTED_BY    VARCHAR2(128),
            GRANT_DATE    TIMESTAMP DEFAULT SYSTIMESTAMP
        )
    """,
}

INDEX_DDL: dict[str, str] = {
    "IDX_SCHEMA_HASH_TBL": "CREATE INDEX IDX_SCHEMA_HASH_TBL ON SCHEMA_HASH(TABLE_OWNER, TABLE_NAME)",
    "IDX_ETL_WM_TBL": "CREATE UNIQUE INDEX IDX_ETL_WM_TBL ON ETL_WATERMARK(TABLE_OWNER, TABLE_NAME)",
}

GRANTS: list[tuple[str, str]] = [
    ("SELECT, INSERT ON SCHEMA_HASH", "APP_USER"),
    ("SELECT, INSERT, UPDATE, DELETE ON ETL_WATERMARK", "APP_USER"),
    ("SELECT ON GRANT_LOG", "APP_USER"),
    ("SELECT, INSERT ON SCHEMA_HASH", "ODI_EXEC"),
    ("SELECT, INSERT, UPDATE ON ETL_WATERMARK", "ODI_EXEC"),
]


def _audit_password(*, load_env: bool = True) -> str | None:
    if load_env:
        try:
            from dotenv import load_dotenv

            base_dir = Path(__file__).resolve().parent
            load_dotenv(base_dir / ".env", override=False)
            env_name = os.getenv("ENVIRONMENT", "dev").lower()
            load_dotenv(base_dir / f".env.{env_name}", override=False)
        except Exception:
            pass

    for env_name in AUDIT_PASSWORD_ENVS:
        value = os.getenv(env_name)
        if value:
            return value
    return None


def _result(status: str, *, message: str, operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "audit_schema_owner": AUDIT_SCHEMA_OWNER,
        "secret_printed": False,
        "operations": operations or [],
        "message": message,
    }


def _table_exists(cursor: Any, table_name: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = :name",
        {"name": table_name.upper()},
    )
    return bool(cursor.fetchone()[0])


def _index_exists(cursor: Any, index_name: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM USER_INDEXES WHERE INDEX_NAME = :name",
        {"name": index_name.upper()},
    )
    return bool(cursor.fetchone()[0])


def _user_exists(cursor: Any, username: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM ALL_USERS WHERE USERNAME = :name",
        {"name": username.upper()},
    )
    return bool(cursor.fetchone()[0])


def _execute(cursor: Any, sql: str, label: str, operations: list[dict[str, Any]]) -> None:
    try:
        cursor.execute(sql)
        operations.append({"label": label, "status": "executed"})
    except Exception as exc:
        text = str(exc)
        if "ORA-00955" in text:
            operations.append({"label": label, "status": "already_exists"})
        else:
            operations.append({"label": label, "status": "failed", "error": text.splitlines()[0]})
            raise


def initialize_audit_infra(*, dry_run: bool = False, load_env: bool = True) -> dict[str, Any]:
    password = _audit_password(load_env=load_env)
    if not password:
        return _result(
            "blocked_missing_password",
            message="SETUP_AUDIT_USER_PASSWORD or AUDIT_USER_PASSWORD is required; no connection attempted.",
        )

    import oracledb
    import config

    operations: list[dict[str, Any]] = []
    if dry_run:
        return _result(
            "dry_run_ready",
            message="AUDIT_USER credential is present; dry-run did not connect or execute DDL.",
            operations=[
                {"label": f"CREATE TABLE {name}", "status": "planned"}
                for name in TABLE_DDL
            ],
        )

    conn = oracledb.connect(user=AUDIT_SCHEMA_OWNER, password=password, dsn=config.ORACLE_DSN)
    try:
        cursor = conn.cursor()
        for table_name, ddl in TABLE_DDL.items():
            if _table_exists(cursor, table_name):
                operations.append({"label": f"CREATE TABLE {table_name}", "status": "already_exists"})
            else:
                _execute(cursor, ddl, f"CREATE TABLE {table_name}", operations)

        for index_name, ddl in INDEX_DDL.items():
            if _index_exists(cursor, index_name):
                operations.append({"label": f"CREATE INDEX {index_name}", "status": "already_exists"})
            else:
                _execute(cursor, ddl, f"CREATE INDEX {index_name}", operations)

        for privilege, grantee in GRANTS:
            label = f"GRANT {privilege} TO {grantee}"
            if not _user_exists(cursor, grantee):
                operations.append({"label": label, "status": "skipped_missing_user"})
                continue
            _execute(cursor, f"GRANT {privilege} TO {grantee}", label, operations)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _result("ready", message="AUDIT_USER audit infrastructure initialized.", operations=operations)


def main() -> None:
    print(json.dumps(initialize_audit_infra(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
