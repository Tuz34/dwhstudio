"""Run PR 16 regressions without dotenv, secrets, DB, network, GPU or filesystem writes.

Usage: <offline-venv-python> -B tests/run_pr16_offline.py
Only Groovy syntax/compile checks and isolated synthetic-object checks may spawn Java.
"""

import ast
import logging
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def guard(event, args):
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)):
            name = os.fsdecode(path).lower()
            if name in {os.devnull, os.path.abspath(os.devnull).lower()}:
                return
            if ".env" in Path(name).name or name.endswith((".properties", ".db", ".sqlite", ".jsonl", ".csv", ".parquet")):
                raise RuntimeError("OFFLINE_GUARD: prohibited file")
            if name.endswith(".json") and Path(path).is_relative_to(ROOT) and Path(path).name != "python_quality_baseline.json":
                raise RuntimeError("OFFLINE_GUARD: runtime data")
        writing = isinstance(mode, str) and any(c in mode for c in "wa+")
        writing |= isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing:
            raise RuntimeError("OFFLINE_GUARD: filesystem write")
    if event in {"socket.connect", "socket.getaddrinfo", "os.system", "os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
        raise RuntimeError("OFFLINE_GUARD: external action " + event)


def deny(*args, **kwargs):
    raise RuntimeError("OFFLINE_GUARD: database connection")


def main(modules=None):
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.dont_write_bytecode = True
    keep = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
    os.environ.clear()
    os.environ.update(keep)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    sys.addaudithook(guard)

    # Evaluate only public constant assignments; skip dotenv, secret-provider imports and log setup.
    config = types.ModuleType("config")
    config.__file__ = str(ROOT / "config.py")
    config.__dict__.update(
        os=os, get_secret=lambda name, default=None: default, get_secret_list=lambda name: [],
        logger=logging.getLogger("offline"),
    )
    tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
    assignments = [
        node for node in tree.body if isinstance(node, ast.Assign)
        and all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets)
    ]
    exec(compile(ast.Module(body=assignments, type_ignores=[]), "config.py", "exec"), config.__dict__)
    config.SOURCE_ORACLE_SCHEMAS = ["HR", "SH"]
    sys.modules["config"] = config

    import oracledb

    oracledb.connect = deny
    oracledb.create_pool = deny
    oracledb.init_oracle_client = deny

    import subprocess

    import odi_orchestrator as oo

    permitted_classpaths = {oo.OdiOrchestrator()._build_classpath(), oo._GROOVY_JAR}
    original_popen = subprocess.Popen

    def compiler_or_helpers_only(command, *args, **kwargs):
        allowed = (
            isinstance(command, list) and len(command) in {6, 7, 8}
            and Path(command[0]).resolve() == Path(oo._JAVA_BIN).resolve()
            and command[1] == "-cp" and command[2] in permitted_classpaths
            and command[3:5] == ["groovy.ui.GroovyMain", "-e"]
        )
        program = command[5] if allowed else ""
        syntax = program.startswith("import org.codehaus.groovy.control.CompilationUnit\n") and "Phases.CONVERSION" in program
        compile_only = program == "new GroovyClassLoader().parseClass(new File(args[0])); println('SDK_COMPILE_OK')"
        helpers = program.startswith('Locale.setDefault(new Locale("tr", "TR"))\n') and "CONTRACT_OK" in program
        from tests.test_e2e_scenario_ownership import SCENARIO_HARNESS, SCRIPT
        from tests.test_remote_run_groovy import REMOTE_HARNESS, ROOT as REMOTE_ROOT

        sdk_scenarios = (
            allowed and len(command) == 8 and program == SCENARIO_HARNESS
            and Path(command[6]).resolve() == SCRIPT.resolve()
        )
        remote = (allowed and len(command) == 7 and program == REMOTE_HARNESS
                  and Path(command[6]).resolve() == (REMOTE_ROOT / "run_scenario_remote.groovy").resolve())
        if not allowed or not (syntax or compile_only or sdk_scenarios or remote or (helpers and command[2] == oo._GROOVY_JAR)):
            raise RuntimeError("OFFLINE_GUARD: only isolated Groovy compiler/helpers may execute")
        return original_popen(command, *args, **kwargs)

    subprocess.Popen = compiler_or_helpers_only

    import pytest

    modules = modules or [
        "test_e2e_generic_path", "test_e2e_generic_groovy", "test_e2e_round2",
        "test_e2e_repository_preflight",
        "test_e2e_scenario_ownership",
        "test_mapping_generator_safety", "test_mapping_replace_ordering", "test_odi_orchestrator",
        "test_odi_groovy_safety", "test_odi_sync_failure", "test_groovy_execution_boundary",
        "test_python_quality_gate", "test_legacy_refactor", "test_odi72_quality_services",
    ]
    return pytest.main([
        "-p", "no:cacheprovider", "-p", "no:tmpdir", "--assert=plain", "--capture=sys",
        "-o", "addopts=", "-q", "--tb=short",
        *[str(ROOT / "tests" / (name + ".py")) for name in modules],
        "-k", "not classpath_includes and not classpath_skips and not test_dim_product_scenario_run_script_sdk_parse_only",
    ])


if __name__ == "__main__":
    raise SystemExit(main())
