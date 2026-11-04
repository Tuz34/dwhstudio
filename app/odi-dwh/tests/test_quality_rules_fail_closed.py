"""Fail-closed quality rules and honest multi-column autofix results.

These tests mock the Oracle executor. They do not open a database connection.
"""

from __future__ import annotations

import oracledb

SECRET = "super-secret-value"
_ADD_COLUMN_SQL = 'ALTER TABLE DWH.ODS_HR_EMPLOYEES ADD "{column}" VARCHAR2(10 CHAR)'


class QualityCursor:
    def __init__(self, null_count: int = 0):
        self.null_count = null_count
        self._row = (0,)

    def execute(self, sql, params=None):
        compact = " ".join(sql.upper().split())
        if " IS NULL" in compact:
            self._row = (self.null_count,)
            return
        if compact.startswith("SELECT COUNT(*)"):
            self._row = (3,)
            return
        raise AssertionError(f"unexpected SQL: {compact}")

    def fetchone(self):
        return self._row

    def close(self):
        return None


class QualityConnection:
    def __init__(self, null_count: int = 0):
        self.null_count = null_count

    def cursor(self):
        return QualityCursor(self.null_count)

    def close(self):
        return None

    def is_healthy(self):
        return True


class RecordingCursor:
    def __init__(self, effects):
        self.effects = list(effects)
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if not self.effects:
            raise AssertionError("unexpected DDL statement")
        effect = self.effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect

    def close(self):
        return None


class RecordingConnection:
    def __init__(self, cursor: RecordingCursor, commit_error: BaseException | None = None):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self._commit_error = commit_error

    def cursor(self):
        return self._cursor

    def commit(self):
        if self._commit_error is not None:
            raise self._commit_error
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        return None


def _oracle_error(message: str) -> BaseException:
    return oracledb.DatabaseError(message)


def _patch_quality_connection(monkeypatch, null_count: int = 0):
    import data_quality

    monkeypatch.setattr(
        data_quality.oracledb,
        "connect",
        lambda **_kwargs: QualityConnection(null_count),
    )


def _post_validate(app_client, extra_rules):
    return app_client.post(
        "/api/quality/validate",
        json={"owner": "DWH", "table": "ODS_STATUS", "extra_rules": extra_rules},
    )


