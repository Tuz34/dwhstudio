/*
  ODI Datastore Registration (Standalone Groovy SDK)
  ===================================================
  Registers a new datastore (table) and its columns in an existing ODI model.
  Creates them programmatically instead of using Reverse Engineering.

  Parameters (System Properties):
    -Dodi.model_code=DWH
    -Dodi.table_name=ODS_CUSTOMER
    -Dodi.resource_name=ODS_CUSTOMER
    -Dodi.columns=COL1:VARCHAR2:50,COL2:NUMBER:10:0,COL3:TIMESTAMP:6
      Format: NAME:DATATYPE[:LENGTH[:SCALE]]
  Optional key registration (update key for IKM merge/update):
    -Dodi.key_columns=COL1,COL2     key columns (must exist on the datastore)
    -Dodi.key_name=UK_ODS_CUSTOMER_COL1
    -Dodi.key_type=ALTERNATE_KEY    or PRIMARY_KEY
    -Dodi.key_only=true             add the key to an EXISTING datastore, never recreate it
*/

import groovy.json.JsonOutput
import oracle.odi.domain.model.OdiKey

import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.finder.IOdiModelFinder
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.ITransactionManager
import oracle.odi.core.persistence.transaction.ITransactionDefinition
import oracle.odi.core.persistence.transaction.ITransactionStatus
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition

// ── OdiInstance connection ──────────────────────────────────────────────
def odi = null
boolean standaloneMode = false

try {
    odi = odiInstance
    println "INFO: Using the ODI Studio connection."
} catch (MissingPropertyException e) {
    standaloneMode = true
    println "INFO: Standalone mode — opening repository connection..."

    String propsPath = args.length > 0 ? args[0] : "odi_connection.properties"
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
    WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(
        props.getProperty("work_name"), poolAttrs
    )

    OdiInstanceConfig config = new OdiInstanceConfig(masterRepo, workRepo)
    odi = OdiInstance.createInstance(config)

    Authentication auth = odi.getSecurityManager().createAuthentication(
        props.getProperty("odi_username"),
        props.getProperty("odi_password").toCharArray()
    )
    odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    println "INFO: Repository connection succeeded."
}

// ── Read parameters ─────────────────────────────────────────────────────
String modelCode   = System.getProperty("odi.model_code")
String tableName   = System.getProperty("odi.table_name")
String resName     = System.getProperty("odi.resource_name", tableName)
String columnsStr  = System.getProperty("odi.columns")
String keyColumnsStr = System.getProperty("odi.key_columns")
String keyName     = System.getProperty("odi.key_name")
String keyTypeStr  = System.getProperty("odi.key_type", "ALTERNATE_KEY")
String keyOnlyStr  = System.getProperty("odi.key_only", "false")

println "PARAMS: model=${modelCode}, table=${tableName}, resource=${resName}"
println "PARAMS: columns=${columnsStr}"
boolean keyRequested = keyColumnsStr != null && keyColumnsStr.trim().length() > 0
boolean keyOnly = "true".equalsIgnoreCase(keyOnlyStr)

// ── Transaction ─────────────────────────────────────────────────────────
ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionDefinition txnDef = new DefaultTransactionDefinition()
ITransactionStatus txnStatus = txnMgr.getTransaction(txnDef)

