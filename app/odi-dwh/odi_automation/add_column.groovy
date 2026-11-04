/*
  ODI Datastore Column Add
  ========================
  Mevcut bir ODI datastore'a yeni kolon ekler.

  Parametreler (System Properties):
    -Dodi.model_code=DWH
    -Dodi.table_name=ODS_PRODUCTION_PRODUCT
    -Dodi.column_name=SKEY
    -Dodi.column_type=NUMBER
    -Dodi.column_length=0
    -Dodi.column_scale=0
    -Dodi.column_position=1   (opsiyonel, yoksa sona eklenir)
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

String modelCode  = System.getProperty("odi.model_code")
String tableName  = System.getProperty("odi.table_name")
String colName    = System.getProperty("odi.column_name")
String colType    = System.getProperty("odi.column_type") ?: "NUMBER"
int colLength     = Integer.parseInt(System.getProperty("odi.column_length") ?: "0")
int colScale      = Integer.parseInt(System.getProperty("odi.column_scale") ?: "0")
int colPosition   = Integer.parseInt(System.getProperty("odi.column_position") ?: "0")

if (!modelCode || !tableName || !colName) {
    throw new Exception("MISSING PARAMS: model_code, table_name, column_name zorunlu")
}

println "PARAMS: model=${modelCode}, table=${tableName}, col=${colName}, type=${colType}"

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()
    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

    OdiDataStore ds = dsFinder.findByName(tableName, modelCode)
    if (ds == null) throw new Exception("Datastore bulunamadi: ${tableName} in ${modelCode}")

    // Kolon zaten var mi kontrol et
    def existingCol = ds.getColumns().find { it.getName().equalsIgnoreCase(colName) }
    if (existingCol != null) {
        println "WARNING: Kolon zaten mevcut: ${colName} — atlanıyor"
        txnMgr.rollback(txnStatus)
    } else {
        // Pozisyon belirle
        if (colPosition <= 0) {
            colPosition = ds.getColumns().size() + 1
        }

        OdiColumn col = new OdiColumn(ds, colName)

        // Tip atama
        String upperType = colType.toUpperCase()
        boolean typeSet = false
        for (String tryType : [upperType, colType, colType.toLowerCase()]) {
            if (typeSet) break
            try {
                col.setDataTypeCode(tryType)
                typeSet = true
            } catch (Exception ignore) {}
        }

        if (typeSet) {
            if (colLength > 0) col.setLength(colLength)
            if (colScale > 0) col.setScale(colScale)
        } else {
            println "WARNING: Tip atanamadi: ${colType}"
        }

        col.setPosition(colPosition)

        txnMgr.commit(txnStatus)
        println "SUCCESS: Kolon ${colName} (${colType}) datastore ${tableName}'e eklendi (pos=${colPosition})"
    }

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"add_column","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
