/* ODI-68 fixed read-only DIM_PRODUCT SCD2 mapping metadata probe. */

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.security.Authentication
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.mapping.component.DatastoreComponent
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.domain.mapping.physical.MapPhysicalNode
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiProjectFinder

final String MAPPING_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
final String SOURCE_TABLE = "ODS_SH_PRODUCTS"
final String TARGET_TABLE = "DIM_PRODUCT"
final String TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
final String TARGET_LKM = "LKM SQL to Oracle (Built-In)"

def odi = null
boolean standaloneMode = false

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
    def em = odi.getTransactionalEntityManager()
    IOdiProjectFinder projectFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projectFinder.findAll().find {
        it.getCode()?.equalsIgnoreCase(projectCode)
    }
    OdiFolder folder = project?.getFolders()?.find {
        it.getName()?.equalsIgnoreCase(folderName)
    }
    Mapping mapping = folder == null ? null :
        em.getFinder(Mapping.class).findByName(folder, MAPPING_NAME)
    if (mapping == null) {
        throw new IllegalStateException("Fixed mapping is unavailable")
    }

    List<DatastoreComponent> components = mapping.getAllComponents().findAll {
        it instanceof DatastoreComponent
    } as List<DatastoreComponent>
    List<DatastoreComponent> sourceComponents = components.findAll {
        it.getBoundDataStore()?.getName()?.equalsIgnoreCase(SOURCE_TABLE)
    }
    List<DatastoreComponent> targetComponents = components.findAll {
        it.getBoundDataStore()?.getName()?.equalsIgnoreCase(TARGET_TABLE)
    }
    if (sourceComponents.size() != 1 || targetComponents.size() != 1) {
        throw new IllegalStateException("Fixed mapping component contract mismatch")
    }
    DatastoreComponent targetComponent = targetComponents[0]
    int targetAttributeCount = targetComponent.getAttributes().size()
    int mappedAttributeCount = targetComponent.getAttributes().count {
        it.getExpression() != null
    }
    if (targetAttributeCount != 26 || mappedAttributeCount != 26) {
        throw new IllegalStateException("Fixed mapping attribute contract mismatch")
    }

    MapPhysicalDesign design = mapping.getPhysicalDesign(0)
    List<MapPhysicalNode> targetNodes = design.getPhysicalNodes().findAll {
        it.isTargetNode() &&
            (it.getName()?.equalsIgnoreCase(TARGET_TABLE) ||
             it.getName()?.equalsIgnoreCase(targetComponent.getName()))
    }
    if (targetNodes.size() != 1) {
        throw new IllegalStateException("Fixed target node contract mismatch")
    }
    String targetIkm = targetNodes[0].getTargetComponentKMName()
    if (targetIkm != TARGET_IKM) {
        throw new IllegalStateException("Fixed target IKM contract mismatch")
    }
    [FLOW_CONTROL: false, STATIC_CONTROL: false, RECYCLE_ERRORS: false,
     TRUNCATE: false, DELETE_ALL: false, CREATE_TARG_TABLE: false, COMMIT: true].each { optionName, expected ->
        def option = targetNodes[0].getIKMOptionValue(optionName)
        if (option == null || !String.valueOf(expected).equalsIgnoreCase(String.valueOf(option.getOptionValue()))) {
            throw new IllegalStateException("Fixed stock IKM option contract mismatch")
        }
    }
    String integrationType = targetComponent.getIntegrationType()
    if (!(integrationType in ["SCD", "SLOWLY_CHANGING_DIMENSION"])) {
        throw new IllegalStateException("Fixed integration type contract mismatch")
    }
    List<MapPhysicalNode> accessPoints = design.getAllAPNodes() as List<MapPhysicalNode>
    int assignedLkmCount = accessPoints.count {
        it.getLKMName() == TARGET_LKM
    }
    if (assignedLkmCount != accessPoints.size()) {
        throw new IllegalStateException("Fixed access-point LKM contract mismatch")
    }

    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_mapping_probe",
        status: "passed",
        mode: "fixed_scd2_mapping_metadata_probe",
        side_effects: false,
        repository_connection_attempted: true,
        *: safeFlags(),
        mapping: [
            name: MAPPING_NAME,
            source_component_count: sourceComponents.size(),
            target_component_count: targetComponents.size(),
            target_attribute_count: targetAttributeCount,
            mapped_attribute_count: mappedAttributeCount,
            integration_type: "SCD",
            target_ikm: targetIkm,
            access_point_count: accessPoints.size(),
            lkm_assigned_count: assignedLkmCount,
        ],
    ])
} catch (Exception e) {
    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_mapping_probe",
        status: "error",
        mode: "fixed_scd2_mapping_metadata_probe",
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
