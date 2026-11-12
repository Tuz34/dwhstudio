/*
  NL-DWH Studio: Mapping Creation (Dynamic & Robust)
  Uses System Properties passed from OdiOrchestrator
*/

import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.project.*
import oracle.odi.domain.mapping.*
import oracle.odi.domain.mapping.component.*
import oracle.odi.domain.mapping.physical.*
import oracle.odi.domain.project.finder.*
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*
import oracle.odi.domain.topology.OdiContext
import oracle.odi.domain.root.RootIssue
import groovy.json.JsonOutput

def selectExactIkm = { allIkms, String requested, String approvedNames, String requiredId = null ->
    Set<String> approved = (approvedNames ?: "").split(",").collect { it.trim().toLowerCase(Locale.ROOT) } as Set
    if (!requested?.trim() || !approved.contains(requested.trim().toLowerCase(Locale.ROOT))) {
        throw new IllegalArgumentException("An exact approved odi.ikm_name is required")
    }
    def matches = allIkms.findAll { it.getName().equalsIgnoreCase(requested.trim()) }
    if (matches.size() != 1) {
        println '{"status":"error","action":"create_mapping","error_code":"ikm_not_found_or_ambiguous"}'
        throw new IllegalStateException("Requested IKM was not found uniquely in the repository")
    }
    if (requiredId != null && matches[0].getInternalId()?.toString() != requiredId) {
        throw new IllegalStateException("Repository IKM identity changed after preflight")
    }
    return matches[0]
}

def selectUpdateKey = { keys, Set<String> requiredColumns, String requiredType ->
    if (requiredType != null && !(requiredType in ["PRIMARY_KEY", "ALTERNATE_KEY"])) {
        throw new IllegalArgumentException("Invalid required update key type")
    }
    def matches = keys.findAll { key ->
        String kind = key.getKeyType().name()
        Set<String> columns = key.getColumns().collect { it.getName().toUpperCase(Locale.ROOT) } as Set
        key.isActive() && key.isInDatabase() && (kind in ["PRIMARY_KEY", "ALTERNATE_KEY"]) &&
            (requiredType == null || kind == requiredType) &&
            (requiredColumns.isEmpty() || columns == requiredColumns)
    }
    if (matches.isEmpty() && !requiredColumns.isEmpty()) {
        throw new IllegalStateException("Required update key is missing or mismatched (type, active, database, columns)")
    }
    return matches.sort { it.getName() }.find { true }
}

def verifySourceBinding = { source, String owner, String resource, context, String requiredId ->
    def physical = source.getModel().getLogicalSchema()?.getPhysicalSchema(context)
    String actualResource = source.getResourceName()?.toUpperCase(Locale.ROOT)
    if (context == null || source.isShortcut() || !physical?.getSchemaName()?.equalsIgnoreCase(owner) ||
        !(actualResource in [resource, owner + "." + resource]) ||
        (requiredId != null && source.getInternalId()?.toString() != requiredId)) {
        throw new IllegalStateException("Source physical identity changed or is unverified")
    }
}

// Same logical schema (hence the same data server and physical schema in every context) means the
// source needs no access point and no load KM. Different logical schemas stay on the LKM path.
def sameLogicalSchema = { a, b ->
    def la = a?.getModel()?.getLogicalSchema()
    def lb = b?.getModel()?.getLogicalSchema()
    if (la == null || lb == null) return false
    if (la.is(lb)) return true
    def ia = la.getInternalId()
    def ib = lb.getInternalId()
    return ia != null && ib != null && ia.toString() == ib.toString() && la.getName() == lb.getName()
}

// Bounded, redacted projection of SDK validation issues for the structured error line.
def summarizeIssues = { issues ->
    List out = []
    (issues ?: []).each { issue ->
        if (out.size() >= 20) return
        String level = "ISSUE"
        String text = ""
        try { level = String.valueOf(issue.getLevel()) } catch (Throwable ignoredLevel) {}
        try { text = String.valueOf(issue.getMessage()) } catch (Throwable ignoredText) { text = issue.getClass().getSimpleName() }
        text = text.replaceAll(/[\r\n\t]+/, " ")
            .replaceAll(/(?i)\b(jdbc|https?|ldaps?|t3s?):\S+/, '***')
            .replaceAll(/(?i)(password|passwd|pwd|secret|token|key|credential)s?\s*[=:]\s*("[^"]*"|'[^']*'|\S+)/, '$1=***')
            .replaceAll(/"[^"]*"|'[^']*'/, "'***'")
            .trim()
        String entry = level + ": " + text
        if (entry.length() > 300) entry = entry.substring(0, 300)
        if (level in ["ERROR", "FATAL", "WARNING"]) out.add(entry)
    }
    return out
}

// True when the SDK reports validation errors; collects the issues for the structured error line.
// SDK 12.2.1.4: validate() can also return FALSE after adding a FATAL RootIssue (non-overridable
// target load order), so both signals block the replace and the commit.
def mappingHasErrors = { mp, List issues ->
    return mp.validate(issues, false) == true || RootIssue.isErrorOrFatal(issues)
}

// On failure also ask the physical design for its own messages (the logical result is often a bare
// false) and record the node names; never throws, so the original failure stays the reported one.
def collectDesignFailure = { dsg, List issues, Map diag ->
    try { dsg.validate(issues, false) } catch (Exception designValidateError) {
        println "DESIGN VALIDATE UNAVAILABLE: ${designValidateError.getClass().getSimpleName()}"
    }
    try { diag.physical_nodes = dsg.getPhysicalNodes().collect { it.getName() } } catch (Exception diagError) {}
    return summarizeIssues(issues)
}

def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
    String propsPath = (args?.length ? args[0] : "odi_connection.properties")
    Properties props = new Properties()
    new FileInputStream(propsPath).withStream { props.load(it) }
    PoolingAttributes poolAttrs = new PoolingAttributes()
    MasterRepositoryDbInfo masterRepo = new MasterRepositoryDbInfo(
        props.getProperty("master_jdbc"), props.getProperty("master_jdbc_driver"),
        props.getProperty("master_username"), props.getProperty("master_password").toCharArray(), poolAttrs
    )
    WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(props.getProperty("work_name"), poolAttrs)
    odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
    Authentication auth = odi.getSecurityManager().createAuthentication(
        props.getProperty("odi_username"), props.getProperty("odi_password").toCharArray()
    )
    odi.getSecurityManager().setCurrentThreadAuthentication(auth)
}

