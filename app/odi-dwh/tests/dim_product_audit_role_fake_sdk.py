"""Isolated fake ODI SDK that runs the real fixed audit-role repair script.

The JVM classpath holds only the Groovy runtime and these fakes, so no repository,
database or ODI connection exists. This is synthetic evidence, not native ODI proof.

Column roles model a transaction: a setter changes the working value, commit copies it
to the persisted value, and rollback restores it.
"""

from __future__ import annotations

import os
from pathlib import Path

from dim_product_audit_role_repair import expected_contract
from tests.dim_product_mapping_fake_sdk import (
    ECLIPSELINK_JAR,
    GROOVY_JAR,
    SDK_LIB,
    _compile_fake_sdk,
    _run,
    java_binary,
)

SCRIPT = Path(__file__).resolve().parents[1] / "odi_automation" / "dim_product_audit_role_repair.groovy"
RAISE_TOKEN = "RAISE_TOKEN_jdbc_internal_id"
EXPRESSION_TOKEN = "EXPRESSION_TOKEN_BODY"

_STUBS = {
    "oracle.odi.core.config.MasterRepositoryDbInfo": "MasterRepositoryDbInfo { MasterRepositoryDbInfo(Object... a) {} }",
    "oracle.odi.core.config.OdiInstanceConfig": "OdiInstanceConfig { OdiInstanceConfig(Object... a) {} }",
    "oracle.odi.core.config.PoolingAttributes": "PoolingAttributes {}",
    "oracle.odi.core.config.WorkRepositoryDbInfo": "WorkRepositoryDbInfo { WorkRepositoryDbInfo(Object... a) {} }",
    "oracle.odi.core.security.Authentication": "Authentication {}",
    "oracle.odi.core.persistence.transaction.ITransactionStatus": "ITransactionStatus {}",
    "oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition": "DefaultTransactionDefinition {}",
    "oracle.odi.domain.runtime.session.OdiSession": "OdiSession {}",
}

