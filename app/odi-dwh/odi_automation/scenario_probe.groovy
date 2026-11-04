/*
  NL-DWH Studio: ODI scenario read-only metadata probe.
  This script only reads scenario metadata and returns redacted JSON.
*/

import groovy.json.JsonOutput
import oracle.odi.core.OdiInstance
import oracle.odi.core.config.MasterRepositoryDbInfo
import oracle.odi.core.config.OdiInstanceConfig
import oracle.odi.core.config.PoolingAttributes
import oracle.odi.core.config.WorkRepositoryDbInfo
import oracle.odi.core.security.Authentication
import oracle.odi.domain.runtime.scenario.OdiScenario
import oracle.odi.domain.runtime.scenario.finder.IOdiScenarioFinder

def checks = []
def odi = null
boolean standaloneMode = false

def safeMessage = { Throwable t ->
    String msg = t?.getMessage() ?: t?.toString() ?: ""
    msg = msg.replaceAll(/(?i)(password|passwd|pwd|token|secret)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    msg = msg.replaceAll(/jdbc:[^\s,)]+/, "jdbc:***")
    msg = msg.replaceAll(/(?i)(user(name)?|uid)\s*[=:]\s*[^\s;,)]+/, "\$1=***")
    return msg.take(240)
}

def addCheck = { String name, String status, String detail, Map metadata = [:] ->
    def check = [name: name, status: status, detail: detail]
    if (!metadata.isEmpty()) {
        check.metadata = metadata
    }
    checks << check
}

def scenarioByNameVersion = { IOdiScenarioFinder finder, String scenarioName, String version ->
    try {
        def latest = finder.findLatestByName(scenarioName)
        if (latest != null && latest.getVersion()?.equalsIgnoreCase(version)) {
            return latest
        }
        return null
    } catch (Throwable ignored) {
        return null
    }
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

    String scenarioName = System.getProperty("odi.scenario.name")
    String scenarioVersion = System.getProperty("odi.scenario.version", "001")
    def em = odi.getTransactionalEntityManager()

    IOdiScenarioFinder scenFinder = (IOdiScenarioFinder) em.getFinder(OdiScenario.class)
    def exact = scenarioByNameVersion(scenFinder, scenarioName, scenarioVersion)
    def latest = scenFinder.findLatestByName(scenarioName)

    addCheck(
        "scenario_found",
        exact != null ? "pass" : "fail",
        exact != null ? "Requested scenario version found." : "Requested scenario version not found.",
        [scenario: scenarioName, version: scenarioVersion, latest_version: latest?.getVersion()]
    )
    addCheck(
        "forbidden_operations_not_called",
        "pass",
        "No persist/remove/commit/create/update, target DDL, data load, scenario start, or load plan start is used by this script."
    )

    def payload = [
        tool: "odi.scenario_probe",
        status: exact != null ? "passed" : "attention_required",
        mode: "scenario_finders_only",
        side_effects: false,
        repository_connection_attempted: true,
        scenario: [name: scenarioName, version: scenarioVersion, latest_version: latest?.getVersion()],
        checks: checks
    ]
    println JsonOutput.toJson(payload)
} catch (Exception e) {
    addCheck("scenario_probe_exception", "fail", "ODI scenario probe failed before completion.")
    def payload = [
        tool: "odi.scenario_probe",
        status: "error",
        mode: "scenario_finders_only",
        side_effects: false,
        repository_connection_attempted: true,
        checks: checks,
        error: e.getClass().getSimpleName(),
        error_detail: safeMessage(e)
    ]
    println JsonOutput.toJson(payload)
} finally {
    if (standaloneMode && odi != null) {
        odi.close()
    }
}