def test_unsupported_rule_fails_closed(app_client, monkeypatch):
    _patch_quality_connection(monkeypatch)

    response = _post_validate(
        app_client,
        [{
            "column": "STATUS_CODE",
            "rule_name": "STATUS_REGEX",
            "rule_type": "regex",
            "severity": "WARNING",
        }],
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "failed"
    assert body["passed"] is False
    assert body["unsupported_rules"]
    unsupported = body["unsupported_rules"][0]
    assert unsupported["rule_type"] == "regex"
    assert "regex" in unsupported["reason"]
    rule = body["rules"][0]
    assert rule["status"] == "unsupported"
    assert rule["passed"] is False
    assert rule["reason"] == unsupported["reason"]


def test_supported_not_null_rule_still_passes(app_client, monkeypatch):
    _patch_quality_connection(monkeypatch, null_count=0)

    response = _post_validate(
        app_client,
        [{"column": "STATUS_CODE", "rule_name": "STATUS_NOT_NULL", "rule_type": "not_null"}],
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "passed"
    assert body["passed"] is True
    assert body["unsupported_rules"] == []
    assert body["errors"] == 0
    rule = body["rules"][0]
    assert rule["rule_type"] == "not_null"
    assert rule["status"] == "passed"
    assert rule["passed"] is True
    assert rule["violation_count"] == 0


def test_supported_not_null_violation_still_fails(app_client, monkeypatch):
    _patch_quality_connection(monkeypatch, null_count=2)

    response = _post_validate(
        app_client,
        [{"column": "STATUS_CODE", "rule_name": "STATUS_NOT_NULL", "rule_type": "not_null"}],
    )

    body = response.get_json()
    assert body["status"] == "failed"
    assert body["passed"] is False
    assert body["unsupported_rules"] == []
    assert body["errors"] == 1
    rule = body["rules"][0]
    assert rule["status"] == "failed"
    assert rule["passed"] is False
    assert rule["violation_count"] == 2
    assert rule["rule_type"] == "not_null"


def _patch_autofix_executor(monkeypatch, effects, sync, commit_error: BaseException | None = None):
    import reconciliator

    cursor = RecordingCursor(effects)
    connection = RecordingConnection(cursor, commit_error=commit_error)
    monkeypatch.setattr(reconciliator, "get_connection", lambda: connection)
    monkeypatch.setattr(
        reconciliator,
        "_source_schema_columns",
        lambda _schema, _table: {
            "COL_A": {"type": "varchar", "len": 10},
            "COL_B": {"type": "varchar", "len": 10},
        },
    )
    monkeypatch.setattr(reconciliator, "sync_odi_mapping", sync)
    return connection, cursor


def _post_autofix(app_client):
    return app_client.post(
        "/api/quality/autofix-all-columns",
        json={
            "approved": True,
            "owner": "DWH",
            "table": "ODS_HR_EMPLOYEES",
            "columns": ["COL_A", "COL_B"],
        },
    )


def test_autofix_second_column_failure_is_partial(app_client, monkeypatch):
    sync_calls = []

    def sync(_owner, _table):
        sync_calls.append("sync")
        return {"ok": True, "message": "synced"}

    connection, cursor = _patch_autofix_executor(
        monkeypatch,
        [None, _oracle_error(f"ORA-00904 {SECRET}")],
        sync,
    )

    response = _post_autofix(app_client)
    body = response.get_json()
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert body["status"] == "partial"
    assert body["added_columns"] == ["COL_A"]
    assert body["columns"][0] == {"column": "COL_A", "status": "applied"}
    assert body["columns"][1]["column"] == "COL_B"
    assert body["columns"][1]["status"] == "failed"
    assert body["columns"][1]["error"] == "Column DDL failed."
    assert body["columns"][1]["error_type"] == "DatabaseError"
    assert "Applied COL_A." in body["error"]
    assert "Failed COL_B." in body["error"]
    assert SECRET not in text
    assert "ORA-00904" not in text
    assert sync_calls == []
    assert connection.committed is False
    assert connection.rolled_back is True
    assert cursor.statements == [
        _ADD_COLUMN_SQL.format(column="COL_A"),
        _ADD_COLUMN_SQL.format(column="COL_B"),
    ]


def test_autofix_all_columns_failing_is_failed(app_client, monkeypatch):
    def sync(_owner, _table):
        raise AssertionError("mapping sync must not run when no column was applied")

    connection, cursor = _patch_autofix_executor(
        monkeypatch,
        [_oracle_error(f"password={SECRET}"), _oracle_error("second failure")],
        sync,
    )

    response = _post_autofix(app_client)
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "failed"
    assert body["added_columns"] == []
    assert [item["column"] for item in body["columns"]] == ["COL_A", "COL_B"]
    assert [item["status"] for item in body["columns"]] == ["failed", "failed"]
    assert body["columns"][0]["error"] == "Column DDL failed."
    assert body["columns"][1]["error"] == "Column DDL was not applied."
    assert "Failed COL_A, COL_B." in body["error"]
    assert "Applied" not in body["error"]
    assert SECRET not in response.get_data(as_text=True)
    assert connection.committed is False
    assert cursor.statements == [_ADD_COLUMN_SQL.format(column="COL_A")]


def test_autofix_all_columns_succeeding_is_success(app_client, monkeypatch):
    sync_calls = []

    def sync(owner, table):
        sync_calls.append((owner, table))
        return {"ok": True, "message": "Table metadata and ODI mapping were synchronized."}

    connection, cursor = _patch_autofix_executor(monkeypatch, [None, None], sync)

    response = _post_autofix(app_client)
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "success"
    assert body["ok"] is True
    assert body["added_columns"] == ["COL_A", "COL_B"]
    assert [item["status"] for item in body["columns"]] == ["applied", "applied"]
    assert "error" not in body
    assert sync_calls == [("DWH", "ODS_HR_EMPLOYEES")]
    assert connection.committed is True
    assert cursor.statements == [
        _ADD_COLUMN_SQL.format(column="COL_A"),
        _ADD_COLUMN_SQL.format(column="COL_B"),
    ]


def test_validate_and_insert_unknown_rule_is_unsupported(monkeypatch):
    import data_quality

    _patch_quality_connection(monkeypatch)
    rule = data_quality.QualityRule(
        rule_name="STATUS_REGEX",
        column="STATUS_CODE",
        rule_type="regex",
        severity="WARNING",
    )
    with data_quality.DataQualityManager() as manager:
        monkeypatch.setattr(manager, "create_error_table", lambda *_args, **_kwargs: None)
        result = manager.validate_and_insert_errors("DWH", "ODS_STATUS", custom_rules=[rule])

    summary = result.summary()
    recorded = summary["rules"][0]
    assert recorded["status"] == "unsupported"
    assert recorded["passed"] is False
    assert recorded["rule_type"] == "regex"
    assert summary["passed"] is False
    assert summary["status"] == "failed"


def test_ora_01430_is_applied_already_present_not_added(app_client, monkeypatch):
    sync_calls = []

    def sync(owner, table):
        sync_calls.append((owner, table))
        return {"ok": True, "message": "synced"}

    connection, cursor = _patch_autofix_executor(
        monkeypatch,
        [_oracle_error(f"ORA-01430: column being added already exists {SECRET}"), None],
        sync,
    )

    response = _post_autofix(app_client)
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "success"
    assert body["added_columns"] == ["COL_B"]
    assert body["columns"][0] == {
        "column": "COL_A",
        "status": "applied",
        "already_present": True,
    }
    assert body["columns"][1] == {"column": "COL_B", "status": "applied"}
    assert SECRET not in response.get_data(as_text=True)
    assert "ORA-01430" not in response.get_data(as_text=True)
    assert sync_calls == [("DWH", "ODS_HR_EMPLOYEES")]
    assert connection.committed is True
    assert cursor.statements == [
        _ADD_COLUMN_SQL.format(column="COL_A"),
        _ADD_COLUMN_SQL.format(column="COL_B"),
    ]


def test_autofix_commit_failure_after_applied_columns_is_partial(app_client, monkeypatch):
    def sync(_owner, _table):
        raise AssertionError("mapping sync must not run when the commit failed")

    connection, cursor = _patch_autofix_executor(
        monkeypatch,
        [None, None],
        sync,
        commit_error=_oracle_error(f"ORA-03113 {SECRET}"),
    )

    response = _post_autofix(app_client)
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "partial"
    assert body["added_columns"] == ["COL_A", "COL_B"]
    assert [item["status"] for item in body["columns"]] == ["applied", "applied"]
    assert "already_present" not in body["columns"][0]
    assert "Applied COL_A, COL_B." in body["error"]
    assert "Failed" not in body["error"]
    assert SECRET not in response.get_data(as_text=True)
    assert connection.committed is False
    assert connection.rolled_back is True
    assert cursor.statements == [
        _ADD_COLUMN_SQL.format(column="COL_A"),
        _ADD_COLUMN_SQL.format(column="COL_B"),
    ]


def test_autofix_sync_failure_after_apply_is_partial(app_client, monkeypatch):
    def sync(_owner, _table):
        return {
            "error": f"ODI mapping synchronization failed. {SECRET}",
            "error_type": "ReconciliationError",
        }

    connection, _cursor = _patch_autofix_executor(monkeypatch, [None, None], sync)

    response = _post_autofix(app_client)
    body = response.get_json()
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert body["status"] == "partial"
    assert body["sync_failed"] is True
    assert body["added_columns"] == ["COL_A", "COL_B"]
    assert [item["status"] for item in body["columns"]] == ["applied", "applied"]
    assert "Applied COL_A, COL_B." in body["error"]
    assert "ODI mapping synchronization failed." in body["error"]
    assert SECRET not in text
    assert connection.committed is True


def test_single_column_autofix_still_hides_driver_text():
    from reconciliation_autofix import AutofixDependencies, autofix_missing_column

    cursor = RecordingCursor([_oracle_error(f"ORA-00904 {SECRET}")])
    connection = RecordingConnection(cursor)

    def close_resource(resource, _label):
        if resource is not None:
            resource.close()

    def sync(_owner, _table):
        raise AssertionError("sync must not run")

    deps = AutofixDependencies(
        source_schema_columns=lambda _schema, _table: {"COL_A": {"type": "varchar", "len": 10}},
        map_sql_to_oracle_type=lambda _value: "VARCHAR2",
        get_connection=lambda: connection,
        close_resource=close_resource,
        sync_mapping=sync,
    )

    result = autofix_missing_column("DWH", "ODS_HR_EMPLOYEES", "COL_A", deps)

    assert result == {
        "error": "The target column could not be added.",
        "error_type": "DatabaseError",
    }
    assert SECRET not in str(result)
