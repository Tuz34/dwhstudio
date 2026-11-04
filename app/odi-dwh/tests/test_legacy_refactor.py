from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_reconciliator_uses_source_connector_not_direct_mssql_runtime():
    content = (PROJECT_ROOT / "reconciliator.py").read_text(encoding="utf-8")

    assert "import pyodbc" not in content
    assert "_get_mssql_conn" not in content
    assert "MSSQL_DEFAULT_SERVER" not in content
    assert "MSSQL_DEFAULT_DATABASE" not in content
    assert "MSSQL_SERVER" not in content
    assert "MSSQL_DATABASE" not in content
    assert "MSSQL_USER" not in content
    assert "MSSQL_PASSWORD" not in content
    assert "get_source_connector" in content