try {
    def em = odi.getTransactionalEntityManager()

    IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

    // Registers the ODI key on a datastore. Idempotent: an existing key with the
    // same columns is accepted, a same-named key with other columns fails closed.
    def registerKey = { OdiDataStore targetDs ->
        if (!(keyName ==~ /[A-Za-z][A-Za-z0-9_$#]{0,127}/)) throw new Exception("Invalid key name")
        List<String> wanted = []
        keyColumnsStr.split(",").each { raw ->
            String colName = raw.trim().toUpperCase(Locale.ROOT)
            if (!(colName ==~ /[A-Z][A-Z0-9_$#]{0,127}/)) throw new Exception("Invalid key column")
            if (!wanted.contains(colName)) wanted.add(colName)
        }
        OdiKey.KeyType keyType = OdiKey.KeyType.valueOf(keyTypeStr.trim().toUpperCase(Locale.ROOT))
        if (keyType != OdiKey.KeyType.ALTERNATE_KEY && keyType != OdiKey.KeyType.PRIMARY_KEY) {
            throw new Exception("Unsupported key type")
        }
        Map<String, OdiColumn> byName = [:]
        targetDs.getColumns().each { c -> byName[c.getName()?.toUpperCase(Locale.ROOT)] = c }
        wanted.each { colName ->
            if (!byName.containsKey(colName)) throw new Exception("Key column is missing from datastore")
        }
        boolean created = false
        OdiKey key = targetDs.getKeys().find { it.getName()?.equalsIgnoreCase(keyName) }
        if (key == null) {
            def matchingColumns = targetDs.getKeys().findAll { k ->
                (k.getColumns().collect { it.getName()?.toUpperCase(Locale.ROOT) } as Set) == (wanted as Set)
            }
            key = matchingColumns.find { k -> k.getKeyType() == keyType && k.isActive() && k.isInDatabase() }
                ?: matchingColumns.find { true }
        }
        if (key != null && key.getKeyType() != keyType) {
            // Never replace or retype an existing key: fail closed with a structured code.
            println '{"status":"error","action":"register_datastore","error_code":"update_key_kind_mismatch"}'
            throw new Exception("Existing key type does not match the requested key kind")
        }
        if (key != null && (!key.isActive() || !key.isInDatabase())) {
            println '{"status":"error","action":"register_datastore","error_code":"update_key_metadata_inactive"}'
            throw new Exception("Existing key must be active and marked in database")
        }
        if (key == null) {
            key = new OdiKey(targetDs, keyName)
            key.setKeyType(keyType)
            key.setActive(true)
            key.setInDatabase(true)
            wanted.each { colName -> key.addColumn(byName[colName]) }
            em.persist(key)
            created = true
        }
        List<String> actual = key.getColumns().collect { it.getName()?.toUpperCase(Locale.ROOT) }
        if ((actual as Set) != (wanted as Set)) throw new Exception("Existing key columns do not match")
        println "STEP KEY: key ${created ? 'created' : 'already present'}: ${key.getName()}"
        return [name: key.getName(), created: created, column_count: actual.size(),
            key_type: key.getKeyType().name(), active: key.isActive(), in_database: key.isInDatabase()]
    }

    // 1. Find the model
    OdiModel model = modelFinder.findByCode(modelCode)
    if (model == null) throw new Exception("Model not found: " + modelCode)
    println "STEP 1: Model found: ${model.getName()} (code=${modelCode})"

    if (keyOnly) {
        // Key-only mode: the datastore must already exist and is never removed or recreated.
        if (!keyRequested) throw new Exception("odi.key_only requires odi.key_columns")
        OdiDataStore existingDs = dsFinder.findByName(tableName, modelCode)
        if (existingDs == null) throw new Exception("Datastore not found for key registration")
        def keyInfo = registerKey(existingDs)
        txnMgr.commit(txnStatus)
        txnStatus = null
        println "SUCCESS: Key registered on existing datastore ${tableName} in model ${modelCode}."
        println JsonOutput.toJson([
            tool: "odi.register_datastore",
            status: "passed",
            mode: "key_only",
            side_effects: keyInfo.created,
            key: keyInfo
        ])
        return
    }

    // 2. Check whether it already exists; if so, delete and recreate it (Reverse Engineer simulation)
    OdiDataStore existing = dsFinder.findByName(tableName, modelCode)
    if (existing != null) {
        println "INFO: Datastore exists: ${tableName} — columns are being refreshed (deleted and recreated)."
        em.remove(existing)
        txnMgr.commit(txnStatus)
        txnStatus = txnMgr.getTransaction(txnDef)
        println "STEP 2: Previous datastore deleted."
    }

    // 3. Create the datastore
    OdiDataStore ds = new OdiDataStore(model, tableName)
    ds.setDefaultAlias(tableName.length() > 30 ? tableName.substring(0, 30) : tableName)
    ds.setResourceName(resName)
    em.persist(ds)
    println "STEP 3: Datastore created: ${tableName}"

    // 4. Add the columns
    if (columnsStr != null && columnsStr.trim().length() > 0) {
        String[] colDefs = columnsStr.split(",")
        int pos = 1
        for (colDef in colDefs) {
            String[] parts = colDef.trim().split(":")
            String colName = parts[0].trim().toUpperCase()  // UPPERCASE required — prevents duplicates
            String dataType = parts.length > 1 ? parts[1].trim() : "VARCHAR2"
            int colLength = parts.length > 2 ? Integer.parseInt(parts[2].trim()) : 0
            int colScale = parts.length > 3 ? Integer.parseInt(parts[3].trim()) : 0

            OdiColumn col = new OdiColumn(ds, colName)
            boolean typeSet = false
            String upperType = dataType.toUpperCase()
            for (String tryType : [upperType, dataType, dataType.toLowerCase()]) {
                if (typeSet) break
                try {
                    col.setDataTypeCode(tryType)
                    typeSet = true
                } catch (Exception ignore) {}
            }
            if (!typeSet) {
                println "WARNING: Could not assign a type for column ${colName}: ${dataType}"
            }
            if (typeSet) {
                if (colLength > 0) col.setLength(colLength)
                if (colScale > 0) col.setScale(colScale)
                if (upperType == "NUMBER" && parts.length == 2) {
                    // Oracle's unconstrained NUMBER sentinel overrides OdiColumn's default length 50.
                    col.setLength(0)
                    col.setScale(-127)
                }
            }
            col.setPosition(pos)
            pos++
        }
        println "STEP 4: ${colDefs.length} columns added (UPPERCASE)"
    } else {
        println "STEP 4: No column information was provided — empty datastore"
    }

    // 4b. Optional key (update key for IKM merge/update)
    def keyInfo = null
    if (keyRequested) {
        keyInfo = registerKey(ds)
    }

    // 5. Commit
    txnMgr.commit(txnStatus)
    println "SUCCESS: Datastore ${tableName} added to model ${modelCode} and committed."
    if (keyInfo != null) {
        println JsonOutput.toJson([
            tool: "odi.register_datastore",
            status: "passed",
            mode: "register_with_key",
            side_effects: true,
            key: keyInfo
        ])
    }

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (Exception ignore) {}
    println '{"status":"error","action":"register_datastore","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
        println "INFO: Repository connection closed."
    }
}
