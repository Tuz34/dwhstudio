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
*/

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

println "PARAMS: model=${modelCode}, table=${tableName}, resource=${resName}"
println "PARAMS: columns=${columnsStr}"

// ── Transaction ─────────────────────────────────────────────────────────
ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionDefinition txnDef = new DefaultTransactionDefinition()
ITransactionStatus txnStatus = txnMgr.getTransaction(txnDef)

try {
    def em = odi.getTransactionalEntityManager()

    IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

    // 1. Find the model
    OdiModel model = modelFinder.findByCode(modelCode)
    if (model == null) throw new Exception("Model not found: " + modelCode)
    println "STEP 1: Model found: ${model.getName()} (code=${modelCode})"

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
            }
            col.setPosition(pos)
            pos++
        }
        println "STEP 4: ${colDefs.length} columns added (UPPERCASE)"
    } else {
        println "STEP 4: No column information was provided — empty datastore"
    }

    // 5. Commit
    txnMgr.commit(txnStatus)
    println "SUCCESS: Datastore ${tableName} added to model ${modelCode} and committed."

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
