"""ODI-91: one shared containment boundary, baseline back at the base value.

These tests are red on c806860: the baseline still says 301, both boundary
functions still own an ``except Exception``, and ``contained`` does not exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Byte-for-byte policy document from c9febe9, compared with newlines normalized.
_BASE_BASELINE = """\
{
  "schema_version": 1,
  "broad_except_max": 300,
  "swallowed_handlers_max": 15,
  "return_annotation_ratio_min": 0.74,
  "ruff_max_by_code": {
    "I001": 71
  },
  "function_line_max": {
    "mapping_generator.py:_create_target_table": 80,
    "routes/misc_write.py:api_odi_mapping_detail": 45,
    "routes/mapping_write.py:api_mapping_run": 80,
    "reconciliator.py:reconcile_table": 45,
    "reconciliator.py:_compare_data_by_pk": 45
  }
}
"""


def _names_exception(handler: ast.ExceptHandler) -> bool:
    node = handler.type
    if isinstance(node, ast.Name):
        return node.id == "Exception"
    if isinstance(node, ast.Tuple):
        return any(isinstance(item, ast.Name) and item.id == "Exception" for item in node.elts)
    return False


def _exception_handler_lines(filename: str, function_name: str) -> list[int]:
    source = (ROOT / filename).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=filename)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    ]
    assert len(functions) == 1, function_name
    return sorted(
        node.lineno
        for node in ast.walk(functions[0])
        if isinstance(node, ast.ExceptHandler) and _names_exception(node)
    )


def _raise(exc_type: type[BaseException]) -> None:
    raise exc_type()


def test_baseline_file_equals_the_base_value():
    text = (ROOT / "python_quality_baseline.json").read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    assert normalized == _BASE_BASELINE


def test_boundary_sites_contain_no_except_exception():
    """AST of the two boundaries. Other handlers in those files stay put."""
    found = {
        "odi_mcp.py:_evidence_worker": _exception_handler_lines("odi_mcp.py", "_evidence_worker"),
        "mapping_run_service.py:_collect_run_evidence": _exception_handler_lines(
            "mapping_run_service.py", "_collect_run_evidence"
        ),
    }
    assert found == {
        "odi_mcp.py:_evidence_worker": [],
        "mapping_run_service.py:_collect_run_evidence": [],
    }


def test_contained_returns_the_result():
    from contained import contained

    assert contained(lambda left, right: left + right, 2, 40) == (42, None)


def test_contained_contains_exception_and_returns_the_class_name():
    from contained import contained

    class MarkerError(Exception):
        def __str__(self) -> str:
            raise AssertionError("helper read the exception text")

    result, name = contained(lambda: (_ for _ in ()).throw(MarkerError("hidden-detail")))
    assert result is None
    assert name == "MarkerError"

    result, name = contained(lambda: (_ for _ in ()).throw(Exception("hidden-detail")))
    assert result is None
    assert name == "Exception"


def test_contained_lets_keyboard_interrupt_through():
    from contained import contained

    with pytest.raises(KeyboardInterrupt):
        contained(_raise, KeyboardInterrupt)


def test_contained_lets_system_exit_through():
    from contained import contained

    with pytest.raises(SystemExit):
        contained(_raise, SystemExit)
