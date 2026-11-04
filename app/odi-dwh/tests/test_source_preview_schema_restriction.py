"""Source preview must honor the configured schema allowlist before any SQL."""

from __future__ import annotations

import pandas as pd
import pytest

import config
from source_connector import (
    MssqlSourceConnector,
    OracleSourceConnector,
    SourceSchemaNotAllowed,
    normalize_source_identifier,
    require_source_schema,
)


def test_invalid_identifier_is_rejected_before_sql():
    with pytest.raises(ValueError):
        normalize_source_identifier("HR' OR 1=1")
    with pytest.raises(ValueError):
        normalize_source_identifier("A" * 31)


def test_unlisted_owner_is_rejected_by_the_choke_point(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_ORACLE_SCHEMAS", ["HR", "SH"])

    with pytest.raises(SourceSchemaNotAllowed) as captured:
        require_source_schema("SYS")

    assert str(captured.value) == "Source schema is not allowed."
    assert "SYS" not in str(captured.value)


def test_empty_allowlist_keeps_historical_unrestricted_preview(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_ORACLE_SCHEMAS", [])

    assert require_source_schema("HR") == "HR"


def test_oracle_sample_data_binds_limit_and_skips_sql_for_unlisted_owner(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_ORACLE_SCHEMAS", ["HR"])
    connector = OracleSourceConnector()
    calls: list[tuple] = []

    def fake_execute(sql, params=None):
        calls.append((sql, params))
        return pd.DataFrame([{"STATUS_CODE": "OK", "PASSWORD": "kept"}])

    connector.execute_select = fake_execute

    with pytest.raises(SourceSchemaNotAllowed):
        connector.sample_data("SYS.USERS", 3)
    with pytest.raises(ValueError):
        connector.sample_data("HR.EMP'; DROP TABLE HR.EMPLOYEES", 3)
    assert calls == []

    frame = connector.sample_data("hr.employees", 3)

    assert calls == [(
        'SELECT * FROM "HR"."EMPLOYEES" FETCH FIRST :preview_limit ROWS ONLY',
        {"preview_limit": 3},
    )]
    assert list(frame.columns) == ["STATUS_CODE", "PASSWORD"]
    assert frame.iloc[0]["PASSWORD"] == "kept"


def test_mssql_sample_data_uses_the_same_allowlist_and_a_bound_limit(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_ORACLE_SCHEMAS", ["DBO"])
    connector = MssqlSourceConnector()
    calls: list[tuple] = []
    connector.execute_select = lambda sql, params=None: calls.append((sql, params)) or pd.DataFrame()

    with pytest.raises(SourceSchemaNotAllowed):
        connector.sample_data("secret.accounts", 3)
    assert calls == []

    connector.sample_data("dbo.customers", 3)
    assert calls == [("SELECT TOP (?) * FROM [DBO].[CUSTOMERS]", (3,))]


def test_source_preview_route_returns_403_without_echoing_the_owner(app_client, monkeypatch):
    import source_connector

    monkeypatch.setattr(config, "SOURCE_ORACLE_SCHEMAS", ["HR"])
    monkeypatch.setattr(
        source_connector,
        "get_source_connector",
        lambda: (_ for _ in ()).throw(AssertionError("preview read must not start")),
    )

    response = app_client.get("/api/source/preview/SYS.USER$")

    assert response.status_code == 403
    assert response.get_json() == {"error": "Source schema is not allowed."}
    body = response.get_data(as_text=True)
    assert "SYS" not in body
    assert "USER$" not in body


def test_source_preview_route_rejects_invalid_names_without_echo(app_client):
    response = app_client.get("/api/source/preview/HR'.EVIL_TABLE")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid table name format."}
    assert "EVIL_TABLE" not in response.get_data(as_text=True)


def test_source_preview_route_keeps_row_cap_and_columns(app_client, monkeypatch):
    import source_connector

    monkeypatch.setattr(config, "SOURCE_ORACLE_SCHEMAS", ["HR"])

    class FakeSourceConnector:
        def sample_data(self, table_name, limit):
            assert table_name == "HR.STATUS_SOURCE"
            assert limit == 3
            return pd.DataFrame([{"STATUS_CODE": "OK", "PASSWORD": "visible"}])

    monkeypatch.setattr(source_connector, "get_source_connector", lambda: FakeSourceConnector())

    response = app_client.get("/api/source/preview/HR.STATUS_SOURCE")

    assert response.status_code == 200
    assert response.get_json() == {
        "columns": ["STATUS_CODE", "PASSWORD"],
        "rows": [{"STATUS_CODE": "OK", "PASSWORD": "visible"}],
    }
