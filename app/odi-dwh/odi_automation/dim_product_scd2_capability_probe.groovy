/*
  ODI-68 DIM_PRODUCT finder/reflection-only metadata probe.
  Fixed identities only. No repository mutation or scenario action.
*/

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.security.Authentication
import oracle.odi.domain.mapping.Mapping
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
final String SOURCE_OWNER = "DWH"
final String SOURCE_TABLE = "ODS_SH_PRODUCTS"
final String TARGET_OWNER = "DWH"
final String TARGET_TABLE = "DIM_PRODUCT"
final String MAPPING_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
final String SCENARIO_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN"
final String SCENARIO_VERSION = "001"
final String TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
final String LKM_NAME = "LKM SQL to Oracle (Built-In)"

def odi = null
boolean standaloneMode = false

def safeFlags = {
    [
        side_effects: false,
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
    ]
}

def orderedColumnNames = { columns ->
    if (columns == null) return []
    return columns.toList().sort { left, right ->
        (left.getPosition() ?: 0) <=> (right.getPosition() ?: 0)
    }.collect { it.getName() }
}

def technologyName = { technology ->
    String name = technology?.getName()
    return name == null ? null : name.toUpperCase()
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

    IOdiDataStoreFinder dataStoreFinder =
        (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
    OdiDataStore sourceDs = dataStoreFinder.findByName(SOURCE_TABLE, MODEL_CODE)
    OdiDataStore targetDs = dataStoreFinder.findByName(TARGET_TABLE, MODEL_CODE)

    IOdiProjectFinder projectFinder =
        (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projectFinder.findAll().find {
        it.getCode()?.equalsIgnoreCase(projectCode)
    }
    OdiFolder folder = project?.getFolders()?.find {
        it.getName()?.equalsIgnoreCase(folderName)
    }
    def mappingFinder = em.getFinder(Mapping.class)
    Mapping mapping = folder == null ? null : mappingFinder.findByName(folder, MAPPING_NAME)

    IOdiScenarioFinder scenarioFinder =
        (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
    OdiScenario scenario = scenarioFinder.findLatestByName(SCENARIO_NAME)

    IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
    OdiIKM ikm = (ikmFinder.findByName(TARGET_IKM) ?: []).find {
        it.getName()?.equalsIgnoreCase(TARGET_IKM)
    }
    IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
    OdiLKM lkm = (lkmFinder.findByName(LKM_NAME) ?: []).find {
        it.getName()?.equalsIgnoreCase(LKM_NAME)
    }

    String targetTechnology = technologyName(targetDs?.getModel()?.getTechnology())
    def ikmTechnology = ikm?.getTargetTechnology() ?: ikm?.getDefaultTargetTechnology()
    String ikmTargetTechnology = technologyName(ikmTechnology)
    boolean ikmAssignable = ikm != null && targetTechnology != null &&
        ikmTargetTechnology != null &&
        targetTechnology.equalsIgnoreCase(ikmTargetTechnology)

    Set supportedScdTypes = OdiColumn.ScdType.values().collect { it.name() } as Set
    boolean scdApiSupported = supportedScdTypes.containsAll([
        "SURROGATE_KEY",
        "NATURAL_KEY",
        "ADD_ROW_ON_CHANGE",
        "CURRENT_RECORD_FLAG",
        "START_TIMESTAMP",
        "END_TIMESTAMP",
    ])

    def roles = [
        surrogate_key: [],
        natural_key: [],
        start_date: [],
        end_date: [],
        current_flag: [],
        tracked_attributes: [],
    ]
    def roleMap = [
        SURROGATE_KEY: "surrogate_key",
        NATURAL_KEY: "natural_key",
        START_TIMESTAMP: "start_date",
        END_TIMESTAMP: "end_date",
        CURRENT_RECORD_FLAG: "current_flag",
        ADD_ROW_ON_CHANGE: "tracked_attributes",
    ]
    targetDs?.getColumns()?.toList()?.sort { left, right ->
        (left.getPosition() ?: 0) <=> (right.getPosition() ?: 0)
    }?.each { column ->
        String roleName = column.getScdType()?.name()
        String outputRole = roleMap[roleName]
        if (outputRole != null) {
            roles[outputRole] << column.getName()
        }
    }

    def primaryKeyColumns = orderedColumnNames(targetDs?.getPrimaryKey()?.getColumns())
    def naturalKeyColumns = targetDs?.getKeys()?.findAll {
        it.isAK()
    }?.collectMany {
        orderedColumnNames(it.getColumns())
    }?.unique() ?: []

    def capability = [
        status: "passed",
        *: safeFlags(),
        target_technology: targetTechnology,
        target_ikm: ikm == null ? null : TARGET_IKM,
        target_ikm_installed: ikm != null,
        target_ikm_assignable: ikmAssignable,
        lkm: lkm == null ? null : LKM_NAME,
        scd2_role_assignment_supported: scdApiSupported,
    ]
    def datastore = [
        status: "passed",
        *: safeFlags(),
        source: [
            owner: SOURCE_OWNER,
            table: SOURCE_TABLE,
            column_count: sourceDs == null ? 0 : sourceDs.getColumns().size(),
        ],
        target: [
            owner: TARGET_OWNER,
            table: TARGET_TABLE,
            column_count: targetDs == null ? 0 : targetDs.getColumns().size(),
        ],
        target_keys: [
            primary_key_columns: primaryKeyColumns,
            natural_key_columns: naturalKeyColumns,
        ],
        scd_roles: roles,
    ]
    def objects = [
        status: "attention_required",
        *: safeFlags(),
        mapping: [name: MAPPING_NAME, exists: folder == null ? null : mapping != null],
        scenario: [
            name: SCENARIO_NAME,
            version: SCENARIO_VERSION,
            exists: scenario != null,
        ],
    ]
    def payload = [
        tool: "odi.dim_product_scd2_capability_probe",
        status: "passed",
        mode: "odi_sdk_finders_reflection_only",
        *: safeFlags(),
        repository_connection_attempted: true,
        capability: capability,
        datastore: datastore,
        objects: objects,
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    def payload = [
        tool: "odi.dim_product_scd2_capability_probe",
        status: "error",
        mode: "odi_sdk_finders_reflection_only",
        side_effects: false,
        repository_connection_attempted: true,
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
        error_type: e.getClass().getSimpleName(),
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}

