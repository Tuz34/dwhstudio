/*
  NL-DWH Studio: ODI load-plan read-only metadata probe.
  It only uses ODI SDK finders and never starts or updates load plans.
*/

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.security.Authentication
import oracle.odi.domain.runtime.loadplan.OdiLoadPlan
import oracle.odi.domain.runtime.loadplan.finder.IOdiLoadPlanFinder

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

def scenarioNameFromStep = { step ->
    try { return step.getScenario()?.getName() } catch (Exception ignored) {}
    try { return step.getScenarioName() } catch (Exception ignored) {}
    try { return step.scenarioName } catch (Exception ignored) {}
    return null
}

def scenarioVersionFromStep = { step ->
    try { return String.valueOf(step.getScenario()?.getVersion()) } catch (Exception ignored) {}
    try { return String.valueOf(step.getScenarioVersion()) } catch (Exception ignored) {}
    try { return String.valueOf(step.scenarioVersion) } catch (Exception ignored) {}
    return null
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

    String loadPlanName = System.getProperty("odi.load_plan.name")
    if (!loadPlanName) throw new Exception("MISSING PARAMS: odi.load_plan.name is required")

    def em = odi.getTransactionalEntityManager()
    IOdiLoadPlanFinder loadPlanFinder = (IOdiLoadPlanFinder) em.getFinder(OdiLoadPlan.class)
    OdiLoadPlan loadPlan = null
    try { loadPlan = loadPlanFinder.findByName(loadPlanName) } catch (Exception ignored) {}
    addCheck("load_plan_found", loadPlan != null ? "pass" : "fail", loadPlan != null ? "Load plan found." : "Load plan not found.", [load_plan: loadPlanName])

    def steps = []
    if (loadPlan != null) {
        try {
            loadPlan.getRootStep()?.getChildrenSteps()?.each { step ->
                steps << [
                    name: step.getName(),
                    scenario_name: scenarioNameFromStep(step),
                    version: scenarioVersionFromStep(step)
                ]
            }
            addCheck("load_plan_steps_read", "pass", "Load plan root steps were read.", [step_count: steps.size()])
        } catch (Exception stepError) {
            addCheck("load_plan_steps_read", "fail", "Load plan steps could not be read.", [error: safeMessage(stepError)])
        }
    }
    addCheck("forbidden_operations_not_called", "pass", "No persist, update, load plan run, scenario run, target DDL, or data load is used by this script.")

    boolean failed = checks.any { it.status == "fail" }
    def payload = [
        tool: "odi.load_plan_probe",
        status: failed ? "attention_required" : "passed",
        mode: "metadata_read_only_no_run",
        side_effects: false,
        will_run_load_plan: false,
        repository_connection_attempted: true,
        load_plan: [name: loadPlanName, steps: steps],
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("load_plan_probe_exception", "fail", "ODI load-plan probe failed before completion.")
    def payload = [
        tool: "odi.load_plan_probe",
        status: "error",
        mode: "metadata_read_only_no_run",
        side_effects: false,
        will_run_load_plan: false,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) odi.close()
}
