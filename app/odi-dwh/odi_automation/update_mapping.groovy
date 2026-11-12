/*
  NL-DWH Studio: Surgeon Mapping Update (ODI SDK)
  ================================================
  Updates an existing mapping surgically, without dropping and recreating it.
  The mapping OID, version history, and scenario link are preserved.

  Dispatcher: runs the surgical block selected by the odi.operation parameter.

  Supported operations:
    update_expression — changes selected target attribute expressions
    update_filter     — adds, updates, or removes a filter component
    update_km         — changes the IKM/LKM (physical design)
    rename            — changes the mapping name

  Shared parameters:
    -Dodi.project=TUTORIOL
    -Dodi.folder=First Folder
    -Dodi.mapping=MAP_PRODUCTION_PRODUCT_TO_ODS
    -Dodi.operation=update_expression

  Operation-specific parameters:
    update_expression:
      -Dodi.expressions=NAMEE=Product.Name;UPDATE_DATE=SYSDATE

    update_filter:
      -Dodi.filter_action=add|modify|remove
      -Dodi.filter_condition=SRC.STATUS = 'ACTIVE'
      -Dodi.filter_name=FLT_Product  (for modify/remove)

    update_km:
      -Dodi.ikm_name=IKM Oracle Merge
      -Dodi.lkm_name=LKM SQL to Oracle (Built-In)

    rename:
      -Dodi.new_mapping_name=MAP_NEW_NAME
      -Dodi.rename_source_table_name=SRC_EMPLOYEES
      -Dodi.rename_target_table_name=ODS_HR_EMPLOYEES
      -Dodi.rename_column_side=target
      -Dodi.rename_column_old_name=EMPLOYEE_ID
      -Dodi.rename_column_new_name=EMP_ID
*/

import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.project.*
import oracle.odi.domain.mapping.*
import oracle.odi.domain.mapping.component.*
import oracle.odi.domain.mapping.physical.*
import oracle.odi.domain.root.RootIssue
import oracle.odi.domain.project.finder.*
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*

// Raised when the selected IKM has no such option. Reported as a status, not a failure.
class IkmOptionUnsupported extends RuntimeException {
    final String optionName
    IkmOptionUnsupported(String optionName) {
        super("IKM option unsupported")
        this.optionName = optionName
    }
}

// Raised when the SDK reports validation errors on a changed mapping. The class name is the only output.
class MappingValidationFailed extends RuntimeException {
    MappingValidationFailed() {
        super("Mapping validation failed")
    }
}

// The class name is the only value Python accepts when a rebuild cannot keep the old text.
// reason is a closed code. It is never expression text, an attribute name, or an ODI message.
class audit_expression_not_restored extends RuntimeException {
    final String reason
    audit_expression_not_restored(String reason) {
        super("audit_expression_not_restored")
        this.reason = reason
    }
}

final Set RESTORE_FAILURE_REASONS = [
    "missing_attribute",
    "duplicate_attribute",
    "null_attribute",
    "read_failed",
    "write_failed",
    "readback_mismatch",
    "count_mismatch",
] as Set

// Read every target expression before a filter rebuild removes the component.
// Null text is a captured absence. A failed read does not continue.
def captureTargetExpressions = { component ->
    if (component == null) {
        throw new audit_expression_not_restored("read_failed")
    }
    Map saved = new LinkedHashMap()
    def attributes
    try {
        attributes = component.getAttributes()
    } catch (Throwable ignored) {
        throw new audit_expression_not_restored("read_failed")
    }
    if (attributes == null) {
        throw new audit_expression_not_restored("read_failed")
    }
    attributes.each { attr ->
        if (attr == null) {
            throw new audit_expression_not_restored("null_attribute")
        }
        String key
        String text
        try {
            String name = attr.getName()
            if (name == null || name.trim().length() == 0) {
                throw new audit_expression_not_restored("null_attribute")
            }
            key = name.toUpperCase(Locale.ROOT)
            def expression = attr.getExpression()
            text = expression == null ? null : expression.getText()
        } catch (audit_expression_not_restored denied) {
            throw denied
        } catch (Throwable ignored) {
            throw new audit_expression_not_restored("read_failed")
        }
        if (saved.containsKey(key)) {
            throw new audit_expression_not_restored("duplicate_attribute")
        }
        saved.put(key, text)
    }
    return saved
}

