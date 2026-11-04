"""Oracle user structure setup.

Run as SYSDBA. Passwords are intentionally read from environment variables;
this script must not contain credential literals.
"""

from __future__ import annotations

import os

import oracledb

from config import ORACLE_DSN, ORACLE_PASSWORD, ORACLE_USER


USER_PASSWORD_ENV: dict[str, tuple[str, ...]] = {
    "APP_USER": ("SETUP_APP_USER_PASSWORD", "APP_USER_PASSWORD", "ORACLE_PASSWORD"),
    "AUDIT_USER": ("SETUP_AUDIT_USER_PASSWORD", "AUDIT_USER_PASSWORD"),
    "ODI_EXEC": ("SETUP_ODI_EXEC_PASSWORD", "ODI_EXEC_PASSWORD"),
    "ODS_OWNER": ("SETUP_ODS_OWNER_PASSWORD", "ODS_OWNER_PASSWORD"),
    "DWH_OWNER": ("SETUP_DWH_OWNER_PASSWORD", "DWH_OWNER_PASSWORD"),
}

USER_TABLESPACES: list[tuple[str, str]] = [
    ("APP_USER", "USERS"),
    ("AUDIT_USER", "USERS"),
    ("ODI_EXEC", "USERS"),
    ("ODS_OWNER", "USERS"),
    ("DWH_OWNER", "USERS"),
]

APP_CATALOG_READ_VIEWS = [
    "SYS.DBA_USERS",
    "SYS.DBA_SYS_PRIVS",
    "SYS.DBA_ROLE_PRIVS",
    "SYS.DBA_TAB_PRIVS",
    "SYS.DBA_TABLES",
    "SYS.DBA_TAB_COLUMNS",
    "SYS.DBA_TAB_COMMENTS",
    "SYS.DBA_COL_COMMENTS",
    "SYS.DBA_CONSTRAINTS",
    "SYS.DBA_CONS_COLUMNS",
    "SYS.DBA_INDEXES",
    "SYS.DBA_OBJECTS",
    "SYS.DBA_DATA_FILES",
    "SYS.DBA_FREE_SPACE",
    "SYS.V_$SESSION",
    "SYS.V_$SQL",
    "SYS.V_$LOCK",
    "SYS.V_$LOG",
    "SYS.V_$DATABASE",
    "SYS.V_$RMAN_BACKUP_JOB_DETAILS",
    "SYS.V_$SYSTEM_EVENT",
]


def get_sys_connection():
    sys_user = os.getenv("ORACLE_SYS_USER") or os.getenv("SETUP_ORACLE_SYS_USER") or ORACLE_USER
    sys_password = os.getenv("ORACLE_SYS_PASSWORD") or os.getenv("SETUP_ORACLE_SYS_PASSWORD") or ORACLE_PASSWORD
    return oracledb.connect(
        user=sys_user,
        password=sys_password,
        dsn=ORACLE_DSN,
        mode=oracledb.AUTH_MODE_SYSDBA,
    )


def _password_for_user(username: str) -> str | None:
    for env_name in USER_PASSWORD_ENV.get(username, ()):
        value = os.getenv(env_name)
        if value:
            return value
    return None


def user_exists(cursor, username: str) -> bool:
    cursor.execute("SELECT COUNT(*) FROM DBA_USERS WHERE USERNAME = :u", {"u": username.upper()})
    return bool(cursor.fetchone()[0])


def run_sql(cursor, sql, label=""):
    """Run one SQL statement and log a sanitized status line."""
    try:
        cursor.execute(sql)
        print(f"  [OK] {label or sql[:60]}")
        return True
    except oracledb.DatabaseError as e:
        err = str(e)
        if "ORA-01920" in err:
            print(f"  [INFO] {label} - already exists; skipping.")
        elif "ORA-01543" in err:
            print(f"  [INFO] {label} - already exists; skipping.")
        elif "ORA-00955" in err:
            print(f"  [INFO] {label} - table already exists; skipping.")
        elif "ORA-01927" in err:
            print(f"  [INFO] {label} - privilege already granted.")
        elif "ORA-01951" in err or "ORA-01952" in err:
            print(f"  [INFO] {label} - privilege already absent.")
        elif "ORA-01430" in err:
            print(f"  [INFO] {label} - column already exists.")
        elif "ORA-00942" in err:
            print(f"  [WARN] {label} - table not found.")
        elif "ORA-01917" in err:
            print(f"  [WARN] {label} - user or role not found.")
        else:
            print(f"  [FAIL] {label} - DatabaseError")
        return False


