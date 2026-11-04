/*
  NL-DWH Studio: Idempotent ODI load-plan metadata preflight.
  Creates a serial load plan with allowlisted Run Scenario steps.
  It never starts a load plan or scenario and never deletes existing metadata.
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
import oracle.odi.domain.runtime.loadplan.OdiLoadPlan
import oracle.odi.domain.runtime.loadplan.finder.IOdiLoadPlanFinder
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder
import oracle.odi.domain.topology.OdiContext
import oracle.odi.domain.topology.OdiLogicalAgent
import oracle.odi.domain.topology.finder.IOdiContextFinder

Set ALLOWED_SCENARIOS = [
    "MAP_HR_EMPLOYEES_TO_ODS_SCN",
    "MAP_SH_SALES_TO_ODS_SCN"
] as Set

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

def scenarioByNameVersion = { IOdiScenarioFinder finder, String scenarioName, String version ->
    def scen = null
    try { scen = finder.findByNameAndVersion(scenarioName, version) } catch (Exception ignored) {}
    if (scen != null) return scen
    try {
        def all = finder.findAll()
        scen = all.find { it.getName()?.equalsIgnoreCase(scenarioName) && String.valueOf(it.getVersion())?.equalsIgnoreCase(version) }
    } catch (Exception ignored) {}
    return scen
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

def describeLoadPlan = { OdiLoadPlan lp ->
    def steps = []
    try {
        lp.getRootStep()?.getChildrenSteps()?.each { step ->
            steps << [
                name: step.getName(),
                scenario_name: scenarioNameFromStep(step),
                version: scenarioVersionFromStep(step)
            ]
        }
    } catch (Exception ignored) {}
    return steps
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
    String contextCode = System.getProperty("odi.context") ?: "GLOBAL"
    String stepsText = System.getProperty("odi.steps") ?: ""
    if (!loadPlanName || !stepsText) throw new Exception("MISSING PARAMS: odi.load_plan.name and odi.steps are required")

    def requestedSteps = []
    stepsText.split(",").each { rawStep ->
        if (!rawStep.trim()) return
        def parts = rawStep.split(":", -1)
        if (parts.length != 3) throw new Exception("Invalid step definition.")
        String stepName = parts[0].trim()
        String scenarioName = parts[1].trim().toUpperCase()
        String version = parts[2].trim()
        if (!ALLOWED_SCENARIOS.contains(scenarioName)) {
            throw new Exception("Scenario is not allowlisted for ODI-25: " + scenarioName)
        }
        requestedSteps << [name: stepName, scenario_name: scenarioName, version: version]
    }
    if (requestedSteps.isEmpty()) throw new Exception("No load-plan steps were provided.")
    addCheck("step_allowlist", "pass", "All requested steps use allowlisted scenarios.", [step_count: requestedSteps.size()])

    ITransactionManager txnMgr = odi.getTransactionManager()
    ITransactionStatus txnStatus = txnMgr.getTransaction(new DefaultTransactionDefinition())

    try {
        def em = odi.getTransactionalEntityManager()
        IOdiContextFinder ctxFinder = (IOdiContextFinder) em.getFinder(OdiContext.class)
        OdiContext context = ctxFinder.findAll().find {
            it.getCode()?.equalsIgnoreCase(contextCode) || it.getName()?.equalsIgnoreCase(contextCode)
        }
        if (context == null) throw new Exception("Context not found: " + contextCode)
        addCheck("context_found", "pass", "Context found.", [context: context.getCode() ?: context.getName()])

        IOdiScenarioFinder scenarioFinder = (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
        def scenarioObjects = []
        requestedSteps.each { step ->
            def scen = scenarioByNameVersion(scenarioFinder, step.scenario_name, step.version)
            if (scen == null) throw new Exception("Scenario not found: " + step.scenario_name + " / " + step.version)
            scenarioObjects << scen
        }
        addCheck("scenarios_found", "pass", "All requested scenarios were found.", [step_count: scenarioObjects.size()])

        IOdiLoadPlanFinder loadPlanFinder = (IOdiLoadPlanFinder) em.getFinder(OdiLoadPlan.class)
        OdiLoadPlan existing = null
        try { existing = loadPlanFinder.findByName(loadPlanName) } catch (Exception ignored) {}
        if (existing != null) {
            def currentSteps = describeLoadPlan(existing)
            boolean sameSize = currentSteps.size() == requestedSteps.size()
            boolean sameSteps = sameSize
            for (int i = 0; i < requestedSteps.size() && sameSteps; i++) {
                sameSteps =
                    currentSteps[i]?.scenario_name?.equalsIgnoreCase(requestedSteps[i].scenario_name) &&
                    currentSteps[i]?.version?.equalsIgnoreCase(requestedSteps[i].version)
            }
            if (!sameSteps) {
                addCheck("existing_load_plan_shape", "fail", "Existing load plan has a different step shape; no destructive recreate was attempted.", [load_plan: loadPlanName, current_step_count: currentSteps.size(), expected_step_count: requestedSteps.size()])
                txnMgr.rollback(txnStatus)
                def payload = [
                    tool: "odi.create_load_plan_metadata",
                    status: "attention_required",
                    mode: "metadata_only_no_run",
                    side_effects: false,
                    will_run_load_plan: false,
                    repository_connection_attempted: true,
                    load_plan: [name: loadPlanName, steps: currentSteps],
                    checks: checks
                ]
                println JsonOutput.toJson(payload)
                return
            }
            addCheck("load_plan_already_ready", "pass", "Existing load plan already has the requested scenario steps.", [load_plan: loadPlanName])
            txnMgr.rollback(txnStatus)
            def payload = [
                tool: "odi.create_load_plan_metadata",
                status: "already_ready",
                mode: "metadata_only_no_run",
                side_effects: false,
                will_run_load_plan: false,
                repository_connection_attempted: true,
                load_plan: [name: loadPlanName, steps: currentSteps],
                checks: checks
            ]
            println JsonOutput.toJson(payload)
            return
        }

        OdiLoadPlan loadPlan = new OdiLoadPlan(loadPlanName)
        em.persist(loadPlan)
        def rootStep = loadPlan.getRootStep()
        for (int i = 0; i < requestedSteps.size(); i++) {
            rootStep.addStepRunScenario(
                requestedSteps[i].name,
                scenarioObjects[i],
                null as OdiLogicalAgent,
                context
            )
        }
        em.persist(loadPlan)
        txnMgr.commit(txnStatus)
        addCheck("load_plan_created", "pass", "Load plan metadata created without running it.", [load_plan: loadPlanName, step_count: requestedSteps.size()])
        addCheck("forbidden_operations_not_called", "pass", "No load plan run, scenario run, target DDL, or data load is used by this script.")

        def payload = [
            tool: "odi.create_load_plan_metadata",
            status: "passed",
            mode: "metadata_only_no_run",
            side_effects: true,
            will_run_load_plan: false,
            repository_connection_attempted: true,
            load_plan: [name: loadPlanName, steps: requestedSteps],
            checks: checks
        ]
        println JsonOutput.toJson(payload)
    } catch (Exception inner) {
        try { txnMgr.rollback(txnStatus) } catch (Exception ignored) {}
        throw inner
    }
} catch (Exception e) {
    addCheck("create_load_plan_metadata_exception", "fail", "ODI load-plan metadata creation failed.")
    def payload = [
        tool: "odi.create_load_plan_metadata",
        status: "error",
        mode: "metadata_only_no_run",
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
