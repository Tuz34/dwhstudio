/*
  NL-DWH Studio: Minimal ODI project/folder seed.
  Idempotently creates missing project/folder metadata in dev/TST WORKREP.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.persistence.transaction.ITransactionManager
import oracle.odi.core.persistence.transaction.ITransactionStatus
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition
import oracle.odi.core.security.Authentication

def checks = []
def odi = null
boolean standaloneMode = false

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) check.metadata = metadata
    checks << check
}

def safeMessage = { Throwable t ->
    String msg = t?.getMessage() ?: t?.toString() ?: ""
    msg = msg.replaceAll(/(?i)(password|passwd|pwd|token|secret)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    msg = msg.replaceAll(/jdbc:[^\s,)]+/, "jdbc:***")
    msg = msg.replaceAll(/(?i)(user(name)?|uid)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    return msg.take(240)
}

try {
    try {
        odi = odiInstance
    } catch (MissingPropertyException e) {
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
        WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(props.getProperty("work_name"), poolAttrs)
        odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
        Authentication auth = odi.getSecurityManager().createAuthentication(
            props.getProperty("odi_username"),
            props.getProperty("odi_password").toCharArray()
        )
        odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    }

    String projectCode = System.getProperty("odi.project")
    String projectName = System.getProperty("odi.project_name") ?: projectCode
    String folderName = System.getProperty("odi.folder")
    if (!projectCode || !folderName) {
        throw new Exception("MISSING PARAMS: odi.project and odi.folder are required")
    }

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

    try {
        def em = odi.getTransactionalEntityManager()
        IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
        OdiProject project = projFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }

        if (project == null) {
            project = new OdiProject(projectCode, projectName)
            em.persist(project)
            addCheck("project_created", "pass", "Project created.", [project: projectCode])
        } else {
            addCheck("project_exists", "pass", "Project already exists.", [project: projectCode])
        }

        OdiFolder folder = project.getFolders().find { it.getName()?.equalsIgnoreCase(folderName) }
        if (folder == null) {
            folder = new OdiFolder(project, folderName)
            em.persist(folder)
            addCheck("folder_created", "pass", "Folder created.", [folder: folderName])
        } else {
            addCheck("folder_exists", "pass", "Folder already exists.", [folder: folderName])
        }

        txnMgr.commit(txnStatus)
        addCheck("commit_completed", "pass", "Idempotent project/folder seed committed.")
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }

    def payload = [
        tool: "odi.seed_project_folder",
        status: "passed",
        mode: "idempotent_project_folder_seed",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("seed_project_folder_exception", "fail", "ODI project/folder seed failed.")
    def payload = [
        tool: "odi.seed_project_folder",
        status: "error",
        mode: "idempotent_project_folder_seed",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