// Put the captured text back. A missing, duplicate, or unverified value fails closed.
// getText() is compared to the captured text. A different read-back does not commit.
def restoreTargetExpressions = { component, Map saved ->
    if (component == null || saved == null) {
        throw new audit_expression_not_restored("read_failed")
    }
    def attributes
    try {
        attributes = component.getAttributes()
    } catch (Throwable ignored) {
        throw new audit_expression_not_restored("read_failed")
    }
    if (attributes == null) {
        throw new audit_expression_not_restored("read_failed")
    }
    Set seen = new LinkedHashSet()
    attributes.each { attr ->
        if (attr == null) {
            throw new audit_expression_not_restored("null_attribute")
        }
        String key
        try {
            String name = attr.getName()
            if (name == null || name.trim().length() == 0) {
                throw new audit_expression_not_restored("null_attribute")
            }
            key = name.toUpperCase(Locale.ROOT)
        } catch (audit_expression_not_restored denied) {
            throw denied
        } catch (Throwable ignored) {
            throw new audit_expression_not_restored("read_failed")
        }
        if (!saved.containsKey(key)) {
            throw new audit_expression_not_restored("count_mismatch")
        }
        if (!seen.add(key)) {
            throw new audit_expression_not_restored("duplicate_attribute")
        }
        def text = saved.get(key)
        if (text == null) {
            String current
            try {
                def expression = attr.getExpression()
                current = expression == null ? null : expression.getText()
            } catch (Throwable ignored) {
                throw new audit_expression_not_restored("read_failed")
            }
            if (current != null && current.trim().length() > 0) {
                throw new audit_expression_not_restored("readback_mismatch")
            }
        } else {
            try {
                attr.setExpressionText(text)
                def expression = attr.getExpression()
                String written = expression == null ? null : expression.getText()
                if (written != text) {
                    throw new audit_expression_not_restored("readback_mismatch")
                }
            } catch (audit_expression_not_restored denied) {
                throw denied
            } catch (Throwable ignored) {
                throw new audit_expression_not_restored("write_failed")
            }
        }
    }
    if (seen.size() != saved.size()) {
        throw new audit_expression_not_restored("missing_attribute")
    }
}

// ── CONNECTION ──────────────────────────────────────────────────────────────
def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
    String propsPath = args?.length ? args[0] : "odi_connection.properties"
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

