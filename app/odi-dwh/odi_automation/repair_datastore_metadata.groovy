/*
  NL-DWH Studio: Idempotent ODI datastore metadata repair.
  Fills missing column dataFormat values required by mapping component wiring.
  It does not create or recreate datastores/columns.
*/

import groovy.json.JsonOutput
import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiDataStoreFormatType
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.domain.model.finder.IOdiDataStoreFormatTypeFinder
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.persistence.transaction.ITransactionManager
import oracle.odi.core.persistence.transaction.ITransactionStatus
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition
import oracle.odi.core.security.Authentication

def checks = []
def odi = null
boolean standaloneMode = false

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) check.metadata = metadata
    checks << check
}

def safeMessage = { Throwable t ->
    String msg = t?.getMessage() ?: t?.toString() ?: ""
    msg = msg.replaceAll(/(?i)(password|passwd|pwd|token|secret)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    msg = msg.replaceAll(/jdbc:[^\s,)]+/, "jdbc:***")
    msg = msg.replaceAll(/(?i)(user(name)?|uid)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    return msg.take(240)
}

try {
    try {
        odi = odiInstance
    } catch (MissingPropertyException e) {
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
        WorkRepositoryDbInfo workRepo = new WorkRepositoryDbInfo(props.getProperty("work_name"), poolAttrs)
        odi = OdiInstance.createInstance(new OdiInstanceConfig(masterRepo, workRepo))
        Authentication auth = odi.getSecurityManager().createAuthentication(
            props.getProperty("odi_username"),
            props.getProperty("odi_password").toCharArray()
        )
        odi.getSecurityManager().setCurrentThreadAuthentication(auth)
    }

    String modelCode = System.getProperty("odi.model_code")
    String tableName = System.getProperty("odi.table_name")
    if (!modelCode || !tableName) {
        throw new Exception("MISSING PARAMS: odi.model_code and odi.table_name are required")
    }

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

    try {
        def em = odi.getTransactionalEntityManager()
        IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)
        OdiDataStore ds = dsFinder.findByName(tableName, modelCode)
        if (ds == null) {
            throw new Exception("Datastore not found: " + tableName + " in " + modelCode)
        }
        addCheck("datastore_found", "pass", "Datastore found.", [model: modelCode, datastore: tableName])

        try {
            def currentFormatType = ds.getTableFormatType()
            if (currentFormatType != null) {
                addCheck("table_format_type_exists", "pass", "Datastore table format type already exists.", [format_type_present: true])
            } else {
                IOdiDataStoreFormatTypeFinder fmtFinder = (IOdiDataStoreFormatTypeFinder) em.getFinder(OdiDataStoreFormatType.class)
                def fmt = null
                for (String candidate : ["TABLE", "Table", "RELATIONAL", "Relational", "RDBMS", "Default", "DEFAULT"]) {
                    if (fmt != null) break
                    try { fmt = fmtFinder.findByName(candidate) } catch (Exception ignored) {}
                }
                if (fmt == null) {
                    try {
                        def genericTypes = fmtFinder.findGenericFormatTypes()
                        fmt = genericTypes?.find { true }
                    } catch (Exception ignored) {}
                }
                if (fmt != null) {
                    ds.setTableFormatType(fmt)
                    addCheck("table_format_type_repaired", "pass", "Datastore table format type filled.", [format_type_present: true])
                } else {
                    addCheck("table_format_type_repaired", "warning", "No generic datastore table format type found; column metadata repair will continue.", [format_type_present: false])
                }
            }
        } catch (Exception fmtEx) {
            addCheck("table_format_type_repair_warning", "warning", "Datastore table format type repair skipped after SDK warning.", [detail: safeMessage(fmtEx)])
        }

        int inspected = 0
        int repaired = 0
        int skipped = 0
        ds.getColumns().each { col ->
            inspected++
            String existingFormat = null
            try { existingFormat = col.getDataFormat() } catch (Exception ignored) {}
            if (existingFormat != null && existingFormat.trim().length() > 0) {
                skipped++
                return
            }

            String inferredFormat = null
            try { inferredFormat = col.getDataType()?.getCode() } catch (Exception ignored) {}
            if (!inferredFormat) {
                try { inferredFormat = col.getDdlDataType()?.replaceAll(/\(.*/, "") } catch (Exception ignored) {}
            }
            if (!inferredFormat) inferredFormat = "VARCHAR2"

            col.setDataFormat(inferredFormat.toUpperCase())
            repaired++
        }
        addCheck("column_data_formats_repaired", "pass", "Missing column dataFormat values filled without changing existing formats.", [inspected: inspected, repaired: repaired, already_present: skipped])

        txnMgr.commit(txnStatus)
        addCheck("commit_completed", "pass", "Datastore metadata repair committed.")
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }

    def payload = [
        tool: "odi.repair_datastore_metadata",
        status: "passed",
        mode: "idempotent_datastore_column_metadata_repair",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("repair_datastore_metadata_exception", "fail", "ODI datastore metadata repair failed.")
    def payload = [
        tool: "odi.repair_datastore_metadata",
        status: "error",
        mode: "idempotent_datastore_column_metadata_repair",
        side_effects: true,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
