"""Run the real generator on an isolated fake SDK; never connect to ODI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import dim_product_mapping_fake_sdk as fake

SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation/generate_scenario_metadata.groovy"

SCENARIOS = {
    "new": {}, "warning": {"generation_warning": True},
    "error_issue": {"generation_error": True}, "fatal_issue": {"generation_fatal": True},
    "returned_null": {"returned_null": True}, "wrong_owner": {"wrong_owner": True},
    "null_owner": {"null_owner": True}, "wrong_name": {"wrong_name": True},
    "wrong_version": {"wrong_version": True}, "readback_missing": {"readback_missing": True},
    "readback_wrong_owner": {"readback_wrong_owner": True}, "lookup_failure": {"lookup_failure": True},
    "generation_raises": {"generation_raises": True}, "commit_raises": {"commit_raises": True},
    "commit_applied_then_raises": {"commit_applied_then_raises": True},
    "rollback_failure": {"wrong_owner": True, "rollback_raises": True},
    "print_failure": {"print_failures": 1},
    "existing_same": {"existing": [{"name": "MAP_TEST_SCN", "version": "001", "owner": 42}]},
    "existing_wrong": {"existing": [{"name": "MAP_TEST_SCN", "version": "001", "owner": 99}]},
    "existing_unowned": {"existing": [{"name": "MAP_TEST_SCN", "version": "001", "owner": None}]},
    "other_name": {"existing": [{"name": "OTHER_SCN", "version": "001", "owner": 42}]},
    "other_version": {"existing": [{"name": "MAP_TEST_SCN", "version": "002", "owner": 99}]},
    "duplicate_exact": {"existing": [{"name": "MAP_TEST_SCN", "version": "001", "owner": 42}] * 2},
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    java = fake.java_binary()
    if not java or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Groovy unavailable; nothing is downloaded")
    root = tmp_path_factory.mktemp("public_generator")
    classes = dict(fake._CLASSES)
    classes["oracle.odi.core.persistence.transaction.ITransactionDefinition"] = "interface ITransactionDefinition {}"
    classes["fake.World"] = classes["fake.World"].replace(
        "static Map spec = [:]", "static Map spec = [:]; static Object generated;"
    ).replace("spec = s;", "spec = s; generated = null;")
    classes["oracle.odi.domain.project.IOdiScenarioSource"] = "interface IOdiScenarioSource {}"
    classes["oracle.odi.domain.mapping.Mapping"] = classes["oracle.odi.domain.mapping.Mapping"].replace(
        "class Mapping {", "class Mapping implements oracle.odi.domain.project.IOdiScenarioSource {"
    ).replace("int validateCalls = 0", "int validateCalls = 0; int getComponentCount() { 2 }; Long getInternalId() { 42L }; List getPhysicalDesigns() { [design] }")
    classes["oracle.odi.domain.mapping.FakeEntityManager"] = classes["oracle.odi.domain.mapping.FakeEntityManager"].replace(
        "World.spec.mapping_exists ? new Object() : null", "World.mapping"
    ).replace("void persist(Object o)", 'void flush() { World.log("flush") }; void persist(Object o)')
    classes["oracle.odi.domain.mapping.physical.MapPhysicalNode"] = classes["oracle.odi.domain.mapping.physical.MapPhysicalNode"].replace(
        "FakeOption getIKMOptionValue", "Object getIKM() { km }; FakeOption getIKMOptionValue"
    )
    classes["oracle.odi.domain.runtime.scenario.OdiScenario"] = r'''
class OdiScenario {
    String name; String version; Long sourceMappingId
    String getName() { name }; String getVersion() { version }; Long getSourceMappingId() { sourceMappingId }
}
'''
    classes["oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder"] = r'''
import fake.World
import oracle.odi.domain.runtime.scenario.OdiScenario
class IOdiScenarioFinder {
    List rows() {
        if (World.spec.lookup_failure) throw new IllegalStateException("LEAKED_LOOKUP_DETAIL")
        def list = (World.spec.existing ?: []).collect { r -> new OdiScenario(name: r.name, version: r.version, sourceMappingId: r.owner) }
        if (World.generated != null && !World.spec.readback_missing) {
            def row = World.generated
            list << new OdiScenario(name: row.name, version: row.version,
                sourceMappingId: World.spec.readback_wrong_owner ? 99L : row.sourceMappingId)
        }
        list
    }
    // Intentionally return extra names: callers must validate identities independently.
    List findByName(String name) { rows() }
    List findAll() { rows() }
    OdiScenario findLatestByName(String name) { rows().findAll { it.name == name }.max { it.version } }
}
'''
    classes["oracle.odi.generation.IOdiScenarioGenerator"] = r'''
import oracle.odi.domain.project.IOdiScenarioSource
import oracle.odi.domain.runtime.scenario.OdiScenario
interface IOdiScenarioGenerator { OdiScenario generateScenario(IOdiScenarioSource source, String name, String version, List issues) }
'''
    classes["oracle.odi.generation.support.OdiScenarioGeneratorImpl"] = r'''
import fake.World
import oracle.odi.core.OdiInstance
import oracle.odi.domain.project.IOdiScenarioSource
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.root.RootIssue
import oracle.odi.generation.IOdiScenarioGenerator
class OdiScenarioGeneratorImpl implements IOdiScenarioGenerator {
    OdiScenarioGeneratorImpl(OdiInstance odi) {}
    OdiScenario generateScenario(IOdiScenarioSource source, String name, String version, List issues) {
        World.log("public_generate")
        if (World.spec.generation_raises) throw new IllegalStateException("LEAKED_GENERATION_DETAIL")
        if (World.spec.generation_warning) issues << new RootIssue(RootIssue.Level.WARNING, "LEAKED_ISSUE")
        if (World.spec.generation_error) issues << new RootIssue(RootIssue.Level.ERROR, "LEAKED_ISSUE")
        if (World.spec.generation_fatal) issues << new RootIssue(RootIssue.Level.FATAL, "LEAKED_ISSUE")
        if (World.spec.returned_null) return null
        def row = new OdiScenario(name: World.spec.wrong_name ? "OTHER_SCN" : name,
            version: World.spec.wrong_version ? "999" : version,
            sourceMappingId: World.spec.null_owner ? null : (World.spec.wrong_owner ? 99L : source.getInternalId()))
        World.generated = row
        row
    }
}
'''
    driver = r'''
import fake.World
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import oracle.odi.core.OdiInstance
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.project.OdiIKM
def input = new JsonSlurper().parse(System.in)
["odi.project":"PRJ", "odi.folder":"FLD", "odi.mapping":"MAP_TEST", "odi.scenario.name":"MAP_TEST_SCN",
 "odi.scenario.version":"001"].each { k, v -> System.setProperty(k, v) }
def results = [:]
input.scenarios.each { id, spec ->
    World.reset(spec)
    def mapping = new Mapping("MAP_TEST", null)
    mapping.design.nodes.find { it.isTargetNode() }.setTargetComponentKM(new OdiIKM(name: "IKM STOCK"))
    World.trace.clear()
    def captured = new StringWriter()
    def vars = [odiInstance: new OdiInstance(), out: new World.FailingOut(writer: captured, failures: spec.print_failures ?: 0)]
    def raised = null
    try { new GroovyShell(this.class.classLoader, new Binding(vars)).evaluate(new File(input.script)) }
    catch (Throwable e) {
        if (e instanceof org.codehaus.groovy.control.MultipleCompilationErrorsException) throw e
        raised = e.class.simpleName
    }
    results[id] = [output: captured.toString(), committed: World.committed, trace: World.trace, raised: raised]
}
print JsonOutput.toJson(results)
'''
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fake, "_CLASSES", classes)
        patch.setattr(fake, "_STUBS", {**fake._STUBS,
            "oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition":
                "DefaultTransactionDefinition implements oracle.odi.core.persistence.transaction.ITransactionDefinition {}"})
        patch.setattr(fake, "_DRIVER", driver)
        patch.setattr(fake, "SCRIPT", SCRIPT)
        return fake.run_scenarios(java, root, SCENARIOS)


@pytest.mark.parametrize("case", ["new", "warning", "other_name", "other_version"])
def test_supported_generator_checks_ownership_before_committing(generated, case):
    run = generated[case]
    report = json.loads(run["output"])
    assert report["status"] == "passed" and run["committed"] is True
    assert report["scenario"]["version"] == "001"
    assert run["trace"].count("public_generate") == run["trace"].count("commit") == 1
    assert "LEAKED" not in run["output"] and "source_mapping_id" not in run["output"]


@pytest.mark.parametrize("case", ["error_issue", "fatal_issue", "returned_null", "wrong_owner", "null_owner",
                                  "wrong_name", "wrong_version", "readback_missing", "readback_wrong_owner",
                                  "lookup_failure", "generation_raises", "duplicate_exact"])
def test_invalid_or_unknown_generation_never_commits(generated, case):
    run = generated[case]
    report = json.loads(run["output"])
    assert report["status"] in {"error", "attention_required"}
    assert run["committed"] is False and "commit" not in run["trace"]
    assert "LEAKED" not in run["output"] and "source_mapping_id" not in run["output"]


@pytest.mark.parametrize("case,status", [("existing_same", "already_ready"), ("existing_wrong", "attention_required"),
                                        ("existing_unowned", "attention_required")])
def test_existing_exact_versions_never_generate_or_commit(generated, case, status):
    run = generated[case]
    assert json.loads(run["output"])["status"] == status
    assert "public_generate" not in run["trace"] and "commit" not in run["trace"]


@pytest.mark.parametrize("case", ["commit_raises", "commit_applied_then_raises", "rollback_failure"])
def test_acknowledgement_or_rollback_loss_is_not_no_effect(generated, case):
    report = json.loads(generated[case]["output"])
    assert report["status"] == "outcome_unknown"
    assert report["side_effects"] is None and report["manual_recovery_review_required"] is True
    assert "LEAKED" not in generated[case]["output"]


def test_lost_success_report_still_acknowledges_committed_state(generated):
    run = generated["print_failure"]
    report = json.loads(run["output"])
    assert run["committed"] is True
    assert report["status"] == "committed_report_incomplete" and report["side_effects"] is True
    assert report["manual_recovery_review_required"] is True


def test_generator_imports_and_public_api_match_installed_sdk(tmp_path, monkeypatch):
    java = fake.java_binary()
    if not java or not fake.GROOVY_JAR.is_file() or not (fake.SDK_LIB / "odi-core.jar").is_file() or not fake.ECLIPSELINK_JAR.is_file():
        pytest.skip("Local ODI SDK unavailable; nothing is downloaded")
    monkeypatch.setattr(fake, "SCRIPT", SCRIPT)
    required = [
        {"type": "oracle.odi.generation.IOdiScenarioGenerator", "method": "generateScenario", "params": [
            "oracle.odi.domain.project.IOdiScenarioSource", "java.lang.String", "java.lang.String", "java.util.List"]},
        {"type": "oracle.odi.domain.runtime.scenario.OdiScenario", "method": "getSourceMappingId", "params": []},
        {"type": "oracle.odi.domain.mapping.Mapping", "method": "getInternalId", "params": []},
    ]
    assert fake.check_real_public_api(java, tmp_path, required) == {"missing": [], "script_imports_resolved": True}
