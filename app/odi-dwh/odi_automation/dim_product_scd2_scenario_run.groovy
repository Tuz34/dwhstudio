/* Fixed ODI-68 DIM_PRODUCT scenario run through OracleDIAgent. */

import groovy.json.JsonOutput
import oracle.odi.runtime.agent.invocation.RemoteRuntimeAgentInvoker
import oracle.odi.runtime.agent.invocation.StartupParams

final String SCENARIO_NAME = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN"
final String SCENARIO_VERSION = "001"
final String CONTEXT_CODE = "GLOBAL"
final String AGENT_NAME = "OracleDIAgent"
final int AGENT_PORT = 15101

def fixedScenario = [name: SCENARIO_NAME, version: SCENARIO_VERSION, context: CONTEXT_CODE]
boolean agentAttempted = false
boolean runAttempted = false
String errorType = "AgentConfigurationUnavailable"

try {
    String propsPath = args?.length ? args[0] : "odi_connection.properties"
    Properties props = new Properties()
    new FileInputStream(propsPath).withStream { props.load(it) }

    String agentUrl = props.getProperty("agent_url") ?: ""
    String agentUser = props.getProperty("agent_username") ?: props.getProperty("odi_username") ?: ""
    String agentPassword = props.getProperty("agent_password") ?: props.getProperty("odi_password") ?: ""
    String workName = props.getProperty("work_name") ?: ""
    URI endpoint = agentUrl ? new URI(agentUrl) : null
    if (
        !endpoint || endpoint.port != AGENT_PORT ||
        !(endpoint.path ?: "").toLowerCase().contains(AGENT_NAME.toLowerCase()) ||
        !agentUser || !agentPassword || !workName
    ) {
        throw new IllegalArgumentException("Fixed ODI agent configuration unavailable")
    }

    RemoteRuntimeAgentInvoker invoker = new RemoteRuntimeAgentInvoker(
        agentUrl, agentUser, agentPassword.toCharArray()
    )
    errorType = "AgentUnavailable"
    agentAttempted = true
    invoker.invokeIsAlive()

    errorType = "ScenarioRunFailed"
    runAttempted = true
    StartupParams startupParams = new StartupParams([:])
    def executionInfo = invoker.invokeStartScenario(
        SCENARIO_NAME, SCENARIO_VERSION, startupParams,
        null, CONTEXT_CODE, 5, null, true, workName
    )
    String sessionStatus = executionInfo?.getSessionStatus()?.toString()?.toUpperCase()
    if (sessionStatus != "DONE") {
        throw new IllegalStateException("Scenario did not finish with DONE status")
    }

    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_scenario_run",
        status: "passed",
        mode: "fixed_scd2_scenario_run",
        side_effects: true,
        repository_connection_attempted: true,
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
        scenario: fixedScenario,
        session_status: "DONE"
    ])
} catch (Exception ignored) {
    println JsonOutput.toJson([
        tool: "odi.dim_product_scd2_scenario_run",
        status: "error",
        mode: "fixed_scd2_scenario_run",
        side_effects: runAttempted,
        repository_connection_attempted: agentAttempted,
        raw_rows_included: false,
        pk_values_included: false,
        secret_printed: false,
        scenario: fixedScenario,
        error_type: errorType
    ])
}
