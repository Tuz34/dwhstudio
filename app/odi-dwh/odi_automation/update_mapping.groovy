/*
  NL-DWH Studio: Surgeon Mapping Update (ODI SDK)
  ================================================
  Mevcut bir mapping'i YIKIP YENIDEN YARATMADAN cerrah gibi gunceller.
  Mapping OID, versiyon gecmisi, senaryo baglantisi KORUNUR.

  Dispatcher: odi.operation parametresine gore ilgili cerrah blogu calisir.

  Desteklenen operasyonlar:
    update_expression — Belirli target attribute expression'larini degistirir
    update_filter     — Filter component ekler/gunceller/siler
    update_km         — IKM/LKM degistirir (physical design)
    rename            — Mapping adini degistirir

  Ortak parametreler:
    -Dodi.project=TUTORIOL
    -Dodi.folder=First Folder
    -Dodi.mapping=MAP_PRODUCTION_PRODUCT_TO_ODS
    -Dodi.operation=update_expression

  Operasyona ozel parametreler:
    update_expression:
      -Dodi.expressions=NAMEE=Product.Name;UPDATE_DATE=SYSDATE

    update_filter:
      -Dodi.filter_action=add|modify|remove
      -Dodi.filter_condition=SRC.STATUS = 'ACTIVE'
      -Dodi.filter_name=FLT_Product  (modify/remove icin)

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
import oracle.odi.domain.project.finder.*
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*

// ── BAGLANTI ────────────────────────────────────────────────────────────────
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

    // ── PARAMETRELER ────────────────────────────────────────────────────────
    String pCode     = System.getProperty("odi.project") ?: "TUTORIOL"
    String fName     = System.getProperty("odi.folder")  ?: "First Folder"
    String mName     = System.getProperty("odi.mapping")
    String operation = System.getProperty("odi.operation") ?: "rename"

    if (!mName) throw new Exception("MISSING PARAM: odi.mapping")

    // ── PROJE / FOLDER / MAPPING BUL ────────────────────────────────────────
    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    if (project == null) throw new Exception("Project not found: " + pCode)

    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    if (folder == null) throw new Exception("Folder not found: " + fName)

    def mapFinder = em.getFinder(Mapping.class)
    Mapping mapping = mapFinder.findByName(folder, mName)
    if (mapping == null) throw new Exception("Mapping not found: " + mName)

    println "SURGEON: Found mapping '${mName}', operation=${operation}"

    // ── YARDIMCI: Tum DatastoreComponent'leri bul ───────────────────────────
    // Target: output connector'u olmayan (veri akisinin son noktasi)
    // Source: output connector'u olan (veri akisinin baslangici)
    DatastoreComponent tgtComp = null
    DatastoreComponent srcComp = null
    DatastoreComponent joinSrcComp = null
    List<DatastoreComponent> allDsComps = []

    // Ilk gecis: tum DatastoreComponent'leri topla
    mapping.getAllComponents().each { comp ->
        if (comp instanceof DatastoreComponent) {
            allDsComps.add(comp)
        }
    }

    // Ikinci gecis: target'i tespit et
    // Target, upstream'den veri alan (expression'lari SET edilmis) component'tir.
    // Yontem: getUpstreamConnectedLeafComponents() bos olmayan = bir yerden veri alıyor = TARGET
    // Veya: attribute'larda expression SET edilmis olan = TARGET
    allDsComps.each { comp ->
        boolean hasUpstream = false
        try {
            comp.getInputConnectorPoints().each { icp ->
                if (icp.getUpstreamConnectedLeafComponents()?.size() > 0) {
                    hasUpstream = true
                }
            }
        } catch (Exception e) {
            // Fallback: attribute expression kontrolu
        }

        // Fallback: expression iceren attribute var mi?
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
    // OPERASYON DISPATCHER
    // ════════════════════════════════════════════════════════════════════════

    switch (operation) {

    // ── UPDATE_EXPRESSION ───────────────────────────────────────────────────
    case "update_expression":
        String expressionsStr = System.getProperty("odi.expressions")
        if (!expressionsStr || expressionsStr.trim().length() == 0) {
            throw new Exception("MISSING PARAM: odi.expressions (format: COL=EXPR;COL2=EXPR2)")
        }
        if (tgtComp == null) throw new Exception("Target component not found in mapping")

        // Expression'lari parse et
        Map<String, String> updates = [:]
        expressionsStr.split(";").each { pair ->
            int eq = pair.indexOf("=")
            if (eq > 0) {
                String key = pair.substring(0, eq).trim().toUpperCase()
                String val = pair.substring(eq + 1).trim()
                // SRC. ve JOIN. prefix'lerini gercek component adlariyla degistir
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

        // Her expression icin target attribute'u bul ve guncelle
        int updateCount = 0
        updates.each { colName, newExpr ->
            def tAttr = tgtComp.getAttributes().find { it.getName().equalsIgnoreCase(colName) }
            if (tAttr == null) {
                // Mevcut kolonlari listele — hata mesajinda yardimci olsun
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

        switch (filterAction.toLowerCase()) {

        case "add":
            if (!filterCondition || filterCondition.trim().length() == 0) {
                throw new Exception("MISSING PARAM: odi.filter_condition (for filter add)")
            }
            if (srcComp == null) throw new Exception("Source component not found in mapping")

            // SRC. prefix'ini cozumle
            String resolvedCond = filterCondition.replaceAll("(?i)SRC\\.", srcComp.getName() + ".")

            // Kaynagin mevcut downstream baglantisini bul
            // srcComp'un output connector point'i → connected input
            def srcOutCPs = srcComp.getOutputConnectorPoints()
            def existingDownstream = null
            srcOutCPs.each { outCP ->
                outCP.getDownstreamConnectedLeafComponents()?.each { ds ->
                    if (existingDownstream == null) existingDownstream = ds
                }
            }

            // Yeni filter olustur
            String fltName = filterName ?: ("FLT_" + srcComp.getName())
            FilterComponent filterComp = new FilterComponent(mapping, fltName)
            filterComp.setFilterCondition(resolvedCond)

            // Mevcut akisi kes ve filtre araya ekle
            // ODI 12c'de mevcut baglanti varsa once hedefi kopar, filter ekle, yeniden bagla
            def downstream = existingDownstream ?: tgtComp
            if (downstream != null) {
                try {
                    // Yontem 1: Dogrudan baglanti (mevcut baglanti yoksa veya override destekliyorsa)
                    srcComp.connectTo(filterComp)
                    filterComp.connectTo(downstream)
                } catch (Exception connEx) {
                    // Yontem 2: Mevcut downstream'i kopar, filter araya ekle
                    try {
                        mapping.removeComponent(downstream)
                        srcComp.connectTo(filterComp)
                        // Downstream'i yeniden ekle (ayni datastore ile)
                        def dsStore = (downstream instanceof DatastoreComponent) ? ((DatastoreComponent)downstream).getBoundDataStore() : tgtComp?.getBoundDataStore()
                        DatastoreComponent newDown = new DatastoreComponent(mapping, dsStore)
                        filterComp.connectTo(newDown)
                        // Attribute expression'lari yeniden kur (auto-map by name)
                        newDown.getAttributes().each { tAttr ->
                            def sAttr = srcComp.getAttributes().find { it.getName().equalsIgnoreCase(tAttr.getName()) }
                            if (sAttr != null) {
                                tAttr.setExpressionText(srcComp.getName() + "." + sAttr.getName())
                            } else {
                                String n = tAttr.getName().toUpperCase()
                                if (n == "INSERT_DATE" || n == "UPDATE_DATE") tAttr.setExpressionText("CURRENT_TIMESTAMP")
                                else if (n == "SOURCE_SYSTEM") tAttr.setExpressionText("'" + System.getProperty("odi.source_system", "ORACLE_SAMPLE_SCHEMAS") + "'")
                                else if (n == "ETL_BATCH_ID") tAttr.setExpressionText("0")
                            }
                        }
                        println "FILTER ADDED (flow rebuilt): ${fltName}"
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

            // Mevcut FilterComponent'i bul
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

            // SRC. prefix cozumle
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
            // Mevcut FilterComponent'i bul
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

            // Filter kaldirilmadan ONCE: tum DatastoreComponent'leri topla ve source/target tespit et
            String removedFilterName = removeFilter.getName() ?: "unknown"
            List<DatastoreComponent> dsComps = []
            mapping.getAllComponents().each { comp ->
                if (comp instanceof DatastoreComponent) dsComps.add(comp)
            }

            // Source/target tespiti: upstream baglantisi OLAN = target, OLMAYAN = source
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
                    // Fallback: attribute expression kontrolu
                    comp.getAttributes().each { attr ->
                        String expr = attr.getExpression()?.getText()
                        if (expr != null && expr.trim().length() > 0 && expr.contains(".")) hasUpstream = true
                    }
                }
                if (hasUpstream) removeTgt = comp
                else if (removeSrc == null) removeSrc = comp
            }

            // Hedef tablo bilgilerini ONCE kaydet (flow rebuild ihtimali icin)
            def tgtDataStore = removeTgt?.getBoundDataStore()
            String tgtName = removeTgt?.getName() ?: "TARGET"

            // Filter'i kaldir
            mapping.removeComponent(removeFilter)
            println "FILTER COMPONENT REMOVED: ${removedFilterName}"

            // Source ve target'i dogrudan bagla
            if (removeSrc != null && removeTgt != null) {
                try {
                    removeSrc.connectTo(removeTgt)
                    println "RECONNECTED: ${removeSrc.getName()} → ${removeTgt.getName()}"
                } catch (Exception connEx) {
                    // connectTo basarisiz — target'in connector state'i bozuk olabilir (flow rebuild sonrasi)
                    // Cozum: target'i kaldir, yeniden olustur, expression'lari auto-map et
                    println "WARNING: Direct reconnect failed (${connEx.getClass().getSimpleName()}), rebuilding target..."
                    try {
                        mapping.removeComponent(removeTgt)
                        DatastoreComponent newTgt = new DatastoreComponent(mapping, tgtDataStore)
                        removeSrc.connectTo(newTgt)
                        // Attribute expression'lari yeniden kur (auto-map by name)
                        newTgt.getAttributes().each { tAttr ->
                            def sAttr = removeSrc.getAttributes().find { it.getName().equalsIgnoreCase(tAttr.getName()) }
                            if (sAttr != null) {
                                tAttr.setExpressionText(removeSrc.getName() + "." + sAttr.getName())
                            } else {
                                String n = tAttr.getName().toUpperCase()
                                if (n == "INSERT_DATE" || n == "UPDATE_DATE") tAttr.setExpressionText("CURRENT_TIMESTAMP")
                                else if (n == "SOURCE_SYSTEM") tAttr.setExpressionText("'" + System.getProperty("odi.source_system", "ORACLE_SAMPLE_SCHEMAS") + "'")
                                else if (n == "ETL_BATCH_ID") tAttr.setExpressionText("0")
                            }
                        }
                        println "RECONNECTED (target rebuilt): ${removeSrc.getName()} → ${newTgt.getName()}"
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
                throw new Exception("IKM option not found on ${node.getName()}: ${optionName}")
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

        // Target node ve AP node bul (create_mapping.groovy pattern)
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
            // Fallback: ilk physical node'u dene
            def nodes = design.getPhysicalNodes()
            if (nodes.size() > 0) tgtNode = nodes[0]
        }

        if (tgtNode == null) throw new Exception("Physical target node not found in mapping design")

        // IKM degistir — tum node'larda dene (ODI-16171 uyumlulugundan)
        if (ikmNameParam != null && ikmNameParam.trim().length() > 0) {
            IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
            def allIkms = ikmFinder.findAll()
            OdiIKM ikm = allIkms.find { it.getName().equalsIgnoreCase(ikmNameParam.trim()) }
            if (ikm == null) {
                def available = allIkms.findAll { it.getName().contains("Oracle") }.collect { it.getName() }.join(", ")
                throw new Exception("IKM not found: ${ikmNameParam}. Available Oracle IKMs: ${available}")
            }
            boolean ikmSet = false
            // Tum physical node'larda dene (farkli node'lar farkli KM uyumluluguna sahip olabilir)
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

        // LKM degistir
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
        mapping.validate()
        txnMgr.commit(txnStatus)
        println "SUCCESS: Mapping '${mName}' updated (operation=${operation})."
    } else {
        txnMgr.commit(txnStatus)
        println "SUCCESS: No changes needed for mapping '${mName}'."
    }

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"update_mapping","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
