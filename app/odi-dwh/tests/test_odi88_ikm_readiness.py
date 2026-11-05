"""ODI-88 item 1: an approved IKM absent from the repository fails closed before any write.

Offline only. The route tests run the real Flask route, generator, graph, orchestrator wrapper and
parser, and stub only ``run_groovy`` and the persistent steps. The SDK tests execute the real
``mapping_preflight.groovy`` on the installed SDK classpath with a fake entity manager, so no
repository, database or scenario is reached. Neither proves behaviour against a live repository.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import ikm_policy
import mapping_generator as mg
import mapping_graph
import odi_orchestrator as oo
import target_table_service as ts

SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation" / "mapping_preflight.groovy"
IKM = "IKM Oracle Merge"
SECRET = "password=hunter2 jdbc:oracle:thin:@secret-host C:\\secret\\km.xml SELECT PK_VALUE FROM T_SECRET"
PARAMS = dict(mapping_name="MAP_TEST", src_model="ORACLE_HR", src_table="HR.TEST", tgt_model="ORACLE_ODS",
              tgt_owner="DWH", tgt_table="ODS_TEST", update_key_columns=["ID"], ikm_name=IKM)
ENGINES = ["classic", "graph"]
PERSISTENT = ("_create_target_table", "_resolve_odi_repository_bindings", "_register_odi_datastores",
              "_create_odi_mapping", "_log_to_mapping_log", "_get_odi_repo_reader")


def _output(status="blocked", **extra):
    return json.dumps({"tool": "odi.mapping_preflight", "status": status, "side_effects": False,
                       "repository_connection_attempted": True, **extra})


PASSED = _output("passed", ikm_name=IKM, ikm_id="7", ikm_match_count=1, source=None)


@pytest.fixture
def stack(monkeypatch):
    """Record every Groovy call and persistent step behind the real route."""
    calls = {"groovy": [], "steps": []}
    monkeypatch.setattr(mg, "SOURCE_ORACLE_SCHEMAS", ["HR", "SH"])
    monkeypatch.setattr(mapping_graph, "_persist_graph_state", lambda *a, **k: None)

    def step(name):
        def record(self, *a, **k):
            calls["steps"].append(name)
            if name == "_create_target_table":
                calls["ikm_id"] = a[0].get("_odi_ikm_id")
            return {"status": "SUCCESS", "table": "ODS_TEST"}
        return record

    for name in PERSISTENT:
        monkeypatch.setattr(mg.MappingGenerator, name, step(name))
    for name in ("_connection", "_drop_target", "_apply_grants", "_write_creation_audit"):
        monkeypatch.setattr(ts, name, lambda *a, _n=name, **k: calls["steps"].append(_n))

    def respond(response):
        def run(self, script, params):
            calls["groovy"].append((script, params))
            if isinstance(response, Exception):
                raise response
            return response
        monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    calls["respond"] = respond
    return calls


def _generate(client, engine, **overrides):
    body = {"approved": True, "engine": engine, "params": {**PARAMS, **overrides}}
    response = client.post("/api/mapping/generate", json=body)
    return response.status_code, response.get_json(), response.get_data(as_text=True)


def _preflight_step(body, engine):
    return body["steps"]["repository_preflight"] if engine == "classic" else body["repository_preflight"]


@pytest.mark.parametrize("engine", ENGINES)
def test_absent_approved_ikm_returns_stable_code_and_stops_before_any_write(app_client, stack, engine):
    stack["respond"](_output(code="ikm_not_imported"))
    _, body, _ = _generate(app_client, engine)
    assert body["error_code"] == "ikm_not_imported"
    assert _preflight_step(body, engine)["code"] == "ikm_not_imported"
    assert [script for script, _ in stack["groovy"]] == ["mapping_preflight.groovy"]
    assert stack["steps"] == []
    assert body["overall_status"].upper() == "FAILED"


@pytest.mark.parametrize("engine", ENGINES)
def test_present_approved_ikm_reaches_persistent_steps_with_its_pinned_id(app_client, stack, engine):
    stack["respond"](PASSED)
    _generate(app_client, engine)
    assert stack["groovy"][0][0] == "mapping_preflight.groovy"
    assert "_create_target_table" in stack["steps"]
    assert stack["ikm_id"] == "7"


@pytest.mark.parametrize("engine", ENGINES)
def test_non_approved_ikm_is_blocked_before_groovy_and_repository_writes(app_client, stack, engine):
    stack["respond"](PASSED)
    _, body, _ = _generate(app_client, engine, ikm_name="IKM Custom Unreviewed")
    assert body["overall_status"].upper() == "FAILED"
    assert stack["groovy"] == []
    # The graph engine's existing validation-failure audit row is a separate, unchanged behaviour.
    assert not set(stack["steps"]) - {"_log_to_mapping_log"}


def test_wrapper_reports_non_approved_ikm_without_claiming_a_repository_call(stack):
    stack["respond"](PASSED)
    result = oo.OdiOrchestrator().preflight_mapping(ikm_name="IKM Custom Unreviewed", approved=True)
    assert result["status"] == "blocked"
    assert result["code"] == "ikm_not_approved"
    assert result["repository_connection_attempted"] is False
    assert stack["groovy"] == []


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("response,code", [
    (_output(code="ikm_ambiguous"), "ikm_ambiguous"),
    (_output(code="repository_lookup_failed"), "repository_lookup_failed"),
    (_output(code="ikm_missing"), "repository_lookup_failed"),
    (_output(code=SECRET), "repository_lookup_failed"),
    (f"Hata: {SECRET}", "repository_lookup_failed"),
    (RuntimeError(SECRET), "repository_lookup_failed"),
])
def test_ambiguous_unknown_or_failed_lookup_fails_closed_and_redacted(app_client, stack, engine, response, code):
    stack["respond"](response)
    _, body, text = _generate(app_client, engine)
    assert body["error_code"] == "repository_preflight_failed"
    assert _preflight_step(body, engine)["code"] == code
    assert stack["steps"] == []
    for fragment in ("hunter2", "secret-host", "km.xml", "PK_VALUE"):
        assert fragment not in text


@pytest.mark.parametrize("engine", ENGINES)
def test_non_literal_approval_stops_before_groovy(app_client, stack, engine):
    stack["respond"](PASSED)
    response = app_client.post("/api/mapping/generate", json={"approved": "true", "engine": engine, "params": PARAMS})
    assert response.status_code in {400, 403}
    assert stack["groovy"] == [] and stack["steps"] == []


def test_policy_projects_only_safe_keys_for_the_absent_ikm():
    result = ikm_policy.normalize_repository_preflight(
        json.loads(_output(code="ikm_not_imported", path="C:\\secret\\km.xml")), IKM, None, None, "GLOBAL")
    assert result["code"] == "ikm_not_imported"
    assert set(result) == {"tool", "status", "side_effects", "repository_connection_attempted", "code"}


# Real script on the installed SDK classpath, fake entity manager, no repository.

HARNESS = r'''
class FakeFinder {
    Class type; List kms; boolean explode; String secret
    List findAll() {
        if (explode) throw new RuntimeException(secret)
        if (type != oracle.odi.domain.project.OdiIKM) throw new IllegalStateException("unexpected finder")
        return kms
    }
}
def km = { name, id -> new Expando(getName: { name }, getInternalId: { id }) }
def kms = __KMS__
def em = new Expando(getFinder: { cls ->
    new FakeFinder(type: cls, kms: kms, explode: __EXPLODE__, secret: __SECRET__)
})
System.setProperty("odi.ikm_name", __IKM__)
System.setProperty("odi.context", "GLOBAL")
new GroovyShell(new Binding([odiInstance: new Expando(getTransactionalEntityManager: { em }),
                             args: new String[0]])).evaluate(new File(__SCRIPT__))
'''


def _real_script(tmp_path, kms, explode=False):
    java = shutil.which("java") or str(
        Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")) / "bin" / "java.exe")
    home = Path(os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    jar = home / "oracle_common/modules/thirdparty" / os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    sdk = home / "odi/sdk/lib"
    if not (Path(java).is_file() and jar.is_file() and (sdk / "odi-core.jar").is_file()):
        pytest.skip("Java, the Groovy jar or the ODI SDK is unavailable; nothing is downloaded.")
    program = (HARNESS.replace("__KMS__", kms).replace("__EXPLODE__", "true" if explode else "false")
               .replace("__SECRET__", json.dumps(SECRET)).replace("__IKM__", json.dumps(IKM))
               .replace("__SCRIPT__", json.dumps(str(SCRIPT))))
    path = tmp_path / "harness.groovy"
    path.write_text(program, encoding="utf-8")
    done = subprocess.run(
        [java, "-cp", os.pathsep.join([str(sdk / "*"), str(jar)]), "groovy.ui.GroovyMain", str(path)],
        capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("kms,explode,error_code,code", [
    ("[]", False, "ikm_not_imported", "ikm_not_imported"),
    ('[km("IKM Oracle Merge Custom", 1), km("IKM Oracle Insert", 2)]', False, "ikm_not_imported", "ikm_not_imported"),
    ('[km("IKM Oracle Merge", 1), km("ikm oracle merge", 2)]', False, "repository_preflight_failed", "ikm_ambiguous"),
    ("[]", True, "repository_preflight_failed", "repository_lookup_failed"),
])
def test_real_script_output_blocks_through_the_real_route(
    app_client, stack, tmp_path, engine, kms, explode, error_code, code,
):
    output = _real_script(tmp_path, kms, explode)
    assert "hunter2" not in output and "km.xml" not in output
    stack["respond"](output)
    _, body, text = _generate(app_client, engine)
    assert body["error_code"] == error_code
    assert _preflight_step(body, engine)["code"] == code
    assert stack["steps"] == []
    assert "hunter2" not in text


@pytest.mark.parametrize("engine", ENGINES)
def test_real_script_present_ikm_pins_id_through_the_real_route(app_client, stack, tmp_path, engine):
    stack["respond"](_real_script(tmp_path, '[km("IKM Oracle Merge", 7), km("IKM Oracle Insert", 8)]'))
    _generate(app_client, engine)
    assert "_create_target_table" in stack["steps"]
    assert stack["ikm_id"] == "7"
