/* Remote-only exact-owned scenario invocation. No fallback and no automatic retry. */
import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.domain.mapping.Mapping
import oracle.odi.domain.project.OdiProject
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.Tag
import oracle.odi.domain.topology.OdiContext
import oracle.odi.runtime.agent.invocation.RemoteRuntimeAgentInvoker
import oracle.odi.runtime.agent.invocation.StartupParams

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

def requireOwnership = { mapping, scenario, String name, String version ->
    def mappingId = mapping?.getInternalId()
    def sourceId = scenario?.getSourceMappingId()
    if (mappingId == null || sourceId == null || mappingId.toString() != sourceId.toString() ||
        scenario.getName() != name || scenario.getVersion() != version) {
        throw new IllegalStateException('ScenarioOwnershipUnavailable')
    }
}

def executeRemote = { Map payload, Map request, Properties props, Closure makeInvoker, Closure checkOwnership ->
    try {
        validateAgent(props)
        def invoker = makeInvoker(props)
        payload.stage = 'health'
        payload.error_code = 'AgentUnavailable'
        payload.agent_contact_attempted = true
        String version = invoker.invokeIsAlive()
        if (!version?.trim()) throw new IllegalStateException('AgentUnavailable')
        payload.stage = 'ownership'
        payload.error_code = 'ScenarioOwnershipUnavailable'
        payload.repository_connection_attempted = true
        checkOwnership(request, props)
        payload.stage = 'run'
        payload.error_code = 'ScenarioOutcomeUnknown'
        payload.status = 'unknown'
        payload.run_may_have_started = true
        payload.side_effects = true
        payload.outcome_unknown = true
        def info = invoker.invokeStartScenario(request.name, request.version, new StartupParams([:]),
            null, request.context, 5, null, true, props.getProperty('work_name'))
        String status = info?.getSessionStatus()?.toString()
        if (status == 'DONE') {
            payload.status = 'passed'
            payload.stage = 'completed'
            payload.error_code = null
            payload.session_status = 'DONE'
            payload.outcome_unknown = false
        } else if (status == 'ERROR') {
            payload.status = 'failed'
            payload.error_code = 'ScenarioRunFailed'
            payload.session_status = 'ERROR'
            payload.outcome_unknown = false
        }
    } catch (Exception ignored) {
        // After invokeStartScenario may have been called, errors are unknown outcomes.
    }
    return payload
}

Map payload = [tool: 'odi.remote_scenario_run', status: 'blocked', stage: 'validation',
    error_code: 'InvalidRunIdentity', agent_contact_attempted: false,
    repository_connection_attempted: false, run_may_have_started: false, side_effects: false,
    outcome_unknown: false, session_status: null,
    raw_rows_included: false, pk_values_included: false, secret_printed: false]
try {
    Map request = [name: System.getProperty('odi.scenario.name'), version: System.getProperty('odi.scenario.version'),
        context: System.getProperty('odi.context'), mapping: System.getProperty('odi.mapping'),
        project: System.getProperty('odi.project'), folder: System.getProperty('odi.folder')]
    if (!(request.name ==~ /[A-Z][A-Z0-9_]{0,127}/) || !(request.mapping ==~ /[A-Z][A-Z0-9_]{0,127}/) ||
        !(request.context ==~ /[A-Z][A-Z0-9_]{0,127}/) || !(request.version ==~ /[A-Za-z0-9_.-]{1,12}/) ||
        !request.project || !request.folder) throw new IllegalArgumentException('InvalidRunIdentity')
    payload.stage = 'configuration'
    payload.error_code = 'AgentConfigurationUnavailable'
    Properties props = new Properties()
    new FileInputStream(args[0]).withStream { props.load(it) }
    for (String key : ['master_jdbc', 'master_jdbc_driver', 'master_username', 'master_password',
                       'odi_username', 'odi_password', 'work_name']) {
        if (!props.getProperty(key)) throw new IllegalArgumentException('AgentConfigurationUnavailable')
    }
    Closure checkOwnership = { Map spec, Properties configuration ->
        def odi = null
        try {
            def pool = new PoolingAttributes()
            def master = new MasterRepositoryDbInfo(configuration.getProperty('master_jdbc'),
                configuration.getProperty('master_jdbc_driver'), configuration.getProperty('master_username'),
                configuration.getProperty('master_password').toCharArray(), pool)
            def work = new WorkRepositoryDbInfo(configuration.getProperty('work_name'), pool)
            odi = OdiInstance.createInstance(new OdiInstanceConfig(master, work))
            def auth = odi.getSecurityManager().createAuthentication(configuration.getProperty('odi_username'),
                configuration.getProperty('odi_password').toCharArray())
            odi.getSecurityManager().setCurrentThreadAuthentication(auth)
            def em = odi.getTransactionalEntityManager()
            def project = em.getFinder(OdiProject.class).findByCode(spec.project)
            def folders = project?.getFolders()?.findAll { it.getName() == spec.folder } ?: []
            def mapping = folders.size() == 1 ? em.getFinder(Mapping.class).findByName(folders[0], spec.mapping) : null
            def scenario = em.getFinder(OdiScenario.class).findByTag(new Tag(spec.name, spec.version))
            if (em.getFinder(OdiContext.class).findByCode(spec.context) == null) {
                throw new IllegalStateException('ScenarioOwnershipUnavailable')
            }
            requireOwnership(mapping, scenario, spec.name, spec.version)
        } finally {
            if (odi != null) {
                try { odi.close() } catch (Exception ignored) {
                    payload.error_code = 'RepositoryCleanupFailed'
                    throw new IllegalStateException('RepositoryCleanupFailed')
                }
            }
        }
    }
    executeRemote(payload, request, props,
        { configuration -> new RemoteRuntimeAgentInvoker(configuration.getProperty('agent_url'),
            configuration.getProperty('agent_username') ?: configuration.getProperty('odi_username'),
            (configuration.getProperty('agent_password') ?: configuration.getProperty('odi_password')).toCharArray()) },
        checkOwnership)
} catch (Exception ignored) {
    // Bootstrap/validation failures are fixed status-only codes as well.
}
println JsonOutput.toJson(payload)
