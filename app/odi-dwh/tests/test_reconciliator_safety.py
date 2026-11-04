from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _function_source(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function_name
    )
    return ast.get_source_segment(source, node) or ""


def test_sync_mapping_does_not_delete_directly_from_odi_repository():
    source = _function_source(PROJECT_ROOT / "reconciliator.py", "sync_odi_mapping")

    assert "DELETE FROM" not in source.upper()
    assert "ODI_REPO_PASSWORD" not in source
    assert "oracledb.connect" not in source


def test_sync_mapping_closes_generator_and_redacts_failure(monkeypatch):
    import reconciliator

    calls = []

    class FakeMappingGenerator:
        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            calls.append("exit")
            return False

        def _register_odi_datastores(self, _params):
            raise RuntimeError("internal credential detail")

        def _create_odi_mapping(self, _params):
            raise AssertionError("mapping creation must not run after registration failure")

    monkeypatch.setitem(
        sys.modules,
        "mapping_generator",
        SimpleNamespace(MappingGenerator=FakeMappingGenerator),
    )

    result = reconciliator.sync_odi_mapping("DWH", "ODS_HR_EMPLOYEES")

    assert calls == ["enter", "exit"]
    assert result == {
        "error": "ODI mapping synchronization failed.",
        "error_type": "RuntimeError",
    }


def test_sync_mapping_rejects_invalid_identifiers_before_generator_import(monkeypatch):
    import reconciliator

    monkeypatch.setitem(
        sys.modules,
        "mapping_generator",
        SimpleNamespace(
            MappingGenerator=lambda: (_ for _ in ()).throw(
                AssertionError("invalid identifiers must fail before generator construction")
            )
        ),
    )

    result = reconciliator.sync_odi_mapping("DWH;DROP", "ODS_HR_EMPLOYEES")

    assert result == {"error": "Invalid owner or table identifier."}