_CLASSES = {
    "fake.World": r'''
class World {
    static Map spec = [:]
    static Map contract = [:]
    static List trace = []
    static List columns = []
    static Map currentExpressions = [:]
    static Map persistedExpressions = [:]
    static Map hints = [:]
    static boolean committed = false
    static void reset(Map s, Map c) {
        spec = s; contract = c; trace = []; columns = []; committed = false
        currentExpressions = [:]; persistedExpressions = [:]; hints = [:]
        String alias = spec.source_alias ?: "ODS_SH_PRODUCTS"
        if (alias != "ODS_SH_PRODUCTS" && spec.rebind_alias) {
            (contract.direct_columns ?: []).each { String column -> currentExpressions[column] = alias + "." + column }
            (contract.literal_expressions ?: [:]).each { key, value -> currentExpressions[key] = value }
        } else {
            (contract.fixed_expressions ?: [:]).each { key, value -> currentExpressions[key] = value }
        }
        Map audits = spec.audit_profile == "stable" ? (contract.stable_expressions ?: [:]) : (contract.audit_expressions ?: [:])
        audits.each { key, value -> currentExpressions[key] = value }
        (spec.expressions ?: [:]).each { key, value -> currentExpressions[key] = value }
        currentExpressions.each { key, value -> persistedExpressions[key] = value }
        (contract.target_columns ?: []).each { String name -> hints[name] = name == "PRODUCT_SK" ? "TARGET" : "NO_HINT" }
        (spec.execute_hints ?: [:]).each { key, value -> hints[key] = value }
    }
    static void log(String event) { trace << event }
    static void commitColumns() {
        columns.each { it.persisted = it.current }
        persistedExpressions = [:]
        currentExpressions.each { key, value -> persistedExpressions[key] = value }
    }
    static void rollbackColumns() {
        columns.each { it.current = it.persisted }
        currentExpressions = [:]
        persistedExpressions.each { key, value -> currentExpressions[key] = value }
    }
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
        World.log("begin")
        if (World.spec.begin_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        new ITransactionStatus()
    }
    void commit(ITransactionStatus s) {
        World.log("commit")
        if (World.spec.commit_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        World.commitColumns()
        World.committed = true
        if (World.spec.commit_applied_then_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (World.spec.commit_applied_then_error) throw new Error("''' + RAISE_TOKEN + r'''")
    }
    void rollback(ITransactionStatus s) {
        World.log("rollback")
        if (World.spec.rollback_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (!World.committed) World.rollbackColumns()
    }
}
''',
    "oracle.odi.domain.model.OdiColumn": r'''
import fake.World
class OdiColumn {
    enum ScdType { SURROGATE_KEY, NATURAL_KEY, OVERWRITE_ON_CHANGE, ADD_ROW_ON_CHANGE, CURRENT_RECORD_FLAG, START_TIMESTAMP, END_TIMESTAMP }
    String name
    ScdType current
    ScdType persisted
    OdiColumn(String n, ScdType t) { name = n; current = t; persisted = t }
    String getName() { name }
    ScdType getScdType() { current }
    void setScdType(ScdType t) {
        World.log("setScdType:" + name + "=" + t)
        if (World.spec.setter_raises_on == name) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (World.spec.setter_ignored_on == name) return
        current = t
    }
    void setName(String n) { World.log("forbidden_write") }
    void setDataTypeCode(String n) { World.log("forbidden_write") }
    void setMandatory(boolean b) { World.log("forbidden_write") }
}
''',
    "oracle.odi.domain.model.OdiModel": r'''
class OdiModel {
    String code; String technologyName
    String getCode() { code }
    Map getTechnology() { [getName: { technologyName }] }
}
''',
    "oracle.odi.domain.model.OdiDataStore": r'''
class OdiDataStore {
    String name; OdiModel model; List columns
    String getName() { name }
    OdiModel getModel() { model }
    List getColumns() { columns }
}
''',
    "oracle.odi.domain.model.finder.IOdiModelFinder": r'''
import fake.World
import oracle.odi.domain.model.OdiModel
class IOdiModelFinder {
    Collection findAll() {
        int count = World.spec.model_count == null ? 1 : World.spec.model_count
        def models = count > 0 ? (1..count).collect { new OdiModel(code: World.contract.model_code, technologyName: World.spec.model_technology ?: "Oracle") } : []
        models << new OdiModel(code: "UNRELATED_MODEL", technologyName: "Oracle")
        models
    }
}
''',
    "oracle.odi.domain.model.finder.IOdiDataStoreFinder": r'''
import fake.World
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiModel
class IOdiDataStoreFinder {
    Collection findByModel(String code) {
        World.log("findByModel")
        // Later reads in one transaction see the same managed objects, as a real session does.
        if (World.spec.stores_cache != null) return World.spec.stores_cache
        def model = new OdiModel(code: code, technologyName: "Oracle")
        Map roles = [:]
        World.contract.tracked.each { roles[it] = "ADD_ROW_ON_CHANGE" }
        roles[World.contract.surrogate_key] = "SURROGATE_KEY"
        roles[World.contract.natural_key] = "NATURAL_KEY"
        roles[World.contract.start_date] = "START_TIMESTAMP"
        roles[World.contract.end_date] = "END_TIMESTAMP"
        roles[World.contract.current_flag] = "CURRENT_RECORD_FLAG"
        (World.spec.roles ?: [:]).each { name, role -> roles[name] = role }
        List names = new ArrayList(World.contract.target_columns)
        (World.spec.remove_columns ?: []).each { names.remove(it) }
        (World.spec.extra_columns ?: []).each { names.add(it) }
        if (World.spec.duplicate_column) names.add(World.spec.duplicate_column)
        World.columns = names.collect { String n ->
            def role = roles[n]
            new OdiColumn(n, role == null ? null : OdiColumn.ScdType.valueOf(role))
        }
        def target = new OdiDataStore(name: "DIM_PRODUCT", model: model, columns: World.columns)
        def sourceNames = new ArrayList(World.contract.source_columns)
        (World.spec.remove_source_columns ?: []).each { sourceNames.remove(it) }
        def source = new OdiDataStore(name: "ODS_SH_PRODUCTS", model: model, columns: sourceNames.collect { new OdiColumn(it, null) })
        def stores = [new OdiDataStore(name: "UNRELATED_TABLE", model: model, columns: [])]
        int targets = World.spec.target_count == null ? 1 : World.spec.target_count
        int sources = World.spec.source_count == null ? 1 : World.spec.source_count
        targets.times { stores << target }
        sources.times { stores << source }
        World.spec.store_snapshot = [target: target, source: source]
        World.spec.stores_cache = stores
        stores
    }
}
''',
    "oracle.odi.domain.project.OdiFolder": "class OdiFolder { String name; String getName() { name } }\n",
    "oracle.odi.domain.project.OdiProject": r'''
import fake.World
class OdiProject {
    String code
    String getCode() { code }
    List getFolders() {
        int count = World.spec.folder_count == null ? 1 : World.spec.folder_count
        count > 0 ? (1..count).collect { new OdiFolder(name: World.spec.folder_name ?: "FLD") } : []
    }
}
''',
    "oracle.odi.domain.project.finder.IOdiProjectFinder": r'''
import fake.World
import oracle.odi.domain.project.OdiProject
class IOdiProjectFinder {
    Collection findAll() {
        int count = World.spec.project_count == null ? 1 : World.spec.project_count
        def projects = count > 0 ? (1..count).collect { new OdiProject(code: World.spec.project_code ?: "PRJ") } : []
        projects << new OdiProject(code: "OTHER_PROJECT")
        projects
    }
}
''',
    "oracle.odi.domain.runtime.session.Status": "enum Status { WAITING, ERROR, RUNNING, DONE, WARNING, QUEUED }\n",
    "oracle.odi.domain.runtime.session.finder.IOdiSessionFinder": r'''
import fake.World
class IOdiSessionFinder {
    int countByStatus(oracle.odi.domain.runtime.session.Status status) {
        World.log("countByStatus:" + status)
        if (World.spec.session_finder_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        (World.spec.active_sessions ?: [:])[status.name()] ?: 0
    }
}
''',
    "oracle.odi.domain.mapping.FakeEntityManager": r'''
import fake.World
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.model.finder.IOdiModelFinder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.session.OdiSession
import oracle.odi.domain.runtime.session.finder.IOdiSessionFinder
class FakeEntityManager {
    def getFinder(Class c) {
        if (c == OdiProject) return new IOdiProjectFinder()
        if (c == OdiModel) return new IOdiModelFinder()
        if (c == OdiDataStore) return new IOdiDataStoreFinder()
        if (c == OdiSession) return new IOdiSessionFinder()
        if (c == Mapping) return [findByName: { f, n -> World.spec.mapping_missing ? null : new Mapping() }]
        throw new IllegalArgumentException("unexpected finder")
    }
    void flush() {
        World.log("flush")
        if (World.spec.flush_raises) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
    }
    void persist(Object o) { World.log("forbidden_write") }
    void remove(Object o) { World.log("forbidden_write") }
}
''',
    "oracle.odi.domain.mapping.Mapping": r'''
import fake.World
import oracle.odi.domain.mapping.component.DatastoreComponent
class Mapping {
    List getAllComponents() {
        Map stores = World.spec.store_snapshot
        def target = stores.target
        def source = stores.source
        if (World.spec.target_redirect) target = new oracle.odi.domain.model.OdiDataStore(name: "OTHER_TARGET", model: target.model, columns: target.columns)
        if (World.spec.source_redirect) source = new oracle.odi.domain.model.OdiDataStore(name: "OTHER_SOURCE", model: source.model, columns: source.columns)
        if (World.spec.target_other_model) target = new oracle.odi.domain.model.OdiDataStore(name: target.name, model: new oracle.odi.domain.model.OdiModel(code: "OTHER_MODEL"), columns: target.columns)
        def components = [new DatastoreComponent(source, false), new DatastoreComponent(target, true)]
        if (World.spec.extra_target_component) components << new DatastoreComponent(target, true)
        components << new Object()
        components
    }
    void setName(String n) { World.log("forbidden_write") }
}
''',
    "oracle.odi.domain.mapping.component.DatastoreComponent": r'''
import fake.World
class FakeExpression {
    String text
    String getText() { text }
}
class Hint {
    String label
    String name() { label }
}
class FakeAttribute {
    String name
    String getName() { name }
    Object getExpression() {
        String text = World.currentExpressions[name]
        text == null ? null : new FakeExpression(text: text)
    }
    void setExpressionText(String t) {
        World.log("setExpressionText:" + name)
        if (World.spec.expression_setter_raises_on == name) throw new RuntimeException("''' + RAISE_TOKEN + r'''")
        if (!(name in ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"])) {
            World.log("forbidden_write")
            return
        }
        World.currentExpressions[name] = t
    }
    Hint getExecuteOnHint() {
        String label = World.hints[name]
        label == null ? null : new Hint(label: label)
    }
    void setExecuteOnHint(Object ignored) { World.log("forbidden_write") }
}
class DatastoreComponent {
    def bound; boolean target
    DatastoreComponent(def b, boolean t) { bound = b; target = t }
    def getBoundDataStore() { bound }
    String getName() { target ? "DIM_PRODUCT" : (World.spec.source_alias ?: "ODS_SH_PRODUCTS") }
    String getIntegrationType() { target ? (World.spec.integration_type ?: "SCD") : null }
    List getAttributes() {
        List names = bound.getColumns()*.getName()
        (World.spec.remove_attributes ?: []).each { if (target) names.remove(it) }
        names.collect { String n -> new FakeAttribute(name: n) }
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
["odi.audit.action", "odi.audit.approved", "odi.audit.approval_scope", "odi.audit.expected_before",
 "odi.audit.changed_columns", "odi.audit.changed_expressions", "odi.audit.expression_profile_before",
 "odi.audit.expression_sql", "odi.project", "odi.folder"].each { System.clearProperty(it) }
Map results = [:]
input.scenarios.each { String id, Map scenario ->
    Map spec = scenario.spec ?: [:]
    World.reset(spec, input.contract)
    ["odi.audit.action", "odi.audit.approved", "odi.audit.approval_scope", "odi.audit.expected_before",
     "odi.audit.changed_columns", "odi.audit.changed_expressions", "odi.audit.expression_profile_before",
     "odi.audit.expression_sql", "odi.project", "odi.folder"].each { System.clearProperty(it) }
    scenario.params.each { key, value -> System.setProperty(key, value) }
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
    results[id] = [
        output: captured.toString(), trace: World.trace, committed: World.committed, script_raised: scriptRaised,
        persisted: World.columns.collectEntries { [(it.getName()): it.persisted?.name()] },
        expressions: World.persistedExpressions, hints: World.hints,
    ]
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
        m.name == row.method && java.lang.reflect.Modifier.isPublic(m.modifiers) && m.parameterTypes*.name == row.params
    }
    if (!found) missing << row.type + "#" + row.method
}
new GroovyShell(loader).parse(new File(input.script))
print JsonOutput.toJson([missing: missing, script_imports_resolved: true])
'''


def available() -> str | None:
    """Return the Java launcher when the local Groovy runtime exists. Nothing is downloaded."""
    binary = java_binary()
    return binary if binary and GROOVY_JAR.is_file() else None


def _write_sources(root: Path) -> None:
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


def prepare_runtime(java: str, workspace: Path) -> str:
    """Compile the fakes once and return the classpath for repeated script runs."""
    sources, classes = workspace / "src", workspace / "classes"
    _write_sources(sources)
    _compile_fake_sdk(java, sources, classes)
    driver = workspace / "driver.groovy"
    driver.write_text(_DRIVER, encoding="utf-8")
    return os.pathsep.join([str(GROOVY_JAR), str(classes)])


def run_scenarios(java: str, workspace: Path, classpath: str, scenarios: dict, script: Path = SCRIPT) -> dict:
    """Run the real script once per scenario on the fake classpath. No connection exists."""
    payload = {
        "script": str(script),
        "workspace": str(workspace),
        "contract": expected_contract(),
        "scenarios": scenarios,
    }
    return _run(java, classpath, workspace / "driver.groovy", payload, workspace)


def check_real_public_api(java: str, workspace: Path, required: list[dict]) -> dict:
    """Load real SDK classes without initializing them. No instance or connection is created."""
    driver = workspace / "api_driver.groovy"
    driver.write_text(_API_DRIVER, encoding="utf-8")
    classpath = os.pathsep.join([str(SDK_LIB / "*"), str(GROOVY_JAR), str(ECLIPSELINK_JAR)])
    return _run(java, classpath, driver, {"script": str(SCRIPT), "required": required}, workspace)
