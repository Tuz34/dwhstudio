from pathlib import Path

import local_app_control


def test_parse_netstat_listeners_returns_unique_listening_pids_for_port():
    text = """
  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       1234
  TCP    0.0.0.0:5000           0.0.0.0:0              LISTENING       5678
  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:56000        127.0.0.1:5000         TIME_WAIT       0
  TCP    127.0.0.1:5001         0.0.0.0:0              LISTENING       9999
"""

    listeners = local_app_control.parse_netstat_listeners(text, 5000)

    assert [listener.pid for listener in listeners] == ["1234", "5678"]


def test_resolve_python_exe_prefers_project_venv(tmp_path):
    root = tmp_path / "odi-dwh"
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    assert local_app_control.resolve_python_exe(root) == venv_python


def test_runtime_paths_use_logs_directory_and_default_port(tmp_path):
    root = tmp_path / "odi-dwh"

    paths = local_app_control.runtime_paths(root, {})

    assert paths.port == 5000
    assert paths.base_url == "http://localhost:5000"
    assert paths.stdout_log == root / "logs" / "local_app_stdout.log"
    assert paths.stderr_log == root / "logs" / "local_app_stderr.log"


def test_start_app_cleans_existing_listener_and_starts_detached_process(tmp_path):
    root = tmp_path / "odi-dwh"
    app_py = root / "app.py"
    python_exe = root / ".venv" / "Scripts" / "python.exe"
    app_py.parent.mkdir(parents=True)
    python_exe.parent.mkdir(parents=True)
    app_py.write_text("print('app')", encoding="utf-8")
    python_exe.write_text("", encoding="utf-8")
    calls = {"stopped": [], "started": []}

    def fake_netstat():
        return "TCP 127.0.0.1:5000 0.0.0.0:0 LISTENING 4321"

    def fake_taskkill(pid):
        calls["stopped"].append(pid)
        return local_app_control.CommandResult(returncode=0, stdout="", stderr="")

    def fake_popen(command, **kwargs):
        calls["started"].append((command, kwargs))
        return 8765

    result = local_app_control.start_app(
        root,
        env={},
        netstat_reader=fake_netstat,
        taskkill_runner=fake_taskkill,
        popen_runner=fake_popen,
        browser_opener=None,
    )

    assert calls["stopped"] == ["4321"]
    assert calls["started"][0][0] == [str(python_exe), "-X", "utf8", str(app_py)]
    assert calls["started"][0][1]["cwd"] == str(root)
    assert result["started_pid"] == 8765
    assert result["stopped_pids"] == ["4321"]
    assert result["base_url"] == "http://localhost:5000"


def test_stop_app_reports_all_stopped_listener_pids():
    calls = []

    def fake_netstat():
        return """
  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       1111
  TCP    127.0.0.1:5000         0.0.0.0:0              LISTENING       2222
"""

    def fake_taskkill(pid):
        calls.append(pid)
        return local_app_control.CommandResult(returncode=0, stdout="", stderr="")

    result = local_app_control.stop_app(
        port=5000,
        netstat_reader=fake_netstat,
        taskkill_runner=fake_taskkill,
    )

    assert calls == ["1111", "2222"]
    assert result["stopped_pids"] == ["1111", "2222"]
    assert result["failed_pids"] == []


def test_batch_launchers_delegate_to_local_app_control():
    root = Path(__file__).resolve().parents[1]
    start_bat = (root / "start_app.bat").read_text(encoding="utf-8")
    stop_bat = (root / "stop_app.bat").read_text(encoding="utf-8")

    assert "local_app_control.py start --open-browser" in start_bat
    assert "local_app_control.py stop" in stop_bat
