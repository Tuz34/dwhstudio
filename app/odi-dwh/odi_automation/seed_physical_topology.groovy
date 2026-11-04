/*
  NL-DWH Studio: Idempotent ODI physical Oracle topology seed.
  Creates a missing data server, physical schema, and logical-to-physical
  context mapping for dev/TST. It never prints JDBC URLs, passwords, or raw data.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.topology.AbstractOdiDataServer
import oracle.odi.domain.topology.OdiContext
import oracle.odi.domain.topology.OdiContextualSchemaMapping
import oracle.odi.domain.topology.OdiDataServer
import oracle.odi.domain.topology.OdiLogicalSchema
import oracle.odi.domain.topology.OdiPhysicalSchema
import oracle.odi.domain.topology.OdiTechnology
import oracle.odi.domain.topology.finder.IOdiContextFinder
import oracle.odi.domain.topology.finder.IOdiContextualSchemaMappingFinder
import oracle.odi.domain.topology.finder.IOdiDataServerFinder
import oracle.odi.domain.topology.finder.IOdiLogicalSchemaFinder
import oracle.odi.domain.topology.finder.IOdiTechnologyFinder
import oracle.odi.domain.util.ObfuscatedString
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.persistence.transaction.ITransactionManager
import oracle.odi.core.persistence.transaction.ITransactionStatus
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition
import oracle.odi.core.security.Authentication

def checks = []
def odi = null
boolean standaloneMode = false

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) check.metadata = metadata
    checks << check
}

def safeMessage = { Throwable t ->
    String msg = t?.getMessage() ?: t?.toString() ?: ""
    msg = msg.replaceAll(/(?i)(password|passwd|pwd|token|secret)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    msg = msg.replaceAll(/jdbc:[^\s,)]+/, "jdbc:***")
    msg = msg.replaceAll(/(?i)(user(name)?|uid)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    return msg.take(240)
}

def loadEnvFile = { File file ->
    Map env = [:]
    if (!file.exists()) return env
    file.eachLine("UTF-8") { line ->
        String trimmed = line.trim()
        if (!trimmed || trimmed.startsWith("#") || !trimmed.contains("=")) return
        int idx = trimmed.indexOf("=")
        String key = trimmed.substring(0, idx).trim()
        String val = trimmed.substring(idx + 1).trim()
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
            val = val.substring(1, val.length() - 1)
        }
        env[key] = val
    }
    return env
}

def envValue = { Map env, String key ->
    String value = System.getenv(key)
    if (value == null || value.trim().length() == 0) value = env[key]
    return value
}

def normalizeJdbcUrl = { String dsn ->
    if (!dsn) return dsn
    if (dsn.toLowerCase().startsWith("jdbc:")) return dsn
    return "jdbc:oracle:thin:@" + dsn
}

try {
    String propsPath = null
    try {
        odi = odiInstance
    } catch (MissingPropertyException e) {
        standaloneMode = true
        propsPath = args?.length ? args[0] : "odi_connection.properties"
        Properties props = new Properties()
        new FileInputStream(propsPath).withStream { props.load(it) }
        PoolingAttributes poolAttrs = new PoolingAttributes()
        MasterRepositoryDbInfo masterRepo = new MasterRepositoryDbInfo(
            props.getProperty("master_jdbc"),
            props.getProperty("master_jdbc_driver"),
            props.getProperty("master_username"),
            props.getProperty("master_password").toCharArray(),
            poolAttrs
        )
        WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(props.getProperty("work_name"), poolAttrs)
        odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
        Authentication auth = odi.getSecurityManager().createAuthentication(
            props.getProperty("odi_username"),
            props.getProperty("odi_password").toCharArray()
        )
        odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    }

    String dataServerName = System.getProperty("odi.data_server")
    String schemaName = System.getProperty("odi.schema_name")
    String workSchemaName = System.getProperty("odi.work_schema_name") ?: schemaName
    String logicalSchemaName = System.getProperty("odi.logical_schema")
    String envPrefix = System.getProperty("odi.env_prefix")
    String contextCode = System.getProperty("odi.context") ?: "GLOBAL"
    String techName = System.getProperty("odi.technology") ?: "Oracle"
    if (!dataServerName || !schemaName || !logicalSchemaName || !envPrefix) {
        throw new Exception("MISSING PARAMS: data_server, schema_name, logical_schema and env_prefix are required")
    }

    File rootDir = propsPath ? new File(propsPath).getParentFile().getParentFile() : new File(".").getCanonicalFile()
    Map localEnv = loadEnvFile(new File(rootDir, ".env.dev"))
    String jdbcUser = envValue(localEnv, envPrefix + "_USER") ?: envValue(localEnv, "ORACLE_USER")
    String jdbcPassword = envValue(localEnv, envPrefix + "_PASSWORD") ?: envValue(localEnv, "ORACLE_PASSWORD")
    String jdbcDsn = envValue(localEnv, envPrefix + "_DSN") ?: envValue(localEnv, "ORACLE_DSN")
    if (!jdbcUser || !jdbcPassword || !jdbcDsn) {
        addCheck("local_connection_values_present", "fail", "Required local connection values are missing.", [env_prefix: envPrefix])
        throw new Exception("Required local connection values are missing for prefix: " + envPrefix)
    }
    addCheck("local_connection_values_present", "pass", "Required local connection values are present.", [env_prefix: envPrefix])

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

    try {
        def em = odi.getTransactionalEntityManager()

        IOdiTechnologyFinder techFinder = (IOdiTechnologyFinder) em.getFinder(OdiTechnology.class)
        OdiTechnology tech = techFinder.findAll().find { it.getName()?.equalsIgnoreCase(techName) }
        if (tech == null) throw new Exception("Technology not found: " + techName)
        addCheck("technology_found", "pass", "Technology found.", [technology: techName])

        def applyConnectionSettings = { OdiDataServer server ->
            server.setConnectionSettings(new AbstractOdiDataServer.JdbcSettings(normalizeJdbcUrl(jdbcDsn), "oracle.jdbc.OracleDriver"))
            server.setUsername(jdbcUser)
            server.setPassword(ObfuscatedString.obfuscate(jdbcPassword.toCharArray(), odi))
        }

        IOdiDataServerFinder dsFinder = (IOdiDataServerFinder) em.getFinder(OdiDataServer.class)
        OdiDataServer dataServer = dsFinder.findByName(dataServerName)
        if (dataServer == null) {
            dataServer = new OdiDataServer(tech, dataServerName)
            applyConnectionSettings(dataServer)
            em.persist(dataServer)
            addCheck("data_server_created", "pass", "Data server created.", [data_server: dataServerName])
        } else {
            applyConnectionSettings(dataServer)
            addCheck("data_server_connection_updated", "pass", "Data server already exists; connection settings refreshed.", [data_server: dataServerName])
        }

        OdiPhysicalSchema physicalSchema = null
        dataServer.getPhysicalSchemas().each { ps ->
            if (ps.getSchemaName()?.equalsIgnoreCase(schemaName)) physicalSchema = ps
        }
        if (physicalSchema == null) {
            physicalSchema = new OdiPhysicalSchema(dataServer)
            physicalSchema.setSchemaName(schemaName)
            physicalSchema.setWorkSchemaName(workSchemaName)
            physicalSchema.setAsDefault()
            em.persist(physicalSchema)
            addCheck("physical_schema_created", "pass", "Physical schema created.", [schema: schemaName, work_schema: workSchemaName])
        } else {
            addCheck("physical_schema_exists", "pass", "Physical schema already exists.", [schema: schemaName])
        }

        IOdiLogicalSchemaFinder lsFinder = (IOdiLogicalSchemaFinder) em.getFinder(OdiLogicalSchema.class)
        OdiLogicalSchema logicalSchema = null
        try { logicalSchema = lsFinder.findByName(logicalSchemaName) } catch (Exception ignored) {}
        if (logicalSchema == null) {
            logicalSchema = new OdiLogicalSchema(tech, logicalSchemaName)
            em.persist(logicalSchema)
            addCheck("logical_schema_created", "pass", "Logical schema created.", [logical_schema: logicalSchemaName])
        } else {
            addCheck("logical_schema_exists", "pass", "Logical schema already exists.", [logical_schema: logicalSchemaName])
        }

        IOdiContextFinder ctxFinder = (IOdiContextFinder) em.getFinder(OdiContext.class)
        OdiContext context = ctxFinder.findAll().find {
            it.getCode()?.equalsIgnoreCase(contextCode) || it.getName()?.equalsIgnoreCase(contextCode)
        }
        if (context == null) {
            context = ctxFinder.findAll().find {
                it.getCode()?.equalsIgnoreCase("DEV") || it.getName()?.toLowerCase()?.contains("development")
            }
        }
        if (context == null) throw new Exception("Context not found: " + contextCode)
        addCheck("context_found", "pass", "Context found.", [context: context.getCode() ?: context.getName()])

        IOdiContextualSchemaMappingFinder mappingFinder = (IOdiContextualSchemaMappingFinder) em.getFinder(OdiContextualSchemaMapping.class)
        OdiContextualSchemaMapping schemaMapping = null
        try { schemaMapping = mappingFinder.findByLogicalSchema(logicalSchemaName, context.getCode()) } catch (Exception ignored) {}
        if (schemaMapping == null) {
            schemaMapping = new OdiContextualSchemaMapping(context, logicalSchema, physicalSchema)
            em.persist(schemaMapping)
            addCheck("context_mapping_created", "pass", "Logical schema mapped to physical schema for context.", [logical_schema: logicalSchemaName, schema: schemaName])
        } else {
            def mappedPhysicalSchema = schemaMapping.getPhysicalSchema()
            String mappedSchemaName = mappedPhysicalSchema?.getSchemaName()
            String mappedDataServerName = null
            try {
                mappedDataServerName = mappedPhysicalSchema?.getDataServer()?.getName()
            } catch (Exception ignored) {
                try { mappedDataServerName = mappedPhysicalSchema?.getOdiDataServer()?.getName() } catch (Exception ignored2) {}
            }

            boolean pointsToRequestedPhysicalSchema =
                mappedSchemaName?.equalsIgnoreCase(schemaName) &&
                mappedDataServerName?.equalsIgnoreCase(dataServerName)

            if (pointsToRequestedPhysicalSchema) {
                addCheck("context_mapping_exists", "pass", "Context mapping already points to target physical schema.", [logical_schema: logicalSchemaName, data_server: dataServerName, schema: schemaName])
            } else {
                schemaMapping.setPhysicalSchema(physicalSchema)
                addCheck("context_mapping_updated", "pass", "Context mapping updated to target physical schema.", [logical_schema: logicalSchemaName, data_server: dataServerName, schema: schemaName])
            }
        }

        txnMgr.commit(txnStatus)
        addCheck("commit_completed", "pass", "Physical topology seed committed.")
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }

    def payload = [
        tool: "odi.seed_physical_topology",
        status: "passed",
        mode: "idempotent_physical_oracle_topology_seed",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("seed_physical_topology_exception", "fail", "ODI physical topology seed failed.")
    def payload = [
        tool: "odi.seed_physical_topology",
        status: "error",
        mode: "idempotent_physical_oracle_topology_seed",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
