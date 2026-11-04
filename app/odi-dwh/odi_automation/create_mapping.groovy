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

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition())

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
    
    // CLEANUP: Only delete the mapping we are about to create (NOT hardcoded MAP_CUSTOMER_TO_ODS)
    def mapFinder = em.getFinder(Mapping.class)
    def existing = mapFinder.findByName(folder, mName)
    if (existing != null) {
        if (!replaceExistingMapping) {
            throw new Exception("Mapping already exists and replacement was not approved: " + mName)
        }
        em.remove(existing)
        txnMgr.commit(txnStatus)
        txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition()) 
    }
    
    Mapping mapping = new Mapping(mName, folder)
    em.persist(mapping)
    
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
    
    OdiDataStore tgtDS = findDsRobust(em, tgtDSName, tgtModName)
    if (tgtDS == null) throw new Exception("Target Datastore not found: " + tgtDSName + " in " + tgtModName)
    
    DatastoreComponent srcComp = new DatastoreComponent(mapping, srcDS)
    DatastoreComponent tgtComp = new DatastoreComponent(mapping, tgtDS)

    // ── CRITICAL FIX: Integration Type = IU (Incremental Update) ─────────────
    // In ODI 12c, IKM Oracle Merge is visible and assignable in the physical design
    // only when the target component Integration Type is "IU" (Incremental Update).
    // In "CA" (Control Append/Insert) mode only INSERT IKMs are listed.
    // MOS Notes 2527685.1 and 2152565.1 document this issue.
    boolean integrationTypeSet = false
    try {
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
                println "WARNING: Integration Type IU failed: ${ite.getClass().getSimpleName()}"
                println "         IKM Oracle Merge may not be visible. Set it manually in ODI Studio."
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
            String normalized = name.trim().toUpperCase()
            if (!(normalized ==~ /[A-Z][A-Z0-9_$#]{0,127}/)) {
                throw new Exception("Invalid required update key column")
            }
            requiredUpdateKeyColumns.add(normalized)
        }
    }
    def keyColumnNames = { key ->
        Set<String> names = new LinkedHashSet<>()
        try {
            key.getColumns().each { col -> names.add(col.getName().toUpperCase()) }
        } catch (Exception ignored) {}
        return names
    }
    if (tgtBoundDS != null) {
        def keys = tgtBoundDS.getKeys()
        // When an explicit contract is present, select the exact column match; otherwise use the previous preference chain.
        def uKey = requiredUpdateKeyColumns.isEmpty()
            ? (keys.find { it.getName().toUpperCase().startsWith("UK_") } ?: keys.find { true })
            : keys.find { keyColumnNames(it) == requiredUpdateKeyColumns }
        if (uKey != null) {
            tgtComp.setUpdateKey(uKey)
            println "MAPPING UPDATE KEY SET: ${uKey.getName()}"

            // Collect the UK column names (for ORA-38104)
            try {
                uKey.getColumns().each { col -> ukColNames.add(col.getName().toUpperCase()) }
            } catch (Exception e) {
                try {
                    uKey.getKeyColumns().each { kc ->
                        ukColNames.add(kc.getName().toUpperCase())
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
        tgtComp.setIntegrationType(DatastoreComponent.INTEGRATION_TYPE_UPDATE)
        String integrationType = tgtComp.getIntegrationType()
        if (!DatastoreComponent.INTEGRATION_TYPE_UPDATE.equals(integrationType)) {
            throw new Exception("Target integration type verify failed")
        }
        println "INTEGRATION TYPE VERIFY: ${integrationType}"
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
        println "CUSTOM EXPRESSIONS: ${customExpressions}"
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
                    else if (attrName == "ETL_BATCH_ID") tAttr.setExpressionText("'<%=odiRef.getSession(\"SESS_NO\")%>'")
                }
            } else {
                // Priority 3: default values
                if (attrName == "INSERT_DATE" || attrName == "UPDATE_DATE") tAttr.setExpressionText("CURRENT_TIMESTAMP")
                else if (attrName == "SOURCE_SYSTEM") tAttr.setExpressionText("'" + sourceSystemName + "'")
                else if (attrName == "ETL_BATCH_ID") tAttr.setExpressionText("'<%=odiRef.getSession(\"SESS_NO\")%>'")
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
    
    // Optional: exact-match KM names (sent from Python)
    String ikmNameParam = System.getProperty("odi.ikm_name")
    String lkmNameParam = System.getProperty("odi.lkm_name")
    String truncateTargetTableParam = System.getProperty("odi.truncate_target_table")
    // Configurable fallback chains (comma-separated, supplied by config.py / .env)
    String ikmPriorityParam = System.getProperty("odi.ikm_priority", "incremental update,oracle merge,oracle update,oracle insert")
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

    // IKM selection: use the exact match when present, otherwise the configurable fallback chain
    OdiIKM ikm = null
    if (ikmNameParam != null && ikmNameParam.trim().length() > 0) {
        ikm = allIkms.find { it.getName().equalsIgnoreCase(ikmNameParam.trim()) }
        if (ikm != null) println "IKM: ${ikm.getName()} (exact match)"
        else println "WARNING: IKM exact match not found: ${ikmNameParam} — fallback chain will be tried"
    }
    if (ikm == null) {
        // The fallback chain comes from configuration (ODI_IKM_PRIORITY)
        ikmPriorityParam.split(",").each { keyword ->
            if (ikm == null) {
                ikm = allIkms.find { it.getName().toLowerCase().contains(keyword.trim().toLowerCase()) }
            }
        }
        if (ikm != null) println "IKM: ${ikm.getName()} (fallback: ${ikmPriorityParam})"
    }

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
            tgtNode.setIntegrationTypeCode("IU")
            println "PHYS INTEGRATION TYPE: IU -> ${tgtNode.getName()}"
            physItSet = true
        } catch (Exception pit1) {
            try {
                tgtNode.integrationTypeCode = "IU"
                println "PHYS INTEGRATION TYPE: IU via property -> ${tgtNode.getName()}"
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
                                    f.set(tgtNode, "IU")
                                    println "PHYS INTEGRATION TYPE: IU forced via field ${f.getName()}"
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
            println "NOTE: Physical Integration Type IU could not be assigned — IKM assignment will continue"
        }

        // IKM: always assigned to the target node and verified by reading it back.
        if (ikm == null) {
            throw new Exception("IKM selection failed. Configure odi.ikm_name or ODI_IKM_PRIORITY before creating the mapping.")
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
        physicalNodes = refreshPhysicalNodes()
        if (physicalNodes.target != null) tgtNode = physicalNodes.target
        apNode = physicalNodes.ap
        println "PHYSICAL NODES REFRESHED BEFORE LKM: target=${tgtNode?.getName()}, ap=${apNode?.getName()}"
        MapPhysicalNode lkmTarget = apNode
        boolean allowDbLinkLkm = "true".equalsIgnoreCase(System.getProperty("odi.allow_dblink_lkm", "false"))
        if (lkmTarget == null) {
            println "LKM NOT REQUIRED: no access point in the target-staged physical design"
        } else {
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
    } else {
        println "WARNING: Physical node not found, KM assignment skipped"
    }
    
    mapping.validate() 
    txnMgr.commit(txnStatus)
    println "SUCCESS: Mapping '${mName}' created successfully."

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"create_mapping","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
