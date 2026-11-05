"""ODI-88 item 3: trusted context and owner bind one source model code.

Offline only. Route tests stub Groovy output and persistent steps. Groovy tests execute the
extracted helper, or the real script against fake SDK objects. Nothing here is a live repository
proof, and client fields cannot choose the model code.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import mapping_generator as mg
import mapping_graph
import odi_orchestrator as oo
import target_table_service as ts
from mapping_generator import RepositoryPreflightBlockedError

GROOVY_ROOT = Path(__file__).resolve().parents[1] / "odi_automation"

IKM = "IKM Oracle Merge"
BINDING = '{"GLOBAL":{"AUDIT_USER":"AUDIT_MODEL"}}'
PARAMS = dict(
    mapping_name="MAP_TEST", src_model="ORACLE_ODS", src_table="AUDIT_USER.FACT_SALES",
    tgt_model="ORACLE_ODS", tgt_owner="DWH", tgt_table="ODS_TEST",
    update_key_columns=["ID"], ikm_name=IKM, context="GLOBAL",
)
SOURCE = dict(
    model_code="AUDIT_MODEL", datastore_name="FACT_ALIAS", resource_name="FACT_SALES",
    physical_schema="AUDIT_USER", context="GLOBAL", datastore_exists=True, datastore_id="42",
)
ENGINES = ["classic", "graph"]
SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation" / "mapping_preflight.groovy"
PERSISTENT = (
    "_create_target_table", "_resolve_odi_repository_bindings", "_register_odi_datastores",
    "_create_odi_mapping", "_log_to_mapping_log", "_get_odi_repo_reader",
)


def _script_text() -> str:
    return (GROOVY_ROOT / "mapping_preflight.groovy").read_text(encoding="utf-8")


def _groovy_command() -> list[str]:
    parse_script = (
        "import org.codehaus.groovy.control.CompilationUnit\n"
        "import org.codehaus.groovy.control.CompilerConfiguration\n"
        "import org.codehaus.groovy.control.Phases\n"
        "def unit = new CompilationUnit(new CompilerConfiguration())\n"
        "unit.addSource(new File(args[0]))\n"
        "unit.compile(Phases.CONVERSION)\n"
        "println 'GROOVY_SYNTAX_OK'\n"
    )
    java_home = os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")
    java = shutil.which("java.exe" if os.name == "nt" else "java") or str(
        Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
    )
    odi_home = os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home")
    jar = Path(odi_home) / "oracle_common" / "modules" / "thirdparty" / os.environ.get(
        "GROOVY_JAR_NAME", "groovy-all-2.4.17.jar",
    )
    if not (Path(java).is_file() and jar.is_file()):
        pytest.fail("Standalone Groovy runtime is unavailable")
    return [java, "-cp", str(jar), "groovy.ui.GroovyMain", "-e", parse_script, str(GROOVY_ROOT / "mapping_preflight.groovy")]


def _run_isolated_helpers(program: str) -> None:
    command = _groovy_command()
    program = 'Locale.setDefault(new Locale("tr", "TR"))\n' + program
    completed = subprocess.run(
        command[:-2] + [program + "\nprintln 'CONTRACT_OK'"],
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "CONTRACT_OK" in completed.stdout


def _proof(**extra):
    payload = {
        "tool": "odi.mapping_preflight", "status": "passed", "side_effects": False,
        "repository_connection_attempted": True, "ikm_name": IKM, "ikm_id": "7",
        "ikm_match_count": 1, "source": None,
    }
    payload.update(extra)
    return payload


def _bind(monkeypatch, raw: object) -> None:
    monkeypatch.setattr("config.ODI_SOURCE_MODEL_BINDINGS", raw, raising=False)


@pytest.fixture
def stack(monkeypatch):
    calls = {"groovy": [], "steps": []}
    monkeypatch.setattr(mapping_graph, "_persist_graph_state", lambda *a, **k: None)

    def step(name):
        def record(self, *args, **kwargs):
            calls["steps"].append(name)
            if name == "_create_target_table":
                calls["ikm_id"] = args[0].get("_odi_ikm_id")
                calls["source_model"] = (args[0].get("_odi_source_identity") or {}).get("model_code")
            return {"status": "SUCCESS", "table": "ODS_TEST"}
        return record

    for name in PERSISTENT:
        monkeypatch.setattr(mg.MappingGenerator, name, step(name))
    for name in ("_connection", "_drop_target", "_apply_grants", "_write_creation_audit"):
        monkeypatch.setattr(ts, name, lambda *a, _name=name, **k: calls["steps"].append(_name))

    def respond(response):
        def run(self, script, params):
            calls["groovy"].append((script, params))
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


def test_empty_and_exact_bindings_parse_and_duplicate_keys_fail():
    import source_model_policy as policy

    assert policy.parse_source_model_bindings("") == {}
    assert policy.parse_source_model_bindings(None) == {}
    assert policy.parse_source_model_bindings("{}") == {}
    assert policy.parse_source_model_bindings("{\n  \"GLOBAL\": {\"AUDIT_USER\": \"AUDIT_MODEL\"}\n}") == {
        "GLOBAL": {"AUDIT_USER": "AUDIT_MODEL"},
    }
    assert policy.trusted_source_model_code("GLOBAL", "AUDIT_USER", BINDING) == "AUDIT_MODEL"
    assert policy.trusted_source_model_code("DEV", "AUDIT_USER", BINDING) is None
    assert policy.trusted_source_model_code("GLOBAL", "DWH", BINDING) is None
    assert policy.trusted_source_model_code("GLOBAL", None, BINDING) is None
    for raw in (
        "{", "[]", "1", "\"GLOBAL\"", {"GLOBAL": {"AUDIT_USER": "AUDIT_MODEL"}},
        '{"global":{"AUDIT_USER":"AUDIT_MODEL"}}',
        '{"GLOBAL":{"audit_user":"AUDIT_MODEL"}}',
        '{"GLOBAL":{"AUDIT_USER":"audit_model"}}',
        '{"GLOBAL":["AUDIT_MODEL"]}',
        '{"GLOBAL":{"AUDIT_USER":1}}',
        '{"GLOBAL":{"AUDIT_USER":"AUDIT_MODEL","AUDIT_USER":"OTHER_MODEL"}}',
        '{"GLOBAL":{},"GLOBAL":{"AUDIT_USER":"AUDIT_MODEL"}}',
        '{"GLOBAL":{"AUDIT_USER":"BAD-MODEL"}}',
    ):
        with pytest.raises(policy.SourceModelConfigError) as caught:
            policy.parse_source_model_bindings(raw)
        assert caught.value.code == "source_model_config_invalid"


def test_normalizer_requires_the_trusted_model_code_on_the_python_return():
    import ikm_policy

    matched = ikm_policy.normalize_repository_preflight(
        _proof(source=SOURCE), IKM, "AUDIT_USER", "FACT_SALES", "GLOBAL", expected_model_code="AUDIT_MODEL",
    )
    assert matched["status"] == "passed"
    assert matched["source"]["model_code"] == "AUDIT_MODEL"
    forged = {**SOURCE, "model_code": "OTHER_MODEL"}
    rejected = ikm_policy.normalize_repository_preflight(
        _proof(source=forged), IKM, "AUDIT_USER", "FACT_SALES", "GLOBAL", expected_model_code="AUDIT_MODEL",
    )
    assert rejected["code"] == "source_identity_unverified"
    assert "source" not in rejected


def test_invalid_binding_stops_before_groovy_and_keeps_approval_and_ikm_gates(monkeypatch):
    _bind(monkeypatch, "{")
    run = Mock(return_value="{}")
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    orchestrator = oo.OdiOrchestrator()
    denied = orchestrator.preflight_mapping(ikm_name=IKM, approved=False)
    assert denied["code"] == "approval_required"
    assert denied["repository_connection_attempted"] is False
    unapproved = orchestrator.preflight_mapping(ikm_name="IKM Custom Unreviewed", approved=True)
    assert unapproved["code"] == "ikm_not_approved"
    assert unapproved["repository_connection_attempted"] is False
    blocked = orchestrator.preflight_mapping(
        ikm_name=IKM, src_owner="AUDIT_USER", src_table="FACT_SALES", approved=True,
    )
    assert blocked["code"] == "source_model_config_invalid"
    assert blocked["repository_connection_attempted"] is False
    run.assert_not_called()


def test_empty_config_keeps_ambiguity_and_sends_no_model_code(monkeypatch):
    _bind(monkeypatch, "")
    run = Mock(return_value=json.dumps({
        "tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False,
        "repository_connection_attempted": True, "code": "source_identity_ambiguous",
    }))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    generator = mg.MappingGenerator()
    params = generator.validate_params(PARAMS)
    with pytest.raises(RepositoryPreflightBlockedError) as caught:
        generator._preflight_odi_repository(params)
    assert caught.value.detail["code"] == "source_identity_ambiguous"
    assert "odi.source_model_code" not in run.call_args.args[1]
    assert run.call_args.args[1]["odi.src_owner"] == "AUDIT_USER"
    assert run.call_args.args[1]["odi.context"] == "GLOBAL"


def test_configured_binding_is_server_side_and_client_fields_cannot_replace_it(monkeypatch):
    _bind(monkeypatch, BINDING)
    run = Mock(return_value=json.dumps(_proof(source=SOURCE)))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    generator = mg.MappingGenerator()
    params = generator.validate_params(dict(PARAMS, src_model="ORACLE_ODS", _odi_source_identity="CLIENT"))
    params["_odi_source_identity"] = {"model_code": "CLIENT_MODEL", "physical_schema": "AUDIT_USER"}
    generator._preflight_odi_repository(params)
    sent = run.call_args.args[1]
    assert sent["odi.source_model_code"] == "AUDIT_MODEL"
    assert "odi.src_model" not in sent
    assert "CLIENT_MODEL" not in sent.values()
    assert params["_odi_source_identity"]["model_code"] == "AUDIT_MODEL"
    assert params["src_model"] == "ORACLE_ODS"


def test_forged_model_code_does_not_become_the_verified_identity(monkeypatch):
    _bind(monkeypatch, BINDING)
    forged = {**SOURCE, "model_code": "OTHER_MODEL"}
    run = Mock(return_value=json.dumps(_proof(source=forged)))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    generator = mg.MappingGenerator()
    params = generator.validate_params(PARAMS)
    params["_odi_source_identity"] = {"model_code": "CLIENT_MODEL"}
    with pytest.raises(RepositoryPreflightBlockedError) as caught:
        generator._preflight_odi_repository(params)
    assert caught.value.detail["code"] == "source_identity_unverified"
    assert "_odi_source_identity" not in params
    assert run.call_args.args[1]["odi.source_model_code"] == "AUDIT_MODEL"


def test_wrong_context_does_not_reuse_another_context_binding(monkeypatch):
    _bind(monkeypatch, BINDING)
    run = Mock(return_value=json.dumps({
        "tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False,
        "repository_connection_attempted": True, "code": "source_identity_ambiguous",
    }))
    monkeypatch.setattr(oo.OdiOrchestrator, "run_groovy", run)
    result = oo.OdiOrchestrator().preflight_mapping(
        ikm_name=IKM, src_owner="AUDIT_USER", src_table="FACT_SALES", context="DEV", approved=True,
    )
    assert result["code"] == "source_identity_ambiguous"
    assert "odi.source_model_code" not in run.call_args.args[1]
    assert run.call_args.args[1]["odi.context"] == "DEV"


@pytest.mark.parametrize("engine", ENGINES)
def test_both_engines_refuse_global_ambiguity_until_the_server_binding_is_present(app_client, stack, engine, monkeypatch):
    _bind(monkeypatch, "")
    stack["respond"](json.dumps({
        "tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False,
        "repository_connection_attempted": True, "code": "source_identity_ambiguous",
    }))
    _, body, _ = _generate(app_client, engine, src_model="ORACLE_ODS", _odi_source_identity={"model_code": "CLIENT_MODEL"})
    assert body["overall_status"].upper() == "FAILED"
    assert _preflight_step(body, engine)["code"] == "source_identity_ambiguous"
    assert stack["steps"] == []
    assert "odi.source_model_code" not in stack["groovy"][0][1]


@pytest.mark.parametrize("engine", ENGINES)
def test_both_engines_accept_one_configured_model_and_keep_graph_checkpoints(app_client, stack, engine, monkeypatch):
    _bind(monkeypatch, BINDING)
    stack["respond"](json.dumps(_proof(source=SOURCE)))
    _, body, _ = _generate(app_client, engine, src_model="ORACLE_ODS")
    assert body["overall_status"].upper() == "SUCCESS"
    assert stack["groovy"][0][1]["odi.source_model_code"] == "AUDIT_MODEL"
    assert stack["source_model"] == "AUDIT_MODEL"
    assert "_create_target_table" in stack["steps"]
    run_source = Path(mapping_graph.__file__).read_text(encoding="utf-8")
    run = run_source.split("def run_mapping_graph", 1)[1].split("def print_graph_ascii", 1)[0]
    assert "get_graph(use_memory_checkpoint=True)" in run
    assert "_persist_graph_state(" in run
    assert "use_memory_checkpoint=False" not in run


@pytest.mark.parametrize("engine", ENGINES)
def test_both_engines_reject_unsafe_config_and_a_forged_model_before_writes(app_client, stack, engine, monkeypatch):
    _bind(monkeypatch, '{"GLOBAL":{"AUDIT_USER":"AUDIT_MODEL","AUDIT_USER":"OTHER_MODEL"}}')
    stack["respond"](json.dumps(_proof(source=SOURCE)))
    _, unsafe, _ = _generate(app_client, engine)
    assert _preflight_step(unsafe, engine)["code"] == "source_model_config_invalid"
    assert _preflight_step(unsafe, engine)["repository_connection_attempted"] is False
    assert stack["groovy"] == []
    assert stack["steps"] == []

    _bind(monkeypatch, BINDING)
    stack["respond"](json.dumps(_proof(source={**SOURCE, "model_code": "OTHER_MODEL"})))
    _, forged, _ = _generate(app_client, engine)
    assert _preflight_step(forged, engine)["code"] == "source_identity_unverified"
    assert stack["groovy"][-1][1]["odi.source_model_code"] == "AUDIT_MODEL"
    assert "_create_target_table" not in stack["steps"]


def test_default_graph_checkpoint_is_not_cleared():
    graph = mapping_graph.get_graph(use_memory_checkpoint=True)
    assert graph.checkpointer is not None
    assert type(graph.checkpointer).__name__ in {"MemorySaver", "InMemorySaver"}


def test_inspect_source_binding_fails_closed_for_every_unproven_shape():
    helpers = _script_text()
    helpers = helpers[helpers.index("def inspectIkm ="):helpers.index("def odi = null")]
    _run_isolated_helpers(helpers + r'''
def model = { code, owner, resource, id, technology, shortcut, datastoreName ->
    def ds = new Expando(getName: { datastoreName }, getResourceName: { resource },
        getInternalId: { id }, isShortcut: { shortcut })
    def physical = owner == null ? null : new Expando(getSchemaName: { owner })
    def logical = new Expando(getPhysicalSchema: { ctx -> ctx == "GLOBAL" ? physical : null })
    new Expando(getCode: { code }, getLogicalSchema: { logical },
        getDataStores: { resource == null ? [] : [ds] },
        getTechnology: { new Expando(getName: { technology }) })
}
def other = model("ORACLE_ODS", "AUDIT_USER", "FACT_SALES", 1, "Oracle", false, "OTHER_ALIAS")
def chosen = model("AUDIT_MODEL", "AUDIT_USER", "FACT_SALES", 42, "Oracle", false, "FACT_ALIAS")
assert inspectSource([other, chosen], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL").code == "source_identity_ambiguous"
def bound = inspectSource([other, chosen], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL")
assert bound.status == "passed" && bound.source.model_code == "AUDIT_MODEL" && bound.source.datastore_id == "42"
assert inspectSource([other, chosen], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "MISSING_MODEL").code == "source_identity_missing"
def wrongOwner = model("AUDIT_MODEL", "DWH", "FACT_SALES", 7, "Oracle", false, "FACT_ALIAS")
assert inspectSource([other, wrongOwner], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL").code == "source_identity_unverified"
assert inspectSource([chosen], "AUDIT_USER", "FACT_SALES", "DEV", "DEV", "AUDIT_MODEL").code == "source_identity_unverified"
assert inspectSource([chosen], "AUDIT_USER", "FACT_SALES", null, "GLOBAL", "AUDIT_MODEL").code == "source_identity_unverified"
def notOracle = model("AUDIT_MODEL", "AUDIT_USER", "FACT_SALES", 8, "PostgreSQL", false, "FACT_ALIAS")
assert inspectSource([other, notOracle], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL").code == "source_identity_unverified"
def shortcut = model("AUDIT_MODEL", "AUDIT_USER", "FACT_SALES", 9, "Oracle", true, "FACT_ALIAS")
assert inspectSource([other, shortcut], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL").code == "source_identity_unverified"
def twin = model("AUDIT_MODEL", "AUDIT_USER", "FACT_SALES", 11, "Oracle", false, "FACT_ALIAS")
assert inspectSource([chosen, twin], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL").code == "source_identity_ambiguous"
def first = new Expando(getName: { "FACT_ALIAS" }, getResourceName: { "FACT_SALES" }, getInternalId: { 12 }, isShortcut: { false })
def second = new Expando(getName: { "FACT_OTHER" }, getResourceName: { "AUDIT_USER.FACT_SALES" }, getInternalId: { 13 }, isShortcut: { false })
def logical = new Expando(getPhysicalSchema: { ctx -> new Expando(getSchemaName: { "AUDIT_USER" }) })
def duplicates = new Expando(getCode: { "AUDIT_MODEL" }, getLogicalSchema: { logical }, getDataStores: { [first, second] },
    getTechnology: { new Expando(getName: { "Oracle" }) })
assert inspectSource([duplicates, other], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL").code == "source_identity_ambiguous"
def collision = model("AUDIT_MODEL", "AUDIT_USER", "OTHER_RESOURCE", 44, "Oracle", false, "FACT_SALES")
assert inspectSource([collision, other], "AUDIT_USER", "FACT_SALES", "GLOBAL", "GLOBAL", "AUDIT_MODEL").code == "source_identity_unverified"
''')


HARNESS = r'''
class FakeFinder {
    Class type; List kms; List models; Object context
    List findAll() {
        if (type == oracle.odi.domain.project.OdiIKM) return kms
        if (type == oracle.odi.domain.model.OdiModel) return models
        throw new IllegalStateException("unexpected finder")
    }
    Object findByCode(String code) {
        if (type != oracle.odi.domain.topology.OdiContext) throw new IllegalStateException("unexpected finder")
        return code == "GLOBAL" ? context : null
    }
}
def km = { name, id -> new Expando(getName: { name }, getInternalId: { id }) }
def model = { code, owner, technology ->
    def ds = new Expando(getName: { code + "_ALIAS" }, getResourceName: { "FACT_SALES" },
        getInternalId: { 42 }, isShortcut: { false })
    def physical = new Expando(getSchemaName: { owner })
    def logical = new Expando(getPhysicalSchema: { ctx -> ctx?.getCode() == "GLOBAL" ? physical : null })
    new Expando(getCode: { code }, getLogicalSchema: { logical }, getDataStores: { [ds] },
        getTechnology: { new Expando(getName: { technology }) })
}
def contextToken = new Expando(getCode: { "GLOBAL" })
def models = __MODELS__
def em = new Expando(getFinder: { cls ->
    new FakeFinder(type: cls, kms: [km("IKM Oracle Merge", 7)], models: models, context: contextToken)
})
System.setProperty("odi.ikm_name", "IKM Oracle Merge")
System.setProperty("odi.context", "GLOBAL")
System.setProperty("odi.src_owner", "AUDIT_USER")
System.setProperty("odi.src_table", "FACT_SALES")
System.setProperty("odi.src_model", "CLIENT_MODEL")
__MODEL_PROPERTY__
new GroovyShell(new Binding([odiInstance: new Expando(getTransactionalEntityManager: { em }),
                             args: new String[0]])).evaluate(new File(__SCRIPT__))
'''


def _sdk_available() -> tuple[str, str] | None:
    java = shutil.which("java") or str(Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")) / "bin" / "java.exe")
    home = Path(os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    jar = home / "oracle_common/modules/thirdparty" / os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    sdk = home / "odi/sdk/lib"
    if Path(java).is_file() and jar.is_file() and (sdk / "odi-core.jar").is_file():
        return java, os.pathsep.join([str(sdk / "*"), str(jar)])
    return None


@pytest.mark.parametrize("models,property_line,code,model_code", [
    ('[model("ORACLE_ODS", "AUDIT_USER", "Oracle"), model("AUDIT_MODEL", "AUDIT_USER", "Oracle")]',
     'System.clearProperty("odi.source_model_code")', "source_identity_ambiguous", None),
    ('[model("ORACLE_ODS", "AUDIT_USER", "Oracle"), model("AUDIT_MODEL", "AUDIT_USER", "Oracle")]',
     'System.setProperty("odi.source_model_code", "AUDIT_MODEL")', "passed", "AUDIT_MODEL"),
    ('[model("ORACLE_ODS", "AUDIT_USER", "Oracle")]',
     'System.setProperty("odi.source_model_code", "MISSING_MODEL")', "source_identity_missing", None),
    ('[model("ORACLE_ODS", "AUDIT_USER", "Oracle"), model("AUDIT_MODEL", "DWH", "Oracle")]',
     'System.setProperty("odi.source_model_code", "AUDIT_MODEL")', "source_identity_unverified", None),
    ('[model("AUDIT_MODEL", "AUDIT_USER", "PostgreSQL"), model("ORACLE_ODS", "AUDIT_USER", "Oracle")]',
     'System.setProperty("odi.source_model_code", "AUDIT_MODEL")', "source_identity_unverified", None),
])
def test_real_preflight_script_honors_only_the_server_model_property(tmp_path, models, property_line, code, model_code):
    runtime = _sdk_available()
    if runtime is None:
        pytest.fail("Java, the Groovy jar or the ODI SDK is unavailable")
    java, classpath = runtime
    program = (HARNESS.replace("__MODELS__", models).replace("__MODEL_PROPERTY__", property_line)
               .replace("__SCRIPT__", json.dumps(str(SCRIPT))))
    path = tmp_path / "harness.groovy"
    path.write_text(program, encoding="utf-8")
    env = os.environ.copy()
    env["JAVA_TOOL_OPTIONS"] = ""
    done = subprocess.run(
        [java, "-cp", classpath, "groovy.ui.GroovyMain", str(path)],
        capture_output=True, text=True, timeout=120, check=False, env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    observed = payload.get("status") if code == "passed" else payload.get("code")
    assert observed == ("passed" if code == "passed" else code), payload
    if model_code is not None:
        assert payload["source"]["model_code"] == model_code
    assert payload["side_effects"] is False
    assert "CLIENT_MODEL" not in done.stdout
