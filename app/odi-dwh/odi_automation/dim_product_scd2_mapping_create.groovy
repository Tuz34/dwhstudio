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

def odi = null
boolean standaloneMode = false
ITransactionManager txnMgr = null
ITransactionStatus txnStatus = null

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

    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
    OdiIKM ikm = (ikmFinder.findByName(TARGET_IKM) ?: []).find {
        it.getName()?.equalsIgnoreCase(TARGET_IKM)
    }
    IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
    OdiLKM lkm = (lkmFinder.findByName(LKM_NAME) ?: []).find {
        it.getName()?.equalsIgnoreCase(LKM_NAME)
    }
    if (ikm == null || lkm == null) {
        throw new IllegalStateException("Fixed knowledge module contract is unavailable")
    }
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
    MapPhysicalNode targetNode = design.getPhysicalNodes().find {
        it.isTargetNode() &&
            (it.getName()?.equalsIgnoreCase(TARGET_TABLE) ||
             it.getName()?.equalsIgnoreCase(targetComponent.getName()))
    }
    if (targetNode == null) {
        throw new IllegalStateException("Fixed target physical node is unavailable")
    }
    String stagingLocation = targetNode.getExecutionUnit()?.getLocation()?.getName()
    if (stagingLocation == null || stagingLocation.trim().isEmpty()) {
        throw new IllegalStateException("Fixed staging location is unavailable")
    }
    design.setStagingLocationName(stagingLocation)
    design.resetSpec()

    targetNode = design.getPhysicalNodes().find {
        it.isTargetNode() &&
            (it.getName()?.equalsIgnoreCase(TARGET_TABLE) ||
             it.getName()?.equalsIgnoreCase(targetComponent.getName()))
    }
    if (targetNode == null) {
        throw new IllegalStateException("Fixed target node was lost after physical reset")
    }
    targetNode.setTargetComponentKM(ikm)
    if (!TARGET_IKM.equalsIgnoreCase(targetNode.getTargetComponentKMName())) {
        throw new IllegalStateException("Fixed target IKM assignment mismatch")
    }

    List<MapPhysicalNode> accessPoints = design.getPhysicalNodes().findAll {
        it.isAPNode()
    }
    accessPoints.each { node ->
        node.setXKM(lkm)
        if (!LKM_NAME.equalsIgnoreCase(node.getXKMName())) {
            throw new IllegalStateException("Fixed LKM assignment mismatch")
        }
    }

    int mappedCount = targetComponent.getAttributes().count {
        it.getExpression() != null
    }
    if (targetComponent.getAttributes().size() != 26 || mappedCount != 26) {
        throw new IllegalStateException("Fixed target mapping coverage mismatch")
    }
    mapping.validate()
    txnMgr.commit(txnStatus)
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
} catch (Exception e) {
    if (txnStatus != null && txnMgr != null) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
    }
    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_mapping_create",
        status: "error",
        mode: "fixed_absent_only_scd2_mapping_creation",
        side_effects: false,
        repository_connection_attempted: true,
        *: safeFlags(),
        error_type: e.getClass().getSimpleName(),
    ])
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}

