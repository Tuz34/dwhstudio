/* Read-only execution-identity proof for one target model and GLOBAL context.
   Uses public getters only. Never reads a password, URL, or connection property.
   Failure payloads carry reason codes and counts, not account names. */
import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.topology.OdiContext
import oracle.odi.domain.topology.OdiContextualSchemaMapping

final String TOOL = "odi.execution_identity"
final String MODE = "topology_bound_execution_identity"
final String ORACLE = "Oracle"
final java.util.regex.Pattern ID = ~/^[A-Z0-9_$#]{1,30}$/

def base = { String status ->
    [
        tool: TOOL,
        status: status,
        mode: MODE,
        side_effects: false,
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
    ]
}

def blocked = { String code, boolean connected, int models, int contexts, int mappings ->
    base("blocked") + [
        code: code,
        repository_connection_attempted: connected,
        model_count: models,
        context_count: contexts,
        mapping_count: mappings,
    ]
}

def sameLogical = { left, right ->
    if (left == null || right == null) return false
    if (left.is(right)) return true
    String a = left.getInternalId()?.toString()
    String b = right.getInternalId()?.toString()
    return a != null && a == b
}

def oracleName = { technology -> technology?.getName() == ORACLE }

def inspectIdentity = { models, contexts, mappings, String modelCode, String contextCode, String schemaName ->
    List namedModels = models.findAll { it?.getCode() == modelCode }
    List namedContexts = contexts.findAll { it?.getCode() == contextCode }
    if (namedModels.size() != 1) {
        String code = namedModels.isEmpty() ? "execution_identity_missing" : "execution_identity_ambiguous"
        return blocked(code, true, namedModels.size(), namedContexts.size(), 0)
    }
    if (namedContexts.size() != 1) {
        String code = namedContexts.isEmpty() ? "execution_identity_missing" : "execution_identity_ambiguous"
        return blocked(code, true, 1, namedContexts.size(), 0)
    }
    def model = namedModels[0]
    def context = namedContexts[0]
    def logical = model.getLogicalSchema()
    if (logical == null || !oracleName(model.getTechnology())) {
        return blocked("execution_identity_unverified", true, 1, 1, 0)
    }
    List bound = mappings.findAll { mapping ->
        mapping?.getContext()?.getCode() == contextCode && sameLogical(mapping.getLogicalSchema(), logical)
    }
    if (bound.size() != 1) {
        String code = bound.isEmpty() ? "execution_identity_missing" : "execution_identity_ambiguous"
        return blocked(code, true, 1, 1, bound.size())
    }
    def physical = bound[0].getPhysicalSchema()
    def resolved = logical.getPhysicalSchema(context)
    boolean samePhysical = physical != null && resolved != null &&
        (physical.is(resolved) || sameLogical(physical, resolved))
    if (!samePhysical) {
        return blocked("execution_identity_unverified", true, 1, 1, 1)
    }
    if (physical.getSchemaName() != schemaName || !oracleName(physical.getTechnology())) {
        return blocked("execution_identity_unverified", true, 1, 1, 1)
    }
    def server = physical.getDataServer()
    if (server == null || !oracleName(server.getTechnology())) {
        return blocked("execution_identity_unverified", true, 1, 1, 1)
    }
    // Inherited public getter on AbstractOdiDataServer. The topology object name is not the account.
    String username = server.getUsername()
    if (!(username ==~ ID)) {
        return blocked("execution_identity_unverified", true, 1, 1, 1)
    }
    return base("passed") + [
        repository_connection_attempted: true,
        model_count: 1,
        context_count: 1,
        mapping_count: 1,
        data_server_count: 1,
        model_code: model.getCode(),
        context_code: context.getCode(),
        schema_name: physical.getSchemaName(),
        technology: ORACLE,
        data_server_technology: ORACLE,
        username: username,
    ]
}

def odi = null
boolean standalone = false
boolean attempted = false
Map payload = blocked("execution_identity_invalid", false, 0, 0, 0)
try {
    String modelCode = System.getProperty("odi.execution.model")
    String contextCode = System.getProperty("odi.execution.context")
    String schemaName = System.getProperty("odi.execution.schema")
    if (!(modelCode ==~ ID) || contextCode != "GLOBAL" || !(schemaName ==~ ID)) {
        payload = blocked("execution_identity_invalid", false, 0, 0, 0)
    } else {
        attempted = true
        try {
            odi = odiInstance
        } catch (MissingPropertyException ignored) {
            standalone = true
            String propsPath = args?.length ? args[0] : "odi_connection.properties"
            Properties props = new Properties()
            new FileInputStream(propsPath).withStream { props.load(it) }
            def pool = new PoolingAttributes()
            def master = new MasterRepositoryDbInfo(
                props.getProperty("master_jdbc"), props.getProperty("master_jdbc_driver"),
                props.getProperty("master_username"), props.getProperty("master_password").toCharArray(), pool)
            def work = new WorkRepositoryDbInfo(props.getProperty("work_name"), pool)
            odi = OdiInstance.createInstance(new OdiInstanceConfig(master, work))
            def auth = odi.getSecurityManager().createAuthentication(
                props.getProperty("odi_username"), props.getProperty("odi_password").toCharArray())
            odi.getSecurityManager().setCurrentThreadAuthentication(auth)
        }
        def em = odi.getTransactionalEntityManager()
        payload = inspectIdentity(
            em.getFinder(OdiModel.class).findAll(),
            em.getFinder(OdiContext.class).findAll(),
            em.getFinder(OdiContextualSchemaMapping.class).findAll(),
            modelCode, contextCode, schemaName)
    }
} catch (Exception ignored) {
    payload = blocked("execution_identity_unverified", attempted, 0, 0, 0)
} finally {
    if (standalone && odi != null) {
        try {
            odi.close()
        } catch (Exception ignored) {
            payload = blocked("execution_identity_unverified", attempted, 0, 0, 0)
        }
    }
}
println JsonOutput.toJson(payload)
