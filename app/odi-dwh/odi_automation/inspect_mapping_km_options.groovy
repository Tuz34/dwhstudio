/*
  NL-DWH Studio: read-only mapping KM option inspection.
  This script only reads mapping physical design metadata and returns JSON.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.component.DatastoreComponent
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.domain.mapping.physical.MapPhysicalNode
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
    if (!metadata.isEmpty()) check.metadata = metadata
    checks << check
}

def optionToBoolean = { optionValue ->
    if (optionValue == null) return null
    def raw = optionValue.getOptionValue()
    if (raw == null) return null
    return String.valueOf(raw).equalsIgnoreCase("true")
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

    if (!mappingName) throw new Exception("MISSING PARAM: odi.mapping")

    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }
    addCheck("project_found", project != null ? "pass" : "fail", project != null ? "Project found." : "Project not found.", [project: projectCode])

    OdiFolder folder = null
    if (project != null) {
        folder = project.getFolders().find { it.getName()?.equalsIgnoreCase(folderName) }
    }
    addCheck("folder_found", folder != null ? "pass" : "fail", folder != null ? "Folder found." : "Folder not found.", [folder: folderName])

    def mapFinder = em.getFinder(Mapping.class)
    Mapping mapping = folder == null ? null : mapFinder.findByName(folder, mappingName)
    addCheck("mapping_found", mapping != null ? "pass" : "fail", mapping != null ? "Mapping found." : "Mapping not found.", [mapping: mappingName])

    MapPhysicalNode targetNode = null
    MapPhysicalNode accessPointNode = null
    String targetComponentName = null
    DatastoreComponent targetComponent = null

    if (mapping != null) {
        mapping.getAllComponents().each { comp ->
            if (comp instanceof DatastoreComponent) {
                boolean hasUpstream = false
                try {
                    comp.getInputConnectorPoints().each { icp ->
                        if (icp.getUpstreamConnectedLeafComponents()?.size() > 0) hasUpstream = true
                    }
                } catch (Exception ignored) {}
                if (!hasUpstream) {
                    comp.getAttributes().each { attr ->
                        String expr = attr.getExpression()?.getText()
                        if (expr != null && expr.trim().length() > 0 && expr.contains(".")) hasUpstream = true
                    }
                }
                if (hasUpstream) targetComponent = comp
            }
        }
        targetComponentName = targetComponent?.getName()

        def designs = mapping.getPhysicalDesigns()
        if (designs && designs.size() > 0) {
            MapPhysicalDesign design = designs.get(0)
            design.getPhysicalNodes().each { node ->
                String nodeName = node.getName()
                String nodeNameUpper = nodeName?.toUpperCase()
                boolean isTarget = false
                try {
                    isTarget = node.isTargetNode()
                } catch (Exception ignored) {}
                if (targetNode == null && isTarget) {
                    targetNode = node
                }
                if (targetNode == null && targetComponentName && nodeNameUpper?.equalsIgnoreCase(targetComponentName)) {
                    targetNode = node
                }
                if (accessPointNode == null && (nodeNameUpper?.endsWith("_AP") || nodeNameUpper?.contains("ACCESS_POINT"))) {
                    accessPointNode = node
                }
            }
            if (targetNode == null && design.getPhysicalNodes()?.size() > 0) {
                targetNode = design.getPhysicalNodes().get(0)
            }
        }
    }

    addCheck("physical_target_node_found", targetNode != null ? "pass" : "fail", targetNode != null ? "Physical target node found." : "Physical target node not found.")

    def truncateOption = null
    if (targetNode != null) {
        truncateOption = targetNode.getIKMOptionValue("TRUNCATE_TARGET_TABLE")
        addCheck("truncate_target_table_option_found", truncateOption != null ? "pass" : "warning", truncateOption != null ? "TRUNCATE_TARGET_TABLE option found." : "TRUNCATE_TARGET_TABLE option not found on target node.")
    }

    String targetIkm = targetNode?.getIKM()?.getName()
    String lkmName = null
    if (accessPointNode != null) {
        try { lkmName = accessPointNode.getLKM()?.getName() } catch (Exception ignored) {}
        if (lkmName == null || lkmName.trim().length() == 0) {
            try { lkmName = accessPointNode.getLKMName() } catch (Exception ignored) {}
        }
    }
    String integrationType = null
    String updateKeyName = null
    List<String> updateKeyColumns = []
    Map<String, String> auditExpressions = [:]
    if (targetComponent != null) {
        try { integrationType = targetComponent.getIntegrationType() } catch (Exception ignored) {}
        try {
            def updateKey = targetComponent.getUpdateKey()
            updateKeyName = updateKey?.getName()
            updateKey?.getColumns()?.each { col -> updateKeyColumns << col.getName()?.toUpperCase() }
        } catch (Exception ignored) {}
        targetComponent.getAttributes().each { attr ->
            String attrName = attr.getName()?.toUpperCase()
            if (attrName in ["ODS_CREATED_AT", "ODS_UPDATED_AT"]) {
                auditExpressions[attrName] = attr.getExpression()?.getText()
            }
        }
    }
    boolean failed = checks.any { it.status == "fail" }

    def payload = [
        tool: "odi.inspect_mapping_km_options",
        status: failed ? "attention_required" : "passed",
        mode: "odi_sdk_read_only",
        side_effects: false,
        repository_connection_attempted: true,
        mapping_name: mappingName,
        physical: [
            target_node: targetNode?.getName(),
            access_point_node: accessPointNode?.getName(),
            target_component: targetComponentName,
            target_ikm: targetIkm,
            lkm: lkmName
        ],
        logical: [
            integration_type: integrationType,
            update_key: updateKeyName,
            update_key_columns: updateKeyColumns,
            audit_expressions: auditExpressions
        ],
        km_options: [
            TRUNCATE_TARGET_TABLE: optionToBoolean(truncateOption)
        ],
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("inspect_mapping_km_options_exception", "fail", "ODI KM options probe failed before completion.")
    def payload = [
        tool: "odi.inspect_mapping_km_options",
        status: "error",
        mode: "odi_sdk_read_only",
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
