/*
  Generic SCD2 read-only readiness probe.
  Reports booleans and counts only: model/datastore/KM/mapping presence.
  Parameters come from the validated SCD2 plan (system properties).
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
import oracle.odi.domain.mapping.physical.MapPhysicalDesign
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.model.finder.IOdiModelFinder
import oracle.odi.domain.project.OdiFolder
import oracle.odi.domain.project.OdiIKM
import oracle.odi.domain.project.OdiLKM
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.project.finder.IOdiIKMFinder
import oracle.odi.domain.project.finder.IOdiLKMFinder
import oracle.odi.domain.project.finder.IOdiProjectFinder
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.Tag
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder

final String TOOL = "odi.scd2_probe"
final String MODE = "generic_scd2_readiness_probe"
final String TARGET_IKM = "IKM Oracle Slowly Changing Dimension"
final String LKM_NAME = "LKM SQL to Oracle (Built-In)"
final java.util.regex.Pattern ID = ~/^[A-Z0-9_#]{1,30}$/
final java.util.regex.Pattern MAPPING_ID = ~/^[A-Z0-9_#]{1,31}$/

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

// Fixed allowlisted reason codes or the exception class simple name; the exception
// message is never returned (it could carry repository values).
final Set<String> REASON_CODES = ["InvalidParameter", "ProjectContextUnavailable"] as Set
def reasonCode = { Exception e ->
    String message = e.getMessage()
    if (e instanceof IllegalStateException && message != null && REASON_CODES.contains(message)) {
        return message
    }
    return e.getClass().getSimpleName()
}

def requireProp = { String key, java.util.regex.Pattern pattern ->
    String value = System.getProperty(key)
    if (value == null || !(value ==~ pattern)) {
        throw new IllegalStateException("InvalidParameter")
    }
    return value
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

    String sourceModel = requireProp("odi.scd2.source_model", ID)
    String sourceTable = requireProp("odi.scd2.source_table", ID)
    String targetModel = requireProp("odi.scd2.target_model", ID)
    String targetTable = requireProp("odi.scd2.target_table", ID)
    String mappingName = requireProp("odi.scd2.mapping", MAPPING_ID)
    String scenarioName = mappingName + "_SCN"
    String scenarioVersion = System.getProperty("odi.scd2.scenario_version")
    if (scenarioVersion != null && !(scenarioVersion ==~ /^[A-Za-z0-9._-]{1,12}$/)) {
        throw new IllegalStateException("InvalidParameter")
    }
    String projectCode = System.getProperty("odi.project")
    String folderName = System.getProperty("odi.folder")
    if (projectCode == null || folderName == null) {
        throw new IllegalStateException("ProjectContextUnavailable")
    }

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    try {
        def em = odi.getTransactionalEntityManager()
        IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
        IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

        OdiModel srcModel = modelFinder.findByCode(sourceModel)
        OdiModel tgtModel = modelFinder.findByCode(targetModel)
        OdiDataStore srcStore = srcModel == null ? null : dsFinder.findByName(sourceTable, sourceModel)
        OdiDataStore tgtStore = tgtModel == null ? null : dsFinder.findByName(targetTable, targetModel)

        IOdiProjectFinder projectFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
        OdiProject project = projectFinder.findAll().find { it.getCode()?.equalsIgnoreCase(projectCode) }
        OdiFolder folder = project?.getFolders()?.find { it.getName()?.equalsIgnoreCase(folderName) }

        IOdiIKMFinder ikmFinder = (IOdiIKMFinder) em.getFinder(OdiIKM.class)
        OdiIKM ikm = (ikmFinder.findByName(TARGET_IKM) ?: []).find { it.getName()?.equalsIgnoreCase(TARGET_IKM) }
        IOdiLKMFinder lkmFinder = (IOdiLKMFinder) em.getFinder(OdiLKM.class)
        OdiLKM lkm = (lkmFinder.findByName(LKM_NAME) ?: []).find { it.getName()?.equalsIgnoreCase(LKM_NAME) }

        Mapping mapping = folder == null ? null : em.getFinder(Mapping.class).findByName(folder, mappingName)
        boolean mappingExists = mapping != null
        IOdiScenarioFinder scenarioFinder = (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
        List<String> scenarioVersions = []
        if (mapping != null) {
            scenarioVersions = (scenarioFinder.findBySourceMapping(mapping.getInternalId()) ?: [])
                .findAll { it.getName()?.equalsIgnoreCase(scenarioName) }
                .collect { it.getVersion() }.findAll { it != null }.sort()
        }
        OdiScenario latestScenario = scenarioFinder.findLatestByName(scenarioName)
        // A newer version owned by this mapping does not establish ownership of the requested version.
        OdiScenario requestedScenario = scenarioVersion == null ? null :
            scenarioFinder.findByTag(new Tag(scenarioName, scenarioVersion))
        boolean scenarioExists = !scenarioVersions.isEmpty() || latestScenario != null
        boolean scenarioNameCollision = [latestScenario, requestedScenario].any { OdiScenario scenario ->
            scenario != null && (
                mapping == null || scenario.getSourceMappingId() == null ||
                scenario.getSourceMappingId().toString() != mapping.getInternalId().toString()
            )
        }

        // Contract-relevant metadata of existing objects (identifiers and counts only).
        List<Map> targetColumns = []
        List<String> targetPk = []
        List<String> targetAk = []
        if (tgtStore != null) {
            targetColumns = tgtStore.getColumns().toList().sort { it.getPosition() }.collect {
                String format = null
                try { format = it.getDataFormat() } catch (MissingMethodException ignoredFormat) {}
                if (format == null) format = it.getDdlDataType()
                format = format?.trim()?.toUpperCase()?.replaceAll(/\s+/, " ")
                String semantics = format?.endsWith(" BYTE)") ? "BYTE" :
                    (format?.endsWith(" CHAR)") ? "CHAR" : null)
                [
                    name: it.getName(), role: it.getScdType()?.name() ?: "NONE",
                    mandatory: it.isMandatory(), data_format: format,
                    type_code: it.getDataType()?.getCode(), length: it.getLength(),
                    scale: it.getScale(),
                    length_semantics: semantics,
                ]
            }
            tgtStore.getKeys()?.each { key ->
                if (key.isPK()) targetPk = key.getColumns()*.getName()
                if (key.isAK()) targetAk = key.getColumns()*.getName()
            }
        }
        Map mappingInfo = null
        if (mapping != null) {
            List<DatastoreComponent> components = mapping.getAllComponents().findAll {
                it instanceof DatastoreComponent
            } as List<DatastoreComponent>
            def identity = { OdiDataStore store ->
                [model: store?.getModel()?.getCode(), table: store?.getName()]
            }
            def isTarget = { DatastoreComponent c ->
                OdiDataStore store = c.getBoundDataStore()
                store?.getModel()?.getCode()?.equalsIgnoreCase(targetModel) &&
                    store?.getName()?.equalsIgnoreCase(targetTable)
            }
            List<DatastoreComponent> targets = components.findAll { isTarget(it) }
            List<DatastoreComponent> sources = components.findAll { !isTarget(it) }
            String integrationType = targets.size() == 1 ? targets[0].getIntegrationType() : null
            String ikmName = null
            List<String> lkmNames = []
            int accessPointCount = 0
            try {
                MapPhysicalDesign design = mapping.getPhysicalDesign(0)
                def tNode = design.getPhysicalNodes().find { it.isTargetNode() }
                ikmName = tNode?.getTargetComponentKMName()
                def apNodes = design.getPhysicalNodes().findAll { it.isAPNode() }
                accessPointCount = apNodes.size()
                lkmNames = apNodes.collect { it.getLKMName() ?: "" }
            } catch (Exception ignoredPhysical) {
                ikmName = null
            }
            mappingInfo = [
                target_count: targets.size(),
                target_table: targets.size() == 1 ? targets[0].getBoundDataStore()?.getName() : null,
                source_tables: sources.collect { it.getBoundDataStore()?.getName() }.findAll { it }.sort(),
                target_component: targets.size() == 1 ? identity(targets[0].getBoundDataStore()) : null,
                source_components: sources.collect { identity(it.getBoundDataStore()) }
                    .sort { "${it.model}.${it.table}" },
                integration_type: integrationType,
                target_ikm: ikmName,
                lkm_names: lkmNames,
                access_point_count: accessPointCount,
                target_attribute_count: targets.size() == 1 ? targets[0].getAttributes().size() : 0,
                mapped_attribute_count: targets.size() == 1 ?
                    targets[0].getAttributes().count { it.getExpression() != null } : 0,
            ]
        }

        String targetTechnology = tgtModel?.getTechnology()?.getName()?.toUpperCase()
        txnMgr.rollback(txnStatus)
        txnStatus = null

        println JsonOutput.toJson([
            tool: TOOL,
            status: "passed",
            mode: MODE,
            side_effects: false,
            repository_connection_attempted: true,
            *: safeFlags(),
            result: [
                source_model_exists: srcModel != null,
                source_datastore_exists: srcStore != null,
                source_column_count: srcStore == null ? 0 : srcStore.getColumns().size(),
                target_model_exists: tgtModel != null,
                target_model_oracle: targetTechnology == "ORACLE",
                target_datastore_exists: tgtStore != null,
                project_folder_exists: folder != null,
                ikm_present: ikm != null,
                ikm_scd_type: ikm != null && ikm.getIntegrationType() == "SLOWLY_CHANGING_DIMENSION",
                lkm_present: lkm != null,
                mapping_exists: mappingExists,
                scenario_exists: scenarioExists,
                scenario_name_collision: scenarioNameCollision,
                scenario_versions: scenarioVersions,
                target_columns: targetColumns,
                target_primary_key: targetPk,
                target_alternate_key: targetAk,
                mapping: mappingInfo,
            ],
        ])
    } catch (Exception inner) {
        if (txnStatus != null) {
            try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
            txnStatus = null
        }
        throw inner
    }
} catch (Exception e) {
    println JsonOutput.toJson([
        tool: TOOL,
        status: "error",
        mode: MODE,
        side_effects: false,
        repository_connection_attempted: true,
        *: safeFlags(),
        error_type: reasonCode(e),
    ])
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}
