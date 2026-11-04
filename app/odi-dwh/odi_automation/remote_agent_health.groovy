/* Configuration and isAlive only. No repository connection or scenario start. */
import groovy.json.JsonOutput
import oracle.odi.runtime.agent.invocation.RemoteRuntimeAgentInvoker

def validateAgent = { Properties props ->
    String url = props.getProperty('agent_url') ?: ''
    URI endpoint = url ? new URI(url) : null
    String user = props.getProperty('agent_username') ?: props.getProperty('odi_username') ?: ''
    String password = props.getProperty('agent_password') ?: props.getProperty('odi_password') ?: ''
    if (!endpoint || !(endpoint.scheme in ['http', 'https']) || !endpoint.host || endpoint.port != 15101 ||
        endpoint.rawPath != '/oraclediagent' || endpoint.rawQuery != null || endpoint.rawFragment != null ||
        endpoint.rawUserInfo != null || !user || !password || !props.getProperty('work_name')) {
        throw new IllegalArgumentException('AgentConfigurationUnavailable')
    }
    return true
}

Map payload = [tool: 'odi.remote_agent_health', status: 'blocked', stage: 'configuration',
    error_code: 'AgentConfigurationUnavailable', agent_contact_attempted: false,
    repository_connection_attempted: false, run_may_have_started: false, side_effects: false,
    outcome_unknown: false, session_status: null,
    raw_rows_included: false, pk_values_included: false, secret_printed: false]
try {
    Properties props = new Properties()
    new FileInputStream(args[0]).withStream { props.load(it) }
    validateAgent(props)
    def invoker = new RemoteRuntimeAgentInvoker(props.getProperty('agent_url'),
        props.getProperty('agent_username') ?: props.getProperty('odi_username'),
        (props.getProperty('agent_password') ?: props.getProperty('odi_password')).toCharArray())
    payload.stage = 'health'
    payload.error_code = 'AgentUnavailable'
    payload.agent_contact_attempted = true
    String version = invoker.invokeIsAlive()
    if (!version?.trim()) throw new IllegalStateException('AgentUnavailable')
    payload.status = 'passed'
    payload.stage = 'completed'
    payload.error_code = null
} catch (Exception ignored) {
    // Only the fixed stage code is emitted; exceptions may contain connection details.
}
println JsonOutput.toJson(payload)