def grant_if_user_exists(cursor, managed_users: set[str], privilege: str, username: str) -> None:
    if username not in managed_users:
        print(f"  [WARN] GRANT {privilege} -> {username} - user not found; skipped.")
        return
    run_sql(cursor, f"GRANT {privilege} TO {username}", label=f"GRANT {privilege} -> {username}")


def revoke_if_user_exists(cursor, managed_users: set[str], privilege: str, username: str) -> None:
    if username not in managed_users:
        print(f"  [WARN] REVOKE {privilege} -> {username} - user not found; skipped.")
        return
    run_sql(cursor, f"REVOKE {privilege} FROM {username}", label=f"REVOKE {privilege} -> {username}")


def create_or_find_users(cursor) -> set[str]:
    print("--- 1/5: Creating users...")
    managed_users: set[str] = set()
    for username, tablespace in USER_TABLESPACES:
        if user_exists(cursor, username):
            managed_users.add(username)
            print(f"  [INFO] CREATE USER {username} - already exists; skipping.")
            continue

        password = _password_for_user(username)
        if not password:
            print(f"  [WARN] CREATE USER {username} - password environment variable missing; user creation skipped.")
            continue

        created = run_sql(
            cursor,
            f'CREATE USER {username} IDENTIFIED BY "{password}" '
            f"DEFAULT TABLESPACE {tablespace} TEMPORARY TABLESPACE TEMP "
            f"QUOTA UNLIMITED ON {tablespace}",
            label=f"CREATE USER {username}",
        )
        if created or user_exists(cursor, username):
            managed_users.add(username)
    return managed_users


