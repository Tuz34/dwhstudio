"""Execute complete generic writers on an isolated synthetic SDK, never ODI."""
import json
from pathlib import Path

import pytest

import repository_write_contract as contract
from tests import dim_product_mapping_fake_sdk as fake
from scd2_manager import _TRACKED_ATTRIBUTES

ROOT = Path(__file__).resolve().parents[1] / "odi_automation"
SCENARIOS = {
    "clean": {}, "zero_ap": {"ap_count": 0}, "two_ap": {"ap_count": 2},
    "first_reset": {"options_reset_on_first_validate": True},
    "final_reset": {"options_reset_on_final_validate": True},
    "final_new_ap": {"materialize_ap_on_final_validate": True},
    "ambiguous_ikm": {"ikms": [{"name": fake.IKM_NAME, "project": None}, {"name": fake.IKM_NAME, "project": "PRJ"}]},
    "ambiguous_lkm": {"lkms": [{"name": fake.LKM_NAME, "project": None}, {"name": fake.LKM_NAME, "project": "PRJ"}]},
    "final_error": {"final_validate": {"levels": ["ERROR"]}},
    "rollback_failed": {"final_validate": {"levels": ["ERROR"]}, "rollback_raises": True},
    "commit_failed": {"commit_raises": True},
    "commit_applied": {"commit_applied_then_raises": True},
    "commit_error": {"commit_applied_then_error": True},
    "report_failed": {"print_failures": 1},
    "begin_failed": {"begin_raises": True},
    "standalone_close": {"standalone": True, "close_raises": True},
    "bad_defaults": {"option_defaults": {"TRUNCATE": True, "DELETE_ALL": True, "CREATE_TARG_TABLE": True, "COMMIT": False}},
}


def _params():
    return {"odi.scd2.source_model": "DWH", "odi.scd2.source_table": "ODS_SH_PRODUCTS",
            "odi.scd2.target_model": "DWH", "odi.scd2.target_owner": "DWH", "odi.scd2.target_table": "DIM_PRODUCT",
            "odi.scd2.mapping": "MAP_TEST", "odi.scd2.sequence": "SEQ_TEST", "odi.scd2.sk": "PRODUCT_SK",
            "odi.scd2.natural_key": "PROD_ID", "odi.scd2.tracked": ",".join(_TRACKED_ATTRIBUTES),
            "odi.scd2.overwrite": "none"}


def _run(java, root, name, classes, params, cases):
    driver = fake._DRIVER.replace('Map results = [:]',
        'new groovy.json.JsonSlurper().parseText(' + json.dumps(json.dumps(params)) + ').each { k,v -> System.setProperty(k,v) }\nMap results = [:]')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fake, "_CLASSES", classes)
        patch.setattr(fake, "_DRIVER", driver)
        patch.setattr(fake, "SCRIPT", ROOT / name)
        return fake.run_scenarios(java, root, cases)


@pytest.fixture(scope="module")
def mapped(tmp_path_factory):
    java = fake.java_binary()
    if java is None or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Groovy unavailable; nothing downloaded")
    classes = dict(fake._CLASSES)
    finder = "oracle.odi.domain.model.finder.IOdiDataStoreFinder"
    classes[finder] = classes[finder].replace(
        "INSERT_DATE: null, UPDATE_DATE: null, SOURCE_SYSTEM: null, ETL_BATCH_ID: null",
        ", ".join(name + ": OdiColumn.ScdType.OVERWRITE_ON_CHANGE" for name in ("INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID")))
    return _run(java, tmp_path_factory.mktemp("generic_mapping"), "scd2_mapping_create.groovy", classes, _params(), SCENARIOS)


@pytest.mark.parametrize("case", ["clean", "zero_ap", "two_ap", "first_reset", "bad_defaults", "standalone_close"])
def test_generic_mapping_commits_only_locked_final_design(mapped, case):
    run = mapped[case]
    report = json.loads(run["output"])
    assert report["status"] == "passed" and run["committed"] is True
    assert report["result"]["access_point_count"] == report["result"]["lkm_assigned_count"]
    assert run["state"]["options"] == fake_defaults()


def fake_defaults():
    return dict(FLOW_CONTROL=False, STATIC_CONTROL=False, RECYCLE_ERRORS=False, TRUNCATE=False,
                DELETE_ALL=False, CREATE_TARG_TABLE=False, COMMIT=True)


@pytest.mark.parametrize("case", ["final_reset", "final_new_ap", "ambiguous_ikm", "ambiguous_lkm", "final_error"])
def test_generic_mapping_never_commits_invalid_or_ambiguous_final_design(mapped, case):
    run = mapped[case]
    assert run["committed"] is False and "commit" not in run["trace"]
    report = json.loads(run["output"])
    assert report["repository_outcome"] == "rolled_back" and report["side_effects"] is False


@pytest.mark.parametrize("case,outcome,effects", [
    ("rollback_failed", "rollback_failed", None), ("commit_failed", "commit_outcome_unknown", None),
    ("commit_applied", "commit_outcome_unknown", None), ("commit_error", "commit_outcome_unknown", None),
    ("report_failed", "committed", True), ("begin_failed", "no_transaction", False),
])
def test_generic_mapping_exact_transaction_outcomes(mapped, case, outcome, effects):
    run = mapped[case]
    report = json.loads(run["output"])
    assert report["repository_outcome"] == outcome and report["side_effects"] is effects
    assert contract.failure(report, "odi.scd2_mapping_create", "generic_absent_only_scd2_mapping_creation") is not None
    assert "RAISE_TOKEN" not in run["output"] and run["script_raised"] is None


