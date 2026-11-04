import oracle.odi.domain.project.*
import oracle.odi.domain.mapping.*
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication

def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
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
}

try {
    def em = odi.getTransactionalEntityManager()
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    
    println "=== ODI INVENTORY ==="
    for (proj in projFinder.findAll()) {
        println "PROJECT: ${proj.getName()} [CODE: ${proj.getCode()}]"
        for (folder in proj.getFolders()) {
            println "  FOLDER: ${folder.getName()}"
            // Use getMappings() directly on folder
            for (mapping in folder.getMappings()) {
                println "    MAPPING: ${mapping.getName()}"
            }
        }
    println "  IKMs:"
    proj.getIKMs().each { ikm -> println "    - ${ikm.getName()}" }
    println "  LKMs:"
    proj.getLKMs().each { lkm -> println "    - ${lkm.getName()}" }
    println "  CKMs:"
    proj.getCKMs().each { ckm -> println "    - ${ckm.getName()}" }
    }

} finally {
    if (standaloneMode && odi != null) odi.close()
}
