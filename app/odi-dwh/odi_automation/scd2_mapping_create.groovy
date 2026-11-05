/*
  Generic SCD2 mapping builder (absent-only).
  Integration type SCD, IKM Oracle Slowly Changing Dimension and
  LKM SQL to Oracle (Built-In) are fixed. Object names come from the validated
  plan. One ODI transaction with rollback; output is structured and value-free.
*/

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.persistence.transaction.ITransactionManager
import oracle.odi.core.persistence.transaction.ITransactionStatus
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition
import oracle.odi.core.security.Authentication
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.component.DatastoreComponent
import oracle.odi.domain.mapping.expression.MapExpression
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.domain.mapping.physical.MapPhysicalNode
import oracle.odi.domain.root.RootIssue
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiLKM
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiIKMFinder
import oracle.odi.domain.project.finder.IOdiLKMFinder
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder

final String TOOL = "odi.scd2_mapping_create"
final String MODE = "generic_absent_only_scd2_mapping_creation"
final List<String> AUDIT_OVERWRITE_COLUMNS = ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"]
final String TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
final String LKM_NAME = "LKM SQL to Oracle (Built-In)"
final java.util.regex.Pattern ID = ~/^[A-Z0-9_#]{1,30}$/
final java.util.regex.Pattern MAPPING_ID = ~/^[A-Z0-9_#]{1,31}$/

def odi = null
boolean standaloneMode = false
ITransactionManager txnMgr = null
ITransactionStatus txnStatus = null
boolean commitAttempted = false
boolean commitAcknowledged = false

def safeFlags = {
    [
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
    ]
}

// Fixed allowlisted reason codes or the exception class simple name; the exception
// message is never returned (it could carry repository values).
final Set<String> REASON_CODES = ["InvalidParameter", "ProjectContextUnavailable", "ProjectFolderUnavailable", "MappingAlreadyExists", "ScenarioAlreadyExists", "SourceDatastoreUnavailable", "TargetDatastoreUnavailable", "SourceColumnsMissing", "ScdRoleContractMismatch", "IkmUnavailable", "LkmUnavailable", "IkmIntegrationTypeMismatch", "IntegrationTypeMismatch", "SourceAttributeUnavailable", "UnmappedTargetAttribute", "TargetPhysicalNodeUnavailable", "StagingLocationUnavailable", "TargetNodeLostAfterReset", "IkmAssignmentMismatch", "LkmAssignmentMismatch", "LkmDbLinkRemains", "MappingCoverageMismatch", "MappingValidationFailed", "IkmOptionMismatch"] as Set
def reasonCode = { Throwable e ->
    String message = e.getMessage()
    if (e instanceof IllegalStateException && message != null && REASON_CODES.contains(message)) {
        return message
    }
    return e.getClass().getSimpleName()
}

def requireProp = { String key, java.util.regex.Pattern pattern ->
    String value = System.getProperty(key)
    if (value == null || !(value ==~ pattern)) {
        throw new IllegalStateException("InvalidParameter")
    }
    return value
}

def parseList = { String value ->
    (value == null || value == "none") ? [] : value.split(",").collect { it.trim() }.findAll { it }
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

    String sourceModel = requireProp("odi.scd2.source_model", ID)
    String sourceTable = requireProp("odi.scd2.source_table", ID)
    String targetModel = requireProp("odi.scd2.target_model", ID)
    String targetOwner = requireProp("odi.scd2.target_owner", ID)
    String targetTable = requireProp("odi.scd2.target_table", ID)
    String mappingName = requireProp("odi.scd2.mapping", MAPPING_ID)
    String sequenceName = requireProp("odi.scd2.sequence", ID)
    String skName = requireProp("odi.scd2.sk", ID)
    List<String> naturalKey = parseList(requireProp("odi.scd2.natural_key", ~/^[A-Z0-9_#,]+$/))
    List<String> tracked = parseList(requireProp("odi.scd2.tracked", ~/^[A-Z0-9_#,]+$/))
    List<String> overwrite = parseList(requireProp("odi.scd2.overwrite", ~/^[A-Za-z0-9_#,]+$/))
    String scenarioName = mappingName + "_SCN"
    String projectCode = System.getProperty("odi.project")
    String folderName = System.getProperty("odi.folder")
    if (projectCode == null || folderName == null) {
        throw new IllegalStateException("ProjectContextUnavailable")
    }
    Set<String> directColumns = (naturalKey + tracked + overwrite) as Set
    Map<String, String> fixedExpressions = [
        (skName): targetOwner + "." + sequenceName + ".NEXTVAL",
        EFFECTIVE_FROM: "CURRENT_DATE",
        EFFECTIVE_TO: "TO_DATE('24000101','YYYYMMDD')",
        CURRENT_FLAG: "1",
        INSERT_DATE: "CURRENT_TIMESTAMP",
        UPDATE_DATE: "CURRENT_TIMESTAMP",
        SOURCE_SYSTEM: "'ODI_SCD2'",
        ETL_BATCH_ID: "<%=odiRef.getSession(\"SESS_NO\")%>",
    ]

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    def em = odi.getTransactionalEntityManager()

    IOdiProjectFinder projectFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projectFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }
    OdiFolder folder = project?.getFolders()?.find { it.getName()?.equalsIgnoreCase(folderName) }
    if (folder == null) {
        throw new IllegalStateException("ProjectFolderUnavailable")
    }
    if (em.getFinder(Mapping.class).findByName(folder, mappingName) != null) {
        throw new IllegalStateException("MappingAlreadyExists")
    }
    IOdiScenarioFinder scenarioFinder = (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
    if (scenarioFinder.findLatestByName(scenarioName) != null) {
        throw new IllegalStateException("ScenarioAlreadyExists")
    }

    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
    OdiDataStore source = dsFinder.findByName(sourceTable, sourceModel)
    OdiDataStore target = dsFinder.findByName(targetTable, targetModel)
    if (source == null) {
        throw new IllegalStateException("SourceDatastoreUnavailable")
    }
    if (target == null) {
        throw new IllegalStateException("TargetDatastoreUnavailable")
    }
    Set<String> sourceNames = source.getColumns().collect { it.getName().toUpperCase() } as Set
    if (!sourceNames.containsAll(directColumns)) {
        throw new IllegalStateException("SourceColumnsMissing")
    }
    Map<String, List<String>> expectedRoles = [
        SURROGATE_KEY: [skName],
        NATURAL_KEY: naturalKey,
        START_TIMESTAMP: ["EFFECTIVE_FROM"],
        END_TIMESTAMP: ["EFFECTIVE_TO"],
        CURRENT_RECORD_FLAG: ["CURRENT_FLAG"],
        ADD_ROW_ON_CHANGE: tracked,
        // The registrar also marks the four fixed audit columns OVERWRITE_ON_CHANGE.
        OVERWRITE_ON_CHANGE: overwrite + AUDIT_OVERWRITE_COLUMNS,
    ]
    expectedRoles.each { String roleName, List<String> expectedColumns ->
        List<String> actual = target.getColumns().findAll { it.getScdType()?.name() == roleName }
            .collect { it.getName() }.sort()
        if (actual != expectedColumns.sort(false)) {
            throw new IllegalStateException("ScdRoleContractMismatch")
        }
    }

    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
    List ikms = (ikmFinder.findByName(TARGET_IKM) ?: []).findAll {
        TARGET_IKM == it.getName() && (it.isGlobal() || it.getProject()?.getCode() == projectCode)
    }
    IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
    List lkms = (lkmFinder.findByName(LKM_NAME) ?: []).findAll {
        LKM_NAME == it.getName() && (it.isGlobal() || it.getProject()?.getCode() == projectCode)
    }
    if (ikms.size() != 1) {
        throw new IllegalStateException("IkmUnavailable")
    }
    if (lkms.size() != 1) {
        throw new IllegalStateException("LkmUnavailable")
    }
    OdiIKM ikm = ikms[0]
    OdiLKM lkm = lkms[0]
    if (ikm.getIntegrationType() != "SLOWLY_CHANGING_DIMENSION") {
        throw new IllegalStateException("IkmIntegrationTypeMismatch")
    }

    Mapping mapping = new Mapping(mappingName, folder)
    em.persist(mapping)
    DatastoreComponent sourceComponent = new DatastoreComponent(mapping, source)
    DatastoreComponent targetComponent = new DatastoreComponent(mapping, target)
    targetComponent.setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_SCD)
    if (targetComponent.getIntegrationType() != DatastoreComponent.INTEGRATION_TYPE_SCD) {
        throw new IllegalStateException("IntegrationTypeMismatch")
    }
    sourceComponent.connectTo(targetComponent)

    targetComponent.getAttributes().each { targetAttribute ->
        String name = targetAttribute.getName().toUpperCase()
        if (directColumns.contains(name)) {
            def sourceAttribute = sourceComponent.getAttributes().find { it.getName()?.equalsIgnoreCase(name) }
            if (sourceAttribute == null) {
                throw new IllegalStateException("SourceAttributeUnavailable")
            }
            targetAttribute.setExpressionText(sourceComponent.getName() + "." + sourceAttribute.getName())
        } else if (fixedExpressions.containsKey(name)) {
            targetAttribute.setExpressionText(fixedExpressions[name])
        } else {
            throw new IllegalStateException("UnmappedTargetAttribute")
        }
        if (name == skName) {
            targetAttribute.setExecuteOnHint(MapExpression.ExecuteOnLocation.TARGET)
            targetAttribute.setCheckNotNullIndicator(false)
            targetAttribute.setUpdateIndicator(false)
        }
    }

    MapPhysicalDesign design = mapping.getPhysicalDesign(0)
    def findTargetNode = {
        design.getPhysicalNodes().find {
            it.isTargetNode() &&
                (it.getName()?.equalsIgnoreCase(targetTable) ||
                 it.getName()?.equalsIgnoreCase(targetComponent.getName()))
        }
    }
    MapPhysicalNode targetNode = findTargetNode()
    if (targetNode == null) {
        throw new IllegalStateException("TargetPhysicalNodeUnavailable")
    }
    String stagingLocation = targetNode.getExecutionUnit()?.getLocation()?.getName()
    if (stagingLocation == null || stagingLocation.trim().isEmpty()) {
        throw new IllegalStateException("StagingLocationUnavailable")
    }
    design.setStagingLocationName(stagingLocation)
    design.resetSpec()

    targetNode = findTargetNode()
    if (targetNode == null) {
        throw new IllegalStateException("TargetNodeLostAfterReset")
    }
    targetNode.setTargetComponentKM(ikm)
    if (!TARGET_IKM.equalsIgnoreCase(targetNode.getTargetComponentKMName())) {
        throw new IllegalStateException("IkmAssignmentMismatch")
    }
    // Data-quality checks are intentionally off for SCD2 loads: with CKM Oracle present FLOW_CONTROL
    // defaults to true and the CKM would divert rows to E$_<target>.
    final Map<String, Boolean> lockedOptions = [FLOW_CONTROL: false, STATIC_CONTROL: false,
        RECYCLE_ERRORS: false, TRUNCATE: false, DELETE_ALL: false, CREATE_TARG_TABLE: false, COMMIT: true]
    def writeOptions = { node ->
        lockedOptions.each { optionName, expected ->
            def optionValue = node.getIKMOptionValue(optionName)
            if (optionValue == null) throw new IllegalStateException("IkmOptionMismatch")
            optionValue.setValue(expected)
        }
    }
    writeOptions(targetNode)
    // Access-point nodes are re-read from the final physical design (after the last reset/staging call)
    // so the built-in LKM is forced on every one of them; ODI otherwise defaults cross-server APs to a DB-Link LKM.
    def readAccessPoints = { design.getAllAPNodes() as List<MapPhysicalNode> }
    def lkmOk = { MapPhysicalNode n -> LKM_NAME.equalsIgnoreCase(n.getLKMName()) }
    readAccessPoints().each { node -> node.setLKM(lkm) }
    // This call only materializes access points for the repair below: an error here may describe a
    // repairable intermediate state, so its result is deliberately not a gate. The final call decides.
    mapping.validate()
    targetNode = findTargetNode()
    if (targetNode == null) throw new IllegalStateException("TargetNodeLostAfterReset")
    writeOptions(targetNode)
    List<MapPhysicalNode> accessPoints = readAccessPoints()
    if (accessPoints.any { !lkmOk(it) }) {
        accessPoints.each { node -> node.setLKM(lkm) }
        accessPoints = readAccessPoints()
    }
    if (accessPoints.any { it.getLKMName()?.toUpperCase()?.contains("DB LINK") }) {
        throw new IllegalStateException("LkmDbLinkRemains")
    }
    if (accessPoints.any { !lkmOk(it) }) {
        throw new IllegalStateException("LkmAssignmentMismatch")
    }
    // Same-server mappings legitimately have zero access points (and therefore no LKM).

    int attributeCount = targetComponent.getAttributes().size()
    int mappedCount = targetComponent.getAttributes().count { it.getExpression() != null }
    if (attributeCount != target.getColumns().size() || mappedCount != attributeCount) {
        throw new IllegalStateException("MappingCoverageMismatch")
    }
    // SDK 12.2.1.4: validate() returns TRUE when errors were found, and it can also return FALSE after
    // adding a FATAL RootIssue (non-overridable target load order), so both signals block the commit.
    // The issue details are never output.
    List finalValidationIssues = new ArrayList()
    if (mapping.validate(finalValidationIssues, false) || RootIssue.isErrorOrFatal(finalValidationIssues)) {
        throw new IllegalStateException("MappingValidationFailed")
    }
    // Validation can materialize an AP or reset an option. Never repair after this final gate.
    MapPhysicalNode finalTarget = findTargetNode()
    if (finalTarget == null || TARGET_IKM != finalTarget.getTargetComponentKMName()) {
        throw new IllegalStateException("IkmAssignmentMismatch")
    }
    lockedOptions.each { optionName, expected ->
        def optionValue = finalTarget.getIKMOptionValue(optionName)
        if (optionValue == null || !String.valueOf(expected).equalsIgnoreCase(String.valueOf(optionValue.getOptionValue()))) {
            throw new IllegalStateException("IkmOptionMismatch")
        }
    }
    List<MapPhysicalNode> finalAccessPoints = readAccessPoints()
    if (finalAccessPoints.size() != accessPoints.size() || finalAccessPoints.any { !lkmOk(it) }) {
        throw new IllegalStateException("LkmAssignmentMismatch")
    }
    commitAttempted = true
    txnMgr.commit(txnStatus)
    commitAcknowledged = true
    txnStatus = null

    println JsonOutput.toJson([
        tool: TOOL,
        status: "passed",
        mode: MODE,
        side_effects: true,
        repository_connection_attempted: true,
        *: safeFlags(),
        result: [
            status: "passed",
            source_component_count: 1,
            target_component_count: 1,
            target_attribute_count: attributeCount,
            mapped_attribute_count: mappedCount,
            integration_type: "SCD",
            target_ikm: TARGET_IKM,
            access_point_count: accessPoints.size(),
            lkm_assigned_count: accessPoints.size(),
            commit_completed: true,
        ],
    ])
} catch (Throwable e) {
    boolean rollbackAttempted = false
    boolean rollbackSucceeded = false
    if (txnStatus != null && txnMgr != null) {
        rollbackAttempted = true
        try { txnMgr.rollback(txnStatus); rollbackSucceeded = true } catch (Throwable ignored) {}
    }
    String repositoryOutcome = commitAcknowledged ? "committed" : commitAttempted ? "commit_outcome_unknown" :
        rollbackAttempted ? (rollbackSucceeded ? "rolled_back" : "rollback_failed") : "no_transaction"
    String reportStatus = commitAcknowledged ? "committed_report_incomplete" :
        (commitAttempted || (rollbackAttempted && !rollbackSucceeded)) ? "outcome_unknown" : "error"
    println JsonOutput.toJson([
        tool: TOOL,
        status: reportStatus,
        mode: MODE,
        side_effects: commitAcknowledged ? true : reportStatus == "outcome_unknown" ? null : false,
        repository_connection_attempted: true,
        *: safeFlags(),
        error_type: reasonCode(e),
        repository_outcome: repositoryOutcome,
        commit_attempted: commitAttempted,
        commit_acknowledged: commitAcknowledged,
        rollback_attempted: rollbackAttempted,
        rollback_succeeded: rollbackSucceeded,
        manual_recovery_review_required: reportStatus != "error",
    ])
} finally {
    if (standaloneMode && odi != null) {
        try { odi.close() } catch (Throwable ignored) {}
    }
}
