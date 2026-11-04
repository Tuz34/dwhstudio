"""Local process control for the DWH Studio Flask app.

This module manages only the workstation-local Flask process. It does not touch
DB, ODI, WebLogic, Docker, Task Scheduler, services, credentials, or raw data.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_PORT = 5000


@dataclass(frozen=True)
class Listener:
    local_address: str
    pid: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    python_exe: Path | str
    app_py: Path
    logs_dir: Path
    stdout_log: Path
    stderr_log: Path
    port: int
    base_url: str


NetstatReader = Callable[[], str]
TaskkillRunner = Callable[[str], CommandResult]
PopenRunner = Callable[..., int]
BrowserOpener = Callable[[str], object]


def parse_netstat_listeners(text: str, port: int) -> list[Listener]:
    listeners: list[Listener] = []
    seen_pids: set[str] = set()
    port_marker = f":{port}"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local_address = parts[1]
        state = parts[-2]
        pid = parts[-1]
        if not local_address.endswith(port_marker):
            continue
        if state.upper() != "LISTENING":
            continue
        if pid == "0" or pid in seen_pids:
            continue
        listeners.append(Listener(local_address=local_address, pid=pid))
        seen_pids.add(pid)
    return listeners


def read_netstat() -> str:
    completed = subprocess.run(
        ["netstat", "-aon"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout or ""


def resolve_python_exe(root: Path) -> Path | str:
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    return "python"


def _port_from_env(env: dict[str, str]) -> int:
    value = env.get("APP_PORT") or str(DEFAULT_PORT)
    try:
        return int(value)
    except ValueError:
        return DEFAULT_PORT


def runtime_paths(root: Path, env: dict[str, str] | None = None) -> RuntimePaths:
    effective_env = env or {}
    port = _port_from_env(effective_env)
    base_url = effective_env.get("APP_URL") or f"http://localhost:{port}"
    logs_dir = root / "logs"
    return RuntimePaths(
        root=root,
        python_exe=resolve_python_exe(root),
        app_py=root / "app.py",
        logs_dir=logs_dir,
        stdout_log=logs_dir / "local_app_stdout.log",
        stderr_log=logs_dir / "local_app_stderr.log",
        port=port,
        base_url=base_url,
    )


def run_taskkill(pid: str) -> CommandResult:
    completed = subprocess.run(
        ["taskkill", "/F", "/T", "/PID", pid],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def stop_app(
    *,
    port: int = DEFAULT_PORT,
    netstat_reader: NetstatReader = read_netstat,
    taskkill_runner: TaskkillRunner = run_taskkill,
) -> dict[str, object]:
    stopped_pids: list[str] = []
    failed_pids: list[str] = []
    for listener in parse_netstat_listeners(netstat_reader(), port):
        result = taskkill_runner(listener.pid)
        if result.returncode == 0:
            stopped_pids.append(listener.pid)
        else:
            failed_pids.append(listener.pid)
    return {
        "action": "stop",
        "port": port,
        "stopped_pids": stopped_pids,
        "failed_pids": failed_pids,
    }


def _creation_flags() -> int:
    flags = 0
    if os.name == "nt":
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return flags


def run_popen(command: list[str], **kwargs) -> int:
    process = subprocess.Popen(command, **kwargs)
    return int(process.pid)


def start_app(
    root: Path,
    *,
    env: dict[str, str] | None = None,
    netstat_reader: NetstatReader = read_netstat,
    taskkill_runner: TaskkillRunner = run_taskkill,
    popen_runner: PopenRunner = run_popen,
    browser_opener: BrowserOpener | None = webbrowser.open,
) -> dict[str, object]:
    effective_env = dict(os.environ)
    if env:
        effective_env.update(env)
    paths = runtime_paths(root, effective_env)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    stop_result = stop_app(
        port=paths.port,
        netstat_reader=netstat_reader,
        taskkill_runner=taskkill_runner,
    )

    command = [str(paths.python_exe), "-X", "utf8", str(paths.app_py)]
    with paths.stdout_log.open("ab") as stdout_file, paths.stderr_log.open("ab") as stderr_file:
        started_pid = popen_runner(
            command,
            cwd=str(root),
            stdout=stdout_file,
            stderr=stderr_file,
            stdin=subprocess.DEVNULL,
            env=effective_env,
            creationflags=_creation_flags(),
            close_fds=True,
        )

    if browser_opener is not None:
        browser_opener(paths.base_url)

    return {
        "action": "start",
        "port": paths.port,
        "base_url": paths.base_url,
        "python_exe": str(paths.python_exe),
        "stdout_log": str(paths.stdout_log),
        "stderr_log": str(paths.stderr_log),
        "stopped_pids": stop_result["stopped_pids"],
        "failed_pids": stop_result["failed_pids"],
        "started_pid": started_pid,
    }


def status_app(
    *,
    port: int = DEFAULT_PORT,
    netstat_reader: NetstatReader = read_netstat,
) -> dict[str, object]:
    listeners = parse_netstat_listeners(netstat_reader(), port)
    return {
        "action": "status",
        "port": port,
        "listener_count": len(listeners),
        "listener_pids": [listener.pid for listener in listeners],
    }


def _print_report(report: dict[str, object]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the local DWH Studio Flask process.")
    parser.add_argument("command", choices=["start", "stop", "status"])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    env = dict(os.environ)
    if args.port is not None:
        env["APP_PORT"] = str(args.port)

    if args.command == "start":
        report = start_app(
            root,
            env=env,
            browser_opener=webbrowser.open if args.open_browser else None,
        )
    elif args.command == "stop":
        port = args.port if args.port is not None else _port_from_env(env)
        report = stop_app(port=port)
    else:
        port = args.port if args.port is not None else _port_from_env(env)
        report = status_app(port=port)

    _print_report(report)
    return 1 if report.get("failed_pids") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
