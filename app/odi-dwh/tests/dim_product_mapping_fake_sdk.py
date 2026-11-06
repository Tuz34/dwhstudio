"""Isolated fake ODI SDK that runs the real fixed DIM_PRODUCT mapping script.

The real Groovy script runs against in-memory fake classes. The JVM classpath
holds only the Groovy runtime and these fakes, so no repository, database or ODI
connection exists. This is synthetic evidence, not native ODI validation.

KM accessor behaviour follows the bytecode of the installed odi-core.jar:
`getLKMName` reads the target-component KM, `getXKMName` reads a separate
extract reference and returns an empty string when it is unset.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ODI_HOME = Path(os.getenv("ODI_HOME", r"C:\Oracle\Middleware\Oracle_Home"))
GROOVY_JAR = ODI_HOME / "oracle_common" / "modules" / "thirdparty" / "groovy-all-2.4.17.jar"
SDK_LIB = ODI_HOME / "odi" / "sdk" / "lib"
ECLIPSELINK_JAR = ODI_HOME / "oracle_common" / "modules" / "oracle.toplink" / "eclipselink.jar"
SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation" / "dim_product_scd2_mapping_create.groovy"
IKM_NAME = "IKM Oracle Slowly Changing Dimension"
LKM_NAME = "LKM SQL to Oracle (Built-In)"
ISSUE_TOKEN = "ISSUE_TOKEN_jdbc_expression_internal_id"
RAISE_TOKEN = "RAISE_TOKEN_jdbc_internal_id"

_STUBS = {
    "oracle.odi.core.config.MasterRepositoryDbInfo": "MasterRepositoryDbInfo { MasterRepositoryDbInfo(Object... a) {} }",
    "oracle.odi.core.config.OdiInstanceConfig": "OdiInstanceConfig { OdiInstanceConfig(Object... a) {} }",
    "oracle.odi.core.config.PoolingAttributes": "PoolingAttributes {}",
    "oracle.odi.core.config.WorkRepositoryDbInfo": "WorkRepositoryDbInfo { WorkRepositoryDbInfo(Object... a) {} }",
    "oracle.odi.core.security.Authentication": "Authentication {}",
    "oracle.odi.core.persistence.transaction.ITransactionStatus": "ITransactionStatus {}",
    "oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition": "DefaultTransactionDefinition {}",
    "oracle.odi.domain.project.OdiFolder": "OdiFolder { String name; String getName() { name } }",
}

_CLASSES = {
    "fake.World": r'''
class World {
    static Map spec = [:]
    static List trace = []
    static Object mapping = null
    static boolean committed = false
    // Expression texts set by the script and the attribute objects, kept in the harness only.
    static Map expressions = [:]
    static Map attributes = [:]
    static void reset(Map s) { spec = s; trace = []; mapping = null; committed = false; expressions = [:]; attributes = [:] }
    static void attributeSet(Object attribute, String text) { attributes[attribute.getName()] = attribute; expressions[attribute.getName()] = text }
    static void log(String event) { trace << event }
    static class FailingOut {
        Writer writer
        int failures
        void println(Object value) {
            if (failures > 0) {
                failures--
                throw new RuntimeException("''' + RAISE_TOKEN + r'''")
            }
            writer.write(String.valueOf(value) + "\n")
        }
    }
    static Map defaults() {
        [FLOW_CONTROL: true, STATIC_CONTROL: false, RECYCLE_ERRORS: false, TRUNCATE: false,
         DELETE_ALL: false, CREATE_TARG_TABLE: false, COMMIT: true]
    }
}
''',
    "oracle.odi.core.OdiInstance": r'''
import fake.World
import oracle.odi.core.persistence.transaction.ITransactionManager
class OdiInstance {
    static OdiInstance createInstance(Object c) { new OdiInstance() }
    ITransactionManager getTransactionManager() { new ITransactionManager() }
    def getTransactionalEntityManager() { new oracle.odi.domain.mapping.FakeEntityManager() }
    def getSecurityManager() {
        [createAuthentication: { String u, char[] p -> new oracle.odi.core.security.Authentication() },
         setCurrentThreadAuthentication: { a -> }]
    }
    void close() {
        World.log("odi_close")
        if (World.spec.close_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
    }
}
''',
    "oracle.odi.core.persistence.transaction.ITransactionManager": r'''
import fake.World
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition
class ITransactionManager {
    ITransactionStatus getTransaction(DefaultTransactionDefinition d) {
        if (World.spec.begin_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        new ITransactionStatus()
    }
    void commit(ITransactionStatus s) {
        World.log("commit")
        // The two commit failures look the same to the script: only the repository differs.
        if (World.spec.commit_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        World.committed = true
        if (World.spec.commit_applied_then_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (World.spec.commit_applied_then_error) throw new Error("''' + RAISE_TOKEN + r'''")
    }
    void rollback(ITransactionStatus s) {
        World.log("rollback")
        if (World.spec.rollback_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (World.spec.rollback_error) throw new Error("''' + RAISE_TOKEN + r'''")
    }
}
''',
    "oracle.odi.domain.root.RootIssue": r'''
class RootIssue {
    enum Level { INFO, WARNING, ERROR, FATAL }
    Level level
    String message
    RootIssue(Level l, String m) { level = l; message = m }
    Level getLevel() { level }
    String getMessage() { message }
    boolean isError() { level == Level.ERROR || level == Level.FATAL }
    boolean isFatal() { level == Level.FATAL }
    static boolean isErrorOrFatal(List<RootIssue> issues) {
        if (issues == null) return false
        for (RootIssue issue : issues) { if (issue.isError() || issue.isFatal()) return true }
        return false
    }
}
''',
    "oracle.odi.domain.mapping.expression.MapExpression": r'''
class MapExpression { enum ExecuteOnLocation { SOURCE, STAGING, TARGET } }
''',
    "oracle.odi.domain.model.OdiColumn": r'''
import fake.World
class OdiColumn {
    enum ScdType { SURROGATE_KEY, NATURAL_KEY, START_TIMESTAMP, END_TIMESTAMP, CURRENT_RECORD_FLAG, ADD_ROW_ON_CHANGE, OVERWRITE_ON_CHANGE }
    String name
    ScdType scdType
    OdiColumn(String n, ScdType t) { name = n; scdType = t }
    String getName() { name }
    ScdType getScdType() { scdType }
    void setScdType(ScdType t) { World.log("datastore_mutation"); scdType = t }
    void setName(String n) { World.log("datastore_mutation"); name = n }
}
''',
    "oracle.odi.domain.model.OdiDataStore": r'''
import fake.World
class OdiDataStore {
    String name
    List<OdiColumn> columns
    OdiDataStore(String n, List<OdiColumn> c) { name = n; columns = c }
    String getName() { name }
    List<OdiColumn> getColumns() { columns }
    void addColumn(Object c) { World.log("datastore_mutation") }
}
''',
    "oracle.odi.domain.model.finder.IOdiDataStoreFinder": r'''
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiDataStore
class IOdiDataStoreFinder {
    static final List DIRECT = ["PROD_ID", "PROD_NAME", "PROD_DESC", "PROD_SUBCATEGORY", "PROD_SUBCATEGORY_ID",
        "PROD_SUBCATEGORY_DESC", "PROD_CATEGORY", "PROD_CATEGORY_ID", "PROD_CATEGORY_DESC", "PROD_WEIGHT_CLASS",
        "PROD_UNIT_OF_MEASURE", "PROD_PACK_SIZE", "SUPPLIER_ID", "PROD_STATUS", "PROD_LIST_PRICE", "PROD_MIN_PRICE",
        "PROD_TOTAL", "PROD_TOTAL_ID"]
    OdiDataStore findByName(String name, String model) {
        if (name == "ODS_SH_PRODUCTS") {
            def extra = (1..6).collect { "ODS_EXTRA_" + it }
            return new OdiDataStore(name, (DIRECT + extra).collect { new OdiColumn(it, null) })
        }
        def roles = [PRODUCT_SK: OdiColumn.ScdType.SURROGATE_KEY, EFFECTIVE_FROM: OdiColumn.ScdType.START_TIMESTAMP,
            EFFECTIVE_TO: OdiColumn.ScdType.END_TIMESTAMP, CURRENT_FLAG: OdiColumn.ScdType.CURRENT_RECORD_FLAG,
            INSERT_DATE: null, UPDATE_DATE: null, SOURCE_SYSTEM: null, ETL_BATCH_ID: null]
        def columns = DIRECT.collect {
            new OdiColumn(it, it == "PROD_ID" ? OdiColumn.ScdType.NATURAL_KEY : OdiColumn.ScdType.ADD_ROW_ON_CHANGE)
        }
        roles.each { n, t -> columns << new OdiColumn(n, t) }
        new OdiDataStore(name, columns)
    }
}
''',
    "oracle.odi.domain.project.OdiProject": r'''
class OdiProject {
    String code
    OdiProject(String c) { code = c }
    String getCode() { code }
    List getFolders() { [new OdiFolder(name: "FLD")] }
}
''',
    "oracle.odi.domain.project.OdiIKM": r'''
class OdiIKM {
    String name; boolean global; OdiProject project; String integrationType = "SLOWLY_CHANGING_DIMENSION"
    String getName() { name }
    boolean isGlobal() { global }
    OdiProject getProject() { project }
    String getIntegrationType() { integrationType }
}
''',
    "oracle.odi.domain.project.OdiLKM": r'''
class OdiLKM {
    String name; boolean global; OdiProject project
    String getName() { name }
    boolean isGlobal() { global }
    OdiProject getProject() { project }
}
''',
    "oracle.odi.domain.project.finder.IOdiProjectFinder": r'''
import oracle.odi.domain.project.OdiProject
class IOdiProjectFinder { Collection findAll() { [new OdiProject("PRJ")] } }
''',
    "oracle.odi.domain.project.finder.IOdiIKMFinder": r'''
import fake.World
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiProject
class IOdiIKMFinder {
    Collection findByName(String requested) {
        (World.spec.ikms ?: [[name: requested, project: "PRJ"]]).collect { row ->
            new OdiIKM(name: row.name, global: row.project == null,
                project: row.project == null ? null : new OdiProject(row.project),
                integrationType: row.integration_type ?: "SLOWLY_CHANGING_DIMENSION")
        }
    }
}
''',
    "oracle.odi.domain.project.finder.IOdiLKMFinder": r'''
import fake.World
import oracle.odi.domain.project.OdiLKM
import oracle.odi.domain.project.OdiProject
class IOdiLKMFinder {
    Collection findByName(String requested) {
        (World.spec.lkms ?: [[name: requested, project: null]]).collect { row ->
            new OdiLKM(name: row.name, global: row.project == null,
                project: row.project == null ? null : new OdiProject(row.project))
        }
    }
}
''',
    "oracle.odi.domain.runtime.scenario.OdiScenario": "class OdiScenario {}\n",
    "oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder": r'''
import fake.World
import oracle.odi.domain.runtime.scenario.OdiScenario
class IOdiScenarioFinder {
    OdiScenario findLatestByName(String n) { World.spec.scenario_exists ? new OdiScenario() : null }
}
''',
    "oracle.odi.domain.mapping.FakeEntityManager": r'''
import fake.World
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiLKM
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiIKMFinder
import oracle.odi.domain.project.finder.IOdiLKMFinder
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder
class FakeEntityManager {
    def getFinder(Class c) {
        if (c == OdiProject) return new IOdiProjectFinder()
        if (c == OdiScenario) return new IOdiScenarioFinder()
        if (c == OdiDataStore) return new IOdiDataStoreFinder()
        if (c == OdiIKM) return new IOdiIKMFinder()
        if (c == OdiLKM) return new IOdiLKMFinder()
        if (c == Mapping) return [findByName: { f, n -> World.spec.mapping_exists ? new Object() : null }]
        throw new IllegalArgumentException("unexpected finder")
    }
    void persist(Object o) { World.log("persist") }
}
''',
    "oracle.odi.domain.mapping.Mapping": r'''
import fake.World
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.domain.root.RootIssue
class Mapping {
    MapPhysicalDesign design
    int validateCalls = 0
    Mapping(String n, Object folder) { design = new MapPhysicalDesign(); World.mapping = this }
    MapPhysicalDesign getPhysicalDesign(int i) { design }
    boolean validate(List issues, boolean flag) {
        validateCalls++
        World.log("validate")
        design.onValidate(validateCalls)
        boolean finalCall = validateCalls >= (World.spec.final_validate_call ?: 2)
        Map outcome = finalCall ? (World.spec.final_validate ?: [:]) : (World.spec.first_validate ?: [:])
        if (outcome.raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (finalCall && World.spec.tamper_expression) {
            // Simulates validation silently rewriting an expression after the builder set it.
            World.attributes[World.spec.tamper_expression]?.expression = "CURRENT_TIMESTAMP"
        }
        if (finalCall && World.spec.tamper_hint) {
            World.attributes[World.spec.tamper_hint]?.hint = oracle.odi.domain.mapping.expression.MapExpression.ExecuteOnLocation.TARGET
        }
        (outcome.levels ?: []).each { issues << new RootIssue(RootIssue.Level.valueOf(it), "''' + ISSUE_TOKEN + r'''") }
        return outcome.flag == true
    }
    boolean validate() { validate(new ArrayList(), false) }
}
''',
    "oracle.odi.domain.mapping.component.DatastoreComponent": r'''
import oracle.odi.domain.mapping.expression.MapExpression
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.model.OdiDataStore
class FakeExpression {
    String text
    String getText() { text }
}
class FakeAttribute {
    String name; String expression; MapExpression.ExecuteOnLocation hint
    String getName() { name }
    // Like the SDK MapAttribute, the expression is an object whose text is read with getText().
    FakeExpression getExpression() { expression == null ? null : new FakeExpression(text: expression) }
    void setExpressionText(String t) { expression = t; fake.World.attributeSet(this, t) }
    void setExecuteOnHint(MapExpression.ExecuteOnLocation l) { hint = l }
    MapExpression.ExecuteOnLocation getExecuteOnHint() { hint }
    void setCheckNotNullIndicator(boolean b) {}
    void setUpdateIndicator(boolean b) {}
}
class DatastoreComponent {
    static final String INTEGRATION_TYPE_SCD = "SCD"
    String name; String integrationType; List attributes
    DatastoreComponent(Mapping m, OdiDataStore ds) {
        name = ds.getName()
        attributes = ds.getColumns().collect { new FakeAttribute(name: it.getName()) }
    }
    String getName() { name }
    String getIntegrationType() { integrationType }
    void setIntegrationType(String t) { integrationType = t }
    void connectTo(DatastoreComponent other) {}
    List getAttributes() { attributes }
}
''',
    "oracle.odi.domain.mapping.physical.MapPhysicalNode": r'''
import fake.World
class FakeOption {
    String name; Object current; boolean sticky
    String getName() { name }
    Object getOptionValue() { current }
    void setValue(Object v) {
        World.log("option:" + name + "=" + v)
        if (!sticky) current = v
    }
}
class MapPhysicalNode {
    String name; boolean target; boolean ap; Object km; Object xkm
    Map options = [:]
    String getName() { name }
    boolean isTargetNode() { target }
    boolean isAPNode() { ap }
    def getExecutionUnit() { [getLocation: { [getName: { "LOC" }] }] }
    void seedOptions() {
        Map values = World.defaults() + (World.spec.option_defaults ?: [:])
        options = [:]
        values.each { n, v ->
            if (!(n in (World.spec.missing_options ?: []))) {
                options[n] = new FakeOption(name: n, current: v, sticky: n in (World.spec.sticky_options ?: []))
            }
        }
    }
    void setTargetComponentKM(Object k) { World.log("setTargetComponentKM"); km = k; seedOptions() }
    String getTargetComponentKMName() {
        if (World.spec.ikm_name_mismatch) return "IKM Some Other"
        km?.getName()
    }
    FakeOption getIKMOptionValue(String n) { options[n] }
    void setLKM(Object k) {
        World.log("setLKM")
        if (!ap) throw new IllegalStateException("not an access point")
        if (!World.spec.lkm_set_ignored) km = k
    }
    String getLKMName() { km?.getName() }
    Object getLKM() { km }
    void setXKM(Object k) { World.log("setXKM"); xkm = k }
    String getXKMName() { xkm == null ? "" : xkm.getName() }
}
''',
    "oracle.odi.domain.mapping.physical.MapPhysicalDesign": r'''
import fake.World
class MapPhysicalDesign {
    List<MapPhysicalNode> nodes = []
    MapPhysicalDesign() { rebuild() }
    private void rebuild() {
        nodes = [new MapPhysicalNode(name: "DIM_PRODUCT", target: true)]
        int count = World.spec.ap_count == null ? 1 : World.spec.ap_count
        if (count > 0) { (1..count).each { nodes << new MapPhysicalNode(name: "AP_" + it, ap: true) } }
    }
    List getPhysicalNodes() { nodes }
    List getAllAPNodes() { nodes.findAll { it.isAPNode() } }
    void setStagingLocationName(String n) {}
    void resetSpec() { World.log("resetSpec"); rebuild() }
    void onValidate(int call) {
        MapPhysicalNode target = nodes.find { it.isTargetNode() }
        if (call == 1 && World.spec.materialize_ap_on_validate) {
            nodes << new MapPhysicalNode(name: "AP_LATE", ap: true)
        }
        if (call == 1 && World.spec.options_reset_on_first_validate) target.seedOptions()
        if (call == 2 && World.spec.options_reset_on_final_validate) target.seedOptions()
        if (call == 2 && World.spec.materialize_ap_on_final_validate) {
            nodes << new MapPhysicalNode(name: "AP_LATE", ap: true)
        }
    }
}
''',
}

_DRIVER = r'''
import fake.World
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import oracle.odi.core.OdiInstance

Map input = new JsonSlurper().parse(System.in)
System.setProperty("odi.project", "PRJ")
System.setProperty("odi.folder", "FLD")
Map results = [:]
input.scenarios.each { String id, Map spec ->
    World.reset(spec)
    StringWriter captured = new StringWriter()
    Map variables = [out: new World.FailingOut(writer: captured, failures: spec.print_failures ?: 0)]
    if (spec.standalone) {
        // Synthetic placeholders only. The fake instance never reads or connects with them.
        File properties = new File(input.workspace, "fake_connection.properties")
        properties.text = ["master_jdbc", "master_jdbc_driver", "master_username", "master_password",
                           "work_name", "odi_username", "odi_password"].collect { it + "=fake" }.join("\n")
        variables.args = [properties.path] as String[]
    } else {
        variables.odiInstance = new OdiInstance()
    }
    String scriptRaised = null
    try {
        new GroovyShell(this.class.classLoader, new Binding(variables)).evaluate(new File(input.script))
    } catch (Throwable raised) {
        scriptRaised = raised.getClass().getSimpleName()
    }
    Map state = World.mapping == null ? [:] : [
        lkm_names: World.mapping.design.getAllAPNodes().collect { it.getLKMName() },
        xkm_names: World.mapping.design.getAllAPNodes().collect { it.getXKMName() },
        options: World.mapping.design.nodes.find { it.isTargetNode() }.options.collectEntries { n, o -> [n, o.getOptionValue()] },
    ]
    results[id] = [output: captured.toString(), trace: World.trace, committed: World.committed,
                   state: state, script_raised: scriptRaised, expressions: World.expressions]
}
print JsonOutput.toJson(results)
'''

_API_DRIVER = r'''
import groovy.json.JsonOutput
import groovy.json.JsonSlurper

Map input = new JsonSlurper().parse(System.in)
ClassLoader loader = this.class.classLoader
List missing = []
input.required.each { row ->
    Class type = Class.forName(row.type, false, loader)
    boolean found = type.methods.any { m ->
        m.name == row.method && java.lang.reflect.Modifier.isPublic(m.modifiers) &&
            m.parameterTypes*.name == row.params
    }
    if (!found) missing << row.type + "#" + row.method
}
new GroovyShell(loader).parse(new File(input.script))
print JsonOutput.toJson([missing: missing, script_imports_resolved: true])
'''


def java_binary() -> str | None:
    """Return the local Java launcher when one exists. Nothing is downloaded."""
    return shutil.which("java")


def _run(java: str, classpath: str, driver: Path, payload: dict, cwd: Path) -> dict:
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP", "PATH") if key in os.environ}
    completed = subprocess.run(
        [java, "-cp", classpath, "groovy.ui.GroovyMain", str(driver)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=cwd,
        env=environment,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-2000:])
    return json.loads(completed.stdout[completed.stdout.index("{"):])


def write_fake_sdk(root: Path) -> None:
    """Write each fake class under its package path, so Groovy resolves the script's imports."""
    for fqcn, body in _CLASSES.items():
        package, name = fqcn.rsplit(".", 1)
        path = root.joinpath(*package.split("."), f"{name}.groovy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"package {package}\n{body}", encoding="utf-8")
    for fqcn, body in _STUBS.items():
        package, name = fqcn.rsplit(".", 1)
        path = root.joinpath(*package.split("."), f"{name}.groovy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"package {package}\nclass {body}\n", encoding="utf-8")


def _compile_fake_sdk(java: str, sources: Path, classes: Path) -> None:
    """Compile the fakes together, because they refer to each other."""
    classes.mkdir(parents=True, exist_ok=True)
    files = sorted(str(path) for path in sources.rglob("*.groovy"))
    completed = subprocess.run(
        [java, "-cp", str(GROOVY_JAR), "org.codehaus.groovy.tools.FileSystemCompiler", "-d", str(classes), *files],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-2000:] or completed.stdout[-2000:])


def run_scenarios(java: str, workspace: Path, scenarios: dict[str, dict]) -> dict[str, dict]:
    """Run the real script once per scenario, in one JVM, on the fake classpath."""
    sources = workspace / "fake_sdk_src"
    classes = workspace / "fake_sdk_classes"
    write_fake_sdk(sources)
    _compile_fake_sdk(java, sources, classes)
    driver = workspace / "fake_sdk_driver.groovy"
    driver.write_text(_DRIVER, encoding="utf-8")
    classpath = os.pathsep.join([str(GROOVY_JAR), str(classes)])
    payload = {"script": str(SCRIPT), "workspace": str(workspace), "scenarios": scenarios}
    return _run(java, classpath, driver, payload, workspace)


def check_real_public_api(java: str, workspace: Path, required: list[dict]) -> dict:
    """Load real SDK classes without initializing them. No instance or connection is created."""
    driver = workspace / "api_driver.groovy"
    driver.write_text(_API_DRIVER, encoding="utf-8")
    classpath = os.pathsep.join([str(SDK_LIB / "*"), str(GROOVY_JAR), str(ECLIPSELINK_JAR)])
    return _run(java, classpath, driver, {"script": str(SCRIPT), "required": required}, workspace)
