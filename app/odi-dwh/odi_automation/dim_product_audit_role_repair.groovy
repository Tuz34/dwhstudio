/*
  ODI-87 fixed DIM_PRODUCT audit-role preflight, repair and restore.
  Object identities, columns, roles and expressions are not caller-controlled.
  Writes only OdiColumn.setScdType on four audit columns, in one transaction.
  No scenario, mapping, database row or other column is changed.
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
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.model.finder.IOdiModelFinder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.session.OdiSession
import oracle.odi.domain.runtime.session.Status
import oracle.odi.domain.runtime.session.finder.IOdiSessionFinder

class AuditRoleBlock extends RuntimeException {
    final String code
    AuditRoleBlock(String code) {
        super(code, null, false, false)
        this.code = code
    }
}

final String TOOL = "odi.dim_product_audit_role_repair"
final String MODE = "fixed_dim_product_audit_role_repair"
final String MODEL_CODE = "TGT_DWH"
final String MAPPING_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
final String SOURCE_TABLE = "ODS_SH_PRODUCTS"
final String TARGET_TABLE = "DIM_PRODUCT"
final String NULL_ROLE = "NULL"
final String OVERWRITE_ROLE = "OVERWRITE_ON_CHANGE"
final Map<String, String> SCOPES = [
    preflight: "odi87_dim_product_audit_role_preflight",
    repair: "odi87_dim_product_audit_role_repair",
    restore: "odi87_dim_product_audit_role_restore",
]
final List<String> AUDIT_COLUMNS = ["INSERT_DATE", "UPDATE_DATE", "SOURCE_SYSTEM", "ETL_BATCH_ID"]
final List<String> TRACKED_COLUMNS = [
    "PROD_NAME", "PROD_DESC", "PROD_SUBCATEGORY", "PROD_SUBCATEGORY_ID", "PROD_SUBCATEGORY_DESC",
    "PROD_CATEGORY", "PROD_CATEGORY_ID", "PROD_CATEGORY_DESC", "PROD_WEIGHT_CLASS",
    "PROD_UNIT_OF_MEASURE", "PROD_PACK_SIZE", "SUPPLIER_ID", "PROD_STATUS", "PROD_LIST_PRICE",
    "PROD_MIN_PRICE", "PROD_TOTAL", "PROD_TOTAL_ID",
]
final Map<String, String> SINGLE_ROLES = [
    PRODUCT_SK: "SURROGATE_KEY",
    PROD_ID: "NATURAL_KEY",
    EFFECTIVE_FROM: "START_TIMESTAMP",
    EFFECTIVE_TO: "END_TIMESTAMP",
    CURRENT_FLAG: "CURRENT_RECORD_FLAG",
]
final Map<String, String> AUDIT_EXPRESSIONS = [
    INSERT_DATE: "CURRENT_TIMESTAMP",
    UPDATE_DATE: "CURRENT_TIMESTAMP",
    SOURCE_SYSTEM: "'SH'",
    ETL_BATCH_ID: "<%=odiRef.getSession(\"SESS_NO\")%>",
]
final int SOURCE_COLUMN_COUNT = 24
final int TARGET_COLUMN_COUNT = 26
final java.util.regex.Pattern PROJECT_NAME = ~/^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,127}$/

def odi = null
boolean standaloneMode = false
boolean connectionAttempted = false
ITransactionManager txnMgr = null
ITransactionStatus txnStatus = null
boolean writeAttempted = false
boolean commitAttempted = false
boolean commitAcknowledged = false
boolean rollbackAttempted = false
boolean rollbackSucceeded = false
String action = System.getProperty("odi.audit.action")
Integer activeSessionCount = null
Map<String, String> beforeImage = null
Map<String, String> afterImage = null
List<String> changedColumns = []

def block = { String code -> throw new AuditRoleBlock(code) }

def report = { String status, String outcome, Boolean sideEffects, String code, String errorType, boolean manual, boolean regeneration ->
    JsonOutput.toJson([
        tool: TOOL,
        mode: MODE,
        action: action in SCOPES.keySet() ? action : "preflight",
        status: status,
        code: code,
        error_type: errorType,
        side_effects: sideEffects,
        repository_connection_attempted: connectionAttempted,
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
        repository_outcome: outcome,
        write_attempted: writeAttempted,
        commit_attempted: commitAttempted,
        commit_acknowledged: commitAcknowledged,
        rollback_attempted: rollbackAttempted,
        rollback_succeeded: rollbackSucceeded,
        manual_recovery_review_required: manual,
        scenario_regeneration_required: regeneration,
        active_session_count: activeSessionCount,
        before_image: beforeImage,
        after_image: afterImage,
        changed_columns: changedColumns,
    ])
}

def parseImage = { String text ->
    List<String> pairs = text == null ? [] : text.split(",", -1).toList()
    if (pairs.size() != AUDIT_COLUMNS.size()) block("invalid_input")
    Map<String, String> image = [:]
    pairs.eachWithIndex { String pair, int index ->
        List<String> parts = pair.split("=", -1).toList()
        if (parts.size() != 2 || parts[0] != AUDIT_COLUMNS[index] || !(parts[1] in [NULL_ROLE, OVERWRITE_ROLE])) {
            block("invalid_input")
        }
        image[parts[0]] = parts[1]
    }
    image
}

// Re-read on every call: one validation before a write and one after it.
def readContract = { em, String project, String folder ->
    List matchingProjects = ((IOdiProjectFinder) em.getFinder(OdiProject.class)).findAll().findAll { it.getCode() == project } as List
    if (matchingProjects.size() != 1) block("project_contract")
    List matchingFolders = matchingProjects[0].getFolders().findAll { it.getName() == folder } as List
    if (matchingFolders.size() != 1) block("project_contract")

    List matchingModels = ((IOdiModelFinder) em.getFinder(OdiModel.class)).findAll().findAll { it.getCode() == MODEL_CODE } as List
    if (matchingModels.size() != 1) block("model_contract")
    if (matchingModels[0].getTechnology()?.getName()?.toUpperCase() != "ORACLE") block("model_contract")

    List stores = ((IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)).findByModel(MODEL_CODE) as List
    List targets = stores.findAll { it.getName() == TARGET_TABLE }
    List sources = stores.findAll { it.getName() == SOURCE_TABLE }
    if (targets.size() != 1 || sources.size() != 1) block("datastore_contract")
    OdiDataStore target = targets[0]
    if (sources[0].getColumns().size() != SOURCE_COLUMN_COUNT) block("column_contract")

    List columns = target.getColumns() as List
    List<String> names = columns*.getName()
    Set<String> expected = (SINGLE_ROLES.keySet() + TRACKED_COLUMNS + AUDIT_COLUMNS) as Set
    if (names.size() != TARGET_COLUMN_COUNT || (names as Set) != expected) block("column_contract")
    Map byName = columns.collectEntries { [(it.getName()): it] }
    SINGLE_ROLES.each { String name, String role ->
        if (byName[name].getScdType()?.name() != role) block("role_contract")
    }
    TRACKED_COLUMNS.each { String name ->
        if (byName[name].getScdType()?.name() != "ADD_ROW_ON_CHANGE") block("role_contract")
    }
    Map<String, String> image = AUDIT_COLUMNS.collectEntries { String name ->
        [(name): byName[name].getScdType()?.name() ?: NULL_ROLE]
    }
    if (image.values().any { !(it in [NULL_ROLE, OVERWRITE_ROLE]) }) block("audit_role_unexpected")

    Mapping mapping = em.getFinder(Mapping.class).findByName(matchingFolders[0], MAPPING_NAME)
    if (mapping == null) block("mapping_contract")
    List components = mapping.getAllComponents().findAll { it instanceof DatastoreComponent } as List
    List sourceComponents = components.findAll { it.getBoundDataStore()?.getName() == SOURCE_TABLE }
    List targetComponents = components.findAll { it.getBoundDataStore()?.getName() == TARGET_TABLE }
    if (components.size() != 2 || sourceComponents.size() != 1 || targetComponents.size() != 1) block("mapping_contract")
    if (sourceComponents[0].getBoundDataStore().getModel()?.getCode() != MODEL_CODE) block("mapping_contract")
    if (targetComponents[0].getBoundDataStore().getModel()?.getCode() != MODEL_CODE) block("mapping_contract")
    if (!(targetComponents[0].getIntegrationType() in ["SCD", "SLOWLY_CHANGING_DIMENSION"])) block("mapping_contract")

    List attributes = targetComponents[0].getAttributes() as List
    List<String> attributeNames = attributes*.getName()
    if (attributeNames.size() != TARGET_COLUMN_COUNT || (attributeNames as Set) != expected) block("mapping_contract")
    attributes.each { attribute ->
        String name = attribute.getName()
        // Expression bodies are compared here and never reported.
        String text = attribute.getExpression()?.getText()
        if (AUDIT_EXPRESSIONS.containsKey(name)) {
            if (text != AUDIT_EXPRESSIONS[name]) block("audit_expression_drift")
        } else if (text == null || text.trim().isEmpty()) {
            block("mapping_contract")
        }
    }
    [byName: byName, image: image]
}

def countActiveSessions = { em ->
    IOdiSessionFinder finder = (IOdiSessionFinder) em.getFinder(OdiSession.class)
    [Status.RUNNING, Status.WAITING, Status.QUEUED].sum { finder.countByStatus(it) } as Integer
}

def endWithoutWrite = {
    rollbackAttempted = true
    txnMgr.rollback(txnStatus)
    rollbackSucceeded = true
    txnStatus = null
}

try {
    String project = System.getProperty("odi.project")
    String folder = System.getProperty("odi.folder")
    if (!(action in SCOPES.keySet())) block("invalid_input")
    if (System.getProperty("odi.audit.approved") != "true") block("invalid_input")
    if (System.getProperty("odi.audit.approval_scope") != SCOPES[action]) block("invalid_input")
    if (project == null || folder == null || !(project ==~ PROJECT_NAME) || !(folder ==~ PROJECT_NAME)) block("invalid_input")
    Map<String, String> expectedBefore = action == "preflight" ? null : parseImage(System.getProperty("odi.audit.expected_before"))
    List<String> restoreColumns = []
    if (action == "restore") {
        String changed = System.getProperty("odi.audit.changed_columns")
        restoreColumns = changed == null || changed.isEmpty() ? [] : changed.split(",", -1).toList()
        if (restoreColumns.isEmpty() || restoreColumns.size() != (restoreColumns as Set).size()) block("invalid_input")
        if (restoreColumns.any { !(it in AUDIT_COLUMNS) || expectedBefore[it] != NULL_ROLE }) block("invalid_input")
    }

    connectionAttempted = true
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
        WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(props.getProperty("work_name"), poolAttrs)
        odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
        Authentication auth = odi.getSecurityManager().createAuthentication(
            props.getProperty("odi_username"),
            props.getProperty("odi_password").toCharArray()
        )
        odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    }

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    def em = odi.getTransactionalEntityManager()

    activeSessionCount = countActiveSessions(em)
    if (activeSessionCount != 0) block("active_odi_sessions")
    Map contract = readContract(em, project, folder)
    Map<String, String> current = contract.image
    beforeImage = current

    if (action == "preflight") {
        String outcomeStatus = current.values().any { it == NULL_ROLE } ? "ready_for_repair" : "already_repaired"
        endWithoutWrite()
        println report(outcomeStatus, "no_write", false, null, null, false, false)
    } else {
        Map<String, String> wanted
        if (action == "repair") {
            if (current != expectedBefore) block("before_image_drift")
            changedColumns = AUDIT_COLUMNS.findAll { current[it] == NULL_ROLE }
            wanted = AUDIT_COLUMNS.collectEntries { [(it): OVERWRITE_ROLE] }
        } else {
            // Only the exact receipt state or the exact repaired state is acceptable.
            Map<String, String> repaired = AUDIT_COLUMNS.collectEntries { [(it): it in restoreColumns ? OVERWRITE_ROLE : expectedBefore[it]] }
            if (current != expectedBefore && current != repaired) block("restore_state_drift")
            changedColumns = current == repaired && repaired != expectedBefore ? new ArrayList<String>(restoreColumns) : []
            wanted = expectedBefore
        }
        if (changedColumns.isEmpty()) {
            String outcomeStatus = action == "repair" ? "already_repaired" : "already_restored"
            endWithoutWrite()
            println report(outcomeStatus, "no_write", false, null, null, false, false)
        } else {
            writeAttempted = true
            changedColumns.each { String name ->
                OdiColumn column = contract.byName[name]
                column.setScdType(wanted[name] == NULL_ROLE ? null : OdiColumn.ScdType.valueOf(wanted[name]))
            }
            em.flush()
            Map verified = readContract(em, project, folder)
            if (verified.image != wanted) block("post_write_verification")
            afterImage = verified.image
            commitAttempted = true
            txnMgr.commit(txnStatus)
            commitAcknowledged = true
            txnStatus = null
            println report(action == "repair" ? "repaired" : "restored", "committed", true, null, null, false, true)
        }
    }
} catch (Throwable failure) {
    String code = failure instanceof AuditRoleBlock ? failure.code : null
    String errorType = code == null ? (failure.getClass().getSimpleName() ?: "UnnamedError") : null
    if (txnStatus != null && txnMgr != null) {
        rollbackAttempted = true
        rollbackSucceeded = false
        try {
            txnMgr.rollback(txnStatus)
            rollbackSucceeded = true
        } catch (Throwable ignored) {}
    }
    // A commit that raised may have applied, and no later rollback can undo or disprove it.
    if (commitAcknowledged) {
        println report("committed_report_incomplete", "committed", true, null, errorType ?: "AuditRoleBlock", true, true)
    } else if (commitAttempted) {
        println report("outcome_unknown", "commit_outcome_unknown", null, null, errorType ?: "AuditRoleBlock", true, true)
    } else if (rollbackAttempted && !rollbackSucceeded) {
        println report("outcome_unknown", "rollback_failed", null, code, errorType, true, writeAttempted)
    } else {
        String outcome = rollbackAttempted ? "rolled_back" : "no_transaction"
        println report(code != null ? "blocked" : "aborted", outcome, false, code, errorType, false, false)
    }
} finally {
    if (standaloneMode && odi != null) {
        // The report is already out. A close failure cannot change the repository outcome.
        try {
            odi.close()
        } catch (Throwable ignored) {}
    }
}
