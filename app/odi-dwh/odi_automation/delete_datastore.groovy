/*
  ODI Datastore Deletion (Standalone Groovy SDK)
  ===============================================
  Deletes the datastore (table definition) in the given model from the ODI repository.
  Does not touch the physical Oracle table; it only removes the ODI model record.

  Parameters (System Properties):
    -Dodi.model_code=DWH
    -Dodi.table_name=DWH_ODS_PRODUCT
*/

import oracle.odi.domain.model.OdiDataStore
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
String modelCode = System.getProperty("odi.model_code")
String tableName = System.getProperty("odi.table_name")

if (!modelCode || !tableName) throw new Exception("MISSING PARAMS: odi.model_code and odi.table_name are required")

println "PARAMS: model=${modelCode}, table=${tableName}"

// ── Transaction ─────────────────────────────────────────────────────────
ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionDefinition txnDef = new DefaultTransactionDefinition()
ITransactionStatus txnStatus = txnMgr.getTransaction(txnDef)

try {
    def em = odi.getTransactionalEntityManager()

    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

    OdiDataStore ds = dsFinder.findByName(tableName, modelCode)
    if (ds == null) {
        println "WARNING: Datastore not found: ${tableName} in ${modelCode} — it may already have been deleted."
        txnMgr.rollback(txnStatus)
    } else {
        em.remove(ds)
        txnMgr.commit(txnStatus)
        println "SUCCESS: Datastore '${tableName}' deleted from model '${modelCode}'."
    }

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (Exception ignore) {}
    println '{"status":"error","action":"delete_datastore","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
        println "INFO: Repository connection closed."
    }
}
