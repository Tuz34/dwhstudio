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
import oracle.odi.core.security.Authentication
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.root.RootIssue
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.generation.IOdiScenarioGenerator
import oracle.odi.generation.support.OdiScenarioGeneratorImpl

def checks = []
def odi = null
boolean standaloneMode = false
ITransactionStatus txnStatus = null
boolean commitAttempted = false
boolean commitAcknowledged = false

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) {
        check.metadata = metadata
    }
    checks << check
}

// Every version of a scenario name. findLatestByName alone only sees the newest
// version, which made an older existing version look absent (ODI-15034).
def scenariosByName = { IOdiScenarioFinder finder, String scenarioName ->
    try {
        def found = finder.findByName(scenarioName)
        if (found != null) {
            return found.findAll { it != null && scenarioName.equalsIgnoreCase(it.getName()) }.toList()
        }
    } catch (Throwable ignored) {
        // fall through to the full scan
    }
    try {
        return finder.findAll().findAll { it != null && scenarioName.equalsIgnoreCase(it.getName()) }.toList()
    } catch (Throwable failure) {
        throw new IllegalStateException("Scenario lookup could not establish absence")
    }
}

def scenarioByNameVersion = { IOdiScenarioFinder finder, String scenarioName, String version ->
    def exact = scenariosByName(finder, scenarioName).findAll {
        it.getVersion()?.equalsIgnoreCase(version)
    }
    if (exact.size() > 1) {
        throw new IllegalStateException("Ambiguous scenario version")
    }
    return exact.isEmpty() ? null : exact[0]
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
    def allVersions = scenariosByName(scenFinder, scenarioName)
    def exactVersions = allVersions.findAll { it.getVersion()?.equalsIgnoreCase(scenarioVersion) }
    if (exactVersions.size() > 1) {
        throw new IllegalStateException("Ambiguous scenario version")
    }
    def existingExact = exactVersions.isEmpty() ? null : exactVersions[0]
    def existingLatest = scenFinder.findLatestByName(scenarioName)
    String latestVersion = existingLatest?.getVersion()
    // Ownership must be checked on the requested version, not the latest one.
    if (existingExact != null) {
        def requestedMappingId = mapping?.getInternalId()
        def sourceMappingId = existingExact.getSourceMappingId()
        boolean sameMapping = requestedMappingId != null && sourceMappingId != null &&
            sourceMappingId.toString() == requestedMappingId.toString()
        addCheck(
            "scenario_exact_mapping_owner",
            sameMapping ? "pass" : "fail",
            sameMapping ? "Requested scenario version belongs to the mapping." :
                "Requested scenario version ownership is missing or does not match the mapping.",
            [scenario: scenarioName, version: scenarioVersion]
        )
    }
    // An existing version is reported as already_ready below and an absent one is
    // generated; neither outcome is a check, so no constant "pass" entry is emitted here.

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
            scenario: [name: scenarioName, version: scenarioVersion, latest_version: latestVersion],
            checks: checks
        ]
        println JsonOutput.toJson(payload)
        return
    }

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionDefinition outerTxnDef = new DefaultTransactionDefinition()
    txnStatus = txnMgr.getTransaction(outerTxnDef)

    em.flush()
    IOdiScenarioGenerator generator = new OdiScenarioGeneratorImpl(odi)
    List<RootIssue> generationIssues = new ArrayList<RootIssue>()
    OdiScenario generated = generator.generateScenario(mapping, scenarioName, scenarioVersion, generationIssues)
    if (RootIssue.isErrorOrFatal(generationIssues) || generated == null ||
            !scenarioName.equalsIgnoreCase(generated.getName()) ||
            !scenarioVersion.equalsIgnoreCase(generated.getVersion()) ||
            mapping.getInternalId() == null || generated.getSourceMappingId() == null ||
            generated.getSourceMappingId().toString() != mapping.getInternalId().toString()) {
        throw new IllegalStateException("Generated scenario ownership could not be verified")
    }
    em.flush()
    def generatedScenario = scenarioByNameVersion(scenFinder, scenarioName, scenarioVersion)
    if (generatedScenario == null || generatedScenario.getSourceMappingId() == null ||
            generatedScenario.getSourceMappingId().toString() != mapping.getInternalId().toString()) {
        throw new IllegalStateException("Generated scenario read-back ownership could not be verified")
    }
    // Public generation participates in this transaction. Never manufacture a source ID.
    // Both the returned object and the finder read-back must prove ownership before commit.
    commitAttempted = true
    txnMgr.commit(txnStatus)
    commitAcknowledged = true
    txnStatus = null

    addCheck("scenario_generated", "pass", "Scenario metadata generated and committed.", [scenario: scenarioName, version: scenarioVersion])
    addCheck("forbidden_operations_not_called", "pass", "No remove, target DDL, data load, scenario start, or load plan start is used by this script.")

    def newLatest = scenFinder.findLatestByName(scenarioName)
    def payload = [
        tool: "odi.generate_scenario_metadata",
        status: "passed",
        mode: "scenario_metadata_generation",
        side_effects: true,
        repository_connection_attempted: true,
        scenario: [name: scenarioName, version: scenarioVersion, latest_version: newLatest?.getVersion()],
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Throwable e) {
    boolean rollbackAttempted = false
    boolean rollbackSucceeded = false
    if (txnStatus != null && odi != null) {
        rollbackAttempted = true
        try {
            odi.getTransactionManager().rollback(txnStatus)
            rollbackSucceeded = true
        } catch (Throwable ignored) {}
    }
    String repositoryOutcome = "no_transaction"
    String reportStatus = "error"
    Boolean sideEffects = false
    if (commitAcknowledged) {
        repositoryOutcome = "committed"
        reportStatus = "committed_report_incomplete"
        sideEffects = true
    } else if (commitAttempted) {
        repositoryOutcome = "commit_outcome_unknown"
        reportStatus = "outcome_unknown"
        sideEffects = null
    } else if (rollbackAttempted && !rollbackSucceeded) {
        repositoryOutcome = "rollback_failed"
        reportStatus = "outcome_unknown"
        sideEffects = null
    } else if (rollbackAttempted) {
        repositoryOutcome = "rolled_back"
    }
    addCheck("scenario_generation_exception", "fail", "Scenario metadata generation did not produce a verified success report.")
    def payload = [
        tool: "odi.generate_scenario_metadata",
        status: reportStatus,
        mode: "scenario_metadata_generation",
        side_effects: sideEffects,
        repository_connection_attempted: true,
        checks: checks,
        error_type: e.getClass().getSimpleName() ?: "UnnamedError",
        repository_outcome: repositoryOutcome,
        commit_attempted: commitAttempted,
        commit_acknowledged: commitAcknowledged,
        rollback_attempted: rollbackAttempted,
        rollback_succeeded: rollbackSucceeded,
        manual_recovery_review_required: reportStatus != "error",
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) {
        try { odi.close() } catch (Throwable ignored) {}
    }
}
