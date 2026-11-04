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
    String pCode   = System.getProperty("odi.project") ?: "TUTORIOL"
    String fName   = System.getProperty("odi.folder")  ?: "First Folder"
    String mName   = System.getProperty("odi.mapping") ?: "MAP_SKEY_SALES_CUSTOMER"

    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    def mapFinder = (IMappingFinder) em.getFinder(Mapping.class)
    Mapping mapping = mapFinder.findByName(folder, mName)
    if (mapping == null) { println "HATA: Mapping not found: ${mName}"; System.exit(1) }
    println "✔ Mapping: ${mapping.getName()}"

    println "=== LOGICAL COMPONENTS ==="
    mapping.getAllComponents().each { comp ->
        println "[${comp.getClass().getSimpleName()}] ${comp.getName()}"
        if (comp instanceof DatastoreComponent) {
            def dsComp = (DatastoreComponent) comp
            println "  UpdateKey      : " + (dsComp.getUpdateKey()?.getName() ?: "NULL")
        }
    }

    println "\n=== PHYSICAL DESIGN ==="
    def designs = mapping.getPhysicalDesigns()
    if (designs && designs.size() > 0) {
        MapPhysicalDesign design = designs.get(0)
        try {
            println "  Staging Location : " + design.getStagingExecutionUnit()?.getLocation()?.getName()
        } catch (Exception e) {
            println "  Staging Location : ERR (${e.getClass().getSimpleName()})"
        }
        design.getPhysicalNodes().each { node ->
            println "\n  [NODE] ${node.getName()}"
            println "    isTargetNode : " + node.isTargetNode()
            println "    Technology   : " + node.getExecutionTechnology()?.getName()
            println "    Location     : " + node.getLocation()?.getName()
            println "    IKM          : " + (node.getIKM()?.getName() ?: "NONE")
            println "    LKM          : " + (node.getLKM()?.getName() ?: "NONE")
            
            node.getNodeAttributes().each { attr ->
                println "      - Attr: ${attr.getName()} | Ins=${attr.isInsertIndicator()} Upd=${attr.isUpdateIndicator()} | Expr: ${attr.getExpression()?.getText()?.take(40)}"
            }

            if (node.isTargetNode()) {
                println "    Compatible Target IKMs:"
                node.getMatchingTargetKMs().each { ikm -> println "      - " + ikm.getName() }
            }
        }
    }

    txnMgr.commit(txnStatus)
} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"diagnose_ikm","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    odi.close()
}