def _registrar_classes():
    classes = dict(fake._CLASSES)
    classes["fake.World"] = classes["fake.World"].replace("static Object mapping = null", "static Object mapping = null\n    static Object target = null")
    classes["oracle.odi.domain.model.OdiModel"] = '''
class OdiModel { def getTechnology() { [getName: { -> 'ORACLE' }] } }
'''
    classes["oracle.odi.domain.model.finder.IOdiModelFinder"] = '''
class IOdiModelFinder { def findByCode(String code) { new oracle.odi.domain.model.OdiModel() } }
'''
    classes["oracle.odi.domain.model.OdiDataStore"] = '''
class OdiDataStore {
    String name; List columns=[]
    OdiDataStore(OdiModel model, String n) { name=n; fake.World.target=this }
    void setDefaultAlias(String n) {}
    void setResourceName(String n) {}
    List getColumns() { columns }
}
'''
    classes["oracle.odi.domain.model.OdiColumn"] = '''
class OdiColumn {
    enum ScdType { SURROGATE_KEY, NATURAL_KEY, START_TIMESTAMP, END_TIMESTAMP, CURRENT_RECORD_FLAG, ADD_ROW_ON_CHANGE, OVERWRITE_ON_CHANGE }
    String name; String dataTypeCode; String dataFormat; Integer length; Integer scale; int position; boolean mandatory; ScdType scdType
    OdiColumn(OdiDataStore store,String n) { name=n; store.columns << this }
    def getDataType() { [getCode: { -> dataTypeCode }] }
}
'''
    classes["oracle.odi.domain.model.OdiKey"] = '''
class OdiKey {
    enum KeyType { PRIMARY_KEY, ALTERNATE_KEY }
    KeyType keyType; boolean active; boolean inDatabase; List columns=[]
    OdiKey(OdiDataStore store,String n) {}
    void addColumn(OdiColumn c) { columns << c }
    boolean isPK() { keyType==KeyType.PRIMARY_KEY }
    boolean isAK() { keyType==KeyType.ALTERNATE_KEY }
}
'''
    classes["oracle.odi.domain.model.finder.IOdiDataStoreFinder"] = '''
class IOdiDataStoreFinder {
    def findByName(String name,String model) {
        if (fake.World.spec.final_validate) throw new IllegalStateException('ColumnMetadataMismatch')
        null
    }
}
'''
    classes["oracle.odi.domain.mapping.FakeEntityManager"] = '''
class FakeEntityManager {
    def getFinder(Class c) {
        if(c==oracle.odi.domain.model.OdiModel) return new oracle.odi.domain.model.finder.IOdiModelFinder()
        if(c==oracle.odi.domain.model.OdiDataStore) return new oracle.odi.domain.model.finder.IOdiDataStoreFinder()
        throw new IllegalStateException('UnexpectedFakeFinder')
    }
    void persist(Object o) { fake.World.log('persist') }
}
'''
    return classes


@pytest.fixture(scope="module", params=["scd2_datastore_register.groovy", "dim_product_scd2_datastore_register.groovy"])
def registered(tmp_path_factory, request):
    java = fake.java_binary()
    if java is None or not fake.GROOVY_JAR.is_file():
        pytest.skip("Local Groovy unavailable; nothing downloaded")
    params = _params()
    params["odi.scd2.tracked"] = "PROD_NAME"
    names = ["PRODUCT_SK", "PROD_ID", "PROD_NAME", "EFFECTIVE_FROM", "EFFECTIVE_TO", "CURRENT_FLAG",
             "INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"]
    params["odi.scd2.column_types"] = ",".join(name + ":NUMBER" for name in names)
    params["odi.scd2.column_nullable"] = ",".join(name + ":N" for name in names)
    cases = {name: SCENARIOS[name] for name in ("clean", "rollback_failed", "commit_failed", "commit_applied", "commit_error", "report_failed", "begin_failed", "standalone_close")}
    return request.param, _run(java, tmp_path_factory.mktemp("generic_registrar"), request.param, _registrar_classes(), params, cases)


@pytest.mark.parametrize("case", ["clean", "standalone_close"])
def test_generic_registrar_runs_complete_writer_before_commit(registered, case):
    name, runs = registered
    run = runs[case]
    expected = 10 if name == "scd2_datastore_register.groovy" else 26
    assert run["committed"] is True and json.loads(run["output"])["result"]["target_column_count"] == expected


@pytest.mark.parametrize("case,outcome,effects", [
    ("rollback_failed", "rollback_failed", None), ("commit_failed", "commit_outcome_unknown", None),
    ("commit_applied", "commit_outcome_unknown", None), ("commit_error", "commit_outcome_unknown", None),
    ("report_failed", "committed", True), ("begin_failed", "no_transaction", False),
])
def test_generic_registrar_exact_transaction_outcomes(registered, case, outcome, effects):
    _name, runs = registered
    run = runs[case]
    report = json.loads(run["output"])
    assert report["repository_outcome"] == outcome and report["side_effects"] is effects
    assert contract.failure(report, report["tool"], report["mode"]) is not None
    assert "RAISE_TOKEN" not in run["output"] and run["script_raised"] is None
