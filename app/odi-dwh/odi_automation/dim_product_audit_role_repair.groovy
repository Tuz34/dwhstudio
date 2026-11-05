/*
  ODI-87 fixed DIM_PRODUCT audit-role preflight, repair and restore.
  Object identities, columns, roles and expressions are not caller-controlled.
  Role actions write only OdiColumn.setScdType on four audit columns.
  Stable actions may also call MapAttribute.setExpressionText on those four.
  No execute hint, scenario, database row or other column is changed.
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
    stable_preflight: "odi87_dim_product_stable_audit_preflight",
    stable_repair: "odi87_dim_product_stable_audit_repair",
    stable_restore: "odi87_dim_product_stable_audit_restore",
]
final String VOLATILE_PROFILE = "odi87_dim_product_audit_volatile_v1"
final String STABLE_PROFILE = "odi87_dim_product_audit_stable_v1"
final Set<String> LEGACY_FLAGS = ["Y", "'Y'", "N", "'N'"] as Set
final Set<String> LEGACY_ENDS = [
    "DATE '9999-12-31'",
    "TO_DATE('99991231','YYYYMMDD')",
    "to_date ('12-31-9999', 'mm-dd-yyyy')",
] as Set
final Map<String, String> FIXED_LITERALS = [
    PRODUCT_SK: "DWH.SEQ_DIM_PRODUCT.NEXTVAL",
    EFFECTIVE_FROM: "CURRENT_DATE",
    EFFECTIVE_TO: "TO_DATE('24000101','YYYYMMDD')",
    CURRENT_FLAG: "1",
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
List<String> changedExpressions = []
String expressionProfile = null

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
        changed_expressions: changedExpressions,
        expression_profile: expressionProfile,
    ])
}

def matchPredicate = { String alias ->
    List<String> parts = ["C.PROD_ID = ${alias}.PROD_ID", "C.CURRENT_FLAG = '1'"]
    TRACKED_COLUMNS.each { String column ->
        parts.add("DECODE(C.${column}, ${alias}.${column}, 0, 1) = 0")
    }
    parts.join(" AND ")
}

def stableText = { String column, String alias ->
    String predicate = matchPredicate(alias)
    String preserved = "SELECT C.${column} FROM DWH.DIM_PRODUCT C WHERE ${predicate}"
    String exists = "SELECT 1 FROM DWH.DIM_PRODUCT C WHERE ${predicate}"
    "CASE WHEN EXISTS (${exists}) THEN (${preserved}) ELSE ${AUDIT_EXPRESSIONS[column]} END"
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

    String alias = sourceComponents[0].getName()
    if (!(alias ==~ /^[A-Z][A-Z0-9_]{0,29}$/)) block("source_alias_invalid")
    List attributes = targetComponents[0].getAttributes() as List
    List<String> attributeNames = attributes*.getName()
    if (attributeNames.size() != TARGET_COLUMN_COUNT || (attributeNames as Set) != expected) block("mapping_contract")
    Map attributesByName = attributes.collectEntries { [(it.getName()): it] }
    Map<String, String> expressions = [:]
    Map<String, String> hints = [:]
    attributes.each { attribute ->
        String name = attribute.getName()
        // Expression bodies are compared here and never reported.
        String text = attribute.getExpression()?.getText()
        def hint = attribute.getExecuteOnHint()
        String hintName = hint == null ? "NO_HINT" : hint.name()
        expressions[name] = text
        hints[name] = hintName
        if (AUDIT_COLUMNS.contains(name) && hintName == "TARGET") block("audit_execute_hint_target")
        if (name == "PRODUCT_SK" && hintName != "TARGET") block("execute_hint_drift")
    }
    (["PROD_ID"] + TRACKED_COLUMNS).each { String name ->
        if (expressions[name] != "${alias}.${name}") block("fixed_expression_drift")
    }
    FIXED_LITERALS.each { String name, String literal ->
        String text = expressions[name]
        if (text == literal) return
        if (name == "CURRENT_FLAG" && text in LEGACY_FLAGS) block("legacy_expression_gap")
        if (name == "EFFECTIVE_TO" && text in LEGACY_ENDS) block("legacy_expression_gap")
        block("fixed_expression_drift")
    }
    boolean volatileProfile = AUDIT_COLUMNS.every { expressions[it] == AUDIT_EXPRESSIONS[it] }
    boolean stableProfile = AUDIT_COLUMNS.every { expressions[it] == stableText(it, alias) }
    if (!volatileProfile && !stableProfile) block("audit_expression_drift")
    [
        byName: byName,
        attributes: attributesByName,
        image: image,
        alias: alias,
        hints: hints,
        profile: volatileProfile ? VOLATILE_PROFILE : STABLE_PROFILE,
    ]
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
    boolean readOnly = action in ["preflight", "stable_preflight"]
    boolean stableAction = action.startsWith("stable_")
    Map<String, String> expectedBefore = readOnly ? null : parseImage(System.getProperty("odi.audit.expected_before"))
    String expectedProfile = null
    List<String> restoreColumns = []
    List<String> declaredExpressions = []
    if (stableAction && !readOnly) {
        expectedProfile = System.getProperty("odi.audit.expression_profile_before")
        if (!(expectedProfile in [VOLATILE_PROFILE, STABLE_PROFILE])) block("invalid_input")
        String rawExpressions = System.getProperty("odi.audit.changed_expressions")
        declaredExpressions = rawExpressions == null || rawExpressions.isEmpty() ? [] : rawExpressions.split(",", -1).toList()
        List<String> orderedExpressions = AUDIT_COLUMNS.findAll { it in declaredExpressions }
        if (declaredExpressions != orderedExpressions || !(declaredExpressions.isEmpty() || declaredExpressions == AUDIT_COLUMNS)) {
            block("invalid_input")
        }
    }
    if (action == "restore" || action == "stable_restore") {
        String changed = System.getProperty("odi.audit.changed_columns")
        restoreColumns = changed == null || changed.isEmpty() ? [] : changed.split(",", -1).toList()
        List<String> orderedColumns = AUDIT_COLUMNS.findAll { it in restoreColumns }
        if (restoreColumns != orderedColumns) block("invalid_input")
        if (action == "restore" && restoreColumns.isEmpty()) block("invalid_input")
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

    if (readOnly) {
        String outcomeStatus
        if (action == "stable_preflight") {
            expressionProfile = contract.profile
            boolean rolesDone = current.values().every { it == OVERWRITE_ROLE }
            boolean expressionsDone = contract.profile == STABLE_PROFILE
            outcomeStatus = rolesDone && expressionsDone ? "already_repaired" : "ready_for_repair"
        } else {
            outcomeStatus = current.values().any { it == NULL_ROLE } ? "ready_for_repair" : "already_repaired"
        }
        endWithoutWrite()
        println report(outcomeStatus, "no_write", false, null, null, false, false)
    } else {
        Map<String, String> wanted
        String wantedProfile = contract.profile
        if (action == "repair" || action == "stable_repair") {
            if (current != expectedBefore) block("before_image_drift")
            if (stableAction && contract.profile != expectedProfile) block("expression_profile_drift")
            changedColumns = AUDIT_COLUMNS.findAll { current[it] == NULL_ROLE }
            if (stableAction) {
                changedExpressions = contract.profile == STABLE_PROFILE ? [] : new ArrayList<String>(AUDIT_COLUMNS)
                String declaredRaw = System.getProperty("odi.audit.changed_columns")
                List<String> declaredColumns = declaredRaw == null || declaredRaw.isEmpty() ? [] : declaredRaw.split(",", -1).toList()
                if (declaredColumns != changedColumns || declaredExpressions != changedExpressions) block("invalid_input")
            }
            wanted = AUDIT_COLUMNS.collectEntries { [(it): OVERWRITE_ROLE] }
            wantedProfile = STABLE_PROFILE
        } else if (action == "stable_restore") {
            Map<String, String> repaired = AUDIT_COLUMNS.collectEntries { [(it): it in restoreColumns ? OVERWRITE_ROLE : expectedBefore[it]] }
            boolean atBefore = current == expectedBefore && contract.profile == expectedProfile
            boolean atRepaired = current == repaired && contract.profile == STABLE_PROFILE
            if (!atBefore && !atRepaired) block("restore_state_drift")
            // A volatile before-image cannot also be the stable repaired profile.
            if (!declaredExpressions.isEmpty() && expectedProfile == STABLE_PROFILE) block("invalid_input")
            changedColumns = atRepaired && repaired != expectedBefore ? new ArrayList<String>(restoreColumns) : []
            changedExpressions = atRepaired && !declaredExpressions.isEmpty() ? new ArrayList<String>(declaredExpressions) : []
            wanted = expectedBefore
            wantedProfile = expectedProfile
        } else {
            // Only the exact receipt state or the exact repaired state is acceptable.
            Map<String, String> repaired = AUDIT_COLUMNS.collectEntries { [(it): it in restoreColumns ? OVERWRITE_ROLE : expectedBefore[it]] }
            if (current != expectedBefore && current != repaired) block("restore_state_drift")
            changedColumns = current == repaired && repaired != expectedBefore ? new ArrayList<String>(restoreColumns) : []
            wanted = expectedBefore
        }
        boolean nothingToWrite = changedColumns.isEmpty() && changedExpressions.isEmpty()
        if (nothingToWrite) {
            if (stableAction) expressionProfile = contract.profile
            String outcomeStatus = action in ["repair", "stable_repair"] ? "already_repaired" : "already_restored"
            endWithoutWrite()
            println report(outcomeStatus, "no_write", false, null, null, false, false)
        } else {
            writeAttempted = true
            Map<String, String> hintsBefore = new LinkedHashMap<String, String>(contract.hints)
            changedColumns.each { String name ->
                OdiColumn column = contract.byName[name]
                column.setScdType(wanted[name] == NULL_ROLE ? null : OdiColumn.ScdType.valueOf(wanted[name]))
            }
            if (stableAction) {
                expressionProfile = wantedProfile
                changedExpressions.each { String name ->
                    String text = wantedProfile == STABLE_PROFILE ? stableText(name, contract.alias) : AUDIT_EXPRESSIONS[name]
                    contract.attributes[name].setExpressionText(text)
                }
            }
            em.flush()
            Map verified = readContract(em, project, folder)
            if (verified.image != wanted) block("post_write_verification")
            if (verified.hints != hintsBefore) block("post_write_verification")
            if (stableAction && verified.profile != wantedProfile) block("post_write_verification")
            if (!stableAction && verified.profile != contract.profile) block("post_write_verification")
            afterImage = verified.image
            activeSessionCount = countActiveSessions(em)
            if (activeSessionCount != 0) block("active_odi_sessions")
            commitAttempted = true
            txnMgr.commit(txnStatus)
            commitAcknowledged = true
            txnStatus = null
            boolean repairing = action in ["repair", "stable_repair"]
            println report(repairing ? "repaired" : "restored", "committed", true, null, null, false, true)
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
