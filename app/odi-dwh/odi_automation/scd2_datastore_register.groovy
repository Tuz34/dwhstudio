/*
  Generic SCD2 target datastore registrar (absent-only).
  Registers the planned target in the ODI model with a primary key, a
  natural-key alternate key and SCD roles. Parameters come from the validated
  plan; the script identity and repository object kinds are fixed.
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

final String TOOL = "odi.scd2_datastore_register"
final String MODE = "generic_absent_only_scd2_datastore_registration"
final java.util.regex.Pattern ID = ~/^[A-Z0-9_#]{1,30}$/
final java.util.regex.Pattern TYPE = ~/^([A-Z][A-Z0-9]*?)(?:_(\d+)(?:_(\d+))?)?(?:_(CHAR))?$/
final Map<String, String> FIXED_ROLES = [
    EFFECTIVE_FROM: "START_TIMESTAMP",
    EFFECTIVE_TO: "END_TIMESTAMP",
    CURRENT_FLAG: "CURRENT_RECORD_FLAG",
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

// Fixed allowlisted reason codes or the exception class simple name; the exception
// message is never returned (it could carry repository values).
final Set<String> REASON_CODES = ["InvalidParameter", "InvalidColumnType", "TargetModelUnavailable", "TargetModelNotOracle", "TargetDatastoreAlreadyExists", "ColumnContractMismatch", "ColumnOrderMismatch", "ColumnMetadataMismatch", "PrimaryKeyMismatch", "NaturalKeyMismatch"] as Set
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

def parseList = { String value ->
    (value == null || value == "none") ? [] : value.split(",").collect { it.trim() }.findAll { it }
}

def parseMap = { String value ->
    Map<String, String> result = [:]
    parseList(value).each { entry ->
        int idx = entry.lastIndexOf(":")
        if (idx <= 0) {
            throw new IllegalStateException("InvalidParameter")
        }
        result[entry.substring(0, idx)] = entry.substring(idx + 1)
    }
    return result
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

    String targetModel = requireProp("odi.scd2.target_model", ID)
    String targetTable = requireProp("odi.scd2.target_table", ID)
    String skName = requireProp("odi.scd2.sk", ID)
    List<String> naturalKey = parseList(requireProp("odi.scd2.natural_key", ~/^[A-Z0-9_#,]+$/))
    List<String> tracked = parseList(requireProp("odi.scd2.tracked", ~/^[A-Z0-9_#,]+$/))
    List<String> overwrite = parseList(requireProp("odi.scd2.overwrite", ~/^[A-Za-z0-9_#,]+$/))
    Map<String, String> types = parseMap(requireProp("odi.scd2.column_types", ~/^[A-Z0-9_#:,]+$/))
    Map<String, String> nullable = parseMap(requireProp("odi.scd2.column_nullable", ~/^[A-Z0-9_#:,]+$/))
    String pkName = ("PK_" + targetTable).take(30)
    String akName = ("AK_" + targetTable).take(30)
    if (naturalKey.isEmpty() || tracked.isEmpty() || types.isEmpty()) {
        throw new IllegalStateException("InvalidParameter")
    }

    txnMgr = odi.getTransactionManager()
    txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())
    try {
        def em = odi.getTransactionalEntityManager()
        IOdiModelFinder modelFinder = (IOdiModelFinder) em.getFinder(OdiModel.class)
        OdiModel model = modelFinder.findByCode(targetModel)
        if (model == null) {
            throw new IllegalStateException("TargetModelUnavailable")
        }
        if (model.getTechnology()?.getName()?.toUpperCase() != "ORACLE") {
            throw new IllegalStateException("TargetModelNotOracle")
        }
        IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
        if (dsFinder.findByName(targetTable, targetModel) != null) {
            throw new IllegalStateException("TargetDatastoreAlreadyExists")
        }

        def roleOf = { String name ->
            if (name == skName) return "SURROGATE_KEY"
            if (naturalKey.contains(name)) return "NATURAL_KEY"
            if (tracked.contains(name)) return "ADD_ROW_ON_CHANGE"
            if (overwrite.contains(name)) return "OVERWRITE_ON_CHANGE"
            return FIXED_ROLES[name]
        }

        OdiDataStore target = new OdiDataStore(model, targetTable)
        target.setDefaultAlias(targetTable)
        target.setResourceName(targetTable)

        Map<String, OdiColumn> columnsByName = [:]
        List<String> expectedNames = []
        int position = 1
        types.each { String name, String encoded ->
            def matcher = (encoded =~ TYPE)
            if (!matcher.matches()) {
                throw new IllegalStateException("InvalidColumnType")
            }
            String base = matcher.group(1)
            Integer length = matcher.group(2) == null ? null : Integer.valueOf(matcher.group(2))
            Integer scale = matcher.group(3) == null ? null : Integer.valueOf(matcher.group(3))
            OdiColumn column = new OdiColumn(target, name)
            column.setDataTypeCode(base)
            String dataFormat = base
            if (matcher.group(4) != null) {
                dataFormat += "(${length} CHAR)"
            } else if (length != null && scale != null) {
                dataFormat += "(${length},${scale})"
            } else if (length != null) {
                dataFormat += "(${length})"
            }
            column.setDataFormat(dataFormat)
            if (length != null) column.setLength(length)
            if (scale != null) column.setScale(scale)
            column.setMandatory(nullable[name] == "N")
            column.setPosition(position++)
            String role = roleOf(name)
            if (role != null) {
                column.setScdType(OdiColumn.ScdType.valueOf(role))
            }
            columnsByName[name] = column
            expectedNames << name
        }
        ([skName, "EFFECTIVE_FROM", "EFFECTIVE_TO", "CURRENT_FLAG"] + naturalKey).each {
            if (!columnsByName.containsKey(it)) {
                throw new IllegalStateException("ColumnContractMismatch")
            }
        }

        OdiKey primaryKey = new OdiKey(target, pkName)
        primaryKey.setKeyType(OdiKey.KeyType.PRIMARY_KEY)
        primaryKey.setActive(true)
        primaryKey.setInDatabase(true)
        primaryKey.addColumn(columnsByName[skName])

        OdiKey naturalKeyObject = new OdiKey(target, akName)
        naturalKeyObject.setKeyType(OdiKey.KeyType.ALTERNATE_KEY)
        naturalKeyObject.setActive(true)
        naturalKeyObject.setInDatabase(false)
        naturalKey.each { naturalKeyObject.addColumn(columnsByName[it]) }

        em.persist(target)
        em.persist(primaryKey)
        em.persist(naturalKeyObject)

        List<OdiColumn> ordered = target.getColumns().toList().sort { l, r -> l.getPosition() <=> r.getPosition() }
        if (ordered.collect { it.getName() } != expectedNames) {
            throw new IllegalStateException("ColumnOrderMismatch")
        }
        ordered.each { column ->
            if (column.getScdType()?.name() != roleOf(column.getName())) {
                throw new IllegalStateException("ColumnMetadataMismatch")
            }
        }
        if (!primaryKey.isPK() || primaryKey.getColumns()*.getName() != [skName] || !primaryKey.isInDatabase()) {
            throw new IllegalStateException("PrimaryKeyMismatch")
        }
        if (!naturalKeyObject.isAK() || naturalKeyObject.getColumns()*.getName() != naturalKey ||
            naturalKeyObject.isInDatabase()) {
            throw new IllegalStateException("NaturalKeyMismatch")
        }

        txnMgr.commit(txnStatus)
        txnStatus = null

        def count = { String role -> ordered.count { it.getScdType()?.name() == role } }
        println JsonOutput.toJson([
            tool: TOOL,
            status: "passed",
            mode: MODE,
            side_effects: true,
            repository_connection_attempted: true,
            *: safeFlags(),
            result: [
                status: "passed",
                target_column_count: ordered.size(),
                primary_key_column_count: 1,
                natural_key_column_count: naturalKey.size(),
                scd_role_column_counts: [
                    surrogate_key: count("SURROGATE_KEY"),
                    natural_key: count("NATURAL_KEY"),
                    start_date: count("START_TIMESTAMP"),
                    end_date: count("END_TIMESTAMP"),
                    current_flag: count("CURRENT_RECORD_FLAG"),
                    tracked_attributes: count("ADD_ROW_ON_CHANGE"),
                    overwrite_attributes: count("OVERWRITE_ON_CHANGE"),
                ],
                commit_completed: true,
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
