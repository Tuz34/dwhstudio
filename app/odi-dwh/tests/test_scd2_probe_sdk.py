"""Execute probe fragments with SDK objects and fake finders, never an ODI instance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import scd2_builder
import scd2_odi
import scd2_service
from tests.scd2_support import REQUEST, SOURCE_COLUMNS, FakeOrchestrator, make_services, probe_payload

PROBE = Path(__file__).resolve().parents[1] / "odi_automation" / "scd2_probe.groovy"


@pytest.fixture(scope="module")
def sdk_run():
    home = Path(os.getenv("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
    java = shutil.which("java")
    jars = [
        home / "odi" / "sdk" / "lib" / "odi-core.jar",
        home / "oracle_common" / "modules" / "thirdparty" / "groovy-all-2.4.17.jar",
        home / "oracle_common" / "modules" / "oracle.toplink" / "eclipselink.jar",
    ]
    if java is None or not all(path.is_file() for path in jars):
        pytest.skip("Local Java/ODI SDK/Groovy not available; no runtime is downloaded")
    classpath = os.pathsep.join([str(home / "odi" / "sdk" / "lib" / "*"), *map(str, jars[1:])])

    def run(code, payload):
        completed = subprocess.run(
            [java, "-cp", classpath, "groovy.ui.GroovyMain", "-e", code, str(PROBE)],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60, check=False,
        )
        assert completed.returncode == 0, completed.stderr[-1500:]
        return json.loads(completed.stdout)

    return run


SCENARIO_HARNESS = r'''
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.Tag
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder

def cases = new JsonSlurper().parse(System.in)
String source = new File(args[0]).text
String fragment = source.substring(source.indexOf('List<String> scenarioVersions = []'),
    source.indexOf('// Contract-relevant metadata'))
String imports = 'import oracle.odi.domain.runtime.scenario.OdiScenario\n' +
    'import oracle.odi.domain.runtime.scenario.Tag\n'
def results = cases.collect { spec ->
    def scenarios = spec.scenarios.collect { row ->
        def scenario = new OdiScenario(new Tag('MAP_TEST_SCN', row.version))
        scenario.setSourceMappingId(row.owner)
        scenario
    }
    def exactLookups = []
    def finder = [
        findLatestByName: { String name -> scenarios.findAll { it.name == name }.max { it.version } },
        findBySourceMapping: { Number id -> scenarios.findAll { it.sourceMappingId == id } },
        findByTag: { Tag tag ->
            exactLookups << tag.version
            scenarios.find { it.tag == tag }
        },
    ] as IOdiScenarioFinder
    def mapping = spec.mapping == null ? null : new Expando(getInternalId: { -> spec.mapping })
    def binding = new Binding([mapping: mapping, scenarioFinder: finder,
        scenarioName: 'MAP_TEST_SCN', scenarioVersion: spec.requested])
    def result = new GroovyShell(this.class.classLoader, binding).evaluate(imports + fragment +
        '\nreturn [scenario_name_collision:scenarioNameCollision, scenario_versions:scenarioVersions]')
    result.exact_lookups = exactLookups
    result
}
println JsonOutput.toJson(results)
'''


@pytest.fixture(scope="module")
def scenario_results(sdk_run):
    cases = [
        # Requested 001 belongs to another mapping, although latest 002 is ours.
        {"mapping": 22, "requested": "001", "scenarios": [{"version": "001", "owner": 11}, {"version": "002", "owner": 22}]},
        {"mapping": 22, "requested": "001", "scenarios": [{"version": "001", "owner": None}, {"version": "002", "owner": 22}]},
        {"mapping": 22, "requested": "001", "scenarios": [{"version": "001", "owner": 22}, {"version": "002", "owner": 22}]},
        {"mapping": 22, "requested": "001", "scenarios": [{"version": "002", "owner": 22}]},
        {"mapping": 22, "requested": "001", "scenarios": [{"version": "002", "owner": 11}]},
        {"mapping": None, "requested": "001", "scenarios": [{"version": "001", "owner": 22}]},
        {"mapping": None, "requested": "001", "scenarios": []},
        {"mapping": 22, "requested": "007", "scenarios": [{"version": "007", "owner": 11}, {"version": "008", "owner": 22}]},
        # Run probes without a requested generation version retain the latest-owner check.
        {"mapping": 22, "requested": None, "scenarios": [{"version": "001", "owner": 11}, {"version": "002", "owner": 22}]},
    ]
    return sdk_run(SCENARIO_HARNESS, cases)


@pytest.mark.parametrize("index, collision", enumerate([True, True, False, False, True, True, False, True, False]))
def test_requested_scenario_ownership_with_sdk_objects(scenario_results, index, collision):
    result = scenario_results[index]
    assert result["scenario_name_collision"] is collision
    assert result["exact_lookups"] == ([] if index == 8 else ["007" if index == 7 else "001"])


@pytest.mark.parametrize("index", [0, 1, 4, 5, 7])
def test_sdk_collision_result_blocks_service_before_any_ddl_or_generation(scenario_results, index):
    plan = scd2_builder.build_scd2_plan(REQUEST, SOURCE_COLUMNS)
    result = scenario_results[index]
    probe = probe_payload(
        plan, target_datastore_exists=True, mapping_exists=True, scenario_exists=True,
        scenario_name_collision=result["scenario_name_collision"], scenario_versions=result["scenario_versions"],
    )
    orchestrator = FakeOrchestrator(plan, responses={"scd2_probe.groovy": probe})
    ddl_log = []
    response, status = scd2_service.generate(REQUEST, make_services(orchestrator, ddl_log=ddl_log), approved=True)
    assert (status, response["status"]) == (409, "blocked_scenario_name_collision")
    assert ddl_log == [] and orchestrator.order == ["scd2_probe.groovy"]


def test_sdk_numeric_fields_are_independent_and_defaults_are_normalized(sdk_run):
    rows = sdk_run(r'''
import groovy.json.JsonOutput
import oracle.odi.domain.model.*
import oracle.odi.domain.topology.*

class OfflineOracle extends OdiTechnology {
    OfflineOracle() { super('Oracle', 'ORACLE') }
}
def technology = new OfflineOracle()
def type = new OdiDataType(technology, 'NUMBER')
type.setCode('NUMBER')
type.setIconLetter('N')
type.setDdlPattern('NUMBER(%L,%P)')
def model = new OdiModel(new OdiLogicalSchema(technology, 'OFFLINE'), 'Offline', 'OFFLINE')
def store = new OdiDataStore(model, 'OFFLINE')
def column = new OdiColumn(store, 'N')
column.setDataType(type)
column.setDataFormat('NUMBER(10)')
column.setLength(10)
def rows = [null, 0, 2].collect { scale ->
    column.setScale(scale)
    [data_format:column.getDataFormat(), type_code:column.getDataType().getCode(),
     length:column.getLength(), scale:column.getScale()]
}
column.setDataFormat('NUMBER')
column.setLength(0)
column.setScale(-127)
rows << [data_format:column.getDataFormat(), type_code:column.getDataType().getCode(),
         length:column.getLength(), scale:column.getScale()]
println JsonOutput.toJson(rows)
''', [])
    assert [(row["length"], row["scale"]) for row in rows] == [(10, None), (10, 0), (10, 2), (None, None)]
    for row, matches in zip(rows, [True, True, False, True]):
        expected = {"name": "N", "role": "NONE", "mandatory": False, "data_format": row["data_format"]}
        actual = {**expected, **row, "length_semantics": None}
        assert scd2_odi._column_matches(actual, expected) is matches
