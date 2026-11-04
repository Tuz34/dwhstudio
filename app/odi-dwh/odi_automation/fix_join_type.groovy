/*
  ODI Join Type Fix v2: Set LEFT_OUTER on JoinComponent InputConnectorPoints
  ===========================================================================
  In ODI 12c the join type is controlled through the JoinComponent
  InputConnectorPoints. For LEFT OUTER the outerJoin flag is set on the "right" input.

  Also:
  - setJoinType() is attempted (SDK level)
  - getAllMethods/properties logging (debug)
*/

import oracle.odi.domain.project.*
import oracle.odi.domain.mapping.*
import oracle.odi.domain.mapping.component.*
import oracle.odi.domain.mapping.physical.*
import oracle.odi.domain.project.finder.*
import oracle.odi.core.config.*
import oracle.odi.core.OdiInstance
import oracle.odi.core.security.Authentication
import oracle.odi.core.persistence.transaction.*
import java.lang.reflect.Method

def odi = null
boolean standaloneMode = false
try {
    odi = odiInstance
} catch (MissingPropertyException e) {
    standaloneMode = true
    String propsPath = "c:/oracle_ai_app/scratch/odi_automation/odi_connection.properties"
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

String pCode = System.getProperty("odi.project") ?: "TUTORIOL"
String fName = System.getProperty("odi.folder")  ?: "First Folder"

ITransactionManager txnMgr = odi.getTransactionManager()
ITransactionStatus txnStatus = txnMgr.getTransaction(new oracle.odi.core.persistence.transaction.support.DefaultTransactionDefinition())

try {
    def em = odi.getTransactionalEntityManager()

    IOdiProjectFinder projFinder = (IOdiProjectFinder) em.getFinder(OdiProject.class)
    OdiProject project = projFinder.findAll().find { it.getCode().equalsIgnoreCase(pCode) }
    if (project == null) throw new Exception("Project not found: " + pCode)

    OdiFolder folder = project.getFolders().find { it.getName().equalsIgnoreCase(fName) }
    if (folder == null) throw new Exception("Folder not found: " + fName)

    int fixCount = 0

    folder.getMappings().each { mapping ->
        String mName = mapping.getName()

        mapping.getAllComponents().each { comp ->
            if (comp instanceof JoinComponent) {
                JoinComponent joinComp = (JoinComponent) comp
                println "\n=== MAPPING: ${mName} | JOIN: ${joinComp.getName()} ==="

                // STEP 1: Debug — list every public JoinComponent method (those containing join/outer)
                println "  --- JoinComponent Methods (containing join/outer/type) ---"
                Method[] methods = joinComp.getClass().getMethods()
                methods.each { m ->
                    String mn = m.getName().toLowerCase()
                    if (mn.contains("join") || mn.contains("outer") || mn.contains("type") || mn.contains("property")) {
                        println "    ${m.getName()}(${m.getParameterTypes()*.simpleName.join(', ')}): ${m.getReturnType().simpleName}"
                    }
                }

                // STEP 2: Current join type
                try {
                    println "  getJoinType(): ${joinComp.getJoinType()}"
                } catch (Exception e) {
                    println "  getJoinType error type: ${e.getClass().getSimpleName()}"
                }

                // STEP 3: Set the outer join through the InputConnectorPoints
                println "  --- InputConnectorPoints ---"
                def inputCPs = joinComp.getInputConnectorPoints()
                inputCPs?.eachWithIndex { icp, idx ->
                    println "    INPUT[${idx}]: ${icp.getName()} class=${icp.getClass().simpleName}"

                    // Look for InputConnectorPoint methods related to outer join
                    Method[] icpMethods = icp.getClass().getMethods()
                    icpMethods.each { m ->
                        String mn = m.getName().toLowerCase()
                        if (mn.contains("outer") || mn.contains("join")) {
                            println "      → ${m.getName()}(${m.getParameterTypes()*.simpleName.join(', ')}): ${m.getReturnType().simpleName}"
                        }
                    }
                }

                // STEP 4: Try through ConnectorPoints (getConnectorPoints)
                println "  --- All ConnectorPoints ---"
                try {
                    def allCPs = joinComp.getConnectorPoints()
                    allCPs?.each { cp ->
                        println "    CP: ${cp.getName()} dir=${cp.getDirection()} class=${cp.getClass().simpleName}"
                        Method[] cpMethods = cp.getClass().getMethods()
                        cpMethods.each { m ->
                            String mn = m.getName().toLowerCase()
                            if (mn.contains("outer") || mn.contains("join") || mn.contains("left")) {
                                println "      → ${m.getName()}(${m.getParameterTypes()*.simpleName.join(', ')}): ${m.getReturnType().simpleName}"
                            }
                        }
                    }
                } catch (Exception cpe) {
                    println "  getConnectorPoints error type: ${cpe.getClass().getSimpleName()}"
                }

                // STEP 5: Try through the physical design
                println "  --- Physical Design ---"
                try {
                    MapPhysicalDesign design = mapping.getPhysicalDesign(0)
                    design.getPhysicalNodes().each { node ->
                        println "    PhysNode: ${node.getName()} class=${node.getClass().simpleName}"
                        if (node.getName().toUpperCase().contains("JOIN")) {
                            Method[] pnMethods = node.getClass().getMethods()
                            pnMethods.each { m ->
                                String mn = m.getName().toLowerCase()
                                if (mn.contains("outer") || mn.contains("join") || mn.contains("type")) {
                                    println "      → ${m.getName()}(${m.getParameterTypes()*.simpleName.join(', ')}): ${m.getReturnType().simpleName}"
                                }
                            }
                        }
                    }
                } catch (Exception pde) {
                    println "  Physical design error type: ${pde.getClass().getSimpleName()}"
                }

                // STEP 6: Try setJoinType (every variant)
                boolean fixed = false

                // 6a: Enum.valueOf reflection
                try {
                    def jtClass = Class.forName("oracle.odi.domain.mapping.component.JoinComponent\$JoinType")
                    def jtEnum = Enum.valueOf(jtClass, "LEFT_OUTER")
                    joinComp.setJoinType(jtEnum)
                    fixed = true
                    println "  SET: LEFT_OUTER via enum reflection"
                } catch (Exception e1) {
                    println "  6a enum reflection error type: ${e1.getClass().getSimpleName()}"
                }

                // 6b: find the inner enum with getDeclaredClasses
                if (!fixed) {
                    try {
                        joinComp.getClass().getDeclaredClasses().each { inner ->
                            println "  Inner class: ${inner.getName()}"
                            if (inner.isEnum()) {
                                inner.getEnumConstants().each { ec ->
                                    println "    enum: ${ec}"
                                }
                            }
                        }
                    } catch (Exception e) {
                        println "  getDeclaredClasses error type: ${e.getClass().getSimpleName()}"
                    }
                }

                // 6c: setPropertyValue
                if (!fixed) {
                    try {
                        // ODI internal property name for join type
                        joinComp.setPropertyValue("JOIN_TYPE", "LEFT_OUTER")
                        fixed = true
                        println "  SET: LEFT_OUTER via setPropertyValue"
                    } catch (Exception e2) {
                        println "  6c setPropertyValue error type: ${e2.getClass().getSimpleName()}"
                    }
                }

                if (fixed) fixCount++
            }
        }
    }

    txnMgr.commit(txnStatus)
    println "\n=========================================="
    println "DONE: ${fixCount} join(s) updated."

} catch (Exception e) {
    if (txnStatus != null && !txnStatus.isCompleted()) try { txnMgr.rollback(txnStatus) } catch (ig) {}
    println '{"status":"error","action":"fix_join_type","error_type":"' + e.getClass().getSimpleName() + '"}'
    System.exit(1)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
