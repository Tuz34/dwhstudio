/*
  ODI Datastore Column Remove
  ============================
  Mevcut bir ODI datastore'dan bir kolonu kaldirir.

  Parametreler (System Properties):
    -Dodi.model_code=DWH
    -Dodi.table_name=ODS_PRODUCTION_PRODUCT
    -Dodi.column_name=SKEY
*/

import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.finder.IOdiModelFinder
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
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
    String propsPath = args.length > 0 ? args[0] : "odi_connection.properties"
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

String modelCode = System.getProperty("odi.model_code")
String tableName = System.getProperty("odi.table_name")
String colName   = System.getProperty("odi.column_name")

if (!modelCode || !tableName || !colName) {
    throw new Exception("MISSING PARAMS: model_code, table_name, column_name zorunlu")
}

println "PARAMS: model=${modelCode}, table=${tableName}, col=${colName}"

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()
    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

    OdiDataStore ds = dsFinder.findByName(tableName, modelCode)
    if (ds == null) throw new Exception("Datastore bulunamadi: ${tableName} in ${modelCode}")

    def existingCol = ds.getColumns().find { it.getName().equalsIgnoreCase(colName) }
    if (existingCol == null) {
        println "WARNING: Kolon bulunamadi: ${colName} in ${tableName} — zaten yok, atlanıyor"
        txnMgr.rollback(txnStatus)
    } else {
        em.remove(existingCol)
        txnMgr.commit(txnStatus)
        println "SUCCESS: Kolon ${colName} datastore ${tableName}'den kaldirildi"
    }

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"remove_column","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