// ── TRANSACTION ─────────────────────────────────────────────────────────────
ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()

    // ── PARAMETERS ──────────────────────────────────────────────────────────
    String pCode     = System.getProperty("odi.project") ?: "TUTORIOL"
    String fName     = System.getProperty("odi.folder")  ?: "First Folder"
    String mName     = System.getProperty("odi.mapping")
    String operation = System.getProperty("odi.operation") ?: "rename"

    if (!mName) throw new Exception("MISSING PARAM: odi.mapping")

    // ── FIND PROJECT / FOLDER / MAPPING ─────────────────────────────────────
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    if (project == null) throw new Exception("Project not found: " + pCode)

    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    if (folder == null) throw new Exception("Folder not found: " + fName)

    def mapFinder = em.getFinder(Mapping.class)
    Mapping mapping = mapFinder.findByName(folder, mName)
    if (mapping == null) throw new Exception("Mapping not found: " + mName)

    println "SURGEON: Found mapping '${mName}', operation=${operation}"

    // ── HELPER: find every DatastoreComponent ───────────────────────────────
    // Target: has no output connector (end of the data flow)
    // Source: has an output connector (start of the data flow)
    DatastoreComponent tgtComp = null
    DatastoreComponent srcComp = null
    DatastoreComponent joinSrcComp = null
    List<DatastoreComponent> allDsComps = []

    // First pass: collect every DatastoreComponent
    mapping.getAllComponents().each { comp ->
        if (comp instanceof DatastoreComponent) {
            allDsComps.add(comp)
        }
    }

    // Second pass: identify the target
    // The target is the component that receives data from upstream (expressions are SET).
    // Method: getUpstreamConnectedLeafComponents() non-empty = receives data from somewhere = TARGET
    // Or: an attribute with an expression SET = TARGET
    allDsComps.each { comp ->
        boolean hasUpstream = false
        try {
            comp.getInputConnectorPoints().each { icp ->
                if (icp.getUpstreamConnectedLeafComponents()?.size() > 0) {
                    hasUpstream = true
                }
            }
        } catch (Exception e) {
            // Fallback: check the attribute expression
        }

        // Fallback: is there an attribute that contains an expression?
        if (!hasUpstream) {
            comp.getAttributes().each { attr ->
                String expr = attr.getExpression()?.getText()
                if (expr != null && expr.trim().length() > 0 && expr.contains(".")) {
                    hasUpstream = true
                }
            }
        }

        if (hasUpstream) {
            tgtComp = comp
        } else if (srcComp == null) {
            srcComp = comp
        } else {
            joinSrcComp = comp
        }
        // Debug: println "  COMP: ${comp.getName()} upstream=${hasUpstream}"
    }

    boolean changed = false

    // ════════════════════════════════════════════════════════════════════════
    // OPERATION DISPATCHER
    // ════════════════════════════════════════════════════════════════════════

    switch (operation) {

    // ── UPDATE_EXPRESSION ───────────────────────────────────────────────────
    case "update_expression":
        String expressionsStr = System.getProperty("odi.expressions")
        if (!expressionsStr || expressionsStr.trim().length() == 0) {
            throw new Exception("MISSING PARAM: odi.expressions (format: COL=EXPR;COL2=EXPR2)")
        }
        if (tgtComp == null) throw new Exception("Target component not found in mapping")

        // Parse the expressions
        Map<String, String> updates = [:]
        expressionsStr.split(";").each { pair ->
            int eq = pair.indexOf("=")
            if (eq > 0) {
                String key = pair.substring(0, eq).trim().toUpperCase()
                String val = pair.substring(eq + 1).trim()
                // Replace SRC. and JOIN. prefixes with the real component names
                if (srcComp != null) {
                    val = val.replaceAll("(?i)SRC\\.", srcComp.getName() + ".")
                }
                if (joinSrcComp != null) {
                    val = val.replaceAll("(?i)JOIN\\.", joinSrcComp.getName() + ".")
                }
                updates[key] = val
            }
        }

        if (updates.isEmpty()) throw new Exception("No valid expressions parsed from: ${expressionsStr}")

        // Find the target attribute for each expression and update it
        int updateCount = 0
        updates.each { colName, newExpr ->
            def tAttr = tgtComp.getAttributes().find { it.getName().equalsIgnoreCase(colName) }
            if (tAttr == null) {
                // List the existing columns — include them in the error message
                def available = tgtComp.getAttributes().collect { it.getName() }.join(", ")
                throw new Exception("Target attribute not found: ${colName}. Available: ${available}")
            }
            String oldExpr = tAttr.getExpression()?.getText() ?: "(null)"
            tAttr.setExpressionText(newExpr)
            println "UPDATED: ${colName} = ${newExpr}  (was: ${oldExpr})"
            updateCount++
        }

        changed = (updateCount > 0)
        println "EXPRESSION UPDATE: ${updateCount} attribute(s) modified"
        break

    // ── UPDATE_FILTER ───────────────────────────────────────────────────────
    case "update_filter":
        String filterAction    = System.getProperty("odi.filter_action") ?: "add"
        String filterCondition = System.getProperty("odi.filter_condition")
        String filterName      = System.getProperty("odi.filter_name")
        Map savedExpressions = null

        switch (filterAction.toLowerCase()) {

        case "add":
            if (!filterCondition || filterCondition.trim().length() == 0) {
                throw new Exception("MISSING PARAM: odi.filter_condition (for filter add)")
            }
            if (srcComp == null) throw new Exception("Source component not found in mapping")

            // Resolve the SRC. prefix
            String resolvedCond = filterCondition.replaceAll("(?i)SRC\\.", srcComp.getName() + ".")

            // Find the source's current downstream connection
            // srcComp output connector point → connected input
            def srcOutCPs = srcComp.getOutputConnectorPoints()
            def existingDownstream = null
            srcOutCPs.each { outCP ->
                outCP.getDownstreamConnectedLeafComponents()?.each { ds ->
                    if (existingDownstream == null) existingDownstream = ds
                }
            }

            // Create the new filter
            String fltName = filterName ?: ("FLT_" + srcComp.getName())
            FilterComponent filterComp = new FilterComponent(mapping, fltName)
            filterComp.setFilterCondition(resolvedCond)

            // Cut the current flow and insert the filter
            // On ODI 12c, when a connection already exists, disconnect the target first, add the filter, then reconnect
            def downstream = existingDownstream ?: tgtComp
            if (downstream != null) {
                try {
                    // Method 1: direct connection (when no connection exists, or when override is supported)
                    srcComp.connectTo(filterComp)
                    filterComp.connectTo(downstream)
                } catch (Exception connEx) {
                    // Method 2: disconnect the current downstream and insert the filter.
                    // Capture stays outside the rebuild try so a read failure is not wrapped.
                    savedExpressions = captureTargetExpressions(downstream)
                    try {
                        mapping.removeComponent(downstream)
                        srcComp.connectTo(filterComp)
                        // Add the downstream again (with the same datastore)
                        def dsStore = (downstream instanceof DatastoreComponent) ? ((DatastoreComponent)downstream).getBoundDataStore() : tgtComp?.getBoundDataStore()
                        DatastoreComponent newDown = new DatastoreComponent(mapping, dsStore)
                        filterComp.connectTo(newDown)
                        restoreTargetExpressions(newDown, savedExpressions)
                        println "FILTER ADDED (flow rebuilt): ${fltName}"
                    } catch (audit_expression_not_restored denied) {
                        throw denied
                    } catch (Exception rebuildEx) {
                        throw new Exception("Filter insertion failed. Flow rebuild error: ${rebuildEx.message}. Use create_mapping.groovy with odi.filter parameter instead.")
                    }
                }
            } else {
                throw new Exception("No downstream component found to insert filter before")
            }

            changed = true
            println "FILTER ADDED: ${fltName} = ${resolvedCond}"
            break

        case "modify":
            if (!filterCondition || filterCondition.trim().length() == 0) {
                throw new Exception("MISSING PARAM: odi.filter_condition (for filter modify)")
            }

            // Find the existing FilterComponent
            FilterComponent existingFilter = null
            mapping.getAllComponents().each { comp ->
                if (comp instanceof FilterComponent) {
                    if (filterName == null || comp.getName().equalsIgnoreCase(filterName)) {
                        existingFilter = comp
                    }
                }
            }
            if (existingFilter == null) {
                throw new Exception("Filter component not found" + (filterName ? ": ${filterName}" : " in mapping"))
            }

            // Resolve the SRC. prefix
            String resolvedModCond = filterCondition
            if (srcComp != null) {
                resolvedModCond = filterCondition.replaceAll("(?i)SRC\\.", srcComp.getName() + ".")
            }

            String oldCond = existingFilter.getFilterConditionText() ?: "(null)"
            existingFilter.setFilterCondition(resolvedModCond)
            changed = true
            println "FILTER MODIFIED: ${existingFilter.getName()} = ${resolvedModCond}  (was: ${oldCond})"
            break

        case "remove":
            // Find the existing FilterComponent
            FilterComponent removeFilter = null
            mapping.getAllComponents().each { comp ->
                if (comp instanceof FilterComponent) {
                    if (filterName == null || comp.getName().equalsIgnoreCase(filterName)) {
                        removeFilter = comp
                    }
                }
            }
            if (removeFilter == null) {
                throw new Exception("Filter component not found" + (filterName ? ": ${filterName}" : " in mapping"))
            }

            // BEFORE removing the filter: collect every DatastoreComponent and identify source/target
            String removedFilterName = removeFilter.getName() ?: "unknown"
            List<DatastoreComponent> dsComps = []
            mapping.getAllComponents().each { comp ->
                if (comp instanceof DatastoreComponent) dsComps.add(comp)
            }

            // Source/target detection: HAS an upstream connection = target, DOES NOT = source
            DatastoreComponent removeSrc = null
            DatastoreComponent removeTgt = null
            dsComps.each { comp ->
                boolean hasUpstream = false
                try {
                    comp.getInputConnectorPoints().each { icp ->
                        if (icp.getUpstreamConnectedLeafComponents()?.size() > 0) hasUpstream = true
                    }
                } catch (Exception ignore) {}
                if (!hasUpstream) {
                    // Fallback: check the attribute expression
                    comp.getAttributes().each { attr ->
                        String expr = attr.getExpression()?.getText()
                        if (expr != null && expr.trim().length() > 0 && expr.contains(".")) hasUpstream = true
                    }
                }
                if (hasUpstream) removeTgt = comp
                else if (removeSrc == null) removeSrc = comp
            }

            // Save the target table information FIRST (in case the flow is rebuilt)
            def tgtDataStore = removeTgt?.getBoundDataStore()
            String tgtName = removeTgt?.getName() ?: "TARGET"

            // Remove the filter
            mapping.removeComponent(removeFilter)
            println "FILTER COMPONENT REMOVED: ${removedFilterName}"

            // Connect the source and target directly
            if (removeSrc != null && removeTgt != null) {
                try {
                    removeSrc.connectTo(removeTgt)
                    println "RECONNECTED: ${removeSrc.getName()} → ${removeTgt.getName()}"
                } catch (Exception connEx) {
                    // connectTo failed — the target connector state may be corrupt (after a flow rebuild)
                    // Capture stays outside the rebuild try so a read failure is not wrapped.
                    println "WARNING: Direct reconnect failed (${connEx.getClass().getSimpleName()}), rebuilding target..."
                    savedExpressions = captureTargetExpressions(removeTgt)
                    try {
                        mapping.removeComponent(removeTgt)
                        DatastoreComponent newTgt = new DatastoreComponent(mapping, tgtDataStore)
                        removeSrc.connectTo(newTgt)
                        restoreTargetExpressions(newTgt, savedExpressions)
                        println "RECONNECTED (target rebuilt): ${removeSrc.getName()} → ${newTgt.getName()}"
                    } catch (audit_expression_not_restored denied) {
                        throw denied
                    } catch (Exception rebuildEx) {
                        println "ERROR: Target rebuild failed (${rebuildEx.getClass().getSimpleName()}). Use create_mapping.groovy to restore."
                        throw new Exception("Filter removed but reconnection failed. Restore mapping with create_mapping.groovy. Error: ${rebuildEx.message}")
                    }
                }
            } else {
                println "WARNING: Could not identify source(${removeSrc?.getName()}) and/or target(${removeTgt?.getName()}) for reconnection."
            }

            changed = true
            println "FILTER REMOVED: ${removedFilterName} — source now connected directly to target"
            break

        default:
            throw new Exception("Invalid filter_action: ${filterAction}. Expected: add, modify, remove")
        }
        break

    // ── UPDATE_KM ───────────────────────────────────────────────────────────
    case "update_km":
        String ikmNameParam = System.getProperty("odi.ikm_name")
        String lkmNameParam = System.getProperty("odi.lkm_name")
        String truncateTargetTableParam = System.getProperty("odi.truncate_target_table")

        if (!ikmNameParam && !lkmNameParam && !truncateTargetTableParam) {
            throw new Exception("MISSING PARAM: odi.ikm_name, odi.lkm_name, or odi.truncate_target_table required")
        }

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
                if (normalized == "false") {
                    // Nothing to switch off: the IKM does not have the option.
                    println "IKM OPTION ABSENT: ${optionName} -> ${node.getName()} (requested false, nothing to change)"
                    return false
                }
                // Requested true on an IKM without the option: report it as a status, not a throw.
                throw new IkmOptionUnsupported(optionName)
            }
            optionValue.setValue(Boolean.valueOf(normalized))
            def verifyValue = node.getIKMOptionValue(optionName)
            String actual = verifyValue == null ? null : String.valueOf(verifyValue.getOptionValue())
            if (actual == null || !actual.equalsIgnoreCase(normalized)) {
                throw new Exception("IKM option verify failed for ${optionName}: expected ${normalized}, actual ${actual ?: 'NULL'}")
            }
            println "IKM OPTION SET: ${optionName}=${normalized} -> ${node.getName()} (surgeon update)"
            return true
        }

        // Physical design
        MapPhysicalDesign design = mapping.getPhysicalDesign(0)

        // Find the target node and the AP node (create_mapping.groovy pattern)
        MapPhysicalNode tgtNode = null
        MapPhysicalNode apNode = null
        design.getPhysicalNodes().each { node ->
            String nodeName = node.getName().toUpperCase()
            if (tgtComp != null) {
                String tgtDSName = tgtComp.getBoundDataStore()?.getName()?.toUpperCase()
                String tgtCompName = tgtComp.getName()?.toUpperCase()
                if (nodeName.equalsIgnoreCase(tgtDSName) || nodeName.equalsIgnoreCase(tgtCompName)) {
                    tgtNode = node
                }
            }
            if (nodeName.endsWith("_AP") || nodeName.contains("ACCESS_POINT")) {
                apNode = node
            }
        }

        if (tgtNode == null) {
            // Fallback: try the first physical node
            def nodes = design.getPhysicalNodes()
            if (nodes.size() > 0) tgtNode = nodes[0]
        }

        if (tgtNode == null) throw new Exception("Physical target node not found in mapping design")

        // Change the IKM — try every node (because of ODI-16171 compatibility)
        if (ikmNameParam != null && ikmNameParam.trim().length() > 0) {
            IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
            def allIkms = ikmFinder.findAll()
            OdiIKM ikm = allIkms.find { it.getName().equalsIgnoreCase(ikmNameParam.trim()) }
            if (ikm == null) {
                def available = allIkms.findAll { it.getName().contains("Oracle") }.collect { it.getName() }.join(", ")
                throw new Exception("IKM not found: ${ikmNameParam}. Available Oracle IKMs: ${available}")
            }
            boolean ikmSet = false
            // Try every physical node (different nodes may accept different KMs)
            design.getPhysicalNodes().each { node ->
                if (!ikmSet) {
                    try {
                        node.setIKM(ikm)
                        ikmSet = true
                        println "IKM: ${ikm.getName()} → ${node.getName()} (surgeon update)"
                    } catch (Exception ex1) {
                        try {
                            node.setIKMByName(ikm.getName())
                            ikmSet = true
                            println "IKM: ${ikm.getName()} → ${node.getName()} (setIKMByName)"
                        } catch (Exception ex2) {
                            println "DEBUG: IKM not valid for node ${node.getName()}: ${ex1.getClass().getSimpleName()}"
                        }
                    }
                }
            }
            if (!ikmSet) {
                println "WARNING: IKM '${ikmNameParam}' could not be assigned to any physical node. This mapping may need to be recreated with create_mapping.groovy to change KM."
            } else {
                changed = true
            }
        }

        // Change the LKM
        // Earlier guard: !ikmNameParam && !lkmNameParam && !truncateTargetTableParam is invalid.
        if (setIkmOptionIfRequested(tgtNode, "TRUNCATE_TARGET_TABLE", truncateTargetTableParam)) {
            changed = true
        }

        if (lkmNameParam != null && lkmNameParam.trim().length() > 0) {
            IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
            def allLkms = lkmFinder.findAll()
            OdiLKM lkm = allLkms.find { it.getName().equalsIgnoreCase(lkmNameParam.trim()) }
            if (lkm == null) {
                def available = allLkms.findAll { it.getName().contains("Oracle") || it.getName().contains("SQL") }.collect { it.getName() }.join(", ")
                throw new Exception("LKM not found: ${lkmNameParam}. Available: ${available}")
            }
            MapPhysicalNode lkmTarget = apNode ?: tgtNode
            if (lkmTarget == null) {
                throw new Exception("LKM target physical node not found")
            }
            boolean allowDbLinkLkm = "true".equalsIgnoreCase(System.getProperty("odi.allow_dblink_lkm", "false"))
            boolean lkmSet = false
            Exception directLkmError = null
            try {
                lkmTarget.setLKM(lkm)
                lkmSet = true
                println "LKM SET: ${lkm.getName()} -> ${lkmTarget.getName()} (surgeon update)"
            } catch (Exception ex) {
                directLkmError = ex
                try {
                    lkmTarget.setLKMByName(lkm.getName())
                    lkmSet = true
                    println "LKM SET VIA setLKMByName: ${lkm.getName()} -> ${lkmTarget.getName()}"
                } catch (Exception lkmNameError) {
                    throw new Exception("LKM assignment failed for ${lkmTarget.getName()}: direct=${directLkmError.message}; byName=${lkmNameError.message}")
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
            changed = true
            println "LKM VERIFY: ${assignedLkmName} -> ${lkmTarget.getName()} (surgeon update)"
        }
        break

    // ── RENAME ──────────────────────────────────────────────────────────────
    case "rename":
        String newMName = System.getProperty("odi.new_mapping_name")
        String newSourceName = System.getProperty("odi.rename_source_table_name")
        String newTargetName = System.getProperty("odi.rename_target_table_name")
        String renameColumnSide = System.getProperty("odi.rename_column_side")
        String renameColumnOldName = System.getProperty("odi.rename_column_old_name")
        String renameColumnNewName = System.getProperty("odi.rename_column_new_name")

        boolean hasMappingRename = newMName != null && newMName.trim().length() > 0
        boolean hasSourceRename = newSourceName != null && newSourceName.trim().length() > 0
        boolean hasTargetRename = newTargetName != null && newTargetName.trim().length() > 0
        boolean hasColumnOld = renameColumnOldName != null && renameColumnOldName.trim().length() > 0
        boolean hasColumnNew = renameColumnNewName != null && renameColumnNewName.trim().length() > 0

        if (!hasMappingRename && !hasSourceRename && !hasTargetRename && !hasColumnOld && !hasColumnNew) {
            throw new Exception("MISSING PARAM: one of odi.new_mapping_name, odi.rename_source_table_name, odi.rename_target_table_name, or column rename params is required")
        }
        if (hasMappingRename && newMName != mName) {
            mapping.setName(newMName.trim())
            changed = true
            println "RENAMED: ${mName} -> ${newMName.trim()}"
        } else if (hasMappingRename) {
            println "RENAME: No change needed (same name)"
        }

        def renameComponentIfRequested = { DatastoreComponent comp, String newName, String label ->
            if (newName == null || newName.trim().length() == 0) return
            if (comp == null) throw new Exception("${label} component not found for rename")
            String cleanName = newName.trim()
            String oldName = comp.getName()
            if (!oldName.equalsIgnoreCase(cleanName)) {
                comp.setName(cleanName)
                changed = true
                println "${label} COMPONENT RENAMED: ${oldName} -> ${cleanName}"
            } else {
                println "${label} COMPONENT RENAME: No change needed (${oldName})"
            }
        }

        renameComponentIfRequested(srcComp, newSourceName, "SOURCE")
        renameComponentIfRequested(tgtComp, newTargetName, "TARGET")

        if (hasColumnOld || hasColumnNew) {
            if (!hasColumnOld || !hasColumnNew) {
                throw new Exception("Column rename requires both odi.rename_column_old_name and odi.rename_column_new_name")
            }
            String side = (renameColumnSide ?: "target").trim().toLowerCase()
            if (!(side in ["source", "target"])) {
                throw new Exception("Invalid odi.rename_column_side: ${renameColumnSide}. Expected source or target.")
            }
            DatastoreComponent columnComp = side == "source" ? srcComp : tgtComp
            if (columnComp == null) throw new Exception("Column rename ${side} component not found")
            def attr = columnComp.getAttributes().find { it.getName().equalsIgnoreCase(renameColumnOldName.trim()) }
            if (attr == null) {
                def available = columnComp.getAttributes().collect { it.getName() }.join(", ")
                throw new Exception("Column not found on ${side} component: ${renameColumnOldName}. Available: ${available}")
            }
            String oldAttrName = attr.getName()
            String newAttrName = renameColumnNewName.trim()
            if (!oldAttrName.equalsIgnoreCase(newAttrName)) {
                attr.setName(newAttrName)
                changed = true
                println "COLUMN RENAMED (${side}): ${oldAttrName} -> ${newAttrName}"
            } else {
                println "COLUMN RENAME (${side}): No change needed (${oldAttrName})"
            }
        }
        break

    // ── UNKNOWN ─────────────────────────────────────────────────────────────
    default:
        throw new Exception("Unknown operation: ${operation}. Expected: update_expression, update_filter, update_km, rename")
    }

    // ── COMMIT ──────────────────────────────────────────────────────────────
    if (changed) {
        // SDK 12.2.1.4: validate() returns TRUE when errors were found, and it can also return FALSE after
        // adding a FATAL RootIssue (non-overridable target load order), so both signals block the commit.
        // The issue details are never output.
        List finalValidationIssues = new ArrayList()
        if (mapping.validate(finalValidationIssues, false) || RootIssue.isErrorOrFatal(finalValidationIssues)) {
            throw new MappingValidationFailed()
        }
        txnMgr.commit(txnStatus)
        println "SUCCESS: Mapping '${mName}' updated (operation=${operation})."
    } else {
        txnMgr.commit(txnStatus)
        println "SUCCESS: No changes needed for mapping '${mName}'."
    }

} catch (IkmOptionUnsupported unsupported) {
    // Expected condition, not a failure: the selected IKM has no such option. Nothing was changed.
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"ikm_option_unsupported","action":"update_mapping","option":"' + unsupported.optionName + '"}'
} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    String restoreReason = null
    if (e instanceof audit_expression_not_restored) {
        restoreReason = ((audit_expression_not_restored) e).reason
    }
    if (restoreReason != null && RESTORE_FAILURE_REASONS.contains(restoreReason)) {
        println '{"status":"error","action":"update_mapping","error_type":"audit_expression_not_restored","reason":"' + restoreReason + '"}'
    } else {
        println '{"status":"error","action":"update_mapping","error_type":"' + e.getClass().getSimpleName() + '"}'
    }
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
