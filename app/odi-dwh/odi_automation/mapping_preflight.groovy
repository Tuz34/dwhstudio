/* Read-only repository identity proof. No target SQL, transaction writes or mapping execution. */
import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.topology.OdiContext

def inspectIkm = { ikms, String requested ->
    def matches = ikms.findAll { it.getName().equalsIgnoreCase(requested) }
    if (matches.size() != 1) {
        return [status: "blocked", code: matches.isEmpty() ? "ikm_not_imported" : "ikm_ambiguous"]
    }
    String identifier = matches[0].getInternalId()?.toString()
    if (!(identifier ==~ /[0-9]+/)) throw new IllegalStateException("RepositoryIdentityUnavailable")
    return [status: "passed", ikm_name: requested, ikm_id: identifier, ikm_match_count: 1]
}

def inspectSource = { models, String owner, String table, context, String contextCode ->
    if (context == null) return [status: "blocked", code: "source_identity_unverified"]
    def ownerModels = []
    def matches = []
    boolean unresolved = false
    models.each { model ->
        def datastores = model.getDataStores().findAll { ds ->
            String resource = ds.getResourceName()?.toUpperCase(Locale.ROOT)
            resource == table || resource == owner + "." + table
        }
        def physical = model.getLogicalSchema()?.getPhysicalSchema(context)
        if (physical == null && !datastores.isEmpty()) unresolved = true
        if (physical?.getSchemaName()?.equalsIgnoreCase(owner) && model.getTechnology()?.getName()?.equalsIgnoreCase("Oracle")) {
            if (model.getDataStores().any { ds -> ds.getName()?.equalsIgnoreCase(table) && !datastores.contains(ds) }) {
                unresolved = true
            }
            ownerModels.add(model)
            datastores.each { ds ->
                if (ds.isShortcut()) unresolved = true
                else matches.add([model: model, datastore: ds])
            }
        }
    }
    if (unresolved) return [status: "blocked", code: "source_identity_unverified"]
    if (matches.size() > 1 || (matches.isEmpty() && ownerModels.size() > 1)) {
        return [status: "blocked", code: "source_identity_ambiguous"]
    }
    if (matches.isEmpty() && ownerModels.isEmpty()) {
        return [status: "blocked", code: "source_identity_missing"]
    }
    def model = matches ? matches[0].model : ownerModels[0]
    def ds = matches ? matches[0].datastore : null
    return [status: "passed", source: [model_code: model.getCode(),
        datastore_name: ds == null ? table : ds.getName(), resource_name: table,
        physical_schema: owner, context: contextCode, datastore_exists: ds != null,
        datastore_id: ds?.getInternalId()?.toString()]]
}

def odi = null
boolean standalone = false
boolean attempted = false
Map payload = [status: "blocked", code: "repository_lookup_failed"]
try {
    String requested = System.getProperty("odi.ikm_name")
    String owner = System.getProperty("odi.src_owner")
    String table = System.getProperty("odi.src_table")
    String contextCode = System.getProperty("odi.context", "GLOBAL")
    if (!(requested ==~ /[A-Za-z0-9][A-Za-z0-9 ()._-]{0,127}/) ||
        !(contextCode ==~ /[A-Z][A-Z0-9_]{0,127}/) ||
        ((owner == null) != (table == null)) ||
        (owner != null && (!(owner ==~ /[A-Z][A-Z0-9_]{0,127}/) || !(table ==~ /[A-Z][A-Z0-9_]{0,127}/)))) {
        throw new IllegalArgumentException("InvalidRequest")
    }
    attempted = true
    try {
        odi = odiInstance
    } catch (MissingPropertyException ignored) {
        standalone = true
        String propsPath = args?.length ? args[0] : "odi_connection.properties"
        Properties props = new Properties()
        new FileInputStream(propsPath).withStream { props.load(it) }
        def pool = new PoolingAttributes()
        def master = new MasterRepositoryDbInfo(props.getProperty("master_jdbc"), props.getProperty("master_jdbc_driver"),
            props.getProperty("master_username"), props.getProperty("master_password").toCharArray(), pool)
        def work = new WorkRepositoryDbInfo(props.getProperty("work_name"), pool)
        odi = OdiInstance.createInstance(new OdiInstanceConfig(master, work))
        def auth = odi.getSecurityManager().createAuthentication(
            props.getProperty("odi_username"), props.getProperty("odi_password").toCharArray())
        odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    }
    def em = odi.getTransactionalEntityManager()
    payload = inspectIkm(em.getFinder(OdiIKM.class).findAll(), requested)
    if (payload.status == "passed" && owner != null) {
        def context = em.getFinder(OdiContext.class).findByCode(contextCode)
        def sourceResult = inspectSource(em.getFinder(OdiModel.class).findAll(), owner, table, context, contextCode)
        payload = sourceResult.status == "passed" ? payload + sourceResult : sourceResult
    } else if (payload.status == "passed") {
        payload.source = null
    }
} catch (Exception ignored) {
    payload = [status: "blocked", code: "repository_lookup_failed"]
} finally {
    if (standalone && odi != null) {
        try { odi.close() } catch (Exception ignored) {
            payload = [status: "blocked", code: "repository_lookup_failed"]
        }
    }
}
println JsonOutput.toJson(payload + [tool: "odi.mapping_preflight", side_effects: false,
    repository_connection_attempted: attempted])
