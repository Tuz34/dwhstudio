/*
  ODI-68 fixed absent-only import of one Oracle SDK reference IKM.
  The caller cannot choose the file, import mode, KM identity, or technology.
*/

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.security.Authentication
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiIKMFinder
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.impexp.IImportService
import oracle.odi.impexp.support.ImportServiceImpl
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition

final String IKM_NAME = "IKM Oracle Slowly Changing Dimension"
final String REFERENCE_FILE_NAME = "KM_IKM Oracle Slowly Changing Dimension.xml"
final String TARGET_TECHNOLOGY = "ORACLE"
final String INTEGRATION_TYPE = "SLOWLY_CHANGING_DIMENSION"

def odi = null
boolean standaloneMode = false
boolean importAttempted = false
def txnMgr = null
def txnStatus = null

def safeFlags = {
    [
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
    ]
}

def technologyName = { technology ->
    if (technology == null) return null
    try {
        String code = technology.getCode()
        if (code != null && !code.trim().isEmpty()) return code.toUpperCase()
    } catch (Exception ignored) {}
    try {
        String name = technology.getName()
        if (name != null && !name.trim().isEmpty()) return name.toUpperCase()
    } catch (Exception ignored) {}
    return null
}

def findExactIkm = { entityManager ->
    IOdiIKMFinder finder = (IOdiIKMFinder) entityManager.getFinder(OdiIKM.class)
    return (finder.findByName(IKM_NAME) ?: []).find {
        it.getName()?.equalsIgnoreCase(IKM_NAME)
    }
}

try {
    try {
        odi = odiInstance
    } catch (MissingPropertyException ignored) {
        standaloneMode = true
        String propsPath = args?.length ? args[0] : "odi_connection.properties"
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
            props.getProperty("work_name"),
            poolAttrs
        )
        odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
        Authentication auth = odi.getSecurityManager().createAuthentication(
            props.getProperty("odi_username"),
            props.getProperty("odi_password").toCharArray()
        )
        odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    }

    String projectCode = System.getProperty("odi.project")
    if (projectCode == null || projectCode.trim().isEmpty()) {
        throw new IllegalStateException("Fixed project context is unavailable")
    }

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    def em = odi.getTransactionalEntityManager()
    IOdiProjectFinder projectFinder =
        (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projectFinder.findAll().find {
        it.getCode()?.equalsIgnoreCase(projectCode)
    }
    if (project == null) {
        throw new IllegalStateException("Fixed project context is unavailable")
    }
    if (findExactIkm(em) != null) {
        throw new IllegalStateException("target IKM already exists")
    }

    String oracleHome = System.getProperty("odi.oracle_home")
    if (oracleHome == null || oracleHome.trim().isEmpty()) {
        throw new IllegalStateException("Oracle home is unavailable")
    }
    File referenceRoot = new File(oracleHome, "odi/sdk/xml-reference").getCanonicalFile()
    File referenceFile = new File(referenceRoot, REFERENCE_FILE_NAME).getCanonicalFile()
    if (referenceFile.getParentFile() != referenceRoot ||
        !referenceFile.isFile() || referenceFile.length() <= 0L) {
        throw new IllegalStateException("Fixed SDK reference file is unavailable")
    }

    ImportServiceImpl importService = new ImportServiceImpl(odi)
    importAttempted = true
    importService.importObjectFromXml(
        IImportService.IMPORT_MODE_DUPLICATION,
        referenceFile.getCanonicalPath(),
        project,
        true
    )

    OdiIKM imported = findExactIkm(odi.getTransactionalEntityManager())
    if (imported == null) {
        throw new IllegalStateException("Imported IKM was not found")
    }
    String technology = technologyName(
        imported.getTargetTechnology() ?: imported.getDefaultTargetTechnology()
    )
    if (technology != TARGET_TECHNOLOGY) {
        throw new IllegalStateException("Imported IKM technology mismatch")
    }
    if (imported.getIntegrationType() != INTEGRATION_TYPE) {
        throw new IllegalStateException("Imported IKM integration type mismatch")
    }
    txnMgr.commit(txnStatus)
    txnStatus = null

    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_ikm_import",
        status: "passed",
        mode: "fixed_sdk_reference_absent_only_ikm_import",
        side_effects: true,
        repository_connection_attempted: true,
        *: safeFlags(),
        result: [
            status: "passed",
            ikm_name: IKM_NAME,
            target_technology: TARGET_TECHNOLOGY,
            integration_type: INTEGRATION_TYPE,
            import_mode: "DUPLICATION",
            existing_before: false,
            import_attempted: true,
            post_import_verified: true,
        ],
    ])
} catch (Exception e) {
    if (txnMgr != null && txnStatus != null) {
        try {
            txnMgr.rollback(txnStatus)
        } catch (Exception ignored) {}
        txnStatus = null
    }
    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_ikm_import",
        status: "error",
        mode: "fixed_sdk_reference_absent_only_ikm_import",
        side_effects: importAttempted,
        repository_connection_attempted: true,
        *: safeFlags(),
        error_type: e.getClass().getSimpleName(),
        import_attempted: importAttempted,
        repository_state_may_have_changed: importAttempted,
    ])
} finally {
    if (txnMgr != null && txnStatus != null) {
        try {
            txnMgr.rollback(txnStatus)
        } catch (Exception ignored) {}
    }
    if (standaloneMode && odi != null) {
        odi.close()
    }
}
