import json
from pathlib import Path

from diagnostic_errors import safe_error_json, safe_error_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_safe_diagnostic_error_contains_fixed_text_and_exception_class_only():
    secret_detail = "internal credential detail must not leak"

    payload = safe_error_payload("Monitoring query", RuntimeError(secret_detail))
    encoded = safe_error_json("Monitoring query", RuntimeError(secret_detail))

    assert payload == {"error": "Monitoring query failed.", "error_type": "RuntimeError"}
    assert json.loads(encoded) == payload
    assert secret_detail not in encoded


def test_read_helper_modules_do_not_serialize_raw_exception_text():
    for filename in ("monitoring_mcp.py", "dba_mcp.py", "odi_mcp.py"):
        source = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
        assert 'json.dumps({"error": str(' not in source
        assert '_json.dumps({"error": str(' not in source
        assert '"error": str(e)' not in source
