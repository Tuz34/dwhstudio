/*
  ODI-68 fixed absent-only DIM_PRODUCT SCD2 mapping builder.
  Object identities, expressions, integration type, and KMs are not caller-controlled.
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
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiLKM
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiIKMFinder
import oracle.odi.domain.project.finder.IOdiLKMFinder
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.root.RootIssue
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder

final String MODEL_CODE = "TGT_DWH"
final String MAPPING_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
final String SOURCE_TABLE = "ODS_SH_PRODUCTS"
final String TARGET_TABLE = "DIM_PRODUCT"
final String SCENARIO_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN"
final String TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
final String LKM_NAME = "LKM SQL to Oracle (Built-In)"

final Set<String> DIRECT_SOURCE_COLUMNS = [
    "PROD_ID", "PROD_NAME", "PROD_DESC", "PROD_SUBCATEGORY",
    "PROD_SUBCATEGORY_ID", "PROD_SUBCATEGORY_DESC", "PROD_CATEGORY",
    "PROD_CATEGORY_ID", "PROD_CATEGORY_DESC", "PROD_WEIGHT_CLASS",
    "PROD_UNIT_OF_MEASURE", "PROD_PACK_SIZE", "SUPPLIER_ID", "PROD_STATUS",
    "PROD_LIST_PRICE", "PROD_MIN_PRICE", "PROD_TOTAL", "PROD_TOTAL_ID",
] as Set
final Map<String, String> FIXED_EXPRESSIONS = [
    PRODUCT_SK: "DWH.SEQ_DIM_PRODUCT.NEXTVAL",
    EFFECTIVE_FROM: "CURRENT_DATE",
    EFFECTIVE_TO: "TO_DATE('24000101','YYYYMMDD')",
    CURRENT_FLAG: "1",
    INSERT_DATE: "CURRENT_TIMESTAMP",
    UPDATE_DATE: "CURRENT_TIMESTAMP",
    SOURCE_SYSTEM: "'SH'",
    ETL_BATCH_ID: "<%=odiRef.getSession(\"SESS_NO\")%>",
]
// Stock IKM option contract. Flow and static control stay off, so no CKM runs and no
// CKM is chosen here. The other options are read back and never written: a stock
// default that differs blocks the commit.
final Map<String, Boolean> STOCK_IKM_OPTIONS = [
    FLOW_CONTROL: false,
    STATIC_CONTROL: false,
    RECYCLE_ERRORS: false,
    TRUNCATE: false,
    DELETE_ALL: false,
    CREATE_TARG_TABLE: false,
    COMMIT: true,
]
final List<String> WRITTEN_IKM_OPTIONS = ["FLOW_CONTROL", "STATIC_CONTROL"]

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
    String folderName = System.getProperty("odi.folder")
    if (projectCode == null || folderName == null) {
        throw new IllegalStateException("Fixed project context is unavailable")
    }

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    def em = odi.getTransactionalEntityManager()

    IOdiProjectFinder projectFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projectFinder.findAll().find {
        it.getCode()?.equalsIgnoreCase(projectCode)
    }
    OdiFolder folder = project?.getFolders()?.find {
        it.getName()?.equalsIgnoreCase(folderName)
    }
    if (folder == null) {
        throw new IllegalStateException("Fixed project folder is unavailable")
    }

    def mappingFinder = em.getFinder(Mapping.class)
    if (mappingFinder.findByName(folder, MAPPING_NAME) != null) {
        throw new IllegalStateException("mapping already exists")
    }
    IOdiScenarioFinder scenarioFinder =
        (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
    if (scenarioFinder.findLatestByName(SCENARIO_NAME) != null) {
        throw new IllegalStateException("Fixed scenario already exists")
    }

    IOdiDataStoreFinder datastoreFinder =
        (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
    OdiDataStore source = datastoreFinder.findByName(SOURCE_TABLE, MODEL_CODE)
    OdiDataStore target = datastoreFinder.findByName(TARGET_TABLE, MODEL_CODE)
    if (source == null || target == null) {
        throw new IllegalStateException("Fixed datastore contract is unavailable")
    }
    if (source.getColumns().size() != 24 || target.getColumns().size() != 26) {
        throw new IllegalStateException("Fixed datastore column contract mismatch")
    }

    Map<String, List<String>> expectedRoles = [
        SURROGATE_KEY: ["PRODUCT_SK"],
        NATURAL_KEY: ["PROD_ID"],
        START_TIMESTAMP: ["EFFECTIVE_FROM"],
        END_TIMESTAMP: ["EFFECTIVE_TO"],
        CURRENT_RECORD_FLAG: ["CURRENT_FLAG"],
        ADD_ROW_ON_CHANGE: DIRECT_SOURCE_COLUMNS.findAll { it != "PROD_ID" }.sort(),
    ]
    expectedRoles.each { roleName, expectedColumns ->
        List<String> actualColumns = target.getColumns().findAll {
            it.getScdType()?.name() == roleName
        }.collect { it.getName() }.sort()
        if (actualColumns != expectedColumns.sort()) {
            throw new IllegalStateException("Fixed SCD role contract mismatch")
        }
    }

    // A KM is usable only when exactly one exact-name candidate is global or owned by
    // this project. Two candidates are ambiguous and block; none is picked arbitrarily.
    def resolveFixedKm = { finder, String kmName ->
        List candidates = (finder.findByName(kmName) ?: []).findAll { km ->
            kmName == km.getName() &&
                (km.isGlobal() || km.getProject()?.getCode()?.equalsIgnoreCase(projectCode))
        }
        if (candidates.size() != 1) {
            throw new IllegalStateException("Fixed knowledge module contract is unavailable")
        }
        return candidates[0]
    }
    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
    OdiIKM ikm = resolveFixedKm(ikmFinder, TARGET_IKM)
    IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
    OdiLKM lkm = resolveFixedKm(lkmFinder, LKM_NAME)
    if (ikm.getIntegrationType() != "SLOWLY_CHANGING_DIMENSION") {
        throw new IllegalStateException("Fixed IKM integration type mismatch")
    }

    Mapping mapping = new Mapping(MAPPING_NAME, folder)
    em.persist(mapping)
    DatastoreComponent sourceComponent = new DatastoreComponent(mapping, source)
    DatastoreComponent targetComponent = new DatastoreComponent(mapping, target)
    targetComponent.setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_SCD)
    if (targetComponent.getIntegrationType() != DatastoreComponent.INTEGRATION_TYPE_SCD) {
        throw new IllegalStateException("Logical SCD integration type mismatch")
    }
    sourceComponent.connectTo(targetComponent)

    targetComponent.getAttributes().each { targetAttribute ->
        String name = targetAttribute.getName().toUpperCase()
        if (DIRECT_SOURCE_COLUMNS.contains(name)) {
            def sourceAttribute = sourceComponent.getAttributes().find {
                it.getName()?.equalsIgnoreCase(name)
            }
            if (sourceAttribute == null) {
                throw new IllegalStateException("Fixed source attribute is unavailable")
            }
            targetAttribute.setExpressionText(
                sourceComponent.getName() + "." + sourceAttribute.getName()
            )
        } else if (FIXED_EXPRESSIONS.containsKey(name)) {
            targetAttribute.setExpressionText(FIXED_EXPRESSIONS[name])
        } else {
            throw new IllegalStateException("Unmapped target attribute in fixed contract")
        }
        if (name == "PRODUCT_SK") {
            targetAttribute.setExecuteOnHint(MapExpression.ExecuteOnLocation.TARGET)
            targetAttribute.setCheckNotNullIndicator(false)
            targetAttribute.setUpdateIndicator(false)
        }
    }

    MapPhysicalDesign design = mapping.getPhysicalDesign(0)
    def findTargetNode = {
        design.getPhysicalNodes().find {
            it.isTargetNode() &&
                (it.getName()?.equalsIgnoreCase(TARGET_TABLE) ||
                 it.getName()?.equalsIgnoreCase(targetComponent.getName()))
        }
    }
    MapPhysicalNode targetNode = findTargetNode()
    if (targetNode == null) {
        throw new IllegalStateException("Fixed target physical node is unavailable")
    }
    String stagingLocation = targetNode.getExecutionUnit()?.getLocation()?.getName()
    if (stagingLocation == null || stagingLocation.trim().isEmpty()) {
        throw new IllegalStateException("Fixed staging location is unavailable")
    }
    design.setStagingLocationName(stagingLocation)
    design.resetSpec()

    targetNode = findTargetNode()
    if (targetNode == null) {
        throw new IllegalStateException("Fixed target node was lost after physical reset")
    }
    targetNode.setTargetComponentKM(ikm)

    def writeIkmOptions = { MapPhysicalNode node ->
        WRITTEN_IKM_OPTIONS.each { String optionName ->
            def optionValue = node.getIKMOptionValue(optionName)
            if (optionValue == null) {
                throw new IllegalStateException("Fixed IKM option is unavailable")
            }
            optionValue.setValue(STOCK_IKM_OPTIONS[optionName])
        }
    }
    // An access point takes its LKM through setLKM and getLKMName. The XKM accessors read
    // a separate extract reference, which is empty on an access point.
    def assignAccessPointLkm = {
        design.getAllAPNodes().each { MapPhysicalNode node ->
            if (LKM_NAME != node.getLKMName()) {
                node.setLKM(lkm)
            }
        }
    }
    // Re-reads the final design. Every check here must hold just before the commit.
    def verifyPhysicalContract = {
        MapPhysicalNode finalTarget = findTargetNode()
        if (finalTarget == null || TARGET_IKM != finalTarget.getTargetComponentKMName()) {
            throw new IllegalStateException("Fixed target IKM assignment mismatch")
        }
        STOCK_IKM_OPTIONS.each { String optionName, Boolean expected ->
            def optionValue = finalTarget.getIKMOptionValue(optionName)
            if (optionValue == null ||
                    !String.valueOf(expected).equalsIgnoreCase(String.valueOf(optionValue.getOptionValue()))) {
                throw new IllegalStateException("Fixed IKM option mismatch")
            }
        }
        List<MapPhysicalNode> nodes = design.getAllAPNodes() as List<MapPhysicalNode>
        if (nodes.any { LKM_NAME != it.getLKMName() }) {
            throw new IllegalStateException("Fixed LKM assignment mismatch")
        }
        return nodes
    }

    writeIkmOptions(targetNode)
    assignAccessPointLkm()
    // This pass only lets the SDK settle the physical design: it can materialize an access
    // point or reset an option. Its issues may describe a repairable state, so its result
    // is not a gate. An exception still blocks, and the final pass below decides.
    mapping.validate(new ArrayList<RootIssue>(), false)
    targetNode = findTargetNode()
    if (targetNode == null) {
        throw new IllegalStateException("Fixed target node was lost after validation")
    }
    writeIkmOptions(targetNode)
    assignAccessPointLkm()
    List<MapPhysicalNode> accessPoints = verifyPhysicalContract()

    int mappedCount = targetComponent.getAttributes().count {
        it.getExpression() != null
    }
    if (targetComponent.getAttributes().size() != 26 || mappedCount != 26) {
        throw new IllegalStateException("Fixed target mapping coverage mismatch")
    }
    // SDK 12.2.1.4: validate returns TRUE when it finds errors, and it can also return FALSE
    // after adding a FATAL issue, so both signals block. TRUE with no issue also blocks.
    // Issue text can hold expressions and repository detail and is never output.
    List<RootIssue> finalIssues = new ArrayList<RootIssue>()
    boolean validationFlagged = mapping.validate(finalIssues, false)
    if (validationFlagged || RootIssue.isErrorOrFatal(finalIssues)) {
        throw new IllegalStateException("Fixed mapping validation failed")
    }
    // Validation can change the design, so the contract is checked again after it.
    if (verifyPhysicalContract().size() != accessPoints.size()) {
        throw new IllegalStateException("Fixed access point count changed during validation")
    }
    commitAttempted = true
    txnMgr.commit(txnStatus)
    commitAcknowledged = true
    txnStatus = null

    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_mapping_create",
        status: "passed",
        mode: "fixed_absent_only_scd2_mapping_creation",
        side_effects: true,
        repository_connection_attempted: true,
        *: safeFlags(),
        result: [
            status: "passed",
            mapping_name: MAPPING_NAME,
            source_component_count: 1,
            target_component_count: 1,
            target_attribute_count: 26,
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
        try {
            txnMgr.rollback(txnStatus)
            rollbackSucceeded = true
        } catch (Throwable ignored) {}
    }
    // A commit that raised may still have applied, and a later rollback cannot undo or
    // disprove it, so that outcome stays unknown whatever the rollback did. A failed
    // rollback before any commit cannot claim a clean result either.
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
    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_mapping_create",
        status: reportStatus,
        mode: "fixed_absent_only_scd2_mapping_creation",
        side_effects: sideEffects,
        repository_connection_attempted: true,
        *: safeFlags(),
        error_type: e.getClass().getSimpleName() ?: "UnnamedError",
        repository_outcome: repositoryOutcome,
        commit_attempted: commitAttempted,
        commit_acknowledged: commitAcknowledged,
        rollback_attempted: rollbackAttempted,
        rollback_succeeded: rollbackSucceeded,
        manual_recovery_review_required: reportStatus != "error",
    ])
} finally {
    if (standaloneMode && odi != null) {
        // The report is already out. A close failure cannot change the repository outcome.
        try {
            odi.close()
        } catch (Throwable ignored) {}
    }
}

