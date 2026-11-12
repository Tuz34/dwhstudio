/*
  ODI Topology Setup: Physical Schema + Logical Schema + Context Mapping + Model
  ==============================================================================
  Creates a new physical schema, logical schema,
  context mapping, and model under an existing data server.

  Parameters (System Properties):
    -Dodi.data_server=ORACLE_SOURCE
    -Dodi.schema_name=HR
    -Dodi.catalog_name=
    -Dodi.logical_schema=LGL_ORACLE_HR
    -Dodi.model_name=ORACLE_HR
    -Dodi.model_code=ORACLE_HR
    -Dodi.context=GLOBAL
    -Dodi.technology=Oracle
*/

import oracle.odi.domain.topology.*
import oracle.odi.domain.topology.finder.*
import oracle.odi.domain.model.*
import oracle.odi.domain.model.finder.*
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition

def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
    String propsPath = args?.length ? args[0] : "odi_connection.properties"
    Properties props = new Properties()
    new FileInputStream(propsPath).withStream { props.load(it) }
    PoolingAttributes poolAttrs = new PoolingAttributes()
    MasterRepositoryDbInfo masterRepo = new MasterRepositoryDbInfo(
        props.getProperty("master_jdbc"), props.getProperty("master_jdbc_driver"),
        props.getProperty("master_username"), props.getProperty("master_password").toCharArray(), poolAttrs
    )
    WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(props.getProperty("work_name"), poolAttrs)
    odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
    Authentication auth = odi.getSecurityManager().createAuthentication(
        props.getProperty("odi_username"), props.getProperty("odi_password").toCharArray()
    )
    odi.getSecurityManager().setCurrentThreadAuthentication(auth)
}

// Read the parameters
String dataServerName = System.getProperty("odi.data_server")
String schemaName     = System.getProperty("odi.schema_name")
String catalogName    = System.getProperty("odi.catalog_name")
String lschemaName    = System.getProperty("odi.logical_schema")
String modelName      = System.getProperty("odi.model_name")
String modelCode      = System.getProperty("odi.model_code")
String contextCode    = System.getProperty("odi.context") ?: "GLOBAL"
String techName       = System.getProperty("odi.technology") ?: "Oracle"
String techCode       = System.getProperty("odi.tech_code") ?: "ORACLE"

if (!dataServerName || !schemaName || !lschemaName || !modelName || !modelCode) {
    throw new Exception("MISSING PARAMS: data_server, schema_name, logical_schema, model_name, model_code are required")
}

println "PARAMS: server=${dataServerName}, schema=${schemaName}, lschema=${lschemaName}, model=${modelName}"

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()

    // 1. Find the data server
    IOdiDataServerFinder dsFinder = (IOdiDataServerFinder) em.getFinder(OdiDataServer.class)
    OdiDataServer dataServer = dsFinder.findByName(dataServerName)
    if (dataServer == null) throw new Exception("Data server not found: " + dataServerName)
    println "STEP 1: Data server found: ${dataServerName}"

    // 2. Find the technology
    IOdiTechnologyFinder techFinder = (IOdiTechnologyFinder) em.getFinder(OdiTechnology.class)
    OdiTechnology tech = null
    techFinder.findAll().each { t ->
        if (t.getName().equalsIgnoreCase(techName)) {
            tech = t
        }
    }
    if (tech == null) throw new Exception("Technology not found: " + techName)
    println "STEP 2: Technology found: ${tech.getName()}"

    // 3. Create or find the physical schema
    OdiPhysicalSchema pSchema = null
    dataServer.getPhysicalSchemas().each { ps ->
        if (ps.getSchemaName()?.equalsIgnoreCase(schemaName)) {
            pSchema = ps
        }
    }

    if (pSchema != null) {
        println "STEP 3: Physical schema already exists: ${schemaName} — skipped"
    } else {
        // ODI 12c: OdiPhysicalSchema(dataServer, schemaName, catalogName) or setters
        try {
            pSchema = new OdiPhysicalSchema(dataServer, schemaName, catalogName)
        } catch (Exception ce1) {
            try {
                // Alternative: schema only
                pSchema = new OdiPhysicalSchema(dataServer)
                pSchema.setSchemaName(schemaName)
                try { pSchema.setCatalogName(catalogName) } catch (Exception ignore) {}
            } catch (Exception ce2) {
                throw new Exception("Could not create physical schema: ${ce1.message} / ${ce2.message}")
            }
        }
        em.persist(pSchema)
        println "STEP 3: Physical schema created: ${schemaName}"
    }

    // 4. Create or find the logical schema
    def lsFinder = em.getFinder(OdiLogicalSchema.class)
    OdiLogicalSchema lSchema = null
    try {
        lSchema = lsFinder.findByName(lschemaName)
    } catch (Exception ignore) {}

    if (lSchema != null) {
        println "STEP 4: Logical schema already exists: ${lschemaName} — skipped"
    } else {
        lSchema = new OdiLogicalSchema(tech, lschemaName)
        em.persist(lSchema)
        println "STEP 4: Logical schema created: ${lschemaName}"
    }

    // 5. Create the context mapping (logical to physical)
    IOdiContextFinder ctxFinder = (IOdiContextFinder) em.getFinder(OdiContext.class)
    OdiContext ctx = null
    ctxFinder.findAll().each { c ->
        if (c.getCode()?.equalsIgnoreCase(contextCode) || c.getName()?.equalsIgnoreCase(contextCode)) {
            ctx = c
        }
    }

    if (ctx == null) {
        // Try the Development context
        ctxFinder.findAll().each { c ->
            if (c.getName()?.toLowerCase()?.contains("development") || c.getCode()?.equalsIgnoreCase("DEV")) {
                ctx = c
            }
        }
    }

    if (ctx != null) {
        try {
            OdiContextualSchemaMapping mapping = new OdiContextualSchemaMapping(ctx, lSchema, pSchema)
            em.persist(mapping)
            println "STEP 5: Context mapping created: ${ctx.getName()} -> ${lschemaName} -> ${schemaName}"
        } catch (Exception cme) {
            println "STEP 5: Context mapping already exists or failed: ${cme.getClass().getSimpleName()}"
        }
    } else {
        println "WARNING: Context not found (${contextCode}) — context mapping skipped"
    }

    // 6. Create or find the model
    IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
    OdiModel model = modelFinder.findByCode(modelCode)

    if (model != null) {
        println "STEP 6: Model already exists: ${modelCode} — skipped"
    } else {
        // ODI 12c: OdiModel(logicalSchema, modelName, technologyCode)
        // technologyCode = TECH_INT_NAME (for example MICROSOFT_SQL_SERVER, ORACLE)
        // tech.getName() contains spaces and raises ODI-17664 — use techCode
        try {
            model = new OdiModel(lSchema, modelName, techCode)
        } catch (Exception me1) {
            // Fallback: try tech.getName() instead of techCode
            try {
                model = new OdiModel(lSchema, modelName, tech.getName())
            } catch (Exception me2) {
                throw new Exception("Could not create model: techCode=${techCode} error=${me1.message} / techName error=${me2.message}")
            }
        }
        model.setCode(modelCode)
        em.persist(model)
        println "STEP 6: Model created: ${modelName} (code=${modelCode})"
    }

    txnMgr.commit(txnStatus)
    println "SUCCESS: Topology setup finished — ${dataServerName}.${schemaName} -> ${lschemaName} -> ${modelName}"

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"setup_topology","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
