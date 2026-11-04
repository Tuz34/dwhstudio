/*
  NL-DWH Studio: idempotent ODI scenario metadata generation.
  Existing scenarios are left in place. This script only creates missing
  scenario metadata for a known mapping and returns redacted JSON.
*/

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.persistence.transaction.ITransactionDefinition
import oracle.odi.core.persistence.transaction.ITransactionManager
import oracle.odi.core.persistence.transaction.ITransactionStatus
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition
import oracle.odi.core.persistence.dwgobject.IWorkRepositoryDwgObjectCallback
import oracle.odi.core.persistence.dwgobject.TransactionalDwgObjectTemplate
import oracle.odi.core.persistence.dwgobject.bridge.DwgObjectBridgeRegistry
import oracle.odi.core.security.Authentication
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.MappingCompatibilityHelper
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.generation.support.ScenarioUtils
import com.sunopsis.dwg.dbobj.DwgMapDeploySpec
import com.sunopsis.dwg.dbobj.SnpDeploySpec
import com.sunopsis.dwg.dbobj.SnpMapping

def checks = []
def odi = null
boolean standaloneMode = false
ITransactionStatus txnStatus = null

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) {
        check.metadata = metadata
    }
    checks << check
}

def scenarioByNameVersion = { IOdiScenarioFinder finder, String scenarioName, String version ->
    try {
        def latest = finder.findLatestByName(scenarioName)
        if (latest != null && latest.getVersion()?.equalsIgnoreCase(version)) {
            return latest
        }
        return null
    } catch (Throwable ignored) {
        return null
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
    String scenarioName = System.getProperty("odi.scenario.name")
    String scenarioVersion = System.getProperty("odi.scenario.version", "001")

    def em = odi.getTransactionalEntityManager()
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }
    addCheck("project_found", project != null ? "pass" : "fail", project != null ? "Project found." : "Project not found.", [project: projectCode])

    OdiFolder folder = null
    if (project != null) {
        folder = project.getFolders().find { it.getName()?.equalsIgnoreCase(folderName) }
    }
    addCheck("folder_found", folder != null ? "pass" : "fail", folder != null ? "Folder found." : "Folder not found.", [folder: folderName])

    Mapping mapping = null
    if (folder != null) {
        def mapFinder = em.getFinder(Mapping.class)
        mapping = mapFinder.findByName(folder, mappingName)
    }
    addCheck("mapping_found", mapping != null ? "pass" : "fail", mapping != null ? "Mapping found." : "Mapping not found.", [mapping: mappingName])

    if (mapping != null) {
        int componentCount = mapping.getComponentCount()
        addCheck(
            "mapping_component_count",
            componentCount > 0 ? "pass" : "fail",
            componentCount > 0 ? "Mapping has logical components." : "Mapping has no logical components.",
            [component_count: componentCount]
        )

        def designs = mapping.getPhysicalDesigns()
        int designCount = designs == null ? 0 : designs.size()
        addCheck(
            "physical_design_count",
            designCount > 0 ? "pass" : "fail",
            designCount > 0 ? "Mapping has a physical design." : "Mapping has no physical design.",
            [physical_design_count: designCount]
        )

        if (designCount > 0) {
            MapPhysicalDesign design = designs.get(0)
            if (design == null) {
                addCheck(
                    "physical_design_object_present",
                    "fail",
                    "Physical design list exists but the first design object is null.",
                    [physical_design_count: designCount]
                )
            } else {
                addCheck("physical_design_object_present", "pass", "Physical design object is present.")
                def nodes = design.getPhysicalNodes()
                int nodeCount = nodes == null ? 0 : nodes.size()
                def targetNodes = nodes == null ? [] : nodes.findAll { it.isTargetNode() }
                def targetKms = targetNodes.collect { it.getIKM()?.getName() }.findAll { it != null }
                addCheck(
                    "physical_node_count",
                    nodeCount > 0 ? "pass" : "fail",
                    nodeCount > 0 ? "Physical nodes are present." : "Physical nodes are missing.",
                    [physical_node_count: nodeCount, target_node_count: targetNodes.size()]
                )
                addCheck(
                    "target_ikm_present",
                    !targetKms.isEmpty() ? "pass" : "fail",
                    !targetKms.isEmpty() ? "Target IKM is assigned." : "Target IKM is missing; scenario generation would fail.",
                    [target_ikm_count: targetKms.size()]
                )
            }
        }
    }

    IOdiScenarioFinder scenFinder = (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
    def existingExact = scenarioByNameVersion(scenFinder, scenarioName, scenarioVersion)
    def existingLatest = scenFinder.findLatestByName(scenarioName)
    addCheck(
        "scenario_exact_absent_or_ready",
        existingExact == null ? "pass" : "pass",
        existingExact == null ? "Requested scenario version is absent." : "Requested scenario version already exists.",
        [scenario: scenarioName, version: scenarioVersion]
    )

    if (checks.any { it.status == "fail" }) {
        def payload = [
            tool: "odi.generate_scenario_metadata",
            status: "attention_required",
            mode: "scenario_metadata_generation",
            side_effects: false,
            repository_connection_attempted: true,
            checks: checks
        ]
        println JsonOutput.toJson(payload)
        return
    }

    if (existingExact != null) {
        addCheck("scenario_already_ready", "pass", "Scenario metadata already exists; no generation was attempted.", [scenario: scenarioName, version: scenarioVersion])
        addCheck("forbidden_operations_not_called", "pass", "No remove, target DDL, data load, scenario start, or load plan start is used by this script.")
        def payload = [
            tool: "odi.generate_scenario_metadata",
            status: "already_ready",
            mode: "scenario_metadata_generation",
            side_effects: false,
            repository_connection_attempted: true,
            scenario: [name: scenarioName, version: scenarioVersion, latest_version: existingLatest?.getVersion()],
            checks: checks
        ]
        println JsonOutput.toJson(payload)
        return
    }

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionDefinition outerTxnDef = new DefaultTransactionDefinition()
    txnStatus = txnMgr.getTransaction(outerTxnDef)

    DwgObjectBridgeRegistry bridgeRegistry = DwgObjectBridgeRegistry.getInstance()
    MapPhysicalDesign design = mapping.getPhysicalDesigns().get(0)
    ITransactionDefinition mandatoryTxnDef = new DefaultTransactionDefinition(2)
    TransactionalDwgObjectTemplate template = new TransactionalDwgObjectTemplate(odi, mandatoryTxnDef)
    template.execute({ securityToken, masterRepository, workRepository ->
        design.initForGeneration()
        MappingCompatibilityHelper.setCompatibleMappingEditable(mapping, true)
        design.getPhysicalNodes().each { node ->
            MappingCompatibilityHelper.setReferencedMappingEditable(node, true)
        }
        odi.getTransactionalEntityManager().flush()
        def mappingBridge = bridgeRegistry.getDwgObjectBridge(mapping.getClass())
        def designBridge = bridgeRegistry.getDwgObjectBridge(design.getClass())
        def dwgMapping = mappingBridge.getDwgObject(mapping, masterRepository, workRepository, securityToken)
        def dwgDeploySpec = designBridge.getDwgObject(design, masterRepository, workRepository, securityToken)
        def scenSource = new DwgMapDeploySpec((SnpMapping) dwgMapping, (SnpDeploySpec) dwgDeploySpec)
        ScenarioUtils.generateNewScen(
            odi,
            scenSource,
            scenarioName,
            scenarioVersion,
            null,
            true,
            [],
            securityToken,
            masterRepository,
            workRepository
        )
        return null
    } as IWorkRepositoryDwgObjectCallback)

    txnMgr.commit(txnStatus)
    txnStatus = null

    def generatedScenario = scenarioByNameVersion(scenFinder, scenarioName, scenarioVersion)
    if (generatedScenario == null) {
        throw new Exception("Scenario generation completed without read-back confirmation.")
    }

    addCheck("scenario_generated", "pass", "Scenario metadata generated and committed.", [scenario: scenarioName, version: scenarioVersion])
    addCheck("forbidden_operations_not_called", "pass", "No remove, target DDL, data load, scenario start, or load plan start is used by this script.")

    def payload = [
        tool: "odi.generate_scenario_metadata",
        status: "passed",
        mode: "scenario_metadata_generation",
        side_effects: true,
        repository_connection_attempted: true,
        scenario: [name: scenarioName, version: scenarioVersion],
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    try {
        if (txnStatus != null && odi != null) {
            odi.getTransactionManager().rollback(txnStatus)
        }
    } catch (ignored) {}
    addCheck("scenario_generation_exception", "fail", "Scenario metadata generation failed before completion.")
    def payload = [
        tool: "odi.generate_scenario_metadata",
        status: "error",
        mode: "scenario_metadata_generation",
        side_effects: false,
        repository_connection_attempted: true,
        checks: checks,
        error_type: e.getClass().getSimpleName()
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}