def main():
    conn = get_sys_connection()
    cursor = conn.cursor()

    print("\n+======================================================+")
    print("|   PHASE 1 -- Oracle User Structure Setup             |")
    print("+======================================================+\n")

    managed_users = create_or_find_users(cursor)
    conn.commit()

    print("\n--- 2/5: Assigning privileges...")
    grants = [
        ("CREATE SESSION", "APP_USER"),
        ("CREATE SESSION", "AUDIT_USER"),
        ("CREATE TABLE", "AUDIT_USER"),
        ("CREATE VIEW", "AUDIT_USER"),
        ("CREATE SEQUENCE", "AUDIT_USER"),
        ("CREATE SESSION", "ODI_EXEC"),
        ("CREATE SESSION", "ODS_OWNER"),
        ("CREATE TABLE", "ODS_OWNER"),
        ("CREATE VIEW", "ODS_OWNER"),
        ("CREATE SEQUENCE", "ODS_OWNER"),
        ("CREATE PROCEDURE", "ODS_OWNER"),
        ("CREATE TRIGGER", "ODS_OWNER"),
        ("CREATE SYNONYM", "ODS_OWNER"),
        ("CREATE SESSION", "DWH_OWNER"),
        ("CREATE TABLE", "DWH_OWNER"),
        ("CREATE VIEW", "DWH_OWNER"),
        ("CREATE SEQUENCE", "DWH_OWNER"),
        ("CREATE PROCEDURE", "DWH_OWNER"),
        ("CREATE TRIGGER", "DWH_OWNER"),
        ("CREATE SYNONYM", "DWH_OWNER"),
        ("CREATE MATERIALIZED VIEW", "DWH_OWNER"),
    ]
    for privilege, username in grants:
        grant_if_user_exists(cursor, managed_users, privilege, username)
    conn.commit()

    print("\n--- 2b/5: Narrowing APP_USER broad catalog privileges...")
    app_user_revokes = [
        "SELECT ANY DICTIONARY",
        "SELECT_CATALOG_ROLE",
    ]
    for privilege in app_user_revokes:
        revoke_if_user_exists(cursor, managed_users, privilege, "APP_USER")
    conn.commit()

    print("\n--- 2c/5: Assigning APP_USER explicit catalog read view privileges...")
    for view_name in APP_CATALOG_READ_VIEWS:
        grant_if_user_exists(cursor, managed_users, f"SELECT ON {view_name}", "APP_USER")
    conn.commit()

    print("\n--- 3/5: Assigning DWH table privileges...")
    cursor.execute("SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = 'DWH' ORDER BY TABLE_NAME")
    dwh_tables = [row[0] for row in cursor.fetchall()]
    for table_name in dwh_tables:
        grant_if_user_exists(cursor, managed_users, f"SELECT ON DWH.{table_name}", "APP_USER")
        grant_if_user_exists(cursor, managed_users, f"SELECT, INSERT, UPDATE, DELETE ON DWH.{table_name}", "ODI_EXEC")
    conn.commit()
    print(f"  [OK] Privilege assignment attempted for {len(dwh_tables)} DWH tables.")

    print("\n--- 4/5: Creating audit tables...")
    audit_tables = [
        (
            "SCHEMA_HASH",
            """CREATE TABLE AUDIT_USER.SCHEMA_HASH (
                HASH_ID         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                TABLE_OWNER     VARCHAR2(128) NOT NULL,
                TABLE_NAME      VARCHAR2(128) NOT NULL,
                COLUMN_HASH     VARCHAR2(64)  NOT NULL,
                CHECK_DATE      TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
                PREVIOUS_HASH   VARCHAR2(64),
                CHANGE_DETECTED CHAR(1) DEFAULT 'N' CHECK (CHANGE_DETECTED IN ('Y','N'))
            )""",
        ),
        (
            "ETL_WATERMARK",
            """CREATE TABLE AUDIT_USER.ETL_WATERMARK (
                WATERMARK_ID        NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                TABLE_OWNER         VARCHAR2(128) NOT NULL,
                TABLE_NAME          VARCHAR2(128) NOT NULL,
                LAST_EXTRACT_DATE   TIMESTAMP,
                LAST_EXTRACT_VALUE  VARCHAR2(256),
                ROW_COUNT           NUMBER,
                UPDATED_AT          TIMESTAMP DEFAULT SYSTIMESTAMP
            )""",
        ),
        (
            "GRANT_LOG",
            """CREATE TABLE AUDIT_USER.GRANT_LOG (
                LOG_ID        NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                GRANTEE       VARCHAR2(128),
                PRIVILEGE     VARCHAR2(128),
                TABLE_OWNER   VARCHAR2(128),
                TABLE_NAME    VARCHAR2(128),
                GRANTED_BY    VARCHAR2(128),
                GRANT_DATE    TIMESTAMP DEFAULT SYSTIMESTAMP
            )""",
        ),
    ]
    if "AUDIT_USER" in managed_users:
        for table_name, ddl in audit_tables:
            run_sql(cursor, ddl, label=f"CREATE TABLE AUDIT_USER.{table_name}")
        conn.commit()
        run_sql(
            cursor,
            "CREATE INDEX AUDIT_USER.IDX_SCHEMA_HASH_TBL ON AUDIT_USER.SCHEMA_HASH(TABLE_OWNER, TABLE_NAME)",
            label="INDEX IDX_SCHEMA_HASH_TBL",
        )
        run_sql(
            cursor,
            "CREATE UNIQUE INDEX AUDIT_USER.IDX_ETL_WM_TBL ON AUDIT_USER.ETL_WATERMARK(TABLE_OWNER, TABLE_NAME)",
            label="UNIQUE INDEX IDX_ETL_WM_TBL",
        )
        conn.commit()
    else:
        print("  [WARN] AUDIT_USER not found; audit table DDL step skipped.")

    print("\n--- 5/5: Assigning audit table privileges...")
    audit_grants = [
        ("SELECT, INSERT ON AUDIT_USER.SCHEMA_HASH", "APP_USER"),
        ("SELECT, INSERT, UPDATE, DELETE ON AUDIT_USER.ETL_WATERMARK", "APP_USER"),
        ("SELECT ON AUDIT_USER.GRANT_LOG", "APP_USER"),
        ("SELECT, INSERT ON AUDIT_USER.SCHEMA_HASH", "ODI_EXEC"),
        ("SELECT, INSERT, UPDATE ON AUDIT_USER.ETL_WATERMARK", "ODI_EXEC"),
    ]
    for privilege, username in audit_grants:
        grant_if_user_exists(cursor, managed_users, privilege, username)
    conn.commit()

    print("\n--- Verification...")
    cursor.execute("""
        SELECT USERNAME, ACCOUNT_STATUS, DEFAULT_TABLESPACE
        FROM DBA_USERS
        WHERE USERNAME IN ('ODS_OWNER','DWH_OWNER','ODI_EXEC','APP_USER','AUDIT_USER')
        ORDER BY USERNAME
    """)
    print(f"\n  {'USER':<15} {'STATUS':<20} {'TABLESPACE':<15}")
    print(f"  {'-'*15} {'-'*20} {'-'*15}")
    for row in cursor.fetchall():
        print(f"  {row[0]:<15} {row[1]:<20} {row[2]:<15}")

    cursor.close()
    conn.close()

    print("\n+======================================================+")
    print("|  [OK] User structure setup completed.                 |")
    print("|  Note: Password values come from the environment.     |")
    print("+======================================================+\n")


if __name__ == "__main__":
    main()
