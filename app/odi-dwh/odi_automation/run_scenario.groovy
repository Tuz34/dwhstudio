/*
  ODI Scenario Runner (Standalone Groovy SDK)
  ============================================
  Mevcut bir ODI senaryosunu calistirir.
  Standalone modda internal agent kullanir.

  Parametreler:
    -Dodi.scenario.name=MAP_CUSTOMER_PHYSICAL
    -Dodi.scenario.version=001
    -Dodi.context=GLOBAL
*/

import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder
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
String runtimeUser = null
char[] runtimePassword = null

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
    runtimeUser = props.getProperty("odi_username")
    runtimePassword = props.getProperty("odi_password").toCharArray()
    odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    println "INFO: Repository baglantisi basarili."
}

// ── Parametreleri Al ────────────────────────────────────────────────────
String scenarioName = System.getProperty("odi.scenario.name")
String version      = System.getProperty("odi.scenario.version", "001")
String contextCode  = System.getProperty("odi.context", "GLOBAL")

println "PARAMS: scenario=${scenarioName}, version=${version}, context=${contextCode}"

// ── Senaryoyu Bul ve Calistir ─────────────────────────────────────────
ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionDefinition txnDef = new DefaultTransactionDefinition()
ITransactionStatus txnStatus = txnMgr.getTransaction(txnDef)

try {
    def em = odi.getTransactionalEntityManager()
    IOdiScenarioFinder sf = (IOdiScenarioFinder) em.getFinder(OdiScenario.class)

    OdiScenario scenario = sf.findLatestByName(scenarioName)
    if (scenario == null) {
        throw new Exception("Scenario not found: " + scenarioName)
    }
    println "STEP 1: Senaryo bulundu: ${scenario.getName()} v${scenario.getVersion()} (ID=${scenario.getInternalId()})"
    txnMgr.rollback(txnStatus) // read-only, sadece senaryo varligini dogruladik

    // ── Standalone modda OdiStartScenario ile calistir ──────────────
    // ODI SDK'da dogrudan senaryo calistirma:
    // oracle.odi.runtime.agent.invocation paketi internal agent icin kullanilir
    // Standalone modda en guvenilir yol: OdiStartScenario (StartScen) sinifi

    println "STEP 2: Senaryo calistiriliyor..."

    if (standaloneMode && (!runtimeUser || runtimePassword == null)) {
        throw new Exception("RuntimeAgent credentials are missing in the local ODI properties file")
    }

    try {
        def rtAgentClass = Class.forName("oracle.odi.runtime.agent.RuntimeAgent")
        def rtConstructor = rtAgentClass.getConstructor(OdiInstance.class, String.class, char[].class)
        def rtAgent = rtConstructor.newInstance(odi, runtimeUser, runtimePassword)

        def spClass = Class.forName("oracle.odi.runtime.agent.invocation.StartupParams")
        def sp = spClass.newInstance()

        def rtMethod = rtAgentClass.getMethod(
            "startScenario",
            String.class, String.class, spClass,
            String.class, String.class, Integer.class, String.class, Boolean.TYPE
        )

        def rtExecInfo = rtMethod.invoke(
            rtAgent,
            scenarioName, version, sp, null, contextCode, 5, null, true
        )

        println "STEP 3: Senaryo tamamlandi (RuntimeAgent)."
        if (rtExecInfo != null) {
            println "  Session ID: ${rtExecInfo.getSessionId()}"
            println "  Status: ${rtExecInfo.getSessionStatus()}"
            String status = rtExecInfo.getSessionStatus()?.toString()
            if (status != null && !status.equalsIgnoreCase("DONE")) {
                throw new Exception("Scenario finished with non-DONE status: ${status}")
            }
        }
        println "SUCCESS: Senaryo basariyla calistirildi."
    } catch (Exception rtEx) {
        throw new Exception("RuntimeAgent scenario execution failed: " + rtEx.getMessage(), rtEx)
    }

} catch (Exception e) {
    try { txnMgr.rollback(txnStatus) } catch (Exception ig) {}
    println '{"status":"error","action":"run_scenario","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
        println "INFO: Repository baglantisi kapatildi."
    }
}
