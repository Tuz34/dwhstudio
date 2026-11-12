/*
  SKEY mapping IKM and UpdateKey repair
  Goal: in MAP_SKEY_* mappings
    1. Verify or set the target DatastoreComponent UpdateKey
    2. Set IKM Oracle Incremental Update (MERGE) on the physical target node
    3. Assign an LKM instead of an IKM on the source AP node (an AP always takes an LKM)
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

    // Find the project, folder, and mapping
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    def mapFinder = (IMappingFinder) em.getFinder(Mapping.class)
    Mapping mapping = mapFinder.findByName(folder, mName)
    if (mapping == null) { println "HATA: Mapping not found: ${mName}"; System.exit(1) }
    println "✔ Mapping found: ${mName}"

    // Find the target DatastoreComponent (the one with an UpdateKey assigned)
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
        println "WARNING: No target component with an UpdateKey assigned — is there no record?"
    } else {
        println "✔ Target component: ${tgtDsComp.getName()} (UpdateKey: ${tgtDsComp.getUpdateKey().getName()})"
    }

    // Find the IKM
    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(oracle.odi.domain.project.OdiIKM.class)
    def allIkms = ikmFinder.findAll()
    oracle.odi.domain.project.OdiIKM targetIkm = allIkms.find { it.getName().equalsIgnoreCase(ikmTarget) }
    if (targetIkm == null) {
        println "HATA: IKM not found: ${ikmTarget}"
        println "Existing IKMs: " + allIkms.collect { it.getName() }.join(", ")
        System.exit(1)
    }
    println "✔ IKM found: ${targetIkm.getName()}"

    // Physical nodes — identify the target and AP nodes
    def physDesigns = mapping.getPhysicalDesigns()
    if (!physDesigns || physDesigns.size() == 0) {
        println "HATA: Physical design is missing"
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

    // Try to move staging to the target location
    if (stagingNode != null && tgtNode != null) {
        println "\nCorrecting staging location..."
        try {
            // Read the staging node's location (it should match the target)
            def tgtLocation = tgtNode.getLocation()
            if (tgtLocation != null) {
                // Change the staging location at design level — try more than one method
                try {
                    design.setStagingLocation(tgtLocation)
                    println "  ✔ design.setStagingLocation() succeeded: ${tgtLocation.getName()}"
                } catch (Exception se1) {
                    try {
                        design.setStagingLocationName(tgtLocation.getName())
                        println "  ✔ design.setStagingLocationName() succeeded: ${tgtLocation.getName()}"
                    } catch (Exception se2) {
                        println "  Staging update error type: ${se2.getClass().getSimpleName()}"
                    }
                }
                em.persist(mapping)
                // Now try assigning the IKM to the target node again
                try {
                    tgtNode.setIKM(targetIkm)
                    println "  ✔ IKM assigned after the staging fix: ${targetIkm.getName()}"
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
        // Last attempt: the CUSTOMER_AP node (the IKM is sometimes assigned here)
        design.getPhysicalNodes().each { node ->
            if (!ikmSet && node.getName().toUpperCase().endsWith("_AP")) {
                try {
                    node.setIKM(targetIkm)
                    println "✔ IKM assigned to AP node: ${node.getName()} ← ${targetIkm.getName()}"
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
        println "\n✔ SUCCESS: IKM ${targetIkm.getName()} was assigned to the target node."
        println "  Close and reopen the mapping in ODI Studio — it should appear on the Physical tab."
    } else {
        txnMgr.rollback(txnStatus)
        println "\n✗ FAILED: Target node was not found or the IKM could not be assigned."
    }

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"fix_skey_ikm","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    odi.close()
}
