/*
  NL-DWH Studio: Minimal absent-only ODI mapping smoke.
  Creates one metadata mapping only if it is absent. It does not modify
  existing mappings, generate scenarios, run scenarios, or assign KMs.
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
        addCheck("project_found", "pass", "Project found.", [project: projectCode])

        OdiFolder folder = project.getFolders().find { it.getName()?.equalsIgnoreCase(folderName) }
        if (folder == null) throw new Exception("Folder not found: " + folderName)
        addCheck("folder_found", "pass", "Folder found.", [folder: folderName])

        def mapFinder = em.getFinder(Mapping.class)
        def existing = mapFinder.findByName(folder, mappingName)
        if (existing != null) {
            addCheck("mapping_absent", "fail", "Mapping already exists; absent-only smoke did not modify it.", [mapping: mappingName])
            throw new Exception("Mapping already exists: " + mappingName)
        }
        addCheck("mapping_absent", "pass", "Mapping is absent and can be created.", [mapping: mappingName])

        OdiDataStore srcDS = findDsRobust(em, srcTable, srcModel)
        if (srcDS == null) throw new Exception("Source datastore not found: " + srcTable + " in " + srcModel)
        addCheck("source_datastore_found", "pass", "Source datastore found.", [model: srcModel, datastore: srcTable])

        OdiDataStore tgtDS = findDsRobust(em, tgtTable, tgtModel)
        if (tgtDS == null) throw new Exception("Target datastore not found: " + tgtTable + " in " + tgtModel)
        addCheck("target_datastore_found", "pass", "Target datastore found.", [model: tgtModel, datastore: tgtTable])

        Mapping mapping = new Mapping(mappingName, folder)
        em.persist(mapping)
        txnMgr.commit(txnStatus)
        addCheck("mapping_created", "pass", "Absent mapping metadata created.", [mapping: mappingName])
        addCheck("mapping_commit_completed", "pass", "Mapping metadata committed before optional component wiring.")

        txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
        try {
            mapping = mapFinder.findByName(folder, mappingName)
            DatastoreComponent srcComp = new DatastoreComponent(mapping, srcDS)
            DatastoreComponent tgtComp = new DatastoreComponent(mapping, tgtDS)
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
            addCheck("component_commit_completed", "pass", "Component wiring committed.")
        } catch (Exception componentEx) {
            try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
            addCheck("components_connected", "warning", "Mapping metadata exists, but component wiring was skipped after SDK error.", [error: componentEx.getClass().getSimpleName(), detail: safeMessage(componentEx)])
        }
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }

    def payload = [
        tool: "odi.create_mapping_smoke",
        status: checks.any { it.status == "warning" } ? "partial" : "passed",
        mode: "absent_only_mapping_metadata_smoke",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    if (!checks.find { it.name == "create_mapping_smoke_exception" }) {
        addCheck("create_mapping_smoke_exception", "fail", "ODI mapping smoke failed.")
    }
    def payload = [
        tool: "odi.create_mapping_smoke",
        status: "error",
        mode: "absent_only_mapping_metadata_smoke",
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
