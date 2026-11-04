/*
  NL-DWH Studio: Inspect Topology (Universal User Extractor)
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
    IOdiDataServerFinder dsFinder = (IOdiDataServerFinder) em.getFinder(OdiDataServer.class)
    
    println "=== DATA SERVER SETTINGS ==="
    for (ds in dsFinder.findAll()) {
        println "NAME: ${ds.getName()} [${ds.getTechnology().getName()}]"
        
        // 12c SDK: User is in the ConnectionSettings object
        def connSettings = ds.getConnectionSettings()
        if (connSettings != null) {
            println "  JDBC Driver Present: ${connSettings.getDriverName() ? 'yes' : 'no'}"
            println "  JDBC URL Present   : ${connSettings.getJdbcUrl() ? 'yes' : 'no'}"
            // Use reflection or try catch to find the user property
            def userStr = "N/A"
            try { userStr = connSettings.getUserName() } catch (e) {
                try { userStr = ds.getUserName() } catch (e2) {}
            }
            println "  JDBC User Present  : ${userStr && userStr != 'N/A' ? 'yes' : 'no'}"
        }
        
        ds.getPhysicalSchemas().each { ps ->
            try {
               println "  PS: ${ps.getSchemaName()} / ${ps.getWorkSchemaName()}"
            } catch (ex) {}
        }
    }

} finally {
    if (standaloneMode && odi != null) odi.close()
}
