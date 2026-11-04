"""WP-E2E-A Groovy contracts: source assertions plus a syntax-only parse.

The parse stops at Groovy's CONVERSION phase, so it never resolves the Oracle ODI
SDK and never contacts a repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


GROOVY_ROOT = Path(__file__).resolve().parents[1] / "odi_automation"
SCRIPTS = (
    "mapping_preflight.groovy",
    "generate_scenario_metadata.groovy",
    "register_datastore.groovy",
    "create_mapping.groovy",
    "update_mapping.groovy",
)


def _text(name: str) -> str:
    return (GROOVY_ROOT / name).read_text(encoding="utf-8")


def _syntax_command(script: Path) -> list[str] | None:
    parse_script = (
        "import org.codehaus.groovy.control.CompilationUnit\n"
        "import org.codehaus.groovy.control.CompilerConfiguration\n"
        "import org.codehaus.groovy.control.Phases\n"
        "def unit = new CompilationUnit(new CompilerConfiguration())\n"
        "unit.addSource(new File(args[0]))\n"
        "unit.compile(Phases.CONVERSION)\n"
        "println 'GROOVY_SYNTAX_OK'\n"
    )
    groovy = shutil.which("groovy") or shutil.which("groovy.bat")
    if groovy:
        return [groovy, "-e", parse_script, str(script)]
    java_home = os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_211")
    java = shutil.which("java.exe" if os.name == "nt" else "java") or str(
        Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
    )
    odi_home = os.environ.get("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home")
    jar_name = os.environ.get("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
    jar = Path(odi_home) / "oracle_common" / "modules" / "thirdparty" / jar_name
    if Path(java).is_file() and jar.is_file():
        return [java, "-cp", str(jar), "groovy.ui.GroovyMain", "-e", parse_script, str(script)]
    return None


@pytest.mark.parametrize("name", SCRIPTS)
def test_changed_scripts_parse(name):
    command = _syntax_command(GROOVY_ROOT / name)
    if command is None:
        pytest.skip("Groovy is not installed and the SDK groovy-all jar was not found.")
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=90, check=False
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, combined
    assert "GROOVY_SYNTAX_OK" in completed.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_changed_scripts_sdk_compile_only(name):
    import odi_orchestrator
    from odi_orchestrator import OdiOrchestrator

    java = Path(odi_orchestrator._JAVA_BIN)
    sdk = Path(odi_orchestrator._ODI_SDK_LIB) / "odi-core.jar"
    if not (java.is_file() and sdk.is_file() and Path(odi_orchestrator._GROOVY_JAR).is_file()):
        pytest.skip("Local SDK/Groovy compiler unavailable.")
    command = [
        str(java), "-cp", OdiOrchestrator()._build_classpath(), "groovy.ui.GroovyMain", "-e",
        "new GroovyClassLoader().parseClass(new File(args[0])); println('SDK_COMPILE_OK')",
        str(GROOVY_ROOT / name),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SDK_COMPILE_OK" in completed.stdout


# --- Scenario version check (Defect A) ---------------------------------------


def test_scenario_check_looks_at_every_version_not_only_the_latest():
    text = _text("generate_scenario_metadata.groovy")
    helper = text[text.index("def scenariosByName"): text.index("try {\n    try {")]

    assert "finder.findByName(scenarioName)" in helper
    assert "finder.findAll()" in helper
    assert "findLatestByName" not in helper
    assert "equalsIgnoreCase(version)" in helper


def test_scenario_exact_check_is_not_a_tautology_and_reports_latest_version():
    text = _text("generate_scenario_metadata.groovy")

    assert 'existingExact == null ? "pass" : "pass"' not in text
    assert "latest_version" in text
    # already_ready and passed envelopes both expose latest_version.
    assert text.count("latest_version: ") == 2
    assert "scenario_exact_absent_or_ready" not in text
    already_ready = text[text.index('status: "already_ready"'):]
    assert "latest_version: latestVersion" in already_ready[:400]
    assert "latest_version: newLatest" in text


def test_existing_scenario_version_short_circuits_before_generation():
    text = _text("generate_scenario_metadata.groovy")

    exact_at = text.index("if (existingExact != null) {")
    generate_at = text.index("ScenarioUtils.generateNewScen")
    assert exact_at < generate_at
    assert 'status: "already_ready"' in text[exact_at:generate_at]


# --- G1 update key ---------------------------------------------------------------


def test_register_datastore_key_parameters_are_read_and_validated():
    text = _text("register_datastore.groovy")

    for prop in ("odi.key_columns", "odi.key_name", "odi.key_type", "odi.key_only"):
        assert f'System.getProperty("{prop}"' in text
    assert "new OdiKey(targetDs, keyName)" in text
    assert "OdiKey.KeyType.valueOf" in text
    assert "Key column is missing from datastore" in text
    assert "Existing key columns do not match" in text
    assert '"odi.register_datastore"' in text


def test_register_datastore_key_only_never_removes_the_datastore():
    text = _text("register_datastore.groovy")
    key_only = text[text.index("if (keyOnly) {"): text.index("// 2. Check whether it already exists")]

    assert "em.remove" not in key_only
    assert "Datastore not found for key registration" in key_only
    assert 'mode: "key_only"' in key_only


def test_register_datastore_default_path_is_unchanged_without_key():
    text = _text("register_datastore.groovy")

    assert "em.remove(existing)" in text
    assert "if (keyRequested) {" in text
    assert "if (keyInfo != null) {" in text


def test_create_mapping_fails_closed_for_merge_ikm_without_key():
    text = _text("create_mapping.groovy")

    assert "Update key is required for a merge or update IKM" in text
    assert "ukColNames.isEmpty()" in text
    assert 'selectedIkmLower.contains("merge")' in text


# --- G4 integration type -------------------------------------------------------------


def test_create_mapping_integration_type_is_restricted_and_defaults_to_iu():
    text = _text("create_mapping.groovy")

    assert 'System.getProperty("odi.integration_type") ?: "IU"' in text
    assert 'in ["IU", "CONTROL_APPEND"]' in text
    assert "Invalid odi.integration_type" in text
    assert "boolean controlAppend" in text


def test_create_mapping_control_append_is_verified_and_insert_only():
    text = _text("create_mapping.groovy")

    assert "INTEGRATION_TYPE_APPEND" in text
    assert "Control append integration type is unavailable" in text
    assert "Control append cannot use a merge or update IKM" in text
    assert "if (tgtBoundDS != null && !controlAppend)" in text
    assert 'physItCode = controlAppend ? "CA" : "IU"' in text
    assert "ikmPriorityParam" not in text
    assert 'System.getProperty("odi.ikm_priority"' not in text
    assert 'selectExactIkm(allIkms, ikmNameParam, System.getProperty("odi.approved_ikms"),' in text


def test_create_mapping_keeps_iu_update_key_contract():
    text = _text("create_mapping.groovy")

    assert "setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_UPDATE)" in text
    assert "Required update key is missing or mismatched" in text


# --- G4 truncate option -------------------------------------------------------------


def test_update_mapping_reports_unsupported_option_as_status_not_throw():
    text = _text("update_mapping.groovy")

    assert "class IkmOptionUnsupported extends RuntimeException" in text
    assert "throw new IkmOptionUnsupported(optionName)" in text
    catch_unsupported = text[text.index("} catch (IkmOptionUnsupported unsupported) {"):]
    first_block = catch_unsupported[: catch_unsupported.index("} catch (Exception e) {")]
    assert '"status":"ikm_option_unsupported"' in first_block
    assert "System.exit" not in first_block
    # The generic failure path is untouched.
    generic = catch_unsupported[catch_unsupported.index("} catch (Exception e) {"):]
    assert '"status":"error"' in generic and "System.exit(1)" in generic


def test_update_mapping_false_on_absent_option_is_a_noop():
    text = _text("update_mapping.groovy")

    assert 'if (normalized == "false")' in text
    assert "IKM OPTION ABSENT" in text


def _run_isolated_helpers(program: str) -> None:
    """Only extracted pure helpers plus fake objects; never evaluate a live script body."""
    command = _syntax_command(GROOVY_ROOT / "create_mapping.groovy")
    if command is None:
        pytest.skip("Standalone Groovy runtime unavailable.")
    program = 'Locale.setDefault(new Locale("tr", "TR"))\n' + program
    # Replace the parse expression and remove its file argument. No ODI SDK in the classpath.
    completed = subprocess.run(
        command[:-2] + [program + "\nprintln 'CONTRACT_OK'"],
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "CONTRACT_OK" in completed.stdout


def test_exact_ikm_helper_never_falls_back_behaviorally():
    text = _text("create_mapping.groovy")
    helper = text[text.index("def selectExactIkm ="):text.index("def selectUpdateKey =")]
    _run_isolated_helpers(helper + r'''
def approved = "IKM Oracle Merge,IKM Oracle Insert"
def merge = new Expando(getName: { "IKM Oracle Merge" })
def insert = new Expando(getName: { "IKM Oracle Insert" })
def custom = new Expando(getName: { "IKM Oracle Merge Custom" })
assert selectExactIkm([merge, insert], "ikm oracle merge", approved).is(merge)
for (args in [
    [[insert], "IKM Oracle Merge", approved],
    [[custom], "IKM Oracle Merge Custom", approved],
    [[merge, insert], null, approved],
    [[merge, merge], "IKM Oracle Merge", approved],
    [[merge], "IKM Oracle Merge", "IKM Oracle Insert"]
]) {
    boolean blocked = false
    try { selectExactIkm(*args) } catch (IllegalArgumentException | IllegalStateException expected) { blocked = true }
    assert blocked
}
''')


def test_update_key_helper_binds_only_active_matching_type_behaviorally():
    text = _text("create_mapping.groovy")
    helper = text[text.index("def selectUpdateKey ="):text.index("def odi = null")]
    _run_isolated_helpers(helper + r'''
enum Kind { PRIMARY_KEY, ALTERNATE_KEY, INDEX }
def key = { name, kind, active, inDb, columns ->
    new Expando(getName: { name }, getKeyType: { kind }, isActive: { active },
        isInDatabase: { inDb }, getColumns: { columns.collect { col -> new Expando(getName: { col }) } })
}
def wrong = key("A_PK", Kind.PRIMARY_KEY, true, true, ["ID"])
def inactive = key("B_UK", Kind.ALTERNATE_KEY, false, true, ["ID"])
def metadataOnly = key("C_UK", Kind.ALTERNATE_KEY, true, false, ["ID"])
def otherColumns = key("D_UK", Kind.ALTERNATE_KEY, true, true, ["ID", "OTHER"])
def wanted = key("Z_UK", Kind.ALTERNATE_KEY, true, true, ["ID"])
assert selectUpdateKey([wrong, inactive, metadataOnly, otherColumns, wanted], ["ID"] as Set, "ALTERNATE_KEY").is(wanted)
assert selectUpdateKey([wanted, wrong], ["ID"] as Set, "PRIMARY_KEY").is(wrong)
for (keys in [[], [wrong], [inactive], [metadataOnly], [otherColumns]]) {
    boolean blocked = false
    try { selectUpdateKey(keys, ["ID"] as Set, "ALTERNATE_KEY") }
    catch (IllegalStateException expected) { blocked = true }
    assert blocked
}
''')


def test_register_key_rejects_inactive_or_wrong_type_without_mutation_behaviorally():
    text = _text("register_datastore.groovy")
    helper = text[text.index("    def registerKey ="):text.index("    // 1. Find the model")]
    _run_isolated_helpers(r'''
class OdiColumn { String name }
class OdiDataStore { List columns = []; List keys = [] }
class OdiKey {
    enum KeyType { PRIMARY_KEY, ALTERNATE_KEY }
    String name
    KeyType keyType
    boolean active
    boolean inDatabase
    List columns = []
    OdiKey(OdiDataStore ds, String name) { this.name = name; ds.keys.add(this) }
    void addColumn(OdiColumn col) { columns.add(col) }
    boolean isInDatabase() { return inDatabase }
    void setInDatabase(boolean value) { inDatabase = value }
}
def keyName = "UK_TEST"
def keyColumnsStr = "ID"
def keyTypeStr = "ALTERNATE_KEY"
def persisted = []
def em = new Expando(persist: { persisted.add(it) })
''' + helper + r'''
for (caseName in ["inactive", "notInDb", "wrongType", "wrongColumns", "valid", "missing"]) {
    def id = new OdiColumn(name: "ID")
    def ds = new OdiDataStore(columns: [id])
    def existing = caseName == "missing" ? null : new OdiKey(ds, keyName)
    if (existing != null) {
        existing.keyType = caseName == "wrongType" ? OdiKey.KeyType.PRIMARY_KEY : OdiKey.KeyType.ALTERNATE_KEY
        existing.active = caseName != "inactive"
        existing.inDatabase = caseName != "notInDb"
        existing.columns = caseName == "wrongColumns" ? [] : [id]
    }
    int before = persisted.size()
    boolean blocked = false
    try {
        def result = registerKey(ds)
        assert result.active && result.in_database && result.key_type == "ALTERNATE_KEY"
        assert result.created == (caseName == "missing")
    } catch (Exception expected) {
        if (caseName in ["valid", "missing"]) throw expected
        blocked = true
    }
    assert blocked == !(caseName in ["valid", "missing"])
    assert persisted.size() == before + (caseName == "missing" ? 1 : 0)
    if (existing != null) {
        assert existing.active == (caseName != "inactive")
        assert existing.inDatabase == (caseName != "notInDb")
        assert existing.keyType == (caseName == "wrongType" ? OdiKey.KeyType.PRIMARY_KEY : OdiKey.KeyType.ALTERNATE_KEY)
    }
}
def id = new OdiColumn(name: "id")
def ds = new OdiDataStore(columns: [id])
def wrong = new OdiKey(ds, "PK_OTHER")
wrong.keyType = OdiKey.KeyType.PRIMARY_KEY
wrong.active = true
wrong.inDatabase = true
wrong.columns = [id]
def right = new OdiKey(ds, "UK_OTHER")
right.keyType = OdiKey.KeyType.ALTERNATE_KEY
right.active = true
right.inDatabase = true
right.columns = [id]
int before = persisted.size()
def result = registerKey(ds)
assert result.name == "UK_OTHER" && !result.created
assert persisted.size() == before
''')


def test_repository_preflight_helpers_are_exact_and_physical_behaviorally():
    text = _text("mapping_preflight.groovy")
    helpers = text[text.index("def inspectIkm ="):text.index("def odi = null")]
    _run_isolated_helpers(helpers + r'''
def km = { name, id -> new Expando(getName: { name }, getInternalId: { id }) }
assert inspectIkm([], "IKM Oracle Merge").code == "ikm_missing"
assert inspectIkm([km("IKM Oracle Merge", 1), km("ikm oracle merge", 2)], "IKM Oracle Merge").code == "ikm_ambiguous"
assert inspectIkm([km("IKM Oracle Merge Custom", 1)], "IKM Oracle Merge").code == "ikm_missing"
assert inspectIkm([km("IKM Oracle Merge", 7)], "IKM Oracle Merge").ikm_id == "7"
def model = { code, owner, resource, id ->
    def ds = new Expando(getName: { code + "_ALIAS" }, getResourceName: { resource },
        getInternalId: { id }, isShortcut: { false })
    def physical = owner == null ? null : new Expando(getSchemaName: { owner })
    def logical = new Expando(getPhysicalSchema: { ctx -> assert ctx == "GLOBAL"; physical })
    new Expando(getCode: { code }, getLogicalSchema: { logical }, getDataStores: { resource ? [ds] : [] },
        getTechnology: { new Expando(getName: { "Oracle" }) })
}
def wrong = model("ORACLE_ODS", "DWH", "ODS_TEST", 1)
def right = model("AUDIT_MODEL", "AUDIT_USER", "ODS_TEST", 42)
def result = inspectSource([wrong, right], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL")
assert result.status == "passed" && result.source.model_code == "AUDIT_MODEL"
assert result.source.physical_schema == "AUDIT_USER" && result.source.datastore_id == "42"
assert inspectSource([wrong], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL").code == "source_identity_missing"
assert inspectSource([right, right], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL").code == "source_identity_ambiguous"
assert inspectSource([right], "AUDIT_USER", "ODS_TEST", null, "GLOBAL").code == "source_identity_unverified"
def unresolved = model("UNKNOWN", null, "ODS_TEST", 9)
assert inspectSource([right, unresolved], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL").code == "source_identity_unverified"
def qualifiedOther = model("WRONG", "AUDIT_USER", "DWH.ODS_TEST", 9)
assert !inspectSource([qualifiedOther], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL").source.datastore_exists
def absent = inspectSource([model("AUDIT_MODEL", "AUDIT_USER", null, null)], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL")
assert absent.status == "passed" && !absent.source.datastore_exists && absent.source.datastore_id == null
def collision = model("AUDIT_MODEL", "AUDIT_USER", "OTHER_RESOURCE", 44)
collision.getDataStores()[0].getName = { "ODS_TEST" }
assert inspectSource([collision], "AUDIT_USER", "ODS_TEST", "GLOBAL", "GLOBAL").code == "source_identity_unverified"
''')


def test_binding_rejects_identity_drift_behaviorally():
    text = _text("create_mapping.groovy")
    ikm = text[text.index("def selectExactIkm ="):text.index("def selectUpdateKey =")]
    source = text[text.index("def verifySourceBinding ="):text.index("def odi = null")]
    _run_isolated_helpers(ikm + source + r'''
def km = new Expando(getName: { "IKM Oracle Merge" }, getInternalId: { 8 })
boolean blocked = false
try { selectExactIkm([km], "IKM Oracle Merge", "IKM Oracle Merge", "7") }
catch (IllegalStateException expected) { blocked = true }
assert blocked
for (spec in [["AUDIT_USER", "ODS_TEST", 42, false], ["DWH", "ODS_TEST", 42, true],
              ["AUDIT_USER", "OTHER", 42, true], ["AUDIT_USER", "ODS_TEST", 43, true]]) {
    def logical = new Expando(getPhysicalSchema: { ctx -> new Expando(getSchemaName: { spec[0] }) })
    def ds = new Expando(getModel: { new Expando(getLogicalSchema: { logical }) },
        getResourceName: { spec[1] }, getInternalId: { spec[2] }, isShortcut: { false })
    blocked = false
    try { verifySourceBinding(ds, "AUDIT_USER", "ODS_TEST", "GLOBAL", "42") }
    catch (IllegalStateException expected) { blocked = true }
    assert blocked == spec[3]
}
''')


def test_repository_preflight_has_no_write_or_data_operations():
    text = _text("mapping_preflight.groovy")
    for forbidden in (".persist(", ".remove(", ".commit(", ".getTransaction(", "createStatement", "executeQuery",
                      "getConnectionSettings", "getJdbcUrl", "getUserName", "getMessage()", "printStackTrace"):
        assert forbidden not in text
    assert '"mapping_preflight.groovy"' in (GROOVY_ROOT.parent / "odi_orchestrator.py").read_text(encoding="utf-8")


# --- same-schema / final-pass physical design and validate() messages ------------------


def test_create_mapping_assigns_load_km_to_access_points_after_final_staging():
    text = _text("create_mapping.groovy")
    staging = text.index("design.setStagingLocationName(locName)")
    final_pass = text[staging:text.index("boolean hasValidationErrors")]

    # The AP nodes are re-read from the final design, after the last staging change.
    assert "FINAL PASS" in final_pass
    assert "design.getAllAPNodes()" in final_pass
    assert "finalAps.each { MapPhysicalNode finalAp ->" in final_pass
    assert "assignLoadKm(finalAp)" in final_pass
    # LKM assignment is shared by the early and the final pass and is only skipped when no AP exists.
    assert text.count("def assignLoadKm =") == 1
    assert "LKM NOT REQUIRED" in text and "FINAL PASS: no access point" in final_pass
    # Cross-schema sources still use the configured load KM (fallback chain keeps SQL to Oracle).
    assert '"sql to oracle,mssql to oracle,sql to sql"' in text


def test_create_mapping_same_schema_moves_source_into_target_execution_unit():
    text = _text("create_mapping.groovy")

    assert "def sameLogicalSchema =" in text
    assert "physicalDiag.same_schema = sameLogicalSchema(srcDS, tgtDS)" in text
    assert "design.moveNodes([srcNode], tgtNode.getExecutionUnit(), moveIssues)" in text
    # Any AP that survives the move still gets the load KM; nothing is silently skipped.
    assert text.index("design.moveNodes(") < text.index("assignLoadKm(finalAp)")


def test_create_mapping_surfaces_validate_messages_in_structured_error():
    text = _text("create_mapping.groovy")
    catch = text[text.index("} catch (Exception e) {"):]

    # SDK 12.2.1.4: both validate() overloads return TRUE when errors were found; the issue-list
    # overload is the single gate and the no-argument call (read as "true = valid") is gone.
    assert "boolean hasValidationErrors = mappingHasErrors(mapping, validationIssues)" in text
    assert "mp.validate(issues, false) == true" in text
    assert "if (hasValidationErrors) {" in text
    assert "mappingValid" not in text and "baselineInvalid" not in text
    assert "mapping.validate()" not in text and "def vr =" not in text
    assert "collectDesignFailure(design, validationIssues, physicalDiag)" in text
    assert '"validation_messages":' in catch and '"physical_design":' in catch
    assert "JsonOutput.toJson(validationMessages)" in catch
    # The contract keys are untouched (additive only).
    for key in ('"status":"error"', '"action":"create_mapping"', '"replaced":false',
                '"kept_existing_on_failure":', '"error_type":"'):
        assert key in catch
    assert '"kept_existing_on_failure":false,"physical_design":' in text


def test_validation_and_schema_helpers_behaviorally():
    text = _text("create_mapping.groovy")
    helpers = text[text.index("def sameLogicalSchema ="):text.index("def odi = null")]
    _run_isolated_helpers(helpers + r'''
def ls = { id, name -> new Expando(getInternalId: { id }, getName: { name }) }
def ds = { logical -> new Expando(getModel: { new Expando(getLogicalSchema: { logical }) }) }
def dwh = ls(7, "LGCL_DWH")
assert sameLogicalSchema(ds(dwh), ds(dwh))
assert sameLogicalSchema(ds(ls(7, "LGCL_DWH")), ds(ls(7, "LGCL_DWH")))
assert !sameLogicalSchema(ds(ls(8, "LGCL_HR")), ds(dwh))
assert !sameLogicalSchema(ds(null), ds(dwh))

def noErrors = new Expando(validate: { issues, only -> false })
def withErrors = new Expando(validate: { issues, only -> issues.add("x"); true })
def sink = []
assert !mappingHasErrors(noErrors, sink)          // false = no errors -> the mapping commits
assert mappingHasErrors(withErrors, sink)         // true = errors found -> the mapping fails
assert sink == ["x"]

def issue = { level, msg -> new Expando(getLevel: { level }, getMessage: { msg }) }
def messages = summarizeIssues([issue("INFO", "ignored"), issue("ERROR", "bad\nnode password=hunter2 x"),
    issue("WARNING", "w" * 500)] + (1..30).collect { issue("ERROR", "e" + it) })
assert messages.size() == 20
assert messages[0] == "ERROR: bad node password=*** x"
assert messages.every { it.length() <= 300 }
assert messages[1].length() == 300
assert !messages.any { it.contains("hunter2") || it.contains("ignored") }
def leaks = summarizeIssues([issue("ERROR", "bad value \"secret words here\" and 'SELECT a FROM t' at jdbc:oracle:thin:@h:1521/x https://h/p"),
    issue("ERROR", "password = \"two words\" next")])
assert leaks[0] == "ERROR: bad value '***' and '***' at *** ***"
assert leaks[1] == "ERROR: password=*** next"

def design = new Expando(validate: { issues, verbose -> issues.add(issue("ERROR", "AP has no LKM")); false },
    getPhysicalNodes: { [new Expando(getName: { "SRC_AP" }), new Expando(getName: { "TGT" })] })
def diag = [:]
def collected = collectDesignFailure(design, [], diag)
assert collected == ["ERROR: AP has no LKM"]
assert diag.physical_nodes == ["SRC_AP", "TGT"]
def broken = new Expando(validate: { issues, verbose -> throw new IllegalStateException("x") },
    getPhysicalNodes: { throw new IllegalStateException("y") })
assert collectDesignFailure(broken, [issue("ERROR", "kept")], [:]) == ["ERROR: kept"]
''')
