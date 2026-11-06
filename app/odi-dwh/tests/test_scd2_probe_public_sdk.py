"""Synthetic SDK execution only; no ODI repository or database is opened."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import dim_product_mapping_fake_sdk as fake

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "odi_automation" / "dim_product_scd2_mapping_probe.groovy"
GENERIC = ROOT / "odi_automation" / "scd2_mapping_create.groovy"


@pytest.fixture(scope="module")
def java():
    binary = fake.java_binary()
    if not binary or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Groovy unavailable; nothing is downloaded")
    return binary


@pytest.fixture(scope="module")
def probe_results(java, tmp_path_factory):
    root = tmp_path_factory.mktemp("probe_sdk")
    sources, classes = root / "sources", root / "classes"
    fake.write_fake_sdk(sources)
    replacements = {
        "oracle.odi.domain.mapping.FakeEntityManager": (
            'World.spec.mapping_exists ? new Object() : null', 'World.mapping'
        ),
        "oracle.odi.domain.mapping.Mapping": (
            'int validateCalls = 0',
            'List components = []; List getAllComponents() { components }; int validateCalls = 0'
        ),
        "oracle.odi.domain.mapping.component.DatastoreComponent": (
            'String name; String integrationType; List attributes',
            'OdiDataStore bound; OdiDataStore getBoundDataStore() { bound }; '
            'String name; String integrationType; List attributes'
        ),
    }
    for fqcn, (needle, replacement) in replacements.items():
        path = sources.joinpath(*fqcn.split(".")).with_suffix(".groovy")
        source = path.read_text(encoding="utf-8")
        assert needle in source
        path.write_text(source.replace(needle, replacement), encoding="utf-8")
    component = sources / "oracle/odi/domain/mapping/component/DatastoreComponent.groovy"
    source = component.read_text(encoding="utf-8")
    component.write_text(source.replace('name = ds.getName()', 'bound = ds; m.components << this; name = ds.getName()'), encoding="utf-8")
    fake._compile_fake_sdk(java, sources, classes)
    driver = root / "probe_driver.groovy"
    driver.write_text(r'''
import fake.World
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import oracle.odi.core.OdiInstance
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.component.DatastoreComponent
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiLKM

def input = new JsonSlurper().parse(System.in)
System.setProperty("odi.project", "PRJ")
System.setProperty("odi.folder", "FLD")
def results = [:]
input.scenarios.each { id, spec ->
    World.reset(spec)
    def mapping = new Mapping("FIXED", null)
    def finder = new IOdiDataStoreFinder()
    new DatastoreComponent(mapping, finder.findByName("ODS_SH_PRODUCTS", "SRC"))
    def target = new DatastoreComponent(mapping, finder.findByName("DIM_PRODUCT", "TGT"))
    target.setIntegrationType("SCD")
    target.attributes.each { it.setExpressionText("SYNTHETIC") }
    if (spec.missing_attribute) target.attributes.remove(0)
    if (spec.missing_expression) target.attributes[0].setExpressionText(null)
    def node = mapping.design.nodes.find { it.isTargetNode() }
    node.setTargetComponentKM(new OdiIKM(name: spec.wrong_ikm ? "OTHER" : "IKM Oracle Slowly Changing Dimension"))
    [FLOW_CONTROL:false, STATIC_CONTROL:false, RECYCLE_ERRORS:false, TRUNCATE:false,
     DELETE_ALL:false, CREATE_TARG_TABLE:false, COMMIT:true].each { n,v -> node.getIKMOptionValue(n)?.setValue(v) }
    if (spec.bad_option) {
        def option = node.getIKMOptionValue(spec.bad_option)
        option.setValue(spec.bad_option != 'COMMIT')
    }
    if (spec.missing_option) node.options.remove(spec.missing_option)
    mapping.design.getAllAPNodes().eachWithIndex { ap, index ->
        boolean wrong = spec.wrong_lkm || (spec.mixed_lkm && index == 1)
        if (!spec.missing_lkm) ap.setLKM(new OdiLKM(name: wrong ? "LKM OTHER" : "LKM SQL to Oracle (Built-In)"))
        if (spec.xkm_only) { ap.km = null; ap.setXKM(new OdiLKM(name: "LKM SQL to Oracle (Built-In)")) }
    }
    World.trace.clear()
    def output = new StringWriter()
    new GroovyShell(this.class.classLoader, new Binding(odiInstance: new OdiInstance(), out: output)).evaluate(new File(input.script))
    results[id] = [output: output.toString(), trace: World.trace]
}
print JsonOutput.toJson(results)
''', encoding="utf-8")
    scenarios = {
        "zero": {"ap_count": 0}, "one": {}, "two": {"ap_count": 2},
        "wrong_lkm": {"wrong_lkm": True}, "missing_lkm": {"missing_lkm": True},
        "xkm_only": {"xkm_only": True}, "wrong_ikm": {"wrong_ikm": True},
        "missing_attribute": {"missing_attribute": True}, "missing_expression": {"missing_expression": True},
        "mixed_lkm": {"ap_count": 2, "mixed_lkm": True},
        **{name: {"bad_option": name} for name in ("FLOW_CONTROL", "STATIC_CONTROL", "RECYCLE_ERRORS", "TRUNCATE", "DELETE_ALL", "CREATE_TARG_TABLE", "COMMIT")},
        "missing_commit": {"missing_option": "COMMIT"},
    }
    return fake._run(java, os.pathsep.join([str(fake.GROOVY_JAR), str(classes)]), driver,
                     {"script": str(PROBE), "scenarios": scenarios}, root)


@pytest.mark.parametrize("scenario,count", [("zero", 0), ("one", 1), ("two", 2)])
def test_probe_uses_real_lkm_not_extract_reference(probe_results, scenario, count):
    result = probe_results[scenario]
    payload = json.loads(result["output"])
    assert payload["status"] == "passed"
    assert payload["mapping"]["access_point_count"] == payload["mapping"]["lkm_assigned_count"] == count
    assert result["trace"] == []


@pytest.mark.parametrize("scenario", ["wrong_lkm", "missing_lkm", "mixed_lkm", "xkm_only", "wrong_ikm", "missing_attribute", "missing_expression",
    "FLOW_CONTROL", "STATIC_CONTROL", "RECYCLE_ERRORS", "TRUNCATE", "DELETE_ALL", "CREATE_TARG_TABLE", "COMMIT", "missing_commit"])
def test_probe_never_certifies_wrong_or_incomplete_physical_contract(probe_results, scenario):
    result = probe_results[scenario]
    payload = json.loads(result["output"])
    assert payload["status"] == "error"
    assert "mapping" not in payload and "SYNTHETIC" not in result["output"]
    assert result["trace"] == []


def test_generic_option_check_executes_public_accessor(java, tmp_path):
    source = GENERIC.read_text(encoding="utf-8")
    start = source.index('    final Map<String, Boolean> lockedOptions =')
    end = source.index('    // Access-point nodes', start)
    block = source[start:end]
    final_checks = source.index('    lockedOptions.each', source.index('// Validation can materialize'))
    block += '\n' + source[final_checks:source.index('    List<MapPhysicalNode> finalAccessPoints', final_checks)].replace('finalTarget.', 'targetNode.')
    driver = tmp_path / "option_driver.groovy"
    driver.write_text('''
import groovy.json.JsonOutput
class Option {
    private Object stored = true
    boolean sticky
    void setValue(Object next) { if (!sticky) stored = next }
    Object getOptionValue() { stored }
}
class Node {
    Map values
    def getIKMOptionValue(String name) { values[name] }
}
def results = [:]
["good", "missing", "sticky"].each { mode ->
    def node = new Node(values: ["FLOW_CONTROL", "STATIC_CONTROL", "RECYCLE_ERRORS", "TRUNCATE",
        "DELETE_ALL", "CREATE_TARG_TABLE", "COMMIT"].collectEntries { n -> [(n): new Option(sticky: mode == "sticky" && n == "FLOW_CONTROL")] })
    if (mode == "missing") node.values.remove("FLOW_CONTROL")
    try {
        new GroovyShell(new Binding(targetNode: node)).evaluate(''' + json.dumps(block) + ''')
        results[mode] = "passed"
    } catch (Exception failure) { results[mode] = failure.class.simpleName }
}
print JsonOutput.toJson(results)
''', encoding="utf-8")
    result = fake._run(java, str(fake.GROOVY_JAR), driver, {}, tmp_path)
    assert result == {"good": "passed", "missing": "IllegalStateException", "sticky": "IllegalStateException"}
