/*
  ODI Datastore Registration (Standalone Groovy SDK)
  ===================================================
  Mevcut bir ODI modeline yeni datastore (tablo) ve kolonlarini kaydeder.
  Reverse Engineering yerine programatik olusturma yapar.

  Parametreler (System Properties):
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

// ── OdiInstance Baglantisi ──────────────────────────────────────────────
def odi = null
boolean standaloneMode = false

try {
    odi = odiInstance
    println "INFO: ODI Studio baglantisi kullaniliyor."
} catch (MissingPropertyException e) {
    standaloneMode = true
    println "INFO: Standalone mod — repository baglantisi kuruluyor..."

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
    println "INFO: Repository baglantisi basarili."
}

// ── Parametreleri Al ────────────────────────────────────────────────────
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

    // 1. Model bul
    OdiModel model = modelFinder.findByCode(modelCode)
    if (model == null) throw new Exception("Model not found: " + modelCode)
    println "STEP 1: Model bulundu: ${model.getName()} (code=${modelCode})"

    // 2. Zaten var mi kontrol et → varsa sil ve yeniden olustur (Reverse Engineer simulasyonu)
    OdiDataStore existing = dsFinder.findByName(tableName, modelCode)
    if (existing != null) {
        println "INFO: Datastore mevcut: ${tableName} — kolonlar guncelleniyor (silinip yeniden yaratiliyor)."
        em.remove(existing)
        txnMgr.commit(txnStatus)
        txnStatus = txnMgr.getTransaction(txnDef)
        println "STEP 2: Eski Datastore silindi."
    }

    // 3. Datastore olustur
    OdiDataStore ds = new OdiDataStore(model, tableName)
    ds.setDefaultAlias(tableName.length() > 30 ? tableName.substring(0, 30) : tableName)
    ds.setResourceName(resName)
    em.persist(ds)
    println "STEP 3: Datastore olusturuldu: ${tableName}"

    // 4. Kolonlari ekle
    if (columnsStr != null && columnsStr.trim().length() > 0) {
        String[] colDefs = columnsStr.split(",")
        int pos = 1
        for (colDef in colDefs) {
            String[] parts = colDef.trim().split(":")
            String colName = parts[0].trim().toUpperCase()  // UPPERCASE zorunlu — duplicate onleme
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
                println "WARNING: Kolon ${colName} icin tip atanamadi: ${dataType}"
            }
            if (typeSet) {
                if (colLength > 0) col.setLength(colLength)
                if (colScale > 0) col.setScale(colScale)
            }
            col.setPosition(pos)
            pos++
        }
        println "STEP 4: ${colDefs.length} kolon eklendi (UPPERCASE)"
    } else {
        println "STEP 4: Kolon bilgisi verilmedi — bos datastore"
    }

    // 5. Commit
    txnMgr.commit(txnStatus)
    println "SUCCESS: Datastore ${tableName} modele ${modelCode} eklendi ve commit edildi."

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (Exception ignore) {}
    println '{"status":"error","action":"register_datastore","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
        println "INFO: Repository baglantisi kapatildi."
    }
}
