from pathlib import Path

import pytest

from rollback_manager import _qualified_object_name


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        ("DWH;DROP", "T1"),
        ("DWH", "T1 CASCADE"),
        ("", "T1"),
        ("DWH", ""),
    ],
)
def test_rollback_object_identifiers_fail_closed(owner, name):
    with pytest.raises(ValueError, match="Invalid rollback object identifier"):
        _qualified_object_name(owner, name)


def test_rollback_object_identifiers_are_quoted_after_validation():
    assert _qualified_object_name("dwh", "ods_status") == '"DWH"."ODS_STATUS"'


def test_rollback_manager_never_returns_raw_database_error_text():
    source = (PROJECT_ROOT / "rollback_manager.py").read_text(encoding="utf-8")

    assert '"error": err_str' not in source
    assert 'logger.error("Rollback hatasi: %s — %s", ddl, err_str)' not in source
