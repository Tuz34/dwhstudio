import operator_smoke


def test_parse_netstat_counts_listeners_without_command_body():
    text = """
  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       5678
  TCP    127.0.0.1:56000        127.0.0.1:5000         TIME_WAIT       0
"""

    result = operator_smoke.parse_netstat_for_port(text, 5000)

    assert result == {"listener_count": 2, "listener_pids": ["1234", "5678"]}


def test_build_smoke_report_records_status_codes_without_response_values():
    def fake_http_get(url, timeout):
        assert timeout == 10.0
        if url.endswith("/api/status"):
            return operator_smoke.HttpCheckResult(status_code=200, error=None)
        return operator_smoke.HttpCheckResult(status_code=None, error="ConnectionRefusedError")

    report = operator_smoke.build_smoke_report(
        base_url="http://127.0.0.1:5000",
        netstat_text="TCP 127.0.0.1:5000 0.0.0.0:0 LISTENING 1234",
        http_get=fake_http_get,
    )

    assert report["base_url"] == "http://127.0.0.1:5000"
    assert report["port"]["listener_count"] == 1
    assert report["checks"][0] == {
        "name": "backend_status",
        "url": "http://127.0.0.1:5000/api/status",
        "status_code": 200,
        "ok": True,
        "error": None,
    }
    assert "secret" not in repr(report).lower()
    assert "password" not in repr(report).lower()


def test_build_smoke_report_includes_incremental_preflight_without_response_body():
    def fake_http_get(url, timeout):
        return operator_smoke.HttpCheckResult(status_code=200, error=None)

    report = operator_smoke.build_smoke_report(
        base_url="http://127.0.0.1:5000",
        netstat_text="TCP 127.0.0.1:5000 0.0.0.0:0 LISTENING 1234",
        http_get=fake_http_get,
    )

    checks_by_name = {check["name"]: check for check in report["checks"]}
    incremental_check = checks_by_name["incremental_preflight_read_only"]

    assert incremental_check == {
        "name": "incremental_preflight_read_only",
        "url": "http://127.0.0.1:5000/api/incremental/preflight?spec_name=SH_SALES_ODS",
        "status_code": 200,
        "ok": True,
        "error": None,
    }
    assert set(incremental_check) == {"name", "url", "status_code", "ok", "error"}


def test_report_status_warns_on_duplicate_listener():
    report = {
        "port": {"listener_count": 2},
        "checks": [{"ok": True}, {"ok": True}],
    }

    assert operator_smoke.overall_status(report) == "warn"
