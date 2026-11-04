/*
  NL-DWH Studio: Existing empty ODI mapping component repair.
  Adds source and target datastore components only when the mapping has no
  existing components. It does not recreate mappings or run scenarios.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.component.DatastoreComponent
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

def safeStack = { Throwable t ->
    return (t?.getStackTrace() ?: []).take(8).collect { frame ->
        [
            class_name: frame.getClassName(),
            method: frame.getMethodName(),
            line: frame.getLineNumber()
        ]
    }
}

def shortName = { String dsName ->
    if (dsName == null) return null
    if (dsName.contains(".")) return dsName.substring(dsName.indexOf(".") + 1)
    return dsName
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

def createDatastoreComponent = { owner, OdiDataStore ds, String role ->
    try {
        return new DatastoreComponent(owner, ds)
    } catch (Exception firstEx) {
        if (!safeMessage(firstEx).contains("DATA_FORMAT")) throw firstEx
        addCheck(role + "_component_standard_constructor_warning", "warning", "Standard datastore component constructor hit DATA_FORMAT warning; trying scoped SDK overload.", [detail: safeMessage(firstEx)])
        return new DatastoreComponent(owner, ds, false)
    }
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
    String folderName = System.getProperty("odi.folder")
    String mappingName = System.getProperty("odi.mapping")
    String srcModel = System.getProperty("odi.src_model")
    String srcTable = System.getProperty("odi.src_table")
    String tgtModel = System.getProperty("odi.tgt_model")
    String tgtTable = System.getProperty("odi.tgt_table")
    if (!projectCode || !folderName || !mappingName || !srcModel || !srcTable || !tgtModel || !tgtTable) {
        throw new Exception("MISSING PARAMS: project, folder, mapping, source and target are required")
    }

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

    try {
        def em = odi.getTransactionalEntityManager()
        IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
        OdiProject project = projFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }
        if (project == null) throw new Exception("Project not found: " + projectCode)
        OdiFolder folder = project.getFolders().find { it.getName()?.equalsIgnoreCase(folderName) }
        if (folder == null) throw new Exception("Folder not found: " + folderName)
        addCheck("folder_found", "pass", "Project and folder found.", [project: projectCode, folder: folderName])

        def mapFinder = em.getFinder(Mapping.class)
        Mapping mapping = mapFinder.findByName(folder, mappingName)
        if (mapping == null) throw new Exception("Mapping not found: " + mappingName)
        addCheck("mapping_found", "pass", "Mapping found.", [mapping: mappingName])

        int componentCount = mapping.getComponentCount()
        addCheck("mapping_component_count", "pass", "Mapping component count inspected.", [component_count: componentCount])
        if (componentCount > 0) {
            addCheck("mapping_already_has_components", "pass", "Mapping already has components; scoped repair did not modify it.", [component_count: componentCount])
            try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
            def payload = [
                tool: "odi.repair_mapping_components",
                status: "already_ready",
                mode: "empty_mapping_component_repair",
                side_effects: false,
                repository_connection_attempted: true,
                checks: checks
            ]
            println JsonOutput.toJson(payload)
            return
        }
        addCheck("mapping_empty_for_repair", "pass", "Mapping has no components; scoped repair can add source and target only.")

        OdiDataStore srcDS = findDsRobust(em, srcTable, srcModel)
        if (srcDS == null) throw new Exception("Source datastore not found: " + srcTable + " in " + srcModel)
        OdiDataStore tgtDS = findDsRobust(em, tgtTable, tgtModel)
        if (tgtDS == null) throw new Exception("Target datastore not found: " + tgtTable + " in " + tgtModel)
        addCheck("datastores_found", "pass", "Source and target datastores found.", [src_model: srcModel, tgt_model: tgtModel])

        DatastoreComponent srcComp = createDatastoreComponent(mapping, srcDS, "source")
        DatastoreComponent tgtComp = createDatastoreComponent(mapping, tgtDS, "target")
        srcComp.connectTo(tgtComp)
        addCheck("components_connected", "pass", "Source and target datastore components connected.")

        int mappedAttrs = 0
        tgtComp.getAttributes().each { tAttr ->
            def sAttr = srcComp.getAttributes().find { it.getName()?.equalsIgnoreCase(tAttr.getName()) }
            if (sAttr != null) {
                tAttr.setExpressionText(srcComp.getName() + "." + sAttr.getName())
                mappedAttrs++
            }
        }
        addCheck("matching_attributes_mapped", "pass", "Matching source attributes mapped by name.", [mapped: mappedAttrs])

        txnMgr.commit(txnStatus)
        addCheck("commit_completed", "pass", "Mapping component repair committed.")
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }

    def payload = [
        tool: "odi.repair_mapping_components",
        status: "passed",
        mode: "empty_mapping_component_repair",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("repair_mapping_components_exception", "fail", "ODI mapping component repair failed.")
    def payload = [
        tool: "odi.repair_mapping_components",
        status: "error",
        mode: "empty_mapping_component_repair",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e),
        error_stack: safeStack(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
