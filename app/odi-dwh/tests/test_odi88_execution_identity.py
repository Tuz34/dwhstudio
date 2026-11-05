"""ODI-88 item 4: topology-bound execution identity for SCD2 grants.

Offline only. The Groovy cases execute the real probe against fake SDK objects when Java,
the Groovy jar, and the ODI SDK are installed. A missing runtime skips with a reason and is
not a pass. Nothing here opens a repository, a database, or a property file.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import execution_identity
import scd2_builder
import scd2_service
import scd2_target_executor as executor
from tests.scd2_support import (
    REQUEST,
    SOURCE_COLUMNS,
    FakeOrchestrator,
    connection_factory,
    identity_payload,
    make_services,
    plan_catalog_rows,
)

SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation" / "execution_identity_probe.groovy"
_SKIP = "Java, the Groovy jar or the ODI SDK is unavailable; nothing is downloaded."
_HIDDEN = (
    "ATTACKER",
    "NOT_THE_USER",
    "PostgreSQL",
    "LEAKED",
    "etl_runner",
    "OTHER_OWNER",
    "bad model",
    "password",
    "jdbc",
)


@pytest.fixture(autouse=True)
def default_grant_config(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", True)
    monkeypatch.setattr(config, "ODI_EXEC_USER", "ODI_EXEC")


@pytest.fixture
def plan():
    return scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)


def _proof(plan, username="ODI_EXEC", **overrides):
    model_code = overrides.get("model_code", plan["models"]["target"])
    schema_name = overrides.get("schema_name", plan["target"]["owner"])
    context_code = overrides.get("context_code", execution_identity.EXECUTION_CONTEXT)
    public, proof = execution_identity.interpret_execution_identity(
        identity_payload(plan, username, **overrides),
        model_code=model_code,
        context_code=context_code,
        schema_name=schema_name,
    )
    assert proof is not None and public["status"] == "passed" and "username" not in public
    return proof


def _run_ddl(plan, handler, proof):
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=True,
        connection_factory=connection_factory(log, handler),
        identity_proof=proof,
    )
    return log, result


def _ddl_handler(plan, users=None):
    def handler(sql, params):
        if "FROM ALL_USERS" in sql:
            return [((users or {}).get(params.get("u"), 1),)]
        if "FROM ALL_TABLES" in sql or "FROM ALL_SEQUENCES" in sql:
            return [(0,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(plan)
        return []

    return handler


def test_probe_script_uses_inherited_public_getters_only():
    text = SCRIPT.read_text(encoding="utf-8")
    for required in ("findAll()", "getUsername()", "getDataServer()", "getSchemaName()", "AbstractOdiDataServer"):
        assert required in text
    for forbidden in (
        "findByCode",
        "findByLogicalSchema",
        ".getPassword(",
        ".getJdbcUrl(",
        ".getConnectionSettings(",
        ".getConnectionProperties(",
        ".getConnectionInfo(",
        ".getDataServerProperties(",
        ".getWsBaseUrl(",
        ".getProviderUrl(",
        "server.getName(",
        "dataServer.getName(",
    ):
        assert forbidden not in text, forbidden


def test_grant_helpers_ignore_config_until_a_proven_account_is_supplied(monkeypatch):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_USER", "CFG_EXEC")
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    assert scd2_builder.render_grant_statements(plan) == []
    assert scd2_builder.required_grants(plan) == []
    assert scd2_builder.required_grantees(plan) == []
    proven_grants = scd2_builder.required_grants(plan, "ETL_RUNNER")
    assert {grantee for _name, grantee, _privilege in proven_grants} == {"ETL_RUNNER"}
    assert scd2_builder.required_grantees(plan, "ETL_RUNNER") == ["ETL_RUNNER"]
    assert "CFG_EXEC" not in str(proven_grants)
    reader = scd2_builder.build_scd2_plan(
        {**REQUEST, "grant_select_to_app_user": True}, SOURCE_COLUMNS, exec_user="DM"
    )
    assert scd2_builder.required_grants(reader, "DM") == [("DIM_CUSTOMER", "APP_USER", "SELECT")]
    assert scd2_builder.required_grantees(reader, "DM") == ["APP_USER"]
    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    assert scd2_builder.required_grants(plan, "ETL_RUNNER") == []
    assert scd2_builder.required_grantees(plan, "ETL_RUNNER") == []


def test_repair_grants_follow_the_proven_account(monkeypatch, plan):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_USER", "CFG_EXEC")
    asked = []

    def handler(sql, params):
        if "FROM ALL_USERS" in sql:
            asked.append(params["u"])
            return [(1,)]
        if "FROM ALL_TABLES" in sql or "FROM ALL_SEQUENCES" in sql:
            return [(1,)]
        if "FROM ALL_TAB_COLUMNS" in sql:
            return plan_catalog_rows(plan)
        return []

    log, result = _run_ddl(plan, handler, _proof(plan, "ETL_RUNNER"))
    assert result["status"] == "repaired_incomplete_target" and result["repaired"] == ["grants"]
    grants = [sql for sql in log if sql.startswith("GRANT")]
    assert len(grants) == 5 and all(sql.endswith("TO ETL_RUNNER") for sql in grants)
    assert asked == ["ETL_RUNNER"]
    assert "CFG_EXEC" not in str(result) and not any("CFG_EXEC" in sql for sql in log)


def test_approval_blocks_before_identity_and_before_any_read(plan):
    forged = SimpleNamespace(username="ATTACKER", model_code="TGT_DM", context_code="GLOBAL", schema_name="DM")
    log = []
    result = executor.execute_target_ddl(
        plan,
        approved=False,
        connection_factory=connection_factory(log, _ddl_handler(plan)),
        identity_proof=forged,
    )
    assert result["status"] == "blocked_until_approval"
    assert result["connection_attempted"] is False and log == []
    assert "ATTACKER" not in str(result)


def test_missing_or_forged_identity_is_refused_before_connection(plan):
    forged = SimpleNamespace(username="ATTACKER", model_code="TGT_DM", context_code="GLOBAL", schema_name="DM")
    for proof in (None, forged, {"username": "ATTACKER"}):
        log, result = _run_ddl(plan, _ddl_handler(plan), proof)
        assert result["status"] == "blocked_execution_identity"
        assert result["code"] == "execution_identity_unverified"
        assert result["connection_attempted"] is False and log == []
        assert "ATTACKER" not in str(result) and "ODI_EXEC" not in str(result)


def test_mismatched_proof_or_explicit_request_is_refused_without_names(plan):
    log, result = _run_ddl(plan, _ddl_handler(plan), _proof(plan, "ODI_EXEC", schema_name="DWH"))
    assert result["code"] == "execution_identity_unverified" and result["connection_attempted"] is False
    assert log == [] and "DWH" not in str(result) and "ODI_EXEC" not in str(result)

    log, result = _run_ddl(plan, _ddl_handler(plan), _proof(plan, "ODI_EXEC", model_code="TGT_DWH"))
    assert result["code"] == "execution_identity_unverified" and log == []
    assert "TGT_DWH" not in str(result) and "ODI_EXEC" not in str(result)

    requested = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS, exec_user="ETL_RUNNER")
    log, result = _run_ddl(requested, _ddl_handler(requested), _proof(requested, "ODI_EXEC"))
    assert result["code"] == "execution_identity_rejected"
    assert result["connection_attempted"] is False and log == []
    assert "ETL_RUNNER" not in str(result) and "ODI_EXEC" not in str(result)


def test_normalizer_rejects_malformed_ambiguous_and_secret_payloads(plan):
    model = plan["models"]["target"]
    owner = plan["target"]["owner"]

    def interpret(output):
        return execution_identity.interpret_execution_identity(
            output, model_code=model, context_code="GLOBAL", schema_name=owner
        )

    public, proof = interpret('{"tool":"odi.execution_identity","tool":"again"}')
    assert proof is None and public["code"] == "execution_identity_unverified"
    public, proof = interpret("Hata: user=ETL_RUNNER password=hunter2")
    assert proof is None and "hunter2" not in str(public) and "ETL_RUNNER" not in str(public)
    leaked = json.loads(identity_payload(plan, "ETL_RUNNER"))
    leaked["password"] = "hunter2"
    public, proof = interpret(json.dumps(leaked))
    assert proof is None and "hunter2" not in str(public) and "ETL_RUNNER" not in str(public)
    leaked = json.loads(identity_payload(plan, "ETL_RUNNER"))
    leaked["model_count"] = True
    public, proof = interpret(json.dumps(leaked))
    assert proof is None and "ETL_RUNNER" not in str(public)
    leaked = json.loads(identity_payload(plan, "etl_runner"))
    public, proof = interpret(json.dumps(leaked))
    assert proof is None and "etl_runner" not in str(public)
    leaked = json.loads(identity_payload(plan, "ETL_RUNNER"))
    leaked["technology"] = "PostgreSQL"
    public, proof = interpret(json.dumps(leaked))
    assert proof is None and "PostgreSQL" not in str(public)
    blocked = {
        "tool": execution_identity.TOOL,
        "status": "blocked",
        "code": "execution_identity_ambiguous",
        "mode": execution_identity.MODE,
        "repository_connection_attempted": True,
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "model_count": 2,
        "context_count": 1,
        "mapping_count": 0,
        "username": "LEAKED_NAME",
    }
    public, proof = interpret(json.dumps(blocked))
    assert proof is None and public["code"] == "execution_identity_unverified"
    assert "LEAKED_NAME" not in str(public)
    del blocked["username"]
    public, proof = interpret(json.dumps(blocked))
    assert proof is None and public["code"] == "execution_identity_ambiguous"
    assert public["model_count"] == 2 and "username" not in public
    with pytest.raises(execution_identity.ExecutionIdentityError) as excinfo:
        execution_identity.ExecutionIdentityProof("bad user", model, "GLOBAL", owner)
    assert str(excinfo.value) == "execution_identity_invalid" and "bad user" not in str(excinfo.value)


def test_request_field_cannot_choose_the_account_and_preview_stays_connectionless(plan):
    catalog_log = []
    services = make_services(FakeOrchestrator(plan), catalog_log=catalog_log)
    body = {**REQUEST, "source_columns": SOURCE_COLUMNS, "exec_user": "ATTACKER"}
    payload, status = scd2_service.plan_preview(body, services)
    assert status == 200 and catalog_log == []
    assert payload["ddl"]["grants"] == []
    assert payload["plan"]["options"]["exec_user_requested"] is False
    assert "ATTACKER" not in str(payload)
    ddl_log = []
    generated, generate_status = scd2_service.generate(
        body, make_services(FakeOrchestrator(plan), ddl_log=ddl_log, ddl_handler=_ddl_handler(plan)), approved=True
    )
    assert generate_status == 200 and generated["status"] == "generated"
    assert "ATTACKER" not in str(generated)
    assert any(sql.endswith("TO ODI_EXEC") for sql in ddl_log if sql.startswith("GRANT"))


def test_disabled_grants_do_not_probe_or_grant(monkeypatch, plan):
    import config

    monkeypatch.setattr(config, "ODI_EXEC_GRANTS_ENABLED", False)
    orchestrator = FakeOrchestrator(plan)
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log, ddl_handler=_ddl_handler(plan)), approved=True
    )
    assert status == 200 and payload["status"] == "generated"
    assert "execution_identity_probe.groovy" not in orchestrator.order
    assert not any(sql.startswith("GRANT") or "ALL_USERS" in sql for sql in ddl_log)


def test_explicit_mismatch_and_ambiguous_probe_stop_before_ddl(monkeypatch, plan):
    requested = {
        **plan,
        "options": {**plan["options"], "exec_user_requested": True, "exec_user": "OTHER_USER"},
    }
    monkeypatch.setattr(scd2_service, "_build_plan", lambda *_args, **_kwargs: (requested, "test", None))
    orchestrator = FakeOrchestrator(plan)
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log, ddl_handler=_ddl_handler(plan)), approved=True
    )
    assert status == 409 and payload["status"] == "blocked_execution_identity"
    identity = payload["steps"][-1]
    assert identity["code"] == "execution_identity_rejected"
    assert ddl_log == [] and "OTHER_USER" not in str(payload) and "ODI_EXEC" not in str(payload)
    assert orchestrator.order == ["scd2_probe.groovy", "execution_identity_probe.groovy"]

    ambiguous = {
        "tool": execution_identity.TOOL,
        "status": "blocked",
        "code": "execution_identity_ambiguous",
        "mode": execution_identity.MODE,
        "repository_connection_attempted": True,
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "secret_printed": False,
        "model_count": 2,
        "context_count": 1,
        "mapping_count": 0,
    }
    monkeypatch.setattr(scd2_service, "_build_plan", lambda *_args, **_kwargs: (plan, "test", None))
    orchestrator = FakeOrchestrator(plan, responses={"execution_identity_probe.groovy": json.dumps(ambiguous)})
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log, ddl_handler=_ddl_handler(plan)), approved=True
    )
    assert status == 409 and payload["steps"][-1]["code"] == "execution_identity_ambiguous"
    assert ddl_log == [] and "username" not in payload["steps"][-1]


def test_malformed_probe_output_is_redacted(plan):
    orchestrator = FakeOrchestrator(
        plan, responses={"execution_identity_probe.groovy": "Hata: user=ETL_RUNNER password=hunter2"}
    )
    ddl_log = []
    payload, status = scd2_service.generate(
        REQUEST, make_services(orchestrator, ddl_log=ddl_log), approved=True
    )
    assert status == 409 and payload["steps"][-1]["code"] == "execution_identity_unverified"
    assert ddl_log == []
    assert "hunter2" not in str(payload) and "ETL_RUNNER" not in str(payload) and "Hata" not in str(payload)


def test_wrapper_status_and_logs_omit_the_proven_account(caplog, plan):
    from odi_orchestrator import OdiOrchestrator

    secret = "ETL_RUNNER"
    orchestrator = OdiOrchestrator()
    orchestrator.run_groovy = lambda _script, _params: identity_payload(plan, secret)
    caplog.set_level(logging.DEBUG)
    public, proof = orchestrator.prove_execution_identity(
        model_code=plan["models"]["target"],
        context_code="GLOBAL",
        schema_name=plan["target"]["owner"],
    )
    assert proof.username == secret
    assert secret not in str(public) and secret not in caplog.text
    assert "username" not in public


def test_wrapper_rejects_a_caller_chosen_context_before_groovy():
    from odi_orchestrator import OdiOrchestrator

    orchestrator = OdiOrchestrator()
    orchestrator.run_groovy = lambda *_args, **_kwargs: pytest.fail("caller context must not reach Groovy")
    public, proof = orchestrator.prove_execution_identity(
        model_code="TGT_DM", context_code="DEV", schema_name="DM"
    )
    assert proof is None and public["code"] == "execution_identity_invalid"
    assert public["repository_connection_attempted"] is False and "DEV" not in str(public)
    with pytest.raises(TypeError):
        orchestrator.prove_execution_identity(
            model_code="TGT_DM", context_code="GLOBAL", schema_name="DM", username="ATTACKER"
        )


def _sdk_available() -> tuple[str, str] | None:
    java = shutil.which("java") or str(
        Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")) / "bin" / "java.exe"
    )
    home = Path(os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    jar = home / "oracle_common/modules/thirdparty" / os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    sdk = home / "odi/sdk/lib"
    if Path(java).is_file() and jar.is_file() and (sdk / "odi-core.jar").is_file():
        return java, os.pathsep.join([str(sdk / "*"), str(jar)])
    return None


_HARNESS = r'''
import groovy.json.JsonOutput

class FakeFinder {
    List items
    List forbidden
    List findAll() { items }
    Object findByCode(Object code) {
        forbidden.add("findByCode")
        throw new IllegalStateException("findByCode")
    }
    Object findByLogicalSchema(Object left, Object right) {
        forbidden.add("findByLogicalSchema")
        throw new IllegalStateException("findByLogicalSchema")
    }
}

def scriptPath = __SCRIPT__
def results = []
def namedTech = { String name -> new Expando(getName: { name }) }
def dataServer = { List forbidden, String username, String technologyName ->
    new Expando(
        getUsername: { username },
        getName: { forbidden.add("getName"); "NOT_THE_USER" },
        getTechnology: { namedTech(technologyName) },
        getPassword: { forbidden.add("getPassword"); throw new IllegalStateException("getPassword") },
        getJdbcUrl: { forbidden.add("getJdbcUrl"); throw new IllegalStateException("getJdbcUrl") },
        getConnectionSettings: { forbidden.add("getConnectionSettings"); throw new IllegalStateException("getConnectionSettings") },
        getConnectionProperties: { forbidden.add("getConnectionProperties"); throw new IllegalStateException("getConnectionProperties") },
        getConnectionInfo: { forbidden.add("getConnectionInfo"); throw new IllegalStateException("getConnectionInfo") },
        getDataServerProperties: { forbidden.add("getDataServerProperties"); throw new IllegalStateException("getDataServerProperties") },
        getWsBaseUrl: { forbidden.add("getWsBaseUrl"); throw new IllegalStateException("getWsBaseUrl") },
        getProviderUrl: { forbidden.add("getProviderUrl"); throw new IllegalStateException("getProviderUrl") },
        getInternalId: { "server" },
    )
}
def arrangement = { List forbidden, Map spec ->
    def logical = new Expando(getInternalId: { "LOGICAL" }, getTechnology: { namedTech("Oracle") })
    def physical = new Expando(
        getSchemaName: { spec.schemaName ?: "DM" },
        getInternalId: { spec.physicalId ?: "PHYS" },
        getTechnology: { namedTech(spec.physicalTechnology ?: "Oracle") },
        getDataServer: {
            spec.nullServer ? null : dataServer(forbidden, spec.username ?: "ETL_RUNNER", spec.serverTechnology ?: "Oracle")
        },
    )
    def resolved = physical
    if (spec.disagree) {
        resolved = new Expando(
            getSchemaName: { "DM" }, getInternalId: { "OTHER_PHYS" }, getTechnology: { namedTech("Oracle") },
            getDataServer: { dataServer(forbidden, spec.username ?: "ETL_RUNNER", "Oracle") },
        )
    } else if (spec.sameIdCopy) {
        resolved = new Expando(
            getSchemaName: { spec.schemaName ?: "DM" }, getInternalId: { spec.physicalId ?: "PHYS" },
            getTechnology: { namedTech(spec.physicalTechnology ?: "Oracle") },
            getDataServer: { dataServer(forbidden, spec.username ?: "ETL_RUNNER", "Oracle") },
        )
    }
    logical.getPhysicalSchema = { ctx -> resolved }
    def makeModel = { String code ->
        new Expando(
            getCode: { code },
            getLogicalSchema: { spec.nullLogical ? null : logical },
            getTechnology: { namedTech(spec.modelTechnology ?: "Oracle") },
        )
    }
    def model = makeModel(spec.modelCode ?: "TGT_DM")
    def models = spec.extraModel ? [model, makeModel(spec.modelCode ?: "TGT_DM")] : [model]
    def context = new Expando(getCode: { spec.contextCode ?: "GLOBAL" }, getInternalId: { "CTX" })
    def contexts = spec.extraContext ? [context, new Expando(getCode: { "GLOBAL" }, getInternalId: { "CTX2" })] : [context]
    def mappingContext = spec.mappingContextCode ? new Expando(getCode: { spec.mappingContextCode }) : context
    def makeMapping = {
        new Expando(
            getContext: { mappingContext }, getLogicalSchema: { logical }, getPhysicalSchema: { physical },
        )
    }
    def mappings = spec.extraMapping ? [makeMapping(), makeMapping()] : [makeMapping()]
    [models: models, contexts: contexts, mappings: mappings]
}
def runCase = { String name, String modelCode, String contextCode, String schemaName, Map spec ->
    def forbidden = []
    def state = arrangement(forbidden, spec)
    int finders = 0
    def em = new Expando(getFinder: { Class cls ->
        finders++
        def items = []
        if (cls == oracle.odi.domain.model.OdiModel) items = state.models
        else if (cls == oracle.odi.domain.topology.OdiContext) items = state.contexts
        else if (cls == oracle.odi.domain.topology.OdiContextualSchemaMapping) items = state.mappings
        else throw new IllegalStateException("unexpected finder")
        new FakeFinder(items: items, forbidden: forbidden)
    })
    System.setProperty("odi.execution.model", modelCode)
    System.setProperty("odi.execution.context", contextCode)
    System.setProperty("odi.execution.schema", schemaName)
    System.setProperty("odi.exec_user", "ATTACKER")
    System.setProperty("odi.execution.username", "ATTACKER")
    def buffer = new ByteArrayOutputStream()
    def writer = new StringWriter()
    def previous = System.out
    System.setOut(new PrintStream(buffer, true, "UTF-8"))
    try {
        new GroovyShell(new Binding([
            odiInstance: new Expando(getTransactionalEntityManager: { em }),
            args: new String[0],
            out: new PrintWriter(writer, true),
        ])).evaluate(new File(scriptPath))
    } finally {
        System.setOut(previous)
    }
    def text = (writer.toString() + buffer.toString("UTF-8")).trim()
    def line = text.readLines().find { it.trim().startsWith("{") }
    results << [name: name, line: line, finders: finders, forbidden: forbidden]
}
runCase("proven", "TGT_DM", "GLOBAL", "DM", [username: "ETL_RUNNER"])
runCase("same_internal_id", "TGT_DM", "GLOBAL", "DM", [username: "ETL_RUNNER", sameIdCopy: true])
runCase("duplicate_model", "TGT_DM", "GLOBAL", "DM", [extraModel: true, username: "LEAKED_MODEL"])
runCase("missing_model", "TGT_DM", "GLOBAL", "DM", [modelCode: "OTHER_MODEL", username: "LEAKED_MODEL"])
runCase("duplicate_context", "TGT_DM", "GLOBAL", "DM", [extraContext: true, username: "LEAKED_CONTEXT"])
runCase("missing_context", "TGT_DM", "GLOBAL", "DM", [contextCode: "DEV", username: "LEAKED_CONTEXT"])
runCase("duplicate_mapping", "TGT_DM", "GLOBAL", "DM", [extraMapping: true, username: "LEAKED_MAP"])
runCase("missing_mapping", "TGT_DM", "GLOBAL", "DM", [mappingContextCode: "DEV", username: "LEAKED_MAP"])
runCase("wrong_schema", "TGT_DM", "GLOBAL", "DM", [schemaName: "OTHER_OWNER", username: "LEAKED_SCHEMA"])
runCase("wrong_model_technology", "TGT_DM", "GLOBAL", "DM", [modelTechnology: "PostgreSQL", username: "LEAKED_TECH"])
runCase("wrong_physical_technology", "TGT_DM", "GLOBAL", "DM", [physicalTechnology: "PostgreSQL", username: "LEAKED_TECH"])
runCase("wrong_server_technology", "TGT_DM", "GLOBAL", "DM", [serverTechnology: "PostgreSQL", username: "LEAKED_TECH"])
runCase("disagreeing_physical", "TGT_DM", "GLOBAL", "DM", [disagree: true, username: "LEAKED_PHYS"])
runCase("lowercase_username", "TGT_DM", "GLOBAL", "DM", [username: "etl_runner"])
runCase("null_server", "TGT_DM", "GLOBAL", "DM", [nullServer: true, username: "LEAKED_NULL"])
runCase("null_logical", "TGT_DM", "GLOBAL", "DM", [nullLogical: true, username: "LEAKED_LOGICAL"])
runCase("invalid_context", "TGT_DM", "DEV", "DM", [username: "LEAKED_CONTEXT"])
runCase("invalid_model", "bad model", "GLOBAL", "DM", [username: "LEAKED_MODEL"])
println JsonOutput.toJson(results)
'''


def test_real_probe_against_fake_sdk(tmp_path):
    runtime = _sdk_available()
    if runtime is None:
        pytest.skip(_SKIP)
    assert SCRIPT.is_file()
    java, classpath = runtime
    path = tmp_path / "harness.groovy"
    path.write_text(_HARNESS.replace("__SCRIPT__", json.dumps(str(SCRIPT))), encoding="utf-8")
    env = os.environ.copy()
    env["JAVA_TOOL_OPTIONS"] = ""
    done = subprocess.run(
        [java, "-cp", classpath, "groovy.ui.GroovyMain", str(path)],
        capture_output=True, text=True, timeout=180, check=False, env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    results = {item["name"]: item for item in json.loads(done.stdout.strip().splitlines()[-1])}
    expected = {
        "proven": ("passed", "ETL_RUNNER", 3),
        "same_internal_id": ("passed", "ETL_RUNNER", 3),
        "duplicate_model": ("execution_identity_ambiguous", None, 3),
        "missing_model": ("execution_identity_missing", None, 3),
        "duplicate_context": ("execution_identity_ambiguous", None, 3),
        "missing_context": ("execution_identity_missing", None, 3),
        "duplicate_mapping": ("execution_identity_ambiguous", None, 3),
        "missing_mapping": ("execution_identity_missing", None, 3),
        "wrong_schema": ("execution_identity_unverified", None, 3),
        "wrong_model_technology": ("execution_identity_unverified", None, 3),
        "wrong_physical_technology": ("execution_identity_unverified", None, 3),
        "wrong_server_technology": ("execution_identity_unverified", None, 3),
        "disagreeing_physical": ("execution_identity_unverified", None, 3),
        "lowercase_username": ("execution_identity_unverified", None, 3),
        "null_server": ("execution_identity_unverified", None, 3),
        "null_logical": ("execution_identity_unverified", None, 3),
        "invalid_context": ("execution_identity_invalid", None, 0),
        "invalid_model": ("execution_identity_invalid", None, 0),
    }
    assert set(results) == set(expected)
    for name, (code, username, finders) in expected.items():
        item = results[name]
        assert item["forbidden"] == [], (name, item["forbidden"])
        assert item["finders"] == finders, name
        assert item["line"], name
        for hidden in _HIDDEN:
            if hidden == "etl_runner" and username == "ETL_RUNNER":
                continue
            assert hidden not in item["line"], (name, hidden, item["line"])
        payload = json.loads(item["line"])
        observed = payload.get("status") if code == "passed" else payload.get("code")
        assert observed == code, (name, payload)
        assert payload.get("username") == username
        if code != "passed":
            assert "username" not in payload
        else:
            assert payload["technology"] == "Oracle" and payload["data_server_count"] == 1
        if name == "duplicate_model":
            assert payload["model_count"] == 2
        if name == "invalid_context":
            assert payload["repository_connection_attempted"] is False


def test_unavailable_optional_runtime_skips_and_does_not_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("missing runtime must skip before a process starts"),
    )
    with pytest.raises(pytest.skip.Exception, match=_SKIP):
        test_real_probe_against_fake_sdk(tmp_path)
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    try:
        runtime = _sdk_available()
    except pytest.skip.Exception as exc:
        pytest.fail(f"a reported SDK runtime must not skip: {exc}")
    assert runtime is not None