// Build the replacement under another name, validate it, then rename-swap
// inside this same transaction. A failed build rolls the delete back.
def temporaryReplacementName = { String finalName ->
    String suffix = "__NEW"
    String base = (finalName == null || finalName.trim().isEmpty()) ? "MAP" : finalName.trim()
    int maxLen = 128
    if (base.length() + suffix.length() > maxLen) {
        base = base.substring(0, maxLen - suffix.length())
    }
    String candidate = base + suffix
    if (finalName != null && candidate.equalsIgnoreCase(finalName)) {
        candidate = base + "_NEW"
    }
    return candidate
}

// The class name is the only value Python accepts for this failure.
class audit_type_unknown extends IllegalStateException {
    audit_type_unknown() { super("audit_type_unknown") }
}

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition())
boolean preserveExistingOnFailure = false
def existingMapping = null
List validationMessages = []
Map physicalDiag = [same_schema: false, access_points: 0, access_points_with_lkm: 0, access_point_names: [],
    physical_nodes: [], source_moved: false]

try {
    def em = odi.getTransactionalEntityManager()
    
    // Dynamic params from System Properties
    String mName = System.getProperty("odi.mapping") ?: "MAP_NEW_DEFAULT"
    String pCode = System.getProperty("odi.project") ?: "TUTORIOL"
    String fName = System.getProperty("odi.folder")  ?: "First Folder"
    
    String srcDSName = System.getProperty("odi.src_table")
    String srcModName = System.getProperty("odi.src_model")
    String tgtDSName = System.getProperty("odi.tgt_table")
    String tgtModName = System.getProperty("odi.tgt_model") ?: "DWH"
    String sourceSystemName = System.getProperty("odi.source_system") ?: "ORACLE_SAMPLE_SCHEMAS"
    String requiredUpdateKeyColumnsStr = System.getProperty("odi.required_update_key_columns")
    String requiredUpdateKeyType = System.getProperty("odi.required_update_key_type")
    // Integration type: IU (default, incremental update) or CONTROL_APPEND (insert-style IKM only).
    String integrationTypeParam = (System.getProperty("odi.integration_type") ?: "IU").trim().toUpperCase(Locale.ROOT)
    if (!(integrationTypeParam in ["IU", "CONTROL_APPEND"])) {
        throw new Exception("Invalid odi.integration_type")
    }
    boolean controlAppend = integrationTypeParam == "CONTROL_APPEND"
    String physItCode = controlAppend ? "CA" : "IU"
    String replaceExistingRaw = System.getProperty("odi.replace_existing_mapping", "false")
    if (!(replaceExistingRaw.equalsIgnoreCase("true") || replaceExistingRaw.equalsIgnoreCase("false"))) {
        throw new Exception("Invalid boolean for odi.replace_existing_mapping")
    }
    boolean replaceExistingMapping = replaceExistingRaw.equalsIgnoreCase("true")

    if (!srcDSName || !tgtDSName) throw new Exception("MISSING PARAMS: src_table or tgt_table")

    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    if (project == null) throw new Exception("Project not found: " + pCode)
    
    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    if (folder == null) throw new Exception("Folder not found: " + fName)
    
    // Do not delete the current mapping until the replacement has been validated.
    def mapFinder = em.getFinder(Mapping.class)
    existingMapping = mapFinder.findByName(folder, mName)
    String persistedName = mName
    if (existingMapping != null) {
        if (!replaceExistingMapping) {
            throw new Exception("Mapping already exists and replacement was not approved: " + mName)
        }
        preserveExistingOnFailure = true
        persistedName = temporaryReplacementName(mName)
        def stagingClash = mapFinder.findByName(folder, persistedName)
        if (stagingClash != null) {
            throw new IllegalStateException("Replacement staging name is already in use")
        }
    }
    
    // Custom helper to find datastore robustly
    def findDsRobust = { emObj, dsName, modName ->
        def finder = (IOdiDataStoreFinder) emObj.getFinder(OdiDataStore.class)
        // 1. Direct match
        def ds = finder.findByName(dsName, modName)
        if (ds != null) return ds
        
        // 2. Strip schema name (e.g. Sales.Customer -> Customer)
        String shortName = dsName
        if (dsName.contains(".")) {
            shortName = dsName.substring(dsName.indexOf(".") + 1)
            ds = finder.findByName(shortName, modName)
            if (ds != null) return ds
        }

        // 3. Exact match uppercase
        ds = finder.findByName(shortName.toUpperCase(), modName)
        if (ds != null) return ds
        
        // 4. In case of lowercase
        ds = finder.findByName(shortName.toLowerCase(), modName)
        if (ds != null) return ds

        // 5. Try capitalizing first letter
        if (shortName.length() > 0) {
            String cap = shortName.substring(0,1).toUpperCase() + shortName.substring(1).toLowerCase()
            ds = finder.findByName(cap, modName)
            if (ds != null) return ds
        }
        
        return null
    }

    OdiDataStore srcDS = findDsRobust(em, srcDSName, srcModName)
    if (srcDS == null) throw new Exception("Source Datastore not found: " + srcDSName + " in " + srcModName)
    String requiredSourceOwner = System.getProperty("odi.required_src_owner")
    if (requiredSourceOwner != null) {
        def sourceContext = em.getFinder(OdiContext.class).findByCode(System.getProperty("odi.source_context"))
        verifySourceBinding(srcDS, requiredSourceOwner, System.getProperty("odi.required_src_resource"),
            sourceContext, System.getProperty("odi.required_src_id"))
    }
    
    OdiDataStore tgtDS = findDsRobust(em, tgtDSName, tgtModName)
    if (tgtDS == null) throw new Exception("Target Datastore not found: " + tgtDSName + " in " + tgtModName)

    Mapping mapping = new Mapping(persistedName, folder)
    em.persist(mapping)
    
    DatastoreComponent srcComp = new DatastoreComponent(mapping, srcDS)
    DatastoreComponent tgtComp = new DatastoreComponent(mapping, tgtDS)

    // ── CRITICAL FIX: Integration Type = IU (Incremental Update) ─────────────
    // In ODI 12c, IKM Oracle Merge is visible and assignable in the physical design
    // only when the target component Integration Type is "IU" (Incremental Update).
    // In "CA" (Control Append/Insert) mode only INSERT IKMs are listed.
    // MOS Notes 2527685.1 and 2152565.1 document this issue.
    if (controlAppend) {
        // Resolve the append constant defensively; fail closed if the SDK does not expose it.
        def appendType = null
        ["INTEGRATION_TYPE_APPEND", "INTEGRATION_TYPE_CONTROL_APPEND"].each { constName ->
            if (appendType == null) {
                try { appendType = DatastoreComponent."${constName}" } catch (Throwable ignoredConst) {}
            }
        }
        if (appendType == null) throw new Exception("Control append integration type is unavailable")
        tgtComp.setIntegrationType(appendType)
        if (!appendType.equals(tgtComp.getIntegrationType())) {
            throw new Exception("Target integration type verify failed")
        }
        println "INTEGRATION TYPE VERIFY: ${tgtComp.getIntegrationType()}"
    }
    boolean integrationTypeSet = controlAppend
    String integrationTypeEarlyFailure = null
    if (!controlAppend) try {
        tgtComp.setIntegrationTypeCode("IU")
        println "INTEGRATION TYPE: IU (Incremental Update) → ${tgtDSName}"
        integrationTypeSet = true
    } catch (Exception ite) {
        // Fallback 1: Groovy dynamic property set
        try {
            tgtComp.integrationTypeCode = "IU"
            println "INTEGRATION TYPE: IU set via property → ${tgtDSName}"
            integrationTypeSet = true
        } catch (Exception ite2) {
            // Fallback 2: reflection
            try {
                def itField = tgtComp.getClass().getDeclaredField("mIntegrationTypeCode")
                itField.setAccessible(true)
                itField.set(tgtComp, "IU")
                println "INTEGRATION TYPE: IU forced via reflection → ${tgtDSName}"
                integrationTypeSet = true
            } catch (Exception ite3) {
                // A later setIntegrationType may still verify. Warn only if it does not.
                integrationTypeEarlyFailure = ite.getClass().getSimpleName()
            }
        }
    }

    // Optional: JOIN component (SKEY lookup and similar)
    // Parameters: odi.join_table, odi.join_model, odi.join_condition, odi.join_type
    String joinTableName = System.getProperty("odi.join_table")
    String joinModelName = System.getProperty("odi.join_model") ?: tgtModName
    String joinCondition = System.getProperty("odi.join_condition")
    String joinType      = System.getProperty("odi.join_type") ?: "LEFT_OUTER"

    DatastoreComponent joinSrcComp = null
    def joinComp = null

    if (joinTableName != null && joinCondition != null) {
        // Find the join table
        OdiDataStore joinDS = ((IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)).findByName(joinTableName, joinModelName)
        if (joinDS == null) throw new Exception("Join Datastore not found: " + joinTableName + " in " + joinModelName)

        joinSrcComp = new DatastoreComponent(mapping, joinDS)

        // Create the JoinComponent
        joinComp = new JoinComponent(mapping, "JOIN_" + joinTableName)

        // Connect the source and join tables to the JoinComponent
        srcComp.connectTo(joinComp)
        joinSrcComp.connectTo(joinComp)

        // Set the join condition and join type
        // Replace SRC. and JOIN. prefixes inside joinCondition with the real component names
        String resolvedCondition = joinCondition
            .replaceAll("(?i)SRC\\.", srcComp.getName() + ".")
            .replaceAll("(?i)JOIN\\.", joinSrcComp.getName() + ".")

        joinComp.setJoinConditionText(resolvedCondition)

        // Join type: LEFT_OUTER, RIGHT_OUTER, INNER, CROSS, FULL_OUTER
        // ODI 12c: the full path is required to reach the JoinComponent.JoinType inner enum
        boolean joinTypeSet = false
        String upperJoinType = joinType.toUpperCase()

        // Method 1: enum access through the full class path
        if (!joinTypeSet) {
            try {
                def jtClass = Class.forName("oracle.odi.domain.mapping.component.JoinComponent\$JoinType")
                def jtEnum = Enum.valueOf(jtClass, upperJoinType)
                joinComp.setJoinType(jtEnum)
                joinTypeSet = true
                println "JOIN TYPE: ${upperJoinType} (assigned via enum reflection)"
            } catch (Exception e1) {
                println "DEBUG: Enum reflection failed: ${e1.getClass().getSimpleName()}"
            }
        }

        // Method 2: Groovy dynamic method — setJoinType(String)
        if (!joinTypeSet) {
            try {
                joinComp.setJoinType(upperJoinType)
                joinTypeSet = true
                println "JOIN TYPE: ${upperJoinType} (assigned via string)"
            } catch (Exception e2) {
                println "DEBUG: String setJoinType failed: ${e2.getClass().getSimpleName()}"
            }
        }

        // Method 3: property setter
        if (!joinTypeSet) {
            try {
                joinComp.joinType = upperJoinType
                joinTypeSet = true
                println "JOIN TYPE: ${upperJoinType} (assigned via property setter)"
            } catch (Exception e3) {
                println "DEBUG: Property setter failed: ${e3.getClass().getSimpleName()}"
            }
        }

        if (!joinTypeSet) {
            println "WARNING: Join type could not be assigned — the default will be used. Select LEFT_OUTER manually in ODI Studio."
        }

        // Connect the JoinComponent to the target
        joinComp.connectTo(tgtComp)
        println "JOIN: ${srcComp.getName()} LEFT JOIN ${joinSrcComp.getName()} ON ${resolvedCondition}"
    } else {
        // Parameter: odi.filter (placed on the SOURCE table to block SKEY / UK duplicates)
        String filterCond = System.getProperty("odi.filter")
        if (filterCond != null && filterCond.trim().length() > 0) {
            String resolvedFilter = filterCond.replaceAll("(?i)SRC\\.", srcComp.getName() + ".")
            FilterComponent filterComp = new FilterComponent(mapping, "FLT_" + srcComp.getName())
            filterComp.setFilterConditionText(resolvedFilter)
            
            // Source -> Filter -> Target
            srcComp.connectTo(filterComp)
            filterComp.connectTo(tgtComp)
            println "FILTER: " + resolvedFilter
        } else {
            // Connect directly when there is no join and no filter
            srcComp.connectTo(tgtComp)
        }
    }

    // ── Update Key + ORA-38104 Prevention ────────────────────────────────────
    // IKM Oracle Merge has two conditions:
    //   1) tgtComp.setUpdateKey(key) — defines the MERGE ON clause columns
    //   2) UK columns must be insert-only — they must not appear in MERGE WHEN MATCHED SET
    //      Otherwise Oracle raises ORA-38104 "Columns referenced in the ON clause cannot be updated"
    def tgtBoundDS = tgtComp.getBoundDataStore()
    Set<String> ukColNames = new HashSet<>()
    Set<String> requiredUpdateKeyColumns = new LinkedHashSet<>()
    if (requiredUpdateKeyColumnsStr != null && requiredUpdateKeyColumnsStr.trim().length() > 0) {
        requiredUpdateKeyColumnsStr.split(",").each { name ->
            String normalized = name.trim().toUpperCase(Locale.ROOT)
            if (!(normalized ==~ /[A-Z][A-Z0-9_$#]{0,127}/)) {
                throw new Exception("Invalid required update key column")
            }
            requiredUpdateKeyColumns.add(normalized)
        }
    }
    if (tgtBoundDS != null && !controlAppend) {
        def keys = tgtBoundDS.getKeys()
        def uKey = selectUpdateKey(keys, requiredUpdateKeyColumns,
            requiredUpdateKeyColumns.isEmpty() ? null : (requiredUpdateKeyType ?: "ALTERNATE_KEY"))
        if (uKey != null) {
            tgtComp.setUpdateKey(uKey)
            if (tgtComp.getUpdateKey() != uKey) {
                throw new IllegalStateException("Update key binding verification failed")
            }
            println "MAPPING UPDATE KEY SET: ${uKey.getName()}"

            // Collect the UK column names (for ORA-38104)
            try {
                uKey.getColumns().each { col -> ukColNames.add(col.getName().toUpperCase(Locale.ROOT)) }
            } catch (Exception e) {
                try {
                    uKey.getKeyColumns().each { kc ->
                        ukColNames.add(kc.getName().toUpperCase(Locale.ROOT))
                    }
                } catch (Exception e2) {}
            }
            if (!ukColNames.isEmpty()) {
                println "UK COLUMNS (insert-only): ${ukColNames}"
            }
        }
    }
    if (!requiredUpdateKeyColumns.isEmpty() && ukColNames != requiredUpdateKeyColumns) {
        throw new Exception("Required update key is missing or mismatched")
    }
    if (!requiredUpdateKeyColumns.isEmpty()) {
        try {
            tgtComp.setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_UPDATE)
            String integrationType = tgtComp.getIntegrationType()
            if (!DatastoreComponent.INTEGRATION_TYPE_UPDATE.equals(integrationType)) {
                throw new Exception("Target integration type verify failed")
            }
            println "INTEGRATION TYPE VERIFY: ${integrationType}"
            integrationTypeEarlyFailure = null
        } catch (Exception verifyError) {
            if (integrationTypeEarlyFailure == null) {
                integrationTypeEarlyFailure = verifyError.getClass().getSimpleName()
            }
            println "WARNING: Integration Type IU failed: ${integrationTypeEarlyFailure}"
            println "         IKM Oracle Merge may not be visible. Set it manually in ODI Studio."
            throw verifyError
        }
    } else if (integrationTypeEarlyFailure != null) {
        println "WARNING: Integration Type IU failed: ${integrationTypeEarlyFailure}"
        println "         IKM Oracle Merge may not be visible. Set it manually in ODI Studio."
    }

    // Attribute Mapping
    // Optional: custom column mappings through odi.expressions
    // Format: TGT_COL=EXPRESSION;TGT_COL2=EXPRESSION2
    String expressionsStr = System.getProperty("odi.expressions")
    Map<String, String> customExpressions = [:]
    if (expressionsStr != null && expressionsStr.trim().length() > 0) {
        expressionsStr.split(";").each { pair ->
            int eq = pair.indexOf("=")
            if (eq > 0) {
                String key = pair.substring(0, eq).trim().toUpperCase()
                String val = pair.substring(eq + 1).trim()
                // Replace SRC. and JOIN. prefixes with the real component names
                val = val.replaceAll("(?i)SRC\\.", srcComp.getName() + ".")
                if (joinSrcComp != null) {
                    val = val.replaceAll("(?i)JOIN\\.", joinSrcComp.getName() + ".")
                }
                customExpressions[key] = val
            }
        }
    }
    List auditColumns = []
    String auditColumnsStr = System.getProperty("odi.audit_preservation_columns")
    if (auditColumnsStr != null && auditColumnsStr.trim().length() > 0) {
        auditColumnsStr.split(",").each { rawName ->
            String cleaned = rawName == null ? "" : rawName.trim().toUpperCase(Locale.ROOT)
            if (cleaned.length() > 0 && !auditColumns.contains(cleaned)) auditColumns.add(cleaned)
        }
    }
    boolean auditQualified = integrationTypeParam == "IU" && !ukColNames.isEmpty() && sameLogicalSchema(srcDS, tgtDS)
    if (!auditColumns.isEmpty() && !auditQualified) {
        throw new IllegalStateException("Audit preservation logical schema does not match the selected profile")
    }
    if (!auditColumns.isEmpty()) {
        String auditLookupAlias = System.getProperty("odi.audit_lookup_alias")
        if (auditLookupAlias == null || !(auditLookupAlias ==~ /[A-Z][A-Z0-9_]{0,127}/)) {
            throw new IllegalStateException("Audit preservation lookup alias is missing")
        }
        String sourceAlias = srcComp == null ? null : srcComp.getName()
        if (sourceAlias != null && sourceAlias.equalsIgnoreCase(auditLookupAlias)) {
            throw new IllegalStateException("Audit preservation source alias collides with the lookup alias")
        }
        boolean aliasMissing = false
        auditColumns.each { auditName ->
            String text = customExpressions[auditName]
            boolean usesAlias = text != null && (text =~ /(?<![A-Z0-9_])${auditLookupAlias}\./).find()
            if (!usesAlias) aliasMissing = true
        }
        if (aliasMissing) {
            throw new IllegalStateException("Audit preservation expression does not use the lookup alias")
        }
        println "AUDIT PRESERVATION: applied count=${auditColumns.size()}"
    }
    if (!customExpressions.isEmpty()) {
        println "CUSTOM EXPRESSIONS: count=${customExpressions.size()}"
    }

    // Quoted session text is only for a known character code. NUMBER, FLOAT, and
    // the binary floats keep their numeric branches. Any other code fails closed.
    String sessionBatchLiteral = "'<%=odiRef.getSession(\"SESS_NO\")%>'"
    def typedSessionBatch = { attribute ->
        String quoted = sessionBatchLiteral
        String typeCode = null
        Integer length = null
        Integer scale = null
        try {
            def dataType = attribute?.getDataType()
            if (dataType != null && dataType.getCode() != null) {
                typeCode = dataType.getCode().toString().trim().toUpperCase(Locale.ROOT)
            }
        } catch (Throwable ignoredType) {
            typeCode = null
        }
        if (typeCode == null || typeCode.length() == 0) {
            throw new audit_type_unknown()
        }
        try {
            def rawLength = attribute.getLength()
            if (rawLength != null) length = Integer.valueOf(rawLength.toString())
        } catch (Throwable ignoredLength) {}
        try {
            def rawScale = attribute.getScale()
            if (rawScale != null) scale = Integer.valueOf(rawScale.toString())
        } catch (Throwable ignoredScale) {}
        boolean lengthOk = length != null && length > 0
        boolean scaleOk = scale != null && scale >= 0
        String numeric = "TO_NUMBER(" + quoted + ")"
        if (typeCode == "NUMBER") {
            if (lengthOk && scaleOk) return "CAST(" + numeric + " AS NUMBER(" + length + "," + scale + "))"
            if (lengthOk && scale == null) return "CAST(" + numeric + " AS NUMBER(" + length + "))"
            return numeric
        }
        if (typeCode == "FLOAT" && lengthOk && length <= 126) {
            return "CAST(" + numeric + " AS FLOAT(" + length + "))"
        }
        if (typeCode == "BINARY_DOUBLE" || typeCode == "BINARY_FLOAT") {
            return "CAST(" + numeric + " AS " + typeCode + ")"
        }
        if (typeCode in ["VARCHAR2", "VARCHAR", "CHAR", "NVARCHAR2", "NCHAR", "CLOB", "NCLOB", "LONG"]) {
            return quoted
        }
        throw new audit_type_unknown()
    }

    // Fail before the assignment loop. A custom or name-matched column never reaches this.
    def needsSessionBatch = { tAttr ->
        String attrName = tAttr.getName().toUpperCase()
        if (attrName != "ETL_BATCH_ID" || customExpressions.containsKey(attrName)) {
            return false
        }
        boolean onSource = srcComp.getAttributes().any {
            it.getName().equalsIgnoreCase(tAttr.getName())
        }
        if (onSource) { return false }
        if (joinSrcComp != null) {
            boolean onJoin = joinSrcComp.getAttributes().any {
                it.getName().equalsIgnoreCase(tAttr.getName())
            }
            if (onJoin) { return false }
        }
        return true
    }
    tgtComp.getAttributes().each { tAttr ->
        if (needsSessionBatch(tAttr)) {
            typedSessionBatch(tAttr)
        }
    }

    tgtComp.getAttributes().each { tAttr ->
        String attrName = tAttr.getName().toUpperCase()
        boolean isUkCol = ukColNames.contains(attrName)

        // Priority 1: use the custom expression when one is defined
        if (customExpressions.containsKey(attrName)) {
            tAttr.setExpressionText(customExpressions[attrName])
        } else {
            // Priority 2: try to match by name (source first, then the join table)
            def sAttr = srcComp.getAttributes().find { it.getName().equalsIgnoreCase(tAttr.getName()) }
            if (sAttr != null) {
                tAttr.setExpressionText(srcComp.getName() + "." + sAttr.getName())
            } else if (joinSrcComp != null) {
                def jAttr = joinSrcComp.getAttributes().find { it.getName().equalsIgnoreCase(tAttr.getName()) }
                if (jAttr != null) {
                    tAttr.setExpressionText(joinSrcComp.getName() + "." + jAttr.getName())
                } else {
                    // Priority 3: default values
                    if (attrName == "INSERT_DATE" || attrName == "UPDATE_DATE") tAttr.setExpressionText("CURRENT_TIMESTAMP")
                    else if (attrName == "SOURCE_SYSTEM") tAttr.setExpressionText("'" + sourceSystemName + "'")
                    else if (attrName == "ETL_BATCH_ID") tAttr.setExpressionText(typedSessionBatch(tAttr))
                }
            } else {
                // Priority 3: default values
                if (attrName == "INSERT_DATE" || attrName == "UPDATE_DATE") tAttr.setExpressionText("CURRENT_TIMESTAMP")
                else if (attrName == "SOURCE_SYSTEM") tAttr.setExpressionText("'" + sourceSystemName + "'")
                else if (attrName == "ETL_BATCH_ID") tAttr.setExpressionText(typedSessionBatch(tAttr))
            }
        }

        // ── ORA-38104 precaution: remove the UK column from UPDATE SET ──────
        // A column used in the MERGE ON clause cannot enter WHEN MATCHED SET.
        // In ODI this is done by setting the "update indicator / isUpdate" flag to false.
        if (isUkCol) {
            boolean flagSet = false
            try { tAttr.setUpdateIndicator(false); flagSet = true } catch (Exception e1) {}
            if (!flagSet) { try { tAttr.setIsUpdate(false); flagSet = true } catch (Exception e2) {} }
            if (!flagSet) { try { tAttr.setAllowsUpdate(false); flagSet = true } catch (Exception e3) {} }
            if (!flagSet) {
                // Reflection fallback
                try {
                    ["mIsUpdate", "mUpdateIndicator", "bUpdate"].each { fn ->
                        if (!flagSet) {
                            try {
                                def f = tAttr.getClass().getDeclaredField(fn)
                                f.setAccessible(true)
                                f.set(tAttr, false)
                                flagSet = true
                            } catch (Exception ex) {}
                        }
                    }
                } catch (Exception e4) {}
            }
            if (flagSet) {
                println "UK_COL INSERT-ONLY: ${attrName} (ORA-38104 prevention)"
            } else {
                println "WARNING: ${attrName} UK column could not be marked INSERT-ONLY — ORA-38104 risk"
            }
        }
    }

    // Physical Design & KM Setting
    MapPhysicalDesign design = mapping.getPhysicalDesign(0)
    MapPhysicalNode initialTargetNode = design.getPhysicalNodes().find { node ->
        node.getName().equalsIgnoreCase(tgtDSName) || node.getName().equalsIgnoreCase(tgtComp.getName())
    }
    String targetLocationName = initialTargetNode?.getExecutionUnit()?.getLocation()?.getName()
    if (targetLocationName == null || targetLocationName.trim().length() == 0) {
        throw new Exception("Target staging location could not be resolved")
    }
    design.setStagingLocationName(targetLocationName)
    design.resetSpec()
    println "PHYSICAL DESIGN RESET: target staging and logical integration contract applied"
    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
    IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
    
    // IKM is an exact approved name pinned by Python; LKM keeps its existing fallback.
    String ikmNameParam = System.getProperty("odi.ikm_name")
    String lkmNameParam = System.getProperty("odi.lkm_name")
    String truncateTargetTableParam = System.getProperty("odi.truncate_target_table")
    String lkmPriorityParam = System.getProperty("odi.lkm_priority", "sql to oracle,mssql to oracle,sql to sql")

    def normalizeBooleanParam = { rawValue, propName ->
        String text = rawValue == null ? "" : rawValue.trim().toLowerCase()
        if (!(text in ["true", "false"])) {
            throw new Exception("Invalid boolean value for ${propName}: ${rawValue}. Expected true or false.")
        }
        return text
    }

    def setIkmOptionIfRequested = { MapPhysicalNode node, String optionName, String rawValue ->
        if (rawValue == null || rawValue.trim().length() == 0) return false
        String normalized = normalizeBooleanParam(rawValue, "odi.truncate_target_table")
        def optionValue = node.getIKMOptionValue(optionName)
        if (optionValue == null) {
            throw new Exception("IKM option not found on ${node.getName()}: ${optionName}")
        }
        optionValue.setValue(Boolean.valueOf(normalized))
        def verifyValue = node.getIKMOptionValue(optionName)
        String actual = verifyValue == null ? null : String.valueOf(verifyValue.getOptionValue())
        if (actual == null || !actual.equalsIgnoreCase(normalized)) {
            throw new Exception("IKM option verify failed for ${optionName}: expected ${normalized}, actual ${actual ?: 'NULL'}")
        }
        println "IKM OPTION SET: ${optionName}=${normalized} -> ${node.getName()}"
        return true
    }

    // Load the KM lists once (performance — every findAll() hits the repository)
    def allIkms = ikmFinder.findAll()
    def allLkms = lkmFinder.findAll()

    OdiIKM ikm = selectExactIkm(allIkms, ikmNameParam, System.getProperty("odi.approved_ikms"),
        System.getProperty("odi.required_ikm_id"))
    println "IKM: ${ikm.getName()} (exact approved match)"

    // LKM selection: use the exact match when present, otherwise the configurable fallback chain
    OdiLKM lkm = null
    if (lkmNameParam != null && lkmNameParam.trim().length() > 0) {
        lkm = allLkms.find { it.getName().equalsIgnoreCase(lkmNameParam.trim()) }
        if (lkm != null) println "LKM: ${lkm.getName()} (exact match)"
        else println "WARNING: LKM exact match not found: ${lkmNameParam} — fallback chain will be tried"
    }
    if (lkm == null) {
        lkmPriorityParam.split(",").each { keyword ->
            if (lkm == null) {
                lkm = allLkms.find { it.getName().toLowerCase().contains(keyword.trim().toLowerCase()) }
            }
        }
        if (lkm != null) println "LKM: ${lkm.getName()} (fallback: ${lkmPriorityParam})"
    }

    // Find the physical node safely (ODI 12c compatible)
    // The AP (Access Point) node is also detected — for LKM assignment (Antigravity suggestion #4)
    def refreshPhysicalNodes = {
        MapPhysicalNode foundTarget = null
        MapPhysicalNode foundAp = null
        design.getPhysicalNodes().each { node ->
            if (node.getName().equalsIgnoreCase(tgtDSName) || node.getName().equalsIgnoreCase(tgtComp.getName())) {
                foundTarget = node
            }
            // AP node: the name ends with _AP or contains ACCESS_POINT
            if (node.getName().toUpperCase().contains("_AP") || node.getName().toUpperCase().contains("ACCESS_POINT")) {
                foundAp = node
            }
        }
        return [target: foundTarget, ap: foundAp]
    }

    def physicalNodes = refreshPhysicalNodes()
    MapPhysicalNode tgtNode = physicalNodes.target
    MapPhysicalNode apNode = physicalNodes.ap

    if (tgtNode != null) {
        // Physical Integration Type: IU on the physical node
        // (Logical DatastoreComponent.setIntegrationTypeCode does not exist in ODI 12c)
        boolean physItSet = false
        try {
            tgtNode.setIntegrationTypeCode(physItCode)
            println "PHYS INTEGRATION TYPE: ${physItCode} -> ${tgtNode.getName()}"
            physItSet = true
        } catch (Exception pit1) {
            try {
                tgtNode.integrationTypeCode = physItCode
                println "PHYS INTEGRATION TYPE: ${physItCode} via property -> ${tgtNode.getName()}"
                physItSet = true
            } catch (Exception pit2) {
                // Reflection: search all fields for integration-related name
                try {
                    def cls = tgtNode.getClass()
                    while (cls != null && !physItSet) {
                        cls.getDeclaredFields().each { f ->
                            if (!physItSet && f.getName().toLowerCase().contains("integrat")) {
                                try {
                                    f.setAccessible(true)
                                    f.set(tgtNode, physItCode)
                                    println "PHYS INTEGRATION TYPE: ${physItCode} forced via field ${f.getName()}"
                                    physItSet = true
                                } catch (Exception ignore) {}
                            }
                        }
                        cls = cls.getSuperclass()
                    }
                } catch (Exception pit3) {}
            }
        }
        if (!physItSet) {
            println "NOTE: Physical Integration Type ${physItCode} could not be assigned — IKM assignment will continue"
        }

        // IKM: always assigned to the target node and verified by reading it back.
        if (ikm == null) {
            throw new Exception("IKM selection failed. Configure an exact approved odi.ikm_name.")
        }
        // Fail closed: a merge/update IKM without an update key would silently insert only,
        // and control append must never run a merge/update IKM.
        String selectedIkmLower = ikm.getName().toLowerCase()
        boolean updateStyleIkm = selectedIkmLower.contains("merge") || selectedIkmLower.contains("update")
        if (controlAppend && updateStyleIkm) {
            throw new Exception("Control append cannot use a merge or update IKM")
        }
        if (!controlAppend && updateStyleIkm && ukColNames.isEmpty()) {
            throw new Exception("Update key is required for a merge or update IKM")
        }
        boolean ikmSet = false
        MapPhysicalNode ikmAssignedNode = tgtNode
        if (ikm != null) {
            try {
                tgtNode.setIKM(ikm)
                println "IKM SET: ${ikm.getName()} → ${tgtNode.getName()}"
                ikmSet = true
            } catch (Exception ikme) {
                println "DEBUG IKM set directly failed (${ikme.getClass().getSimpleName()}), trying setIKMByName..."

                // ODI 12c public API: assign the integration KM by exact name.
                try {
                    tgtNode.setIKMByName(ikm.getName())
                    println "IKM SET VIA setIKMByName: ${ikm.getName()} -> ${tgtNode.getName()}"
                    ikmSet = true
                } catch (Exception ikmNameError) {
                    println "WARNING: setIKMByName failed: ${ikmNameError.getClass().getSimpleName()}"
                }
                
                // Try setting staging to target location
                if (!ikmSet) {
                    try {
                        def loc = tgtNode.getExecutionUnit()?.getLocation()
                        if (loc) {
                            design.setStagingLocationName(loc.getName())
                            em.persist(mapping)
                            println "STAGING FIXED TO: ${loc.getName()}"
                        }
                    } catch (Exception e) {}
                }

                // Try name-based assignment (setTargetComponentKMByName)
                if (!ikmSet) {
                    try {
                    tgtNode.setTargetComponentKMByName(ikm.getName())
                    println "IKM SET VIA setTargetComponentKMByName: ${ikm.getName()} → ${tgtNode.getName()}"
                    ikmSet = true
                    } catch (Exception e_tk) {
                        println "WARNING: setTargetComponentKMByName failed: ${e_tk.getClass().getSimpleName()}"
                    }
                }

                // Last resort: find AP node and try there if target failed
                if (!ikmSet && apNode != null) {
                    try {
                        apNode.setIKM(ikm)
                        ikmAssignedNode = apNode
                        println "IKM SET ON AP NODE: ${ikm.getName()} → ${apNode.getName()}"
                        ikmSet = true
                    } catch (Exception e3) {}
                }

                if (!ikmSet) {
                    println "FORCING IKM via JPA reflection..."
                    try {
                        def field = tgtNode.getClass().getDeclaredField("mIKM")
                        field.setAccessible(true)
                        field.set(tgtNode, ikm)
                        println "IKM FORCED: ${ikm.getName()}"
                        ikmSet = true
                    } catch (Exception e4) {
                        println "FORCING FAILED: ${e4.getClass().getSimpleName()}"
                    }
                }
            }
        }

        if (!ikmSet) {
            throw new Exception("IKM assignment failed for ${tgtNode.getName()}: no assignment path succeeded")
        }
        String assignedIkmName = null
        try {
            assignedIkmName = ikmAssignedNode.getIKM()?.getName()
        } catch (Exception ikmReadError) {
            throw new Exception("IKM VERIFY FAILED for ${ikmAssignedNode.getName()}: cannot read assigned IKM (${ikmReadError.message})")
        }
        if (assignedIkmName == null || !assignedIkmName.equalsIgnoreCase(ikm.getName())) {
            throw new Exception("IKM VERIFY FAILED for ${ikmAssignedNode.getName()}: expected '${ikm.getName()}', actual '${assignedIkmName ?: 'NULL'}'")
        }
        println "IKM VERIFY: ${assignedIkmName} -> ${ikmAssignedNode.getName()}"

        setIkmOptionIfRequested(tgtNode, "TRUNCATE_TARGET_TABLE", truncateTargetTableParam)

        // LKM: a source-to-staging move is required only when an AP node exists.
        // LKM assignment cannot be skipped silently, or runtime ORA-02019 is possible.
        // IKM/staging fallbacks can recompute the physical design, so
        // the AP node reference is resolved again before the LKM assignment.
        // Assigns and verifies the configured load KM on one access-point node.
        def assignLoadKm = { MapPhysicalNode lkmTarget ->
            boolean allowDbLinkLkm = "true".equalsIgnoreCase(System.getProperty("odi.allow_dblink_lkm", "false"))
            if (lkm == null) {
                throw new Exception("LKM selection failed. Configure odi.lkm_name or ODI_LKM_PRIORITY before creating the mapping.")
            }
            boolean lkmSet = false
            Exception directLkmError = null
            try {
                lkmTarget.setLKM(lkm)
                lkmSet = true
                println "LKM SET: ${lkm.getName()} -> ${lkmTarget.getName()}"
            } catch (Exception lkme) {
                directLkmError = lkme
                println "DEBUG LKM set directly failed (${lkme.getClass().getSimpleName()}), trying setLKMByName..."
                try {
                    lkmTarget.setLKMByName(lkm.getName())
                    lkmSet = true
                    println "LKM SET VIA setLKMByName: ${lkm.getName()} -> ${lkmTarget.getName()}"
                } catch (Exception lkmNameError) {
                    throw new Exception("LKM assignment failed for ${lkmTarget.getName()}: direct=${directLkmError?.getClass()?.getSimpleName()}; byName=${lkmNameError.getClass().getSimpleName()}")
                }
            }
            if (!lkmSet) {
                throw new Exception("LKM assignment failed for ${lkmTarget.getName()}: no assignment path succeeded")
            }

            String assignedLkmName = null
            try {
                assignedLkmName = lkmTarget.getLKM()?.getName()
            } catch (Exception ignored) {}
            if (assignedLkmName == null || assignedLkmName.trim().length() == 0) {
                try {
                    assignedLkmName = lkmTarget.getLKMName()
                } catch (Exception lkmReadError) {
                    throw new Exception("LKM VERIFY FAILED for ${lkmTarget.getName()}: cannot read assigned LKM (${lkmReadError.getClass().getSimpleName()})")
                }
            }
            if (assignedLkmName == null || !assignedLkmName.equalsIgnoreCase(lkm.getName())) {
                throw new Exception("LKM VERIFY FAILED for ${lkmTarget.getName()}: expected '${lkm.getName()}', actual '${assignedLkmName ?: 'NULL'}'")
            }
            String assignedLkmUpper = assignedLkmName.toUpperCase()
            if (!allowDbLinkLkm && (assignedLkmUpper.contains("DB LINK") || assignedLkmUpper.contains("DBLINK"))) {
                throw new Exception("LKM VERIFY FAILED: DB Link LKM '${assignedLkmName}' is blocked unless odi.allow_dblink_lkm=true")
            }
            println "LKM VERIFY: ${assignedLkmName} -> ${lkmTarget.getName()}"
        }

        physicalNodes = refreshPhysicalNodes()
        if (physicalNodes.target != null) tgtNode = physicalNodes.target
        apNode = physicalNodes.ap
        println "PHYSICAL NODES REFRESHED BEFORE LKM: target=${tgtNode?.getName()}, ap=${apNode?.getName()}"
        MapPhysicalNode lkmTarget = apNode
        if (lkmTarget == null) {
            println "LKM NOT REQUIRED: no access point in the target-staged physical design (re-checked after final staging)"
        } else {
            assignLoadKm(lkmTarget)
        }

        try {
            // ODI 14c+ compatible: use setStagingLocationName(String)
            // setStagingLocation(Location) can have a signature mismatch on ODI 12c
            String locName = tgtNode.getExecutionUnit().getLocation().getName()
            design.setStagingLocationName(locName)
            println "STAGING: ${locName}"
        } catch (Exception stg) {
            // Fallback: previous method (works on some ODI 12c patches)
            try {
                design.setStagingLocation(tgtNode.getExecutionUnit().getLocation())
            } catch (Exception stg2) {
                println "WARNING: Staging location failed: " + stg.getClass().getSimpleName()
            }
        }

        // FINAL PASS: access points can appear (or be recomputed) after the staging location is
        // resolved. Re-read every AP node from the final physical design and give each the load KM.
        physicalDiag.same_schema = sameLogicalSchema(srcDS, tgtDS)
        List finalAps = design.getAllAPNodes() ?: []
        if (!finalAps.isEmpty() && physicalDiag.same_schema) {
            // Same logical schema: the source belongs in the target execution unit, no staging hop.
            try {
                MapPhysicalNode srcNode = design.findNode(srcComp)
                List moveIssues = new ArrayList()
                if (srcNode != null && srcNode.getExecutionUnit() != null && tgtNode.getExecutionUnit() != null &&
                        !srcNode.isAPNode() && design.moveNodes([srcNode], tgtNode.getExecutionUnit(), moveIssues)) {
                    physicalDiag.source_moved = true
                    println "SAME SCHEMA: source node moved into the target execution unit"
                } else {
                    println "SAME SCHEMA: source node move not applied (${summarizeIssues(moveIssues)})"
                }
            } catch (Exception moveError) {
                println "SAME SCHEMA: source node move failed (${moveError.getClass().getSimpleName()})"
            }
            finalAps = design.getAllAPNodes() ?: []
        }
        physicalDiag.access_points = finalAps.size()
        if (finalAps.isEmpty()) {
            println "FINAL PASS: no access point in the final physical design, LKM not required"
        }
        finalAps.each { MapPhysicalNode finalAp ->
            assignLoadKm(finalAp)
            physicalDiag.access_points_with_lkm = physicalDiag.access_points_with_lkm + 1
        }
        physicalDiag.access_point_names = finalAps.collect { it.getName() }
        physicalDiag.physical_nodes = design.getPhysicalNodes().collect { it.getName() }
    } else {
        println "WARNING: Physical node not found, KM assignment skipped"
    }
    
    // SDK 12.2.1.4: BOTH validate() overloads return TRUE when errors were found (the no-argument
    // form delegates to the issue-list form). Only the issue-list overload is called; its result and
    // its issues both gate.
    List validationIssues = new ArrayList()
    boolean hasValidationErrors = mappingHasErrors(mapping, validationIssues)
    if (hasValidationErrors) {
        validationMessages = collectDesignFailure(design, validationIssues, physicalDiag)
        throw new IllegalStateException("Mapping validation failed")
    }
    boolean replacedExisting = false
    if (preserveExistingOnFailure) {
        em.remove(existingMapping)
        em.flush()
        // After a replace the new mapping has a new internal id; existing <MAPPING>_SCN scenarios are not re-pointed and must be regenerated.
        mapping.setName(mName)
        if (mapping.getName() == null || !mName.equals(mapping.getName())) {
            throw new IllegalStateException("Mapping rename-swap failed")
        }
        replacedExisting = true
    }
    txnMgr.commit(txnStatus)
    txnStatus = null
    preserveExistingOnFailure = false
    println "SUCCESS: Mapping '${mName}' created successfully."
    println '{"status":"passed","action":"create_mapping","replaced":' + replacedExisting + ',"kept_existing_on_failure":false,"physical_design":' + JsonOutput.toJson(physicalDiag) + '}'

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    String errorType = e.getClass().getSimpleName()
    if (!(errorType ==~ '[A-Za-z][A-Za-z0-9_$]{0,127}')) {
        errorType = "Exception"
    }
    println '{"status":"error","action":"create_mapping","replaced":false,"kept_existing_on_failure":' + preserveExistingOnFailure + ',"error_type":"' + errorType + '","validation_messages":' + JsonOutput.toJson(validationMessages) + ',"physical_design":' + JsonOutput.toJson(physicalDiag) + '}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
