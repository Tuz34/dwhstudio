/*
  ODI Mapping Deletion (Standalone Groovy SDK)
  =============================================
  Bir ODI mapping'i SDK uzerinden guvenli siler.
  Parametreler:
    -Dodi.project=TUTORIOL
    -Dodi.mapping=MAP_CUSTOMER
*/

import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.finder.IMappingFinder
import oracle.odi.domain.project.finder.IOdiProjectFinder
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
String projectName = System.getProperty("odi.project")
String folderName  = System.getProperty("odi.folder")
String mappingName = System.getProperty("odi.mapping")

println "PARAMS: project=${projectName}, folder=${folderName}, mapping=${mappingName}"

// ── Transaction ─────────────────────────────────────────────────────────
ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionDefinition txnDef = new DefaultTransactionDefinition()
ITransactionStatus txnStatus = txnMgr.getTransaction(txnDef)

try {
    def em = odi.getTransactionalEntityManager()
    IMappingFinder mapFinder = (IMappingFinder) em.getFinder(Mapping.class)
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)

    OdiProject project = projFinder.findByCode(projectName)
    if (project == null) throw new Exception("Project not found: " + projectName)

    // Folder bul
    OdiFolder folder = null
    for (f in project.getFolders()) {
        if (f.getName().equals(folderName)) {
            folder = f
            break
        }
    }
    if (folder == null) throw new Exception("Folder not found: " + folderName)

    // Mapping bul (findByName: folder, name)
    Mapping mapping = mapFinder.findByName(folder, mappingName)

    if (mapping == null) {
        println "WARNING: Mapping bulunamadi: ${mappingName} — zaten silinmis olabilir."
        txnMgr.rollback(txnStatus)
    } else {
        em.remove(mapping)
        txnMgr.commit(txnStatus)
        println "SUCCESS: Mapping ${mappingName} silindi."
    }

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (Exception ignore) {}
    println '{"status":"error","action":"delete_mapping","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
        println "INFO: Repository baglantisi kapatildi."
    }
}
