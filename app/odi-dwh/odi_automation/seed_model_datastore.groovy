/*
  NL-DWH Studio: Minimal ODI logical schema/model/datastore seed.
  Idempotently creates missing metadata without deleting existing objects.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.OdiKey
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.model.finder.IOdiModelFinder
import oracle.odi.domain.topology.OdiLogicalSchema
import oracle.odi.domain.topology.OdiTechnology
import oracle.odi.domain.topology.finder.IOdiTechnologyFinder
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

try {
    try {
        odi = odiInstance
    } catch (MissingPropertyException e) {
        standaloneMode = true
        String propsPath = args?.length ? args[0] : "odi_connection.properties"
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

    String modelCode = System.getProperty("odi.model_code")
    String modelName = System.getProperty("odi.model_name") ?: modelCode
    String logicalSchemaName = System.getProperty("odi.logical_schema") ?: "LGL_" + modelCode
    String tableName = System.getProperty("odi.table_name")
    String resourceName = System.getProperty("odi.resource_name") ?: tableName
    String columnsStr = System.getProperty("odi.columns") ?: ""
    String updateKeyName = System.getProperty("odi.update_key_name")
    String updateKeyColumnsStr = System.getProperty("odi.update_key_columns") ?: ""
    String techName = System.getProperty("odi.technology") ?: "Oracle"
    String techCode = System.getProperty("odi.tech_code") ?: "ORACLE"

    if (!modelCode || !tableName) {
        throw new Exception("MISSING PARAMS: odi.model_code and odi.table_name are required")
    }

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

    try {
        def em = odi.getTransactionalEntityManager()

        IOdiTechnologyFinder techFinder = (IOdiTechnologyFinder) em.getFinder(OdiTechnology.class)
        OdiTechnology tech = techFinder.findAll().find { it.getName()?.equalsIgnoreCase(techName) }
        if (tech == null) {
            throw new Exception("Technology not found: " + techName)
        }
        addCheck("technology_found", "pass", "Technology found.", [technology: techName])

        def lsFinder = em.getFinder(OdiLogicalSchema.class)
        OdiLogicalSchema logicalSchema = null
        try { logicalSchema = lsFinder.findByName(logicalSchemaName) } catch (Exception ignored) {}
        if (logicalSchema == null) {
            logicalSchema = new OdiLogicalSchema(tech, logicalSchemaName)
            em.persist(logicalSchema)
            addCheck("logical_schema_created", "pass", "Logical schema created.", [logical_schema: logicalSchemaName])
        } else {
            addCheck("logical_schema_exists", "pass", "Logical schema already exists.", [logical_schema: logicalSchemaName])
        }

        IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
        OdiModel model = modelFinder.findByCode(modelCode)
        if (model == null) {
            model = new OdiModel(logicalSchema, modelName, techCode)
            model.setCode(modelCode)
            em.persist(model)
            addCheck("model_created", "pass", "Model created.", [model: modelCode])
        } else {
            addCheck("model_exists", "pass", "Model already exists.", [model: modelCode])
        }

        IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
        OdiDataStore ds = dsFinder.findByName(tableName, modelCode)
        if (ds == null) {
            ds = new OdiDataStore(model, tableName)
            ds.setDefaultAlias(tableName.length() > 30 ? tableName.substring(0, 30) : tableName)
            ds.setResourceName(resourceName)
            em.persist(ds)
            addCheck("datastore_created", "pass", "Datastore created.", [model: modelCode, datastore: tableName])
        } else {
            addCheck("datastore_exists", "pass", "Datastore already exists.", [model: modelCode, datastore: tableName])
        }

        Set existingCols = [] as Set
        try {
            ds.getColumns().each { c -> existingCols << c.getName()?.toUpperCase() }
        } catch (Exception ignored) {}

        int addedCols = 0
        int seenCols = 0
        if (columnsStr.trim().length() > 0) {
            String[] colDefs = columnsStr.split(",")
            int pos = existingCols.size() + 1
            for (colDef in colDefs) {
                String[] parts = colDef.trim().split(":")
                if (parts.length == 0 || parts[0].trim().length() == 0) continue
                String colName = parts[0].trim().toUpperCase()
                seenCols++
                if (existingCols.contains(colName)) continue

                String dataType = parts.length > 1 ? parts[1].trim() : "VARCHAR2"
                int colLength = parts.length > 2 && parts[2].isInteger() ? Integer.parseInt(parts[2].trim()) : 0
                int colScale = parts.length > 3 && parts[3].isInteger() ? Integer.parseInt(parts[3].trim()) : 0
                OdiColumn col = new OdiColumn(ds, colName)
                boolean typeSet = false
                for (String tryType : [dataType.toUpperCase(), dataType, dataType.toLowerCase()]) {
                    if (typeSet) break
                    try {
                        col.setDataTypeCode(tryType)
                        typeSet = true
                    } catch (Exception ignored) {}
                }
                if (typeSet) {
                    try { col.setDataFormat(dataType.toUpperCase()) } catch (Exception ignored) {}
                    if (colLength > 0) col.setLength(colLength)
                    if (colScale > 0) col.setScale(colScale)
                }
                col.setPosition(pos)
                pos++
                addedCols++
            }
        }
        addCheck("columns_seeded", "pass", "Missing columns added without deleting existing columns.", [provided: seenCols, added: addedCols])

        if (updateKeyName || updateKeyColumnsStr.trim().length() > 0) {
            if (!updateKeyName || updateKeyColumnsStr.trim().length() == 0) {
                throw new Exception("Update key name and columns must be provided together")
            }
            if (!(updateKeyName ==~ /[A-Z][A-Z0-9_$#]{0,127}/)) {
                throw new Exception("Invalid update key name")
            }

            List<String> requestedKeyColumns = updateKeyColumnsStr.split(",")
                .collect { it.trim().toUpperCase() }
                .findAll { it.length() > 0 }
            if (requestedKeyColumns.isEmpty() || requestedKeyColumns.toSet().size() != requestedKeyColumns.size()) {
                throw new Exception("Invalid update key column list")
            }

            Map<String, OdiColumn> columnsByName = [:]
            ds.getColumns().each { col -> columnsByName[col.getName()?.toUpperCase()] = col }
            requestedKeyColumns.each { colName ->
                if (!columnsByName.containsKey(colName)) {
                    throw new Exception("Update key column is missing from datastore metadata: " + colName)
                }
            }

            OdiKey updateKey = ds.getKeys().find { it.getName()?.equalsIgnoreCase(updateKeyName) }
            if (updateKey == null) {
                updateKey = new OdiKey(ds, updateKeyName)
                updateKey.setKeyType(OdiKey.KeyType.ALTERNATE_KEY)
                updateKey.setActive(true)
                updateKey.setInDatabase(true)
                requestedKeyColumns.each { colName -> updateKey.addColumn(columnsByName[colName]) }
                em.persist(updateKey)
                addCheck("update_key_created", "pass", "Missing update key metadata created.", [key: updateKeyName])
            } else {
                addCheck("update_key_exists", "pass", "Update key metadata already exists.", [key: updateKeyName])
            }

            List<String> actualKeyColumns = updateKey.getColumns().collect { it.getName()?.toUpperCase() }
            if (actualKeyColumns != requestedKeyColumns) {
                throw new Exception("Existing update key columns do not match the approved contract")
            }
            if (updateKey.getKeyType() != OdiKey.KeyType.ALTERNATE_KEY) {
                throw new Exception("Existing update key type does not match the approved contract")
            }
            addCheck("update_key_verified", "pass", "Update key metadata matches the approved contract.", [key: updateKeyName, column_count: actualKeyColumns.size()])
        }

        txnMgr.commit(txnStatus)
        addCheck("commit_completed", "pass", "Idempotent model/datastore seed committed.")
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }

    def payload = [
        tool: "odi.seed_model_datastore",
        status: "passed",
        mode: "idempotent_logical_model_datastore_seed",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("seed_model_datastore_exception", "fail", "ODI model/datastore seed failed.")
    def payload = [
        tool: "odi.seed_model_datastore",
        status: "error",
        mode: "idempotent_logical_model_datastore_seed",
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
