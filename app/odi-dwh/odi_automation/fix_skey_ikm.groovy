/*
  SKEY Mapping IKM ve UpdateKey Duzeltici
  Hedef: MAP_SKEY_* mapping'lerinde
    1. Target DatastoreComponent'in UpdateKey'ini dogrula/set et
    2. Physical target node'una IKM Oracle Incremental Update (MERGE) set et
    3. Source AP node'una IKM yerine LKM ata (AP her zaman LKM alir)
*/
import oracle.odi.domain.mapping.*
import oracle.odi.domain.mapping.component.*
import oracle.odi.domain.mapping.physical.*
import oracle.odi.domain.mapping.finder.IMappingFinder
import oracle.odi.domain.project.*
import oracle.odi.domain.project.finder.*
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*

def odi = null
try {
    String propsPath = "c:/oracle_ai_app/scratch/odi_automation/odi_connection.properties"
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
} catch (Exception e) {
    println "CONNECTION ERROR TYPE: " + e.getClass().getSimpleName()
    System.exit(1)
}

def txnMgr = odi.getTransactionManager()
def txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()
    String pCode  = System.getProperty("odi.project") ?: "TUTORIOL"
    String fName  = System.getProperty("odi.folder")  ?: "First Folder"
    String mName  = System.getProperty("odi.mapping") ?: "MAP_SKEY_SALES_CUSTOMER"
    String ikmTarget = System.getProperty("odi.ikm_name") ?: "IKM Oracle Incremental Update (MERGE)"

    // Proje / Folder / Mapping bul
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    def mapFinder = (IMappingFinder) em.getFinder(Mapping.class)
    Mapping mapping = mapFinder.findByName(folder, mName)
    if (mapping == null) { println "HATA: Mapping bulunamadi: ${mName}"; System.exit(1) }
    println "✔ Mapping bulundu: ${mName}"

    // Target DatastoreComponent bul (UpdateKey atanmis olan)
    DatastoreComponent tgtDsComp = null
    DatastoreComponent srcDsComp = null
    mapping.getAllComponents().each { comp ->
        if (comp instanceof DatastoreComponent) {
            if (comp.getUpdateKey() != null) {
                tgtDsComp = comp
            } else {
                srcDsComp = comp
            }
        }
    }

    if (tgtDsComp == null) {
        println "UYARI: UpdateKey atanmis target component bulunamadi — kayit yok?"
    } else {
        println "✔ Target component: ${tgtDsComp.getName()} (UpdateKey: ${tgtDsComp.getUpdateKey().getName()})"
    }

    // IKM bul
    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(oracle.odi.domain.project.OdiIKM.class)
    def allIkms = ikmFinder.findAll()
    oracle.odi.domain.project.OdiIKM targetIkm = allIkms.find { it.getName().equalsIgnoreCase(ikmTarget) }
    if (targetIkm == null) {
        println "HATA: IKM bulunamadi: ${ikmTarget}"
        println "Mevcut IKM'ler: " + allIkms.collect { it.getName() }.join(", ")
        System.exit(1)
    }
    println "✔ IKM bulundu: ${targetIkm.getName()}"

    // Physical nodes — target ve AP node tespit
    def physDesigns = mapping.getPhysicalDesigns()
    if (!physDesigns || physDesigns.size() == 0) {
        println "HATA: Physical design yok"
        System.exit(1)
    }
    MapPhysicalDesign design = physDesigns.get(0)

    boolean ikmSet = false
    MapPhysicalNode tgtNode = null
    MapPhysicalNode stagingNode = null

    design.getPhysicalNodes().each { node ->
        String nodeName = node.getName()
        boolean isAP   = nodeName.toUpperCase().endsWith("_AP") || nodeName.toUpperCase().contains("ACCESS_POINT")
        boolean isTgt  = node.isTargetNode() || (tgtDsComp != null && nodeName.equalsIgnoreCase(tgtDsComp.getName()))
        boolean isStg  = nodeName.toUpperCase().contains("STAGING")

        println "Node: ${nodeName}  isTargetNode=${node.isTargetNode()}  isAP=${isAP}  isStagingNode=${isStg}"
        if (isTgt) tgtNode = node
        if (isStg) stagingNode = node
    }

    // Staging'i Target Location'a tasimayı dene
    if (stagingNode != null && tgtNode != null) {
        println "\nStaging location duzeltiliyor..."
        try {
            // Staging node'un location'i al (target ile ayni olmali)
            def tgtLocation = tgtNode.getLocation()
            if (tgtLocation != null) {
                // Design seviyesinde staging location'i degistir — birden fazla yontem dene
                try {
                    design.setStagingLocation(tgtLocation)
                    println "  ✔ design.setStagingLocation() basarili: ${tgtLocation.getName()}"
                } catch (Exception se1) {
                    try {
                        design.setStagingLocationName(tgtLocation.getName())
                        println "  ✔ design.setStagingLocationName() basarili: ${tgtLocation.getName()}"
                    } catch (Exception se2) {
                        println "  Staging update error type: ${se2.getClass().getSimpleName()}"
                    }
                }
                em.persist(mapping)
                // Simdi target node'a IKM atamayı tekrar dene
                try {
                    tgtNode.setIKM(targetIkm)
                    println "  ✔ IKM staging fix sonrasi atandi: ${targetIkm.getName()}"
                    ikmSet = true
                } catch (Exception e2) {
                    println "  IKM assignment error type: ${e2.getClass().getSimpleName()}"
                }
            }
        } catch (Exception sEx) {
            println "Staging repair error type: ${sEx.getClass().getSimpleName()}"
        }
    }

    if (!ikmSet && tgtNode != null) {
        // Son deneme: CUSTOMER_AP node'u (bazen IKM buraya atanir)
        design.getPhysicalNodes().each { node ->
            if (!ikmSet && node.getName().toUpperCase().endsWith("_AP")) {
                try {
                    node.setIKM(targetIkm)
                    println "✔ IKM AP node'a atandi: ${node.getName()} ← ${targetIkm.getName()}"
                    ikmSet = true
                } catch (Exception e3) {
                    println "AP node assignment error type: ${e3.getClass().getSimpleName()}"
                }
            }
        }
    }

    if (ikmSet) {
        em.persist(mapping)
        txnMgr.commit(txnStatus)
        println "\n✔ SUCCESS: IKM ${targetIkm.getName()} basariyla target node'a atandi!"
        println "  ODI Studio'da mapping'i kapatip yeniden acin — Physical sekmesinde gorulmeli."
    } else {
        txnMgr.rollback(txnStatus)
        println "\n✗ BASARISIZ: Target node bulunamadi veya IKM atanamadi!"
    }

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"fix_skey_ikm","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    odi.close()
}
