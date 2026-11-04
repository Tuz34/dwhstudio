/*
  NL-DWH Studio: Topology Update (Attempt 3)
*/

import oracle.odi.domain.topology.*
import oracle.odi.domain.topology.finder.*
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
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
}

def txnMgr = odi.getTransactionManager()
def txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()
    IOdiDataServerFinder dsFinder = (IOdiDataServerFinder) em.getFinder(OdiDataServer.class)
    
    // Parametrelerden sifre al
    String serverName = System.getProperty("odi.server_name")
    String newPassword = System.getProperty("odi.new_password")

    if (!serverName || !newPassword) {
        throw new Exception("MISSING PARAMS: odi.server_name ve odi.new_password zorunlu")
    }

    OdiDataServer ds = dsFinder.findByName(serverName)
    if (ds == null) {
        throw new Exception("Data server bulunamadi: " + serverName)
    }

    println "UPDATE: ${serverName}"
    // ODI 12c: setPassword(ObfuscatedString)
    ds.setPassword(oracle.odi.domain.util.ObfuscatedString.obfuscate(newPassword.toCharArray()))

    txnMgr.commit(txnStatus)
    println "SUCCESS: Topology updated with PasswordString."

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"update_topology_pass","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
