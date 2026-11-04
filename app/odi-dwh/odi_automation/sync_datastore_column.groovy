/*
  ODI Datastore Column / Table Metadata Sync
  ==========================================
  Updates ODI datastore metadata after a reviewed DB schema change.
  This script does not execute physical DB DDL.

  Parameters:
    -Dodi.model_code=TGT_DWH
    -Dodi.table_name=ODS_HR_EMPLOYEES
    -Dodi.operation=modify_type|rename_column|rename_table
    -Dodi.column_name=FIRST_NAME
    -Dodi.column_type=VARCHAR2
    -Dodi.column_length=30
    -Dodi.column_scale=0
    -Dodi.new_name=ODS_HR_EMPLOYEES_V2 or FIRST_NAME2
*/

import oracle.odi.domain.model.OdiDataStore
import oracle.odi.domain.model.OdiColumn
import oracle.odi.domain.model.finder.IOdiDataStoreFinder
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*
import oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition

def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
    String propsPath = args.length > 0 ? args[0] : "odi_connection.properties"
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

String modelCode = System.getProperty("odi.model_code")
String tableName = System.getProperty("odi.table_name")
String operation = System.getProperty("odi.operation")
String colName = System.getProperty("odi.column_name")
String colType = System.getProperty("odi.column_type")
String colLengthText = System.getProperty("odi.column_length")
String colScaleText = System.getProperty("odi.column_scale")
String newName = System.getProperty("odi.new_name")

if (!modelCode || !tableName || !operation) {
    throw new Exception("MISSING PARAMS: odi.model_code, odi.table_name, odi.operation are required")
}

int colLength = colLengthText ? Integer.parseInt(colLengthText) : 0
int colScale = colScaleText ? Integer.parseInt(colScaleText) : 0

println "PARAMS: model=${modelCode}, table=${tableName}, operation=${operation}, column=${colName ?: ''}"

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()
    IOdiDataStoreFinder dsFinder = (IOdiDataStoreFinder) em.getFinder(OdiDataStore.class)

    OdiDataStore ds = dsFinder.findByName(tableName, modelCode)
    if (ds == null) throw new Exception("Datastore not found: ${tableName} in ${modelCode}")

    if (operation == "modify_type") {
        if (!colName || !colType) throw new Exception("modify_type requires odi.column_name and odi.column_type")
        OdiColumn col = ds.getColumns().find { it.getName().equalsIgnoreCase(colName) }
        if (col == null) throw new Exception("Column not found: ${colName} in ${tableName}")

        boolean typeSet = false
        for (String tryType : [colType.toUpperCase(), colType, colType.toLowerCase()]) {
            if (typeSet) break
            try {
                col.setDataTypeCode(tryType)
                typeSet = true
            } catch (Exception ignore) {}
        }
        if (!typeSet) throw new Exception("Could not set ODI data type: ${colType}")
        if (colLength > 0) col.setLength(colLength)
        if (colScale >= 0) col.setScale(colScale)
        println "COLUMN TYPE SYNCED: ${colName} -> ${colType}(${colLength})"

    } else if (operation == "rename_column") {
        if (!colName || !newName) throw new Exception("rename_column requires odi.column_name and odi.new_name")
        OdiColumn col = ds.getColumns().find { it.getName().equalsIgnoreCase(colName) }
        if (col == null) throw new Exception("Column not found: ${colName} in ${tableName}")
        col.setName(newName.trim())
        println "COLUMN RENAMED: ${colName} -> ${newName}"

    } else if (operation == "rename_table") {
        if (!newName) throw new Exception("rename_table requires odi.new_name")
        ds.setName(newName.trim())
        ds.setResourceName(newName.trim())
        println "DATASTORE RENAMED: ${tableName} -> ${newName}"

    } else {
        throw new Exception("Unsupported operation: ${operation}")
    }

    txnMgr.commit(txnStatus)
    println "SUCCESS: ODI datastore metadata sync committed"

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ignored) {}
    println '{"status":"error","action":"sync_datastore_column","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
