/*
  ODI-68 fixed absent-only DIM_PRODUCT ODI datastore registrar.
  No caller-controlled repository object identities.
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
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiKey
import oracle.odi.domain.model.OdiModel
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.model.finder.IOdiModelFinder

final String MODEL_CODE = "TGT_DWH"
final String TARGET_OWNER = "DWH"
final String TARGET_TABLE = "DIM_PRODUCT"
final String PRIMARY_KEY_NAME = "PK_DIM_PRODUCT"
final String NATURAL_KEY_NAME = "AK_DIM_PRODUCT_PROD_ID"

final List<Map<String, Object>> COLUMN_CONTRACT = [
    [name: "PRODUCT_SK", dataType: "NUMBER", length: null, scale: null, mandatory: true, scd: "SURROGATE_KEY"],
    [name: "PROD_ID", dataType: "NUMBER", length: 6, scale: null, mandatory: true, scd: "NATURAL_KEY"],
    [name: "PROD_NAME", dataType: "VARCHAR2", length: 50, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_DESC", dataType: "VARCHAR2", length: 4000, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_SUBCATEGORY", dataType: "VARCHAR2", length: 50, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_SUBCATEGORY_ID", dataType: "NUMBER", length: null, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_SUBCATEGORY_DESC", dataType: "VARCHAR2", length: 2000, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_CATEGORY", dataType: "VARCHAR2", length: 50, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_CATEGORY_ID", dataType: "NUMBER", length: null, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_CATEGORY_DESC", dataType: "VARCHAR2", length: 2000, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_WEIGHT_CLASS", dataType: "NUMBER", length: 3, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_UNIT_OF_MEASURE", dataType: "VARCHAR2", length: 20, scale: null, mandatory: false, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_PACK_SIZE", dataType: "VARCHAR2", length: 30, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "SUPPLIER_ID", dataType: "NUMBER", length: 6, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_STATUS", dataType: "VARCHAR2", length: 20, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_LIST_PRICE", dataType: "NUMBER", length: 8, scale: 2, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_MIN_PRICE", dataType: "NUMBER", length: 8, scale: 2, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_TOTAL", dataType: "VARCHAR2", length: 13, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "PROD_TOTAL_ID", dataType: "NUMBER", length: null, scale: null, mandatory: true, scd: "ADD_ROW_ON_CHANGE"],
    [name: "EFFECTIVE_FROM", dataType: "DATE", length: null, scale: null, mandatory: true, scd: "START_TIMESTAMP"],
    [name: "EFFECTIVE_TO", dataType: "DATE", length: null, scale: null, mandatory: true, scd: "END_TIMESTAMP"],
    [name: "CURRENT_FLAG", dataType: "VARCHAR2", length: 1, scale: null, mandatory: true, scd: "CURRENT_RECORD_FLAG"],
    [name: "INSERT_DATE", dataType: "TIMESTAMP", length: 6, scale: null, mandatory: true, scd: null],
    [name: "UPDATE_DATE", dataType: "TIMESTAMP", length: 6, scale: null, mandatory: false, scd: null],
    [name: "SOURCE_SYSTEM", dataType: "VARCHAR2", length: 50, scale: null, mandatory: true, scd: null],
    [name: "ETL_BATCH_ID", dataType: "NUMBER", length: null, scale: null, mandatory: true, scd: null],
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

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    try {
        def em = odi.getTransactionalEntityManager()
        IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
        OdiModel model = modelFinder.findByCode(MODEL_CODE)
        if (model == null) {
            throw new IllegalStateException("Fixed target model is unavailable")
        }
        String technologyName = model.getTechnology()?.getName()?.toUpperCase()
        if (technologyName != "ORACLE") {
            throw new IllegalStateException("Fixed target model technology is not Oracle")
        }

        IOdiDataStoreFinder dataStoreFinder =
            (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
        OdiDataStore existing = dataStoreFinder.findByName(TARGET_TABLE, MODEL_CODE)
        if (existing != null) {
            throw new IllegalStateException("target datastore already exists")
        }

        OdiDataStore target = new OdiDataStore(model, TARGET_TABLE)
        target.setDefaultAlias(TARGET_TABLE)
        target.setResourceName(TARGET_TABLE)

        Map<String, OdiColumn> columnsByName = [:]
        int position = 1
        COLUMN_CONTRACT.each { definition ->
            OdiColumn column = new OdiColumn(target, definition.name as String)
            column.setDataTypeCode(definition.dataType as String)
            String dataFormat = definition.dataType as String
            if (definition.length != null && definition.scale != null) {
                dataFormat += "(${definition.length},${definition.scale})"
            } else if (definition.length != null) {
                dataFormat += "(${definition.length})"
            }
            if (definition.dataType == "TIMESTAMP") {
                // Fractional-seconds precision 6 is the Oracle default; data format stays bare TIMESTAMP.
                dataFormat = "TIMESTAMP"
            }
            column.setDataFormat(dataFormat)
            if (definition.length != null) {
                column.setLength(definition.length as Integer)
            }
            if (definition.scale != null) {
                column.setScale(definition.scale as Integer)
            }
            if (definition.dataType == "NUMBER" && definition.length == null && definition.scale == null) {
                // Oracle's unconstrained NUMBER sentinel overrides OdiColumn's default length 50.
                column.setLength(0)
                column.setScale(-127)
            }
            column.setMandatory(definition.mandatory as boolean)
            column.setPosition(position++)
            if (definition.scd != null) {
                column.setScdType(OdiColumn.ScdType.valueOf(definition.scd as String))
            }
            columnsByName[definition.name as String] = column
        }

        OdiKey primaryKey = new OdiKey(target, PRIMARY_KEY_NAME)
        primaryKey.setKeyType(OdiKey.KeyType.PRIMARY_KEY)
        primaryKey.setActive(true)
        primaryKey.setInDatabase(true)
        primaryKey.addColumn(columnsByName["PRODUCT_SK"])

        OdiKey naturalKey = new OdiKey(target, NATURAL_KEY_NAME)
        naturalKey.setKeyType(OdiKey.KeyType.ALTERNATE_KEY)
        naturalKey.setActive(true)
        naturalKey.setInDatabase(false)
        naturalKey.addColumn(columnsByName["PROD_ID"])

        em.persist(target)
        em.persist(primaryKey)
        em.persist(naturalKey)

        List<OdiColumn> orderedColumns = target.getColumns().toList().sort {
            left, right -> left.getPosition() <=> right.getPosition()
        }
        List<String> expectedNames = COLUMN_CONTRACT.collect { it.name as String }
        if (orderedColumns.collect { it.getName() } != expectedNames) {
            throw new IllegalStateException("Created column order does not match fixed contract")
        }
        COLUMN_CONTRACT.eachWithIndex { definition, index ->
            OdiColumn column = orderedColumns[index]
            String actualRole = column.getScdType()?.name()
            if (column.isMandatory() != (definition.mandatory as boolean) ||
                actualRole != (definition.scd as String)) {
                throw new IllegalStateException("Created column metadata does not match fixed contract")
            }
            Integer actualLength = column.getLength()
            if (definition.dataType == "TIMESTAMP" &&
                actualLength != 6) {
                throw new IllegalStateException("Created column metadata does not match fixed contract")
            }
        }
        if (!primaryKey.isPK() || primaryKey.getColumns()*.getName() != ["PRODUCT_SK"] ||
            !primaryKey.isInDatabase()) {
            throw new IllegalStateException("Created primary key metadata does not match fixed contract")
        }
        if (!naturalKey.isAK() || naturalKey.getColumns()*.getName() != ["PROD_ID"] ||
            naturalKey.isInDatabase()) {
            throw new IllegalStateException("Created natural key metadata does not match fixed contract")
        }

        txnMgr.commit(txnStatus)
        txnStatus = null

        def roleCounts = [
            surrogate_key: orderedColumns.count { it.getScdType()?.name() == "SURROGATE_KEY" },
            natural_key: orderedColumns.count { it.getScdType()?.name() == "NATURAL_KEY" },
            start_date: orderedColumns.count { it.getScdType()?.name() == "START_TIMESTAMP" },
            end_date: orderedColumns.count { it.getScdType()?.name() == "END_TIMESTAMP" },
            current_flag: orderedColumns.count { it.getScdType()?.name() == "CURRENT_RECORD_FLAG" },
            tracked_attributes: orderedColumns.count { it.getScdType()?.name() == "ADD_ROW_ON_CHANGE" },
        ]
        def payload = [
            tool: "odi.dim_product_scd2_datastore_registration",
            status: "passed",
            mode: "fixed_absent_only_odi_datastore_registration",
            side_effects: true,
            repository_connection_attempted: true,
            *: safeFlags(),
            result: [
                status: "passed",
                model_code: MODEL_CODE,
                target_owner: TARGET_OWNER,
                target_table: TARGET_TABLE,
                target_column_count: orderedColumns.size(),
                primary_key_columns: ["PRODUCT_SK"],
                natural_key_columns: ["PROD_ID"],
                scd_role_column_counts: roleCounts,
                commit_completed: true,
            ],
        ]
        println JsonOutput.toJson(payload)
    } catch (Exception inner) {
        if (txnStatus != null) {
            try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
            txnStatus = null
        }
        throw inner
    }
} catch (Exception e) {
    def payload = [
        tool: "odi.dim_product_scd2_datastore_registration",
        status: "error",
        mode: "fixed_absent_only_odi_datastore_registration",
        side_effects: false,
        repository_connection_attempted: true,
        *: safeFlags(),
        error_type: e.getClass().getSimpleName(),
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}
