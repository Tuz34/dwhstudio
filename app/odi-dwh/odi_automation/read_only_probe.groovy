/*
  NL-DWH Studio: ODI SDK read-only metadata probe.
  This script must not persist, remove, commit, create, update, delete, or run
  scenarios. It only uses ODI SDK finders and returns redacted JSON checks.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.security.Authentication

def checks = []
def odi = null
boolean standaloneMode = false

def safeMessage = { Throwable t ->
    String msg = t?.getMessage() ?: t?.toString() ?: ""
    msg = msg.replaceAll(/(?i)(password|passwd|pwd|token|secret)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    msg = msg.replaceAll(/jdbc:[^\s,)]+/, "jdbc:***")
    msg = msg.replaceAll(/(?i)(user(name)?|uid)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    return msg.take(240)
}

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) {
        check.metadata = metadata
    }
    checks << check
}

def shortName = { String dsName ->
    if (dsName == null) return null
    if (dsName.contains(".")) {
        return dsName.substring(dsName.indexOf(".") + 1)
    }
    return dsName
}

def columnCount = { ds ->
    try {
        return ds?.getColumns()?.size()
    } catch (Exception ignored) {
        return null
    }
}

def findDsRobust = { emObj, String dsName, String modName ->
    if (!dsName || !modName) return null
    def finder = (IOdiDataStoreFinder) emObj.getFinder(OdiDataStore.class)
    def candidates = []
    candidates << dsName
    String stripped = shortName(dsName)
    candidates << stripped
    candidates << stripped?.toUpperCase()
    candidates << stripped?.toLowerCase()
    if (stripped && stripped.length() > 0) {
        candidates << stripped.substring(0, 1).toUpperCase() + stripped.substring(1).toLowerCase()
    }

    for (candidate in candidates.findAll { it != null }.unique()) {
        def ds = finder.findByName(candidate, modName)
        if (ds != null) return ds
    }
    return null
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

    def em = odi.getTransactionalEntityManager()

    String projectCode = System.getProperty("odi.project")
    String folderName = System.getProperty("odi.folder")
    String mappingName = System.getProperty("odi.mapping")
    String srcModel = System.getProperty("odi.src_model")
    String srcTable = System.getProperty("odi.src_table")
    String tgtModel = System.getProperty("odi.tgt_model")
    String tgtTable = System.getProperty("odi.tgt_table")

    OdiProject project = null
    OdiFolder folder = null

    if (projectCode) {
        IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
        project = projFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }
        addCheck(
            "project_found",
            project != null ? "pass" : "fail",
            project != null ? "Project found." : "Project not found.",
            [project: projectCode]
        )
    } else {
        addCheck("project_requested", "warning", "No project code was provided.")
    }

    if (project != null && folderName) {
        folder = project.getFolders().find { it.getName()?.equalsIgnoreCase(folderName) }
        addCheck(
            "folder_found",
            folder != null ? "pass" : "fail",
            folder != null ? "Folder found." : "Folder not found.",
            [folder: folderName]
        )
    } else if (folderName) {
        addCheck("folder_found", "fail", "Folder lookup skipped because project was not found.", [folder: folderName])
    } else {
        addCheck("folder_requested", "warning", "No folder name was provided.")
    }

    if (mappingName) {
        if (folder != null) {
            def mapFinder = em.getFinder(Mapping.class)
            def mapping = mapFinder.findByName(folder, mappingName)
            addCheck(
                "mapping_found",
                mapping != null ? "pass" : "fail",
                mapping != null ? "Mapping found." : "Mapping not found.",
                [mapping: mappingName]
            )
        } else {
            addCheck("mapping_found", "fail", "Mapping lookup skipped because folder was not found.", [mapping: mappingName])
        }
    } else {
        addCheck("mapping_requested", "warning", "No mapping name was provided.")
    }

    if (srcTable || srcModel) {
        def srcDs = findDsRobust(em, srcTable, srcModel)
        addCheck(
            "source_datastore_found",
            srcDs != null ? "pass" : "fail",
            srcDs != null ? "Source datastore found." : "Source datastore not found.",
            [model: srcModel, datastore: srcTable, column_count: columnCount(srcDs)]
        )
    }

    if (tgtTable || tgtModel) {
        def tgtDs = findDsRobust(em, tgtTable, tgtModel)
        addCheck(
            "target_datastore_found",
            tgtDs != null ? "pass" : "fail",
            tgtDs != null ? "Target datastore found." : "Target datastore not found.",
            [model: tgtModel, datastore: tgtTable, column_count: columnCount(tgtDs)]
        )
    }

    addCheck(
        "forbidden_operations_not_called",
        "pass",
        "No persist/remove/commit/create/update/delete/scenario operation is used by this script."
    )

    boolean failed = checks.any { it.status == "fail" }
    def payload = [
        tool: "odi.read_only_probe",
        status: failed ? "attention_required" : "passed",
        mode: "odi_sdk_finders_only",
        side_effects: false,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("read_only_probe_exception", "fail", "ODI read-only probe failed before completion.")
    def payload = [
        tool: "odi.read_only_probe",
        status: "error",
        mode: "odi_sdk_finders_only",
        side_effects: false,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}
