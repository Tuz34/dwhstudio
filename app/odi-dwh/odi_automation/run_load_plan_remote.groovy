/*
  NL-DWH Studio: controlled ODI load-plan run smoke.
  Starts exactly one allowlisted load plan through RemoteRuntimeAgentInvoker.
*/

import groovy.json.JsonOutput
import oracle.odi.runtime.agent.invocation.LoadPlanStartupParams
import oracle.odi.runtime.agent.invocation.RemoteRuntimeAgentInvoker

Set ALLOWED_LOAD_PLANS = [
    "LP_HR_SH_ODS_LOAD"
] as Set
Set RUNNING_STATUS_CODES = ["RUNNING", "WAITING", "R", "W"] as Set
Set DONE_STATUS_CODES = ["DONE", "D"] as Set
int POLL_INTERVAL_MS = Integer.parseInt(
    System.getProperty("odi.loadplan.poll.ms") ?: System.getenv("ODI_LOAD_PLAN_POLL_MS") ?: "5000"
)

def checks = []

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
    msg = msg.replaceAll(/https?:\/\/[^\s,)]+/, "agent:***")
    return msg.take(240)
}

try {
    String propsPath = args?.length ? args[0] : "odi_connection.properties"
    Properties props = new Properties()
    new FileInputStream(propsPath).withStream { props.load(it) }

    String loadPlanName = (System.getProperty("odi.load_plan.name") ?: "").trim().toUpperCase()
    String contextCode = (System.getProperty("odi.context") ?: "GLOBAL").trim().toUpperCase()
    String agentUrl = props.getProperty("agent_url") ?: ""
    String agentUser = props.getProperty("agent_username") ?: props.getProperty("odi_username") ?: ""
    String agentPassword = props.getProperty("agent_password") ?: props.getProperty("odi_password") ?: ""
    String workName = props.getProperty("work_name") ?: ""

    if (!ALLOWED_LOAD_PLANS.contains(loadPlanName)) {
        throw new Exception("Load plan is not allowlisted for ODI-26.")
    }
    addCheck("load_plan_allowlist", "pass", "Load plan is allowlisted.", [load_plan: loadPlanName])

    if (!agentUrl || !agentUser || !agentPassword || !workName) {
        addCheck("agent_config_present", "fail", "Remote agent config is incomplete.")
        def payload = [
            tool: "odi.run_load_plan_safe",
            status: "blocked_until_agent_config",
            mode: "remote_agent_load_plan_run",
            side_effects: false,
            will_run_load_plan: false,
            repository_connection_attempted: false,
            load_plan: [name: loadPlanName, context: contextCode],
            checks: checks
        ]
        println JsonOutput.toJson(payload)
        return
    }
    addCheck("agent_config_present", "pass", "Remote agent config is present.")

    RemoteRuntimeAgentInvoker invoker = new RemoteRuntimeAgentInvoker(agentUrl, agentUser, agentPassword.toCharArray())
    try {
        invoker.invokeIsAlive()
        addCheck("agent_alive", "pass", "Remote agent responded.")
    } catch (Exception aliveError) {
        addCheck("agent_alive", "fail", "Remote agent did not respond.", [error: safeMessage(aliveError)])
        throw aliveError
    }

    LoadPlanStartupParams startupParams = new LoadPlanStartupParams([:])
    def executionInfo = invoker.invokeStartLoadPlan(loadPlanName, contextCode, startupParams, null, workName, 5)
    Long instanceId = executionInfo?.getLoadPlanInstanceId()
    Long runCount = executionInfo?.getRunCount()
    addCheck("load_plan_started", "pass", "Load plan start request returned execution info.", [has_instance: instanceId != null, has_run_count: runCount != null])

    String finalStatus = null
    String finalStatusCode = null
    String finalMessage = null
    for (int i = 0; i < 48; i++) {
        def statuses = invoker.invokeGetLoadPlanStatus([instanceId], [runCount], workName)
        def statusInfo = statuses ? statuses[0] : null
        finalStatus = statusInfo?.getLoadPlanRunStatus()?.toString()
        finalStatusCode = finalStatus?.trim()?.toUpperCase()
        finalMessage = statusInfo?.getLoadPlanRunMessage()
        if (finalStatusCode != null && !RUNNING_STATUS_CODES.contains(finalStatusCode)) {
            break
        }
        Thread.sleep(POLL_INTERVAL_MS)
    }

    boolean passed = finalStatusCode != null && DONE_STATUS_CODES.contains(finalStatusCode)
    if (passed) {
        addCheck("load_plan_completed", "pass", "Load plan finished with DONE status.")
    } else {
        addCheck("load_plan_completed", "fail", "Load plan did not finish with DONE status.", [status: finalStatus ?: "UNKNOWN"])
    }

    def payload = [
        tool: "odi.run_load_plan_safe",
        status: passed ? "passed" : "failed",
        mode: "remote_agent_load_plan_run",
        side_effects: true,
        will_run_load_plan: true,
        repository_connection_attempted: true,
        load_plan: [name: loadPlanName, context: contextCode],
        session: [id: instanceId != null ? String.valueOf(instanceId) : null, run_count: runCount != null ? String.valueOf(runCount) : null, status: finalStatus],
        error_class: passed ? null : safeMessage(new Exception(finalMessage ?: "Load plan did not finish with DONE status.")),
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("run_load_plan_exception", "fail", "ODI load-plan run failed before completion.")
    def payload = [
        tool: "odi.run_load_plan_safe",
        status: "failed",
        mode: "remote_agent_load_plan_run",
        side_effects: true,
        will_run_load_plan: true,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_class: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
}
