import json
import logging
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

import remote_run_contract
import repository_write_contract as write_contract
from config import (
    GROOVY_JAR_NAME,
    JAVA_FALLBACK_PATHS,
    JAVA_HOME,
    ODI_APPROVED_IKMS,
    ODI_FOLDER,
    ODI_HOME,
    ODI_IKM_PRIORITY,
    ODI_LKM_PRIORITY,
    ODI_MODE,
    ODI_PROJECT,
    ODI_REST_PASSWORD,
    ODI_REST_URL,
    ODI_REST_USER,
    OJDBC_JAR_NAME,
    SOURCE_SYSTEM_NAME,
    TABLE_NAME_MAX_LEN,
)
from ikm_policy import normalize_repository_preflight, resolve_approved_ikm, verified_source_identity

logger = logging.getLogger("odi_orchestrator")

@dataclass(frozen=True)
class GroovyActionSpec:
    category: str
    approval_required: bool
    failure_contract: str = "fixed_status_action_error_type"
    output_policy: str = "central_redaction_before_log_parse_return"
    generic_property_transport: bool = True


def _action(category: str, *, generic_property_transport: bool = True) -> GroovyActionSpec:
    return GroovyActionSpec(
        category=category,
        approval_required=category != "read_only",
        generic_property_transport=generic_property_transport,
    )


GROOVY_ACTION_REGISTRY = {
    "add_column.groovy": _action("repository_write"),
    "create_load_plan_metadata.groovy": _action("repository_write"),
    "create_mapping.groovy": _action("repository_write"),
    "create_mapping_smoke.groovy": _action("repository_write"),
    "delete_datastore.groovy": _action("repository_write"),
    "delete_mapping.groovy": _action("repository_write"),
    "diagnose_ikm.groovy": _action("read_only"),
    "execution_identity_probe.groovy": _action("read_only"),
    "dim_product_audit_role_repair.groovy": _action("repository_write"),
    "dim_product_scd2_capability_probe.groovy": _action("read_only"),
    "dim_product_scd2_datastore_register.groovy": _action("repository_write"),
    "dim_product_scd2_ikm_import.groovy": _action("repository_write"),
    "dim_product_scd2_mapping_create.groovy": _action("repository_write"),
    "dim_product_scd2_mapping_probe.groovy": _action("read_only"),
    "dim_product_scd2_scenario_run.groovy": _action("scenario_run"),
    "fix_join_type.groovy": _action("repository_write"),
    "fix_skey_ikm.groovy": _action("repository_write"),
    "generate_scenario_metadata.groovy": _action("repository_write"),
    "inspect_mapping_km_options.groovy": _action("read_only"),
    "inspect_topology.groovy": _action("read_only"),
    "list_projects.groovy": _action("read_only"),
    "load_plan_probe.groovy": _action("read_only"),
    "read_only_probe.groovy": _action("read_only"),
    "mapping_preflight.groovy": _action("read_only"),
    "register_datastore.groovy": _action("repository_write"),
    "remove_column.groovy": _action("repository_write"),
    "repair_datastore_metadata.groovy": _action("repository_write"),
    "repair_mapping_components.groovy": _action("repository_write"),
    "run_load_plan_remote.groovy": _action("load_plan_run"),
    "run_scenario.groovy": _action("scenario_run"),
    "remote_agent_health.groovy": _action("read_only"),
    "run_scenario_remote.groovy": _action("scenario_run"),
    "scd2_datastore_register.groovy": _action("repository_write"),
    "scd2_mapping_create.groovy": _action("repository_write"),
    "scd2_probe.groovy": _action("read_only"),
    "scenario_probe.groovy": _action("read_only"),
    "seed_model_datastore.groovy": _action("repository_write"),
    "seed_physical_topology.groovy": _action("topology_write"),
    "seed_project_folder.groovy": _action("repository_write"),
    "setup_topology.groovy": _action("topology_write"),
    "sync_datastore_column.groovy": _action("repository_write"),
    "update_mapping.groovy": _action("repository_write"),
    "update_topology_pass.groovy": _action(
        "credential_change", generic_property_transport=False
    ),
}

# K5: Registry-backed whitelist prevents path traversal and unclassified execution.
_ALLOWED_SCRIPTS = frozenset(GROOVY_ACTION_REGISTRY)
_GROOVY_APPROVED: ContextVar[bool] = ContextVar("groovy_approved", default=False)
_SECRET_PARAM_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|credential|api[_-]?key|access[_-]?key|jdbc|dsn|connection)"
)
_ODI24_ALLOWED_SCENARIOS = {
    "MAP_HR_EMPLOYEES_TO_ODS_SCN",
    "MAP_SH_SALES_TO_ODS_SCN",
}
_PRODUCTS_ODS_SCENARIO = "MAP_SH_PRODUCTS_TO_ODS_SCN"
_PRODUCTS_ODS_SCENARIO_VERSION = "001"
_PRODUCTS_ODS_SCENARIO_CONTEXT = "GLOBAL"
_PRODUCTS_ODS_RUN_SCOPE = "odi68_products_ods_scenario_run_and_rerun_proof"
_DIM_PRODUCT_SCD2_SCENARIO = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT_SCN"
_DIM_PRODUCT_SCD2_SCENARIO_VERSION = "001"
_DIM_PRODUCT_SCD2_SCENARIO_CONTEXT = "GLOBAL"
_DIM_PRODUCT_SCD2_RUN_SCOPE = "odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof"
_DIM_PRODUCT_SCD2_RUN_ERROR_TYPES = frozenset(
    {"AgentConfigurationUnavailable", "AgentUnavailable", "ScenarioRunFailed"}
)
_ODI25_DEFAULT_LOAD_PLAN_STEPS = [
    {"name": "Run HR Employees", "scenario_name": "MAP_HR_EMPLOYEES_TO_ODS_SCN", "version": "V44A"},
    {"name": "Run SH Sales", "scenario_name": "MAP_SH_SALES_TO_ODS_SCN", "version": "V46B"},
]
_ODI26_ALLOWED_LOAD_PLANS = {"LP_HR_SH_ODS_LOAD"}

# K5: Allowed param key/value patterns (alphanumeric, dots, dashes, underscores, slashes, spaces, hashtag)
_PARAM_KEY_RE   = re.compile(r'^[a-zA-Z0-9._-]+$')
_PARAM_VAL_RE   = re.compile(r'^[a-zA-Z0-9._\-/\\ :,()#]+$')
# Extended regex for expression values (=, ;, ', ||, CASE WHEN, LIKE '%X%', !=, &, [], etc.)
_EXPR_VAL_RE    = re.compile(r"^[a-zA-Z0-9._\-/\\ :,()#=;'|*+<>%!&\[\]{}\"]+$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$]{0,127}$")
_SCD2_ERROR_KEYS = frozenset(
    {
        "tool",
        "status",
        "mode",
        "side_effects",
        "repository_connection_attempted",
        "raw_rows_included",
        "pk_values_included",
        "secret_printed",
        "error_type",
    }
)
_CREATE_MAPPING_ERROR_KEYS = frozenset(
    {
        "status",
        "action",
        "replaced",
        "kept_existing_on_failure",
        "error_type",
        "validation_messages",
        "physical_design",
    }
)
_STRICT_STRUCTURED_ERROR_CONTRACTS = {
    "generate_scenario_metadata.groovy": {
        "tool": "odi.generate_scenario_metadata",
        "mode": "scenario_metadata_generation",
        "keys": (write_contract.FAILURE_KEYS - write_contract.SAFETY_KEYS) | {"checks"},
    },
    "scd2_probe.groovy": {
        "tool": "odi.scd2_probe",
        "mode": "generic_scd2_readiness_probe",
        "keys": _SCD2_ERROR_KEYS,
    },
    "scd2_datastore_register.groovy": {
        "tool": "odi.scd2_datastore_register",
        "mode": "generic_absent_only_scd2_datastore_registration",
        "keys": write_contract.FAILURE_KEYS,
    },
    "scd2_mapping_create.groovy": {
        "tool": "odi.scd2_mapping_create",
        "mode": "generic_absent_only_scd2_mapping_creation",
        "keys": write_contract.FAILURE_KEYS,
    },
    "dim_product_scd2_datastore_register.groovy": {
        "tool": "odi.dim_product_scd2_datastore_registration",
        "mode": "fixed_absent_only_odi_datastore_registration",
        "keys": write_contract.FAILURE_KEYS,
    },
    "dim_product_scd2_scenario_run.groovy": {
        "tool": "odi.dim_product_scd2_scenario_run",
        "mode": "fixed_scd2_scenario_run",
        "keys": frozenset(
            {
                "tool",
                "status",
                "mode",
                "side_effects",
                "repository_connection_attempted",
                "raw_rows_included",
                "pk_values_included",
                "secret_printed",
                "scenario",
                "error_type",
            }
        ),
    },
    "dim_product_scd2_ikm_import.groovy": {
        "tool": "odi.dim_product_scd2_ikm_import",
        "mode": "fixed_sdk_reference_absent_only_ikm_import",
        "keys": frozenset(
            {
                "tool",
                "status",
                "mode",
                "side_effects",
                "repository_connection_attempted",
                "raw_rows_included",
                "pk_values_included",
                "secret_printed",
                "error_type",
                "import_attempted",
                "repository_state_may_have_changed",
            }
        ),
    },
    "dim_product_scd2_mapping_create.groovy": {
        "tool": "odi.dim_product_scd2_mapping_create",
        "mode": "fixed_absent_only_scd2_mapping_creation",
        "keys": frozenset(
            {
                "tool",
                "status",
                "mode",
                "side_effects",
                "repository_connection_attempted",
                "raw_rows_included",
                "pk_values_included",
                "secret_printed",
                "error_type",
            }
        ),
    },
}


def _redact_runtime_text(text: str) -> str:
    redacted = str(text or "")
    redacted = re.sub(
        r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)\s*[=:]\s*[^\s;,)]+",
        r"\1=***",
        redacted,
    )
    redacted = re.sub(r"(?i)(authorization\s*:\s*bearer)\s+[^\s,;]+", r"\1 ***", redacted)
    redacted = re.sub(r"jdbc:[^\s,)]+", "jdbc:***", redacted)
    redacted = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s,;)]+", "connection://***", redacted)
    redacted = re.sub(r"(?i)(dsn|connection[_-]?string)\s*[=:]\s*[^\s,)]+", r"\1=***", redacted)
    redacted = re.sub(r"(?i)(user(name)?|uid)\s*[=:]\s*[^\s;,)]+", r"\1=***", redacted)
    redacted = re.sub(r"(?i)[A-Z0-9_]+/[^@\s]+@[^)\s]+", "***@***", redacted)
    return redacted


@contextmanager
def groovy_approval_context(approved: bool):
    """Bind strict request-scoped approval for nested Groovy actions."""
    token = _GROOVY_APPROVED.set(approved is True)
    try:
        yield
    finally:
        _GROOVY_APPROVED.reset(token)


def groovy_is_approved() -> bool:
    """Read the existing request approval without widening its scope."""
    return _GROOVY_APPROVED.get() is True


_JMX_NOISE_START_RE = re.compile(r"(?i)^SEVERE:\s+Error initializing the JMX\s+FRAMEWORK\s+SPI\.?$")
_JMX_NOISE_DETAIL_RE = re.compile(
    r"^\s+(?:at\s+[\w.$/]+\([^\r\n]*\)|\.\.\.\s+\d+\s+more)\s*$"
)
_LOG_HEADER_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4} .*\b(AM|PM)\b\s+\S+")
_JMX_MISSING_CLASS_ERROR_RE = re.compile(
    r"^(?:(?:Caused by:|ERROR:)\s*)?java\.lang\.(?:NoClassDefFoundError|ClassNotFoundException):\s*"
    r"javax[./]management[./]j2ee(?:[./][A-Za-z_$][\w$]*)+\s*$"
)
_JMX_OTHER_EXCEPTION_RE = re.compile(r"^\s*(?:Caused by:|Suppressed:|[\w.$]+(?:Exception|Error)\b)")
_SEVERE_LINE_RE = re.compile(r"(?i)^SEVERE\b")


def _strip_known_jmx_noise(text: str) -> str:
    """Drop only the known JMX framework SPI missing-class SEVERE block from Java output.

    The block is the log header, the SEVERE line naming the JMX framework SPI and
    its stack trace, and it is dropped only when that trace is a
    NoClassDefFoundError/ClassNotFoundException for javax.management.j2ee classes.
    Any other JMX failure, and every other line including any other SEVERE line,
    is kept so it can still classify the output.
    """
    lines = str(text or "").splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if _JMX_NOISE_START_RE.match(stripped):
            end = index + 1
            missing_class = end < len(lines) and _JMX_MISSING_CLASS_ERROR_RE.fullmatch(lines[end].strip())
            if missing_class:
                end += 1
                while end < len(lines) and (
                    _JMX_NOISE_DETAIL_RE.fullmatch(lines[end])
                    or _JMX_MISSING_CLASS_ERROR_RE.fullmatch(lines[end].strip())
                ):
                    end += 1
            other_cause = end < len(lines) and _JMX_OTHER_EXCEPTION_RE.match(lines[end])
            if missing_class and not other_cause:
                if kept and _LOG_HEADER_RE.match(kept[-1].strip()):
                    kept.pop()
                index = end
                continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept)


def _groovy_output_failed(text: str) -> bool:
    output = _strip_known_jmx_noise(text).strip()
    if not output:
        return False
    upper = output.upper()
    if "FATAL ERROR" in upper or "LKM VERIFY FAILED" in upper:
        return True
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(("ERROR:", "HATA:", "FATAL:")):
            return True
        if _SEVERE_LINE_RE.match(stripped):
            return True
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("status", "")).lower() in {
            "error",
            "fail",
            "failed",
        }:
            return True
    return False


def _create_mapping_failure_envelope(stdout: object) -> str | None:
    """Rebuild the create_mapping failure line from allowlisted fields, or return None.

    The script prints this line last and then exits 1. Free text is validated by type
    only and never returned, so raw stdout can be parsed without being forwarded.
    """
    lines = str(stdout or "").strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != _CREATE_MAPPING_ERROR_KEYS
        or payload["status"] != "error"
        or payload["action"] != "create_mapping"
        or payload["replaced"] is not False
        or type(payload["kept_existing_on_failure"]) is not bool
        or not isinstance(payload["error_type"], str)
        or not _ERROR_TYPE_RE.fullmatch(payload["error_type"])
        or not isinstance(payload["validation_messages"], list)
        or not isinstance(payload["physical_design"], dict)
    ):
        return None
    envelope = {
        "status": "error",
        "action": "create_mapping",
        "replaced": False,
        "kept_existing_on_failure": payload["kept_existing_on_failure"],
        "error_type": payload["error_type"],
    }
    marker = f"Hata: ODI Groovy action failed. error_type={payload['error_type']}"
    return "\n".join((marker, json.dumps(envelope)))


def groovy_sync_failure_reason(output: str) -> str | None:
    """Return a credential-safe reason when a datastore sync did not succeed."""
    if not isinstance(output, str):
        return "ODI Groovy returned an invalid result."
    if not _groovy_output_failed(output):
        # This fixed script emits the acknowledgement only after commit returns.
        acknowledgements = sum(
            line.strip() == "SUCCESS: ODI datastore metadata sync committed"
            for line in _strip_known_jmx_noise(output).splitlines()
        )
        if acknowledgements != 1:
            return "ODI Groovy did not confirm the metadata sync commit."
        return None
    error_type = re.search(r"\berror_type=([A-Za-z][A-Za-z0-9_$]{0,127})\b", output)
    if error_type:
        return f"ODI Groovy action failed ({error_type.group(1)})."
    return "ODI Groovy output reported an error."

def _strict_structured_error_output(script_name: str, text: str) -> bool:
    """Allow only reviewed fixed error envelopes to reach strict normalizers."""
    contract = _STRICT_STRUCTURED_ERROR_CONTRACTS.get(script_name)
    if contract is None:
        return False
    try:
        payload = write_contract.parse_json(text)
    except (TypeError, ValueError):
        return False
    if script_name == "generate_scenario_metadata.groovy":
        return write_contract.failure(payload, contract["tool"], contract["mode"], generator=True) is not None
    if script_name in {"scd2_datastore_register.groovy", "scd2_mapping_create.groovy",
                       "dim_product_scd2_mapping_create.groovy", "dim_product_scd2_datastore_register.groovy"}:
        return write_contract.failure(payload, contract["tool"], contract["mode"]) is not None
    if (
        not isinstance(payload, dict)
        or set(payload) != contract["keys"]
        or payload.get("tool") != contract["tool"]
        or payload.get("status") != "error"
        or payload.get("mode") != contract["mode"]
        or (
            payload.get("repository_connection_attempted") is not True
            and script_name != "dim_product_scd2_scenario_run.groovy"
        )
        or payload.get("raw_rows_included") is not False
        or payload.get("pk_values_included") is not False
        or payload.get("secret_printed") is not False
        or not isinstance(payload.get("error_type"), str)
        or not _ERROR_TYPE_RE.fullmatch(payload["error_type"])
        or type(payload.get("side_effects")) is not bool
    ):
        return False
    if script_name == "dim_product_scd2_scenario_run.groovy":
        scenario = {
            "name": _DIM_PRODUCT_SCD2_SCENARIO,
            "version": _DIM_PRODUCT_SCD2_SCENARIO_VERSION,
            "context": _DIM_PRODUCT_SCD2_SCENARIO_CONTEXT,
        }
        attempted = payload["side_effects"]
        connected = payload.get("repository_connection_attempted")
        return (
            type(connected) is bool
            and payload.get("scenario") == scenario
            and type(payload.get("scenario")) is dict
            and payload["error_type"] in _DIM_PRODUCT_SCD2_RUN_ERROR_TYPES
            and (not attempted or connected)
            and (
                payload["error_type"] != "AgentConfigurationUnavailable"
                or (not attempted and not connected)
            )
        )
    if script_name == "dim_product_scd2_ikm_import.groovy":
        attempted = payload.get("import_attempted")
        state_changed = payload.get("repository_state_may_have_changed")
        return (
            type(attempted) is bool
            and type(state_changed) is bool
            and attempted is state_changed
            and payload["side_effects"] is state_changed
        )
    return payload["side_effects"] is False


# ODI runtime paths come from config.py.
_ODI_MW_HOME = ODI_HOME or r"C:\Oracle\Middleware\Oracle_Home"
_JAVA_HOME   = JAVA_HOME


def _resolve_java_bin(java_home: str | None) -> str:
    candidates = []
    if java_home:
        candidates.append(os.path.join(java_home, "bin", "java.exe"))
        candidates.append(os.path.join(java_home, "jdk1.8.0_211", "bin", "java.exe"))
        candidates.append(os.path.join(java_home, "jre1.8.0_211", "bin", "java.exe"))
    path_java = shutil.which("java")
    if path_java:
        candidates.append(path_java)
    candidates.extend(JAVA_FALLBACK_PATHS)
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return os.path.join(java_home or "", "bin", "java.exe")


_JAVA_BIN    = _resolve_java_bin(_JAVA_HOME)

# Classpath components — JAR names come from config (version-independent)
_ODI_SDK_LIB   = os.path.join(_ODI_MW_HOME, "odi", "sdk", "lib")
_OJDBC_JAR     = os.path.join(_ODI_MW_HOME, "oracle_common", "modules", "oracle.jdbc", OJDBC_JAR_NAME)
_GROOVY_JAR    = os.path.join(_ODI_MW_HOME, "oracle_common", "modules", "thirdparty", GROOVY_JAR_NAME)
# Provides javax.management.j2ee.statistics.*; without it the ODI SDK logs a JMX SEVERE trace.
_J2EE_JAR      = os.path.join(_ODI_MW_HOME, "oracle_common", "modules", "javax.management.j2ee.jar")
_PROPS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odi_automation", "odi_connection.properties")
_LOCAL_PROPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odi_automation", "odi_connection.local.properties")
_PROPS_PLACEHOLDER = "<set-in-local-generated-file>"


def _ensure_local_props_file():
    """Creates a git-ignored ODI connection properties file with actual values from
    `.env`/environment variables and returns its path. Returns None if values are missing.
    Secret values are not logged; the file stays local (template placeholder is preserved)."""
    dsn  = os.getenv("ODI_REPO_DSN") or ""
    m_us = os.getenv("ODI_REPO_USER") or ""
    m_pw = os.getenv("ODI_REPO_PASSWORD") or ""
    work = os.getenv("ODI_WORK_REPO_NAME") or "WORKREP"
    o_us = os.getenv("ODI_SUPERVISOR_USER") or ""
    o_pw = os.getenv("ODI_SUPERVISOR_PASSWORD") or ""
    if not all([dsn, m_us, m_pw, o_us, o_pw]):
        return None
    master_jdbc = dsn if dsn.lower().startswith("jdbc:") else f"jdbc:oracle:thin:@{dsn}"
    content = "\n".join([
        "# AUTO-GENERATED local ODI connection from .env (git-ignored). Do not commit.",
        f"master_jdbc={master_jdbc}",
        "master_jdbc_driver=oracle.jdbc.OracleDriver",
        f"master_username={m_us}",
        f"master_password={m_pw}",
        f"work_name={work}",
        f"odi_username={o_us}",
        f"odi_password={o_pw}",
        f"agent_url={ODI_REST_URL or ''}",
        f"agent_username={ODI_REST_USER or ''}",
        f"agent_password={ODI_REST_PASSWORD or ''}",
        "",
    ])
    try:
        with open(_LOCAL_PROPS_FILE, "w", encoding="utf-8") as fh:
            fh.write(content)
        try:
            os.chmod(_LOCAL_PROPS_FILE, 0o600)
        except Exception:
            pass
        return _LOCAL_PROPS_FILE
    except Exception:
        return None


class OdiOrchestrator:
    def __init__(self):
        self.odi_home = _ODI_MW_HOME
        self.scripts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odi_automation")

    def _build_classpath(self) -> str:
        """Builds the ODI SDK + OJDBC + Groovy classpath."""
        sep = ";"
        # odi-core.jar is a large jar (29MB) — mapping, project, and domain classes
        odi_core = os.path.join(_ODI_SDK_LIB, "odi-core.jar")
        # All JARs under sdk/lib (wildcard)
        sdk_wildcard = os.path.join(_ODI_SDK_LIB, "*")
        # eclipselink — required for JPA/persistence
        eclipselink = os.path.join(_ODI_MW_HOME, "oracle_common", "modules", "oracle.toplink", "eclipselink.jar")
        entries = [odi_core, _OJDBC_JAR, _GROOVY_JAR, sdk_wildcard, eclipselink]
        # Optional jar: skipped silently when the installation does not ship it.
        if _J2EE_JAR and os.path.exists(_J2EE_JAR):
            entries.append(_J2EE_JAR)
        return sep.join(entries)

    def run_groovy(self, script_name: str, params: dict) -> str:
        """Run one registered Groovy action behind the common safety boundary."""
        if script_name == "run_scenario.groovy":
            return "Hata: Legacy standalone scenario execution is disabled. error_type=LegacyRuntimeDisabled"
        spec = GROOVY_ACTION_REGISTRY.get(script_name)
        if spec is None:
            return "Hata: ODI Groovy action blocked. error_type=UnregisteredAction"
        if spec.approval_required and _GROOVY_APPROVED.get() is not True:
            return "Hata: ODI Groovy action blocked. error_type=ApprovalRequired"

        scripts_base = os.path.abspath(self.scripts_path)
        script_file = os.path.abspath(os.path.join(self.scripts_path, script_name))
        if not script_file.startswith(scripts_base + os.sep) and script_file != scripts_base:
            return "Hata: ODI Groovy action blocked. error_type=InvalidScriptPath"
        if not os.path.exists(script_file):
            return "Hata: ODI Groovy action failed. error_type=ScriptNotFound"

        for key, val in params.items():
            key_text = str(key)
            if _SECRET_PARAM_KEY_RE.search(key_text):
                return "Hata: ODI Groovy action blocked. error_type=SecretTransportRejected"
            if not _PARAM_KEY_RE.match(key_text):
                return "Hata: ODI Groovy action blocked. error_type=InvalidParameterKey"
            val_regex = (
                _EXPR_VAL_RE
                if key in ("odi.expressions", "odi.join_condition", "odi.filter", "odi.filter_condition")
                else _PARAM_VAL_RE
            )
            if not val_regex.match(str(val)):
                return "Hata: ODI Groovy action blocked. error_type=InvalidParameterValue"

        if not spec.generic_property_transport:
            return "Hata: ODI Groovy action blocked. error_type=CredentialTransportUnavailable"
        if not os.path.exists(_JAVA_BIN):
            return "Hata: ODI Groovy action failed. error_type=JavaNotFound"

        cmd = [_JAVA_BIN, "-cp", self._build_classpath()]
        cmd.extend(f"-D{key}={val}" for key, val in params.items())

        props_path = _ensure_local_props_file() or _PROPS_FILE
        if props_path == _PROPS_FILE:
            try:
                with open(_PROPS_FILE, "r", encoding="utf-8") as props_file:
                    if _PROPS_PLACEHOLDER in props_file.read():
                        return "Hata: ODI Groovy action blocked. error_type=RuntimeConfigurationUnavailable"
            except OSError:
                return "Hata: ODI Groovy action blocked. error_type=RuntimeConfigurationUnavailable"

        cmd.extend(["groovy.lang.GroovyShell", script_file, props_path])
        param_keys = ",".join(sorted(str(key) for key in params)) or "none"
        logger.info(
            "ODI Groovy launch: action=%s category=%s param_keys=%s",
            script_name,
            spec.category,
            param_keys,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=self.scripts_path,
            )
            stdout = _redact_runtime_text(result.stdout.strip())
            stderr = _redact_runtime_text(result.stderr.strip())

            if script_name in remote_run_contract.SCRIPTS:
                action = remote_run_contract.SCRIPTS[script_name]
                if result.returncode != 0 or _groovy_output_failed(stderr):
                    return json.dumps(remote_run_contract.transport_failure(action, "RemoteProcessFailed"))
                return json.dumps(remote_run_contract.parse(stdout, action))

            if script_name == "create_mapping.groovy" and result.returncode == 1:
                envelope = _create_mapping_failure_envelope(result.stdout)
                if envelope is not None:
                    logger.error("ODI Groovy returned reviewed structured error: action=%s", script_name)
                    return envelope

            if result.returncode != 0:
                logger.error(
                    "ODI Groovy failed: action=%s error_type=GroovyProcessError exit_code=%s",
                    script_name,
                    result.returncode,
                )
                return "Hata: ODI Groovy action failed. error_type=GroovyProcessError"

            combined_output = "\n".join(part for part in (stdout, stderr) if part)
            if _groovy_output_failed(combined_output):
                if (
                    _strict_structured_error_output(script_name, stdout)
                    and not _groovy_output_failed(stderr)
                ):
                    logger.error(
                        "ODI Groovy returned reviewed structured error: action=%s",
                        script_name,
                    )
                    return stdout
                logger.error(
                    "ODI Groovy failed: action=%s error_type=GroovyFatalOutput",
                    script_name,
                )
                return "Hata: ODI Groovy action failed. error_type=GroovyFatalOutput"

            logger.info(
                "ODI Groovy completed: action=%s stdout_present=%s stderr_present=%s",
                script_name,
                bool(stdout),
                bool(stderr),
            )
            return stdout
        except subprocess.TimeoutExpired:
            if script_name in remote_run_contract.SCRIPTS:
                return json.dumps(remote_run_contract.transport_failure(
                    remote_run_contract.SCRIPTS[script_name], "RemoteTimeout",
                ))
            return "Hata: ODI Groovy action failed. error_type=TimeoutExpired"
        except FileNotFoundError:
            return "Hata: ODI Groovy action failed. error_type=JavaNotFound"
        except Exception as exc:
            logger.error(
                "ODI Groovy failed: action=%s error_type=%s",
                script_name,
                type(exc).__name__,
            )
            return f"Hata: ODI Groovy action failed. error_type={type(exc).__name__}"

    def generate_mapping(self, mapping_name, src_model, src_table, tgt_model, tgt_table,
                         sequence_name="NONE", expressions: dict = None,
                         join_table: str = None, join_model: str = None,
                         join_condition: str = None, join_type: str = "LEFT_OUTER",
                         ikm_name: str = None, lkm_name: str = None,
                         filter_condition: str = None,
                         truncate_target_table: bool | None = None,
                         replace_existing_mapping: bool = False,
                         required_update_key_columns: list[str] | None = None,
                         integration_type: str | None = None,
                         required_update_key_type: str | None = None,
                         required_ikm_id: str | None = None,
                         source_identity: dict | None = None):
        """Creates a new ODI mapping with create_mapping.groovy.

        Args:
            expressions: Dictionary of custom column mappings. Example:
                {"SKEY": "NVL(JOIN.SKEY, -1)"} — SRC./JOIN. prefixes are resolved in Groovy.
            join_table: LEFT Name of table to JOIN (e.g. SKEY_PROD_PRODUCT).
            join_model: Model of the joined table (default: tgt_model).
            join_condition: Join condition (e.g. "JOIN.NATURAL_KEY = TO_CHAR(SRC.ProductID)").
            join_type: LEFT_OUTER (default), INNER, FULL_OUTER.
            ikm_name: Exact approved IKM name. If omitted, resolve the configured first preference locally.
            lkm_name: Full LKM name (e.g. "LKM SQL to Oracle (Built-In)"). Searched automatically if omitted.
            filter_condition: Filter applied to the source table (e.g. "NOT EXISTS (...)").
            truncate_target_table: IKM Oracle Insert option override. If None, the script default remains unchanged.
            required_update_key_columns: Mapping creation must fail unless the
                target datastore exposes an update key with exactly these columns.
            required_update_key_type: PRIMARY_KEY or ALTERNATE_KEY (default for a requested key).
            integration_type: IU (default in the script) or CONTROL_APPEND.
        """
        params = {
            "odi.project": ODI_PROJECT,
            "odi.folder": ODI_FOLDER,
            "odi.mapping": mapping_name,
            "odi.src_model": src_model,
            "odi.src_table": src_table,
            "odi.tgt_model": tgt_model,
            "odi.tgt_table": tgt_table,
            "odi.sequence_name": sequence_name,
            "odi.source_system": SOURCE_SYSTEM_NAME,
            "odi.replace_existing_mapping": (
                "true" if replace_existing_mapping is True else "false"
            ),
        }
        if expressions:
            expr_str = ";".join(f"{k}={v}" for k, v in expressions.items())
            params["odi.expressions"] = expr_str
        if join_table and join_condition:
            params["odi.join_table"] = join_table
            params["odi.join_model"] = join_model or tgt_model
            params["odi.join_condition"] = join_condition
            params["odi.join_type"] = join_type
        params["odi.ikm_name"] = resolve_approved_ikm(
            ikm_name, str(integration_type or "IU").strip().upper(), ODI_APPROVED_IKMS, ODI_IKM_PRIORITY,
        )
        params["odi.approved_ikms"] = ODI_APPROVED_IKMS
        if required_ikm_id is not None:
            if not isinstance(required_ikm_id, str) or not re.fullmatch(r"[0-9]+", required_ikm_id):
                raise ValueError("Invalid repository IKM identity")
            params["odi.required_ikm_id"] = required_ikm_id
        if source_identity is not None:
            source = verified_source_identity(
                source_identity, source_identity.get("physical_schema"),
                source_identity.get("resource_name"), source_identity.get("context"),
            )
            if source["model_code"] != src_model or source["datastore_name"] != src_table:
                raise ValueError("Source binding does not match verified identity")
            params.update({"odi.required_src_owner": source["physical_schema"],
                           "odi.required_src_resource": source["resource_name"],
                           "odi.source_context": source["context"]})
            if source["datastore_exists"]:
                params["odi.required_src_id"] = source["datastore_id"]
        if lkm_name:
            params["odi.lkm_name"] = lkm_name
        if filter_condition:
            params["odi.filter"] = filter_condition
        if truncate_target_table is not None:
            params["odi.truncate_target_table"] = "true" if truncate_target_table else "false"
        if integration_type:
            params["odi.integration_type"] = str(integration_type).strip().upper()
        if required_update_key_columns:
            key_type = required_update_key_type or "ALTERNATE_KEY"
            if key_type not in {"PRIMARY_KEY", "ALTERNATE_KEY"}:
                raise ValueError("Invalid required_update_key_type")
            params["odi.required_update_key_type"] = key_type
            params["odi.required_update_key_columns"] = ",".join(
                str(name).strip().upper() for name in required_update_key_columns
            )
        # Only LKM selection retains the existing priority chain.
        if not lkm_name:
            params["odi.lkm_priority"] = ODI_LKM_PRIORITY
        return self.run_groovy("create_mapping.groovy", params)

    def update_mapping(self, mapping_name: str, *,
                       operation: str = "rename",
                       new_mapping_name: str = None,
                       expressions: dict = None,
                       filter_action: str = None,
                       filter_condition: str = None,
                       filter_name: str = None,
                       ikm_name: str = None,
                       lkm_name: str = None,
                       truncate_target_table: bool | None = None,
                       rename_source_table_name: str = None,
                       rename_target_table_name: str = None,
                       rename_column_side: str = None,
                       rename_column_old_name: str = None,
                       rename_column_new_name: str = None) -> str:
        """Surgical update: Updates an existing ODI mapping without destroying and recreating it.

        Supported operations:
            update_expression — Changes specified target attribute expressions
            update_filter     — Adds, updates, or deletes a filter component
            update_km         — Changes IKM/LKM (physical design)
            rename            — Changes the mapping name

        Args:
            mapping_name: Existing mapping name.
            operation: Operation type (update_expression, update_filter, update_km, rename).
            new_mapping_name: New mapping name (for rename).
            expressions: Column mapping dictionary (for update_expression).
                Example: {"NAMEE": "Product.Name", "UPDATE_DATE": "SYSDATE"}
            filter_action: add, modify, remove (for update_filter).
            filter_condition: Filter condition (for update_filter add/modify).
            filter_name: Existing filter name (for update_filter modify/remove).
            ikm_name: Full IKM name (for update_km).
            lkm_name: Full LKM name (for update_km).
            truncate_target_table: IKM Oracle Insert TRUNCATE_TARGET_TABLE option'i.
            rename_source_table_name: New source datastore component name.
            rename_target_table_name: New target datastore component name.
            rename_column_side: Column rename scope (source or target).
            rename_column_old_name: Old column name.
            rename_column_new_name: New column name.
        """
        params = {
            "odi.project": ODI_PROJECT,
            "odi.folder": ODI_FOLDER,
            "odi.mapping": mapping_name,
            "odi.operation": operation,
        }
        # Operation-specific parameters
        if operation == "update_expression" and expressions:
            expr_str = ";".join(f"{k}={v}" for k, v in expressions.items())
            params["odi.expressions"] = expr_str
        if operation == "update_filter":
            if filter_action:
                params["odi.filter_action"] = filter_action
            if filter_condition:
                params["odi.filter_condition"] = filter_condition
            if filter_name:
                params["odi.filter_name"] = filter_name
        if operation == "update_km":
            if ikm_name:
                params["odi.ikm_name"] = ikm_name
            if lkm_name:
                params["odi.lkm_name"] = lkm_name
            if truncate_target_table is not None:
                params["odi.truncate_target_table"] = "true" if truncate_target_table else "false"
        if operation == "rename" and new_mapping_name:
            params["odi.new_mapping_name"] = new_mapping_name
        if operation == "rename":
            if rename_source_table_name:
                params["odi.rename_source_table_name"] = rename_source_table_name
            if rename_target_table_name:
                params["odi.rename_target_table_name"] = rename_target_table_name
            if rename_column_side:
                params["odi.rename_column_side"] = rename_column_side
            if rename_column_old_name:
                params["odi.rename_column_old_name"] = rename_column_old_name
            if rename_column_new_name:
                params["odi.rename_column_new_name"] = rename_column_new_name
        return self.run_groovy("update_mapping.groovy", params)

    def sync_datastore_column(self, *, model_code: str, table_name: str, operation: str,
                              column_name: str = None, column_type: str = None,
                              column_length: int | None = None,
                              column_scale: int | None = None,
                              new_name: str = None) -> str:
        """Sync ODI datastore metadata with an already-reviewed DB schema change."""
        params = {
            "odi.model_code": model_code,
            "odi.table_name": table_name,
            "odi.operation": operation,
        }
        if column_name:
            params["odi.column_name"] = column_name
        if column_type:
            params["odi.column_type"] = column_type
        if column_length is not None:
            params["odi.column_length"] = str(column_length)
        if column_scale is not None:
            params["odi.column_scale"] = str(column_scale)
        if new_name:
            params["odi.new_name"] = new_name
        return self.run_groovy("sync_datastore_column.groovy", params)

    def validate_expression(self, target_table: str, target_owner: str,
                            expressions: dict) -> dict:
        """Validates expressions in Oracle DB before execution (EXPLAIN PLAN).

        Simple TABLE.COLUMN references (ODI level) are skipped; only
        computed expressions (SYSDATE, CASE WHEN, CAST, etc.) are validated.

        Args:
            target_table: Target table name (e.g. ODS_PRODUCTION_PRODUCT).
            target_owner: Target schema (e.g. DWH).
            expressions: Column-to-expression mapping dictionary.

        Returns:
            {"valid": True} or {"valid": False, "errors": {"COL": "ORA-..."}}
        """
        import re as _re
        simple_ref_re = _re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$')
        errors = {}
        try:
            from db import get_connection
            conn = get_connection()
            cur = conn.cursor()
            for col_name, expr in expressions.items():
                # Skip simple TABLE.COLUMN references — these are resolved at the ODI level
                if simple_ref_re.match(expr.strip()):
                    continue
                try:
                    test_sql = f"EXPLAIN PLAN FOR SELECT {expr} AS X FROM {target_owner}.{target_table} WHERE 1=0"
                    cur.execute(test_sql)
                except Exception as e:
                    err_msg = str(e).split("\n")[0]
                    errors[col_name] = err_msg
                    logger.warning("Expression validation failed for %s: %s", col_name, err_msg)
            cur.close()
        except Exception as e:
            logger.error("validate_expression DB connection failed: %s", str(e))
            return {"valid": True, "warning": f"DB validation skipped: {str(e)}"}

        if errors:
            return {"valid": False, "errors": errors}
        return {"valid": True}

    def delete_mapping(self, mapping_name: str) -> str:
        """Physically deletes an existing ODI mapping with delete_mapping.groovy."""
        params = {
            "odi.project": ODI_PROJECT,
            "odi.folder": ODI_FOLDER,
            "odi.mapping": mapping_name,
        }
        return self.run_groovy("delete_mapping.groovy", params)

    def generate_scenario(self, name: str, mapping: str, version: str = "001"):
        """Use the absent-only, cast-safe scenario metadata generator."""
        params = {
            "odi.project": ODI_PROJECT,
            "odi.folder": ODI_FOLDER,
            "odi.scenario.name": name,
            "odi.mapping": mapping,
            "odi.scenario.version": version,
        }
        output = self.run_groovy("generate_scenario_metadata.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError, AttributeError):
            return "Hata: ODI scenario generation failed. error_type=InvalidGeneratorOutput"
        status = str(parsed.get("status") or "").lower()
        if status in {"passed", "already_ready"}:
            return f"SUCCESS: Scenario {name} version {version} is ready."
        error_type = str(parsed.get("error_type") or "ScenarioGenerationError")
        return f"Hata: ODI scenario generation failed. error_type={error_type}"

    def generate_scenario_metadata(self, *,
                                   approved: bool = False,
                                   mapping_name: str,
                                   scenario_name: str,
                                   version: str = "001",
                                   project: str = None,
                                   folder: str = None) -> dict:
        """Generate one ODI scenario metadata object without deleting existing scenarios."""
        if approved is not True:
            return {
                "tool": "odi.generate_scenario_metadata",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_scenario_generation_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST scenario metadata generation.",
                    }
                ],
            }

        params = {
            "odi.project": project or ODI_PROJECT,
            "odi.folder": folder or ODI_FOLDER,
            "odi.mapping": mapping_name,
            "odi.scenario.name": scenario_name,
            "odi.scenario.version": version,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("generate_scenario_metadata.groovy", params)
        try:
            from repository_write_contract import parse_json
            parsed = parse_json(output)
            if not isinstance(parsed, dict):
                raise ValueError("Invalid generation output")
            return parsed
        except Exception as exc:
            return {
                "status": "outcome_unknown",
                "side_effects": None,
                "repository_outcome": "unverified",
                "manual_recovery_review_required": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "generate_scenario_metadata_json",
                        "status": "fail",
                        "detail": "ODI scenario metadata generation did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def scenario_probe(self, *,
                       approved: bool = False,
                       scenario_name: str,
                       version: str = "001") -> dict:
        """Read-only ODI scenario metadata probe by name/version."""
        if not approved:
            return {
                "tool": "odi.scenario_probe",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_REPOSITORY_READ_ONLY",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_scenario_probe_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only after the ODI_REPOSITORY_READ_ONLY gate is approved.",
                    }
                ],
            }

        params = {
            "odi.scenario.name": scenario_name,
            "odi.scenario.version": version,
        }
        output = self.run_groovy("scenario_probe.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", False)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": False,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "scenario_probe_json",
                        "status": "fail",
                        "detail": "ODI scenario probe did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def register_datastore(self, model_code: str, table_name: str, columns: list[dict],
                            resource_name: str = None,
                            key_columns: list[str] | None = None,
                            key_name: str | None = None,
                            key_type: str | None = None,
                            key_only: bool = False) -> str:
        """Registers a new datastore and its columns in the ODI model.

        Args:
            model_code: ODI model code (e.g. DWH, ORACLE_HR, ORACLE_SH)
            table_name: Datastore name (e.g. ODS_CUSTOMER, Customer)
            columns: Column list — each item {"name": str, "type": str, "length": int, "scale": int}
            resource_name: Physical table name (default: table_name)
            key_columns: Optional ODI key columns (registered as a datastore key).
            key_name: ODI key name (the database constraint name).
            key_type: ALTERNATE_KEY (update key) or PRIMARY_KEY.
            key_only: Only add the key to an existing datastore; never recreate it.
        """
        # Build the column string: NAME:TYPE:LENGTH:SCALE,...
        col_parts = []
        for c in columns:
            part = c["name"] + ":" + c.get("type", "VARCHAR2")
            if c.get("length"):
                part += ":" + str(c["length"])
            if c.get("scale") is not None and c.get("scale", 0) > 0:
                part += ":" + str(c["scale"])
            col_parts.append(part)

        params = {
            "odi.model_code": model_code,
            "odi.table_name": table_name,
            "odi.resource_name": resource_name or table_name,
            "odi.columns": ",".join(col_parts),
        }
        if key_columns:
            params["odi.key_columns"] = ",".join(str(name).strip().upper() for name in key_columns)
            params["odi.key_name"] = key_name or ""
            params["odi.key_type"] = key_type or "ALTERNATE_KEY"
            if key_only:
                params["odi.key_only"] = "true"
        return self.run_groovy("register_datastore.groovy", params)

    def delete_datastore(self, model_code: str, table_name: str) -> str:
        """Deletes a datastore registration from the ODI model. Does not touch the physical table.

        Args:
            model_code: ODI model code (e.g. DWH)
            table_name: Datastore name to delete (e.g. DWH_ODS_PRODUCT)
        """
        params = {
            "odi.model_code": model_code,
            "odi.table_name": table_name,
        }
        return self.run_groovy("delete_datastore.groovy", params)

    def add_column(self, model_code: str, table_name: str, col_name: str, 
                   col_type: str = "NUMBER", col_len: int = 10, col_prec: int = 0) -> str:
        """Adds a new column to an existing ODI datastore."""
        params = {
            "odi.model_code": model_code,
            "odi.table_name": table_name,
            "odi.column_name": col_name,
            "odi.column_type": col_type,
            "odi.column_length": str(col_len),
            "odi.column_precision": str(col_prec),
        }
        return self.run_groovy("add_column.groovy", params)

    def run_scenario(self, name: str, version: str = "001", context: str = "GLOBAL"):
        """Compatibility adapter; execution is remote-only and failure text is fixed."""
        mapping = name[:-4] if isinstance(name, str) and name.endswith("_SCN") else ""
        result = self.run_scenario_remote(
            approved=groovy_is_approved(), mapping_name=mapping,
            scenario_name=name, version=version, context=context,
        )
        if result["status"] == "passed":
            return "SUCCESS: Remote scenario completed. Status: DONE"
        return "Hata: Remote scenario did not complete. error_type=" + result["error_code"]

    @staticmethod
    def _remote_configuration_ready() -> bool:
        try:
            endpoint = urlsplit(ODI_REST_URL or "")
            return bool(endpoint.scheme in {"http", "https"} and endpoint.hostname
                        and endpoint.port == 15101 and endpoint.path == "/oraclediagent"
                        and not endpoint.query and not endpoint.fragment and endpoint.username is None
                        and endpoint.password is None)
        except (ValueError, TypeError):
            return False

    def remote_agent_health(self, *, approved: object = False) -> dict:
        """Read-only isAlive gate; no caller-controlled URL, credential or startup params."""
        if approved is not True:
            return remote_run_contract.result("health", stage="approval", error_code="ApprovalRequired")
        if not self._remote_configuration_ready():
            return remote_run_contract.result("health", error_code="AgentConfigurationUnavailable")
        with groovy_approval_context(True):
            output = self.run_groovy("remote_agent_health.groovy", {})
        return remote_run_contract.parse(output, "health")

    def run_scenario_remote(self, *, approved: object = False, mapping_name: str,
                            scenario_name: str, version: str, context: str = "GLOBAL") -> dict:
        """Exact mapping ownership is checked again in the remote script before start."""
        if approved is not True:
            return remote_run_contract.result("run", stage="approval", error_code="ApprovalRequired")
        identifiers = (mapping_name, scenario_name, context)
        if (any(not isinstance(v, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", v) is None for v in identifiers)
                or not isinstance(version, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,12}", version) is None):
            return remote_run_contract.result("run", stage="validation", error_code="InvalidRunIdentity")
        if not self._remote_configuration_ready():
            return remote_run_contract.result("run", error_code="AgentConfigurationUnavailable")
        params = {"odi.project": ODI_PROJECT, "odi.folder": ODI_FOLDER, "odi.mapping": mapping_name,
                  "odi.scenario.name": scenario_name, "odi.scenario.version": version, "odi.context": context}
        with groovy_approval_context(True):
            output = self.run_groovy("run_scenario_remote.groovy", params)
        return remote_run_contract.parse(output, "run")

    def run_scenario_safe(self, *,
                          approved: bool = False,
                          scenario_name: str,
                          version: str,
                          context: str = "GLOBAL",
                          extra_allowed_scenarios: frozenset | None = None) -> dict:
        """Run an allowlisted ODI scenario and return redacted structured evidence."""
        scenario = str(scenario_name or "").strip().upper()
        scen_version = str(version or "").strip()
        ctx = str(context or "GLOBAL").strip().upper() or "GLOBAL"
        if approved is not True:
            return {
                "tool": "odi.run_scenario_safe",
                "status": "blocked_until_approval",
                "side_effects": False,
                "will_run_scenario": False,
                "repository_connection_attempted": False,
                "checks": [
                    {
                        "name": "odi_scenario_run_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for the ODI-24 dev/TST scenario run smoke.",
                    }
                ],
            }
        if scenario not in _ODI24_ALLOWED_SCENARIOS and scenario not in (extra_allowed_scenarios or ()):
            return {
                "tool": "odi.run_scenario_safe",
                "status": "blocked_by_allowlist",
                "side_effects": False,
                "will_run_scenario": False,
                "repository_connection_attempted": False,
                "scenario": {"name": scenario, "version": scen_version},
                "checks": [
                    {
                        "name": "scenario_allowlist",
                        "status": "fail",
                        "detail": "Only the ODI-24 HR/SH smoke scenarios are allowed by this wrapper.",
                    }
                ],
            }
        if not re.match(r"^[A-Za-z0-9_.-]+$", scen_version):
            return {
                "tool": "odi.run_scenario_safe",
                "status": "blocked_invalid_version",
                "side_effects": False,
                "will_run_scenario": False,
                "repository_connection_attempted": False,
                "scenario": {"name": scenario, "version": scen_version},
                "checks": [
                    {
                        "name": "scenario_version_valid",
                        "status": "fail",
                        "detail": "Scenario version contains unsupported characters.",
                    }
                ],
            }

        mapping = scenario[:-4] if scenario.endswith("_SCN") else ""
        return self.run_scenario_remote(approved=True, mapping_name=mapping, scenario_name=scenario,
                                        version=scen_version, context=ctx)

    def run_products_ods_scenario_safe(
        self,
        *,
        approved: object = False,
        approval_scope: str,
        scenario_name: str,
        version: str,
        context: str = "GLOBAL",
    ) -> dict:
        """Run only the fixed ODI-68 PRODUCTS scenario behind its exact scope."""
        scenario = str(scenario_name or "").strip().upper()
        scen_version = str(version or "").strip()
        ctx = str(context or "").strip().upper()
        identity = {
            "name": scenario,
            "version": scen_version,
            "context": ctx,
        }
        blocked = {
            "scenario": identity,
            "side_effects": False,
            "raw_output_included": False,
            "session_id_included": False,
        }
        if approved is not True:
            return {**blocked, "status": "blocked_until_exact_operator_approval"}
        if approval_scope != _PRODUCTS_ODS_RUN_SCOPE:
            return {**blocked, "status": "blocked_approval_scope"}
        if identity != {
            "name": _PRODUCTS_ODS_SCENARIO,
            "version": _PRODUCTS_ODS_SCENARIO_VERSION,
            "context": _PRODUCTS_ODS_SCENARIO_CONTEXT,
        }:
            return {**blocked, "status": "blocked_scenario_identity"}

        remote = remote_run_contract.parse(self.run_scenario_remote(
            approved=True, mapping_name=scenario[:-4], scenario_name=scenario,
            version=scen_version, context=ctx,
        ), "run")
        return {**blocked, **remote}

    def run_dim_product_scd2_scenario_safe(
        self,
        *,
        approved: object = False,
        approval_scope: object,
        scenario_name: object,
        version: object,
        context: object = "GLOBAL",
    ) -> dict:
        """Run only the reviewed fixed DIM_PRODUCT scenario via the ODI agent."""
        fixed_scenario = {
            "name": _DIM_PRODUCT_SCD2_SCENARIO,
            "version": _DIM_PRODUCT_SCD2_SCENARIO_VERSION,
            "context": _DIM_PRODUCT_SCD2_SCENARIO_CONTEXT,
        }
        base = {
            "scenario": fixed_scenario,
            "side_effects": False,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "raw_output_included": False,
            "session_id_included": False,
        }
        if approved is not True:
            return {**base, "status": "blocked_until_exact_operator_approval"}
        if approval_scope != _DIM_PRODUCT_SCD2_RUN_SCOPE:
            return {**base, "status": "blocked_approval_scope"}
        if (scenario_name, version, context) != (
            _DIM_PRODUCT_SCD2_SCENARIO,
            _DIM_PRODUCT_SCD2_SCENARIO_VERSION,
            _DIM_PRODUCT_SCD2_SCENARIO_CONTEXT,
        ):
            return {**base, "status": "blocked_scenario_identity"}

        with groovy_approval_context(True):
            output = self.run_groovy("dim_product_scd2_scenario_run.groovy", {})
        try:
            payload = json.loads(output)
        except (TypeError, ValueError):
            payload = None
        envelope = {
            "tool": "odi.dim_product_scd2_scenario_run",
            "mode": "fixed_scd2_scenario_run",
            "repository_connection_attempted": True,
            "raw_rows_included": False,
            "pk_values_included": False,
            "secret_printed": False,
            "scenario": fixed_scenario,
        }
        success = (
            isinstance(payload, dict)
            and set(payload) == set(envelope) | {"status", "side_effects", "session_status"}
            and all(payload.get(key) == value for key, value in envelope.items())
            and payload.get("status") == "passed"
            and payload.get("side_effects") is True
            and payload.get("session_status") == "DONE"
        )
        if success:
            return {**base, "status": "passed", "side_effects": True,
                    "session_status": "DONE"}
        failure = {**base, "status": "failed", "side_effects": True,
                   "error_type": "InvalidScenarioRunOutput"}
        if (
            isinstance(payload, dict)
            and set(payload) == set(envelope) | {"status", "side_effects", "error_type"}
            and all(payload.get(key) == value for key, value in envelope.items())
            and payload.get("status") == "error"
            and type(payload.get("side_effects")) is bool
            and isinstance(payload.get("error_type"), str)
            and payload.get("error_type") in _DIM_PRODUCT_SCD2_RUN_ERROR_TYPES
        ):
            failure["side_effects"] = payload["side_effects"]
            failure["error_type"] = payload["error_type"]
        return failure

    def create_load_plan_metadata(self, *,
                                  approved: bool = False,
                                  load_plan_name: str,
                                  steps: list[dict] = None,
                                  context: str = "GLOBAL") -> dict:
        """Create or validate an ODI load-plan metadata object without running it."""
        plan_name = str(load_plan_name or "").strip()
        ctx = str(context or "GLOBAL").strip().upper() or "GLOBAL"
        selected_steps = steps or _ODI25_DEFAULT_LOAD_PLAN_STEPS
        if not approved:
            return {
                "tool": "odi.create_load_plan_metadata",
                "status": "blocked_until_approval",
                "side_effects": False,
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "checks": [
                    {
                        "name": "odi_load_plan_metadata_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for ODI-25 dev/TST load-plan metadata creation.",
                    }
                ],
            }
        if not re.match(r"^[A-Za-z0-9_. -]+$", plan_name):
            return {
                "tool": "odi.create_load_plan_metadata",
                "status": "blocked_invalid_name",
                "side_effects": False,
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "checks": [{"name": "load_plan_name_valid", "status": "fail", "detail": "Load plan name contains unsupported characters."}],
            }

        encoded_steps = []
        for raw_step in selected_steps:
            step_name = str(raw_step.get("name") or "").strip()
            scenario_name = str(raw_step.get("scenario_name") or "").strip().upper()
            version = str(raw_step.get("version") or "").strip()
            if scenario_name not in _ODI24_ALLOWED_SCENARIOS:
                return {
                    "tool": "odi.create_load_plan_metadata",
                    "status": "blocked_by_allowlist",
                    "side_effects": False,
                    "will_run_load_plan": False,
                    "repository_connection_attempted": False,
                    "checks": [{"name": "load_plan_step_allowlist", "status": "fail", "detail": "Only known HR/SH smoke scenarios are allowed in ODI-25 load-plan metadata."}],
                }
            if not re.match(r"^[A-Za-z0-9_. -]+$", step_name) or not re.match(r"^[A-Za-z0-9_.-]+$", version):
                return {
                    "tool": "odi.create_load_plan_metadata",
                    "status": "blocked_invalid_step",
                    "side_effects": False,
                    "will_run_load_plan": False,
                    "repository_connection_attempted": False,
                    "checks": [{"name": "load_plan_step_valid", "status": "fail", "detail": "Step name or version contains unsupported characters."}],
                }
            encoded_steps.append(f"{step_name}:{scenario_name}:{version}")

        with groovy_approval_context(approved):
            output = self.run_groovy(
                "create_load_plan_metadata.groovy",
                {
                    "odi.load_plan.name": plan_name,
                    "odi.context": ctx,
                    "odi.steps": ",".join(encoded_steps),
                },
            )
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("will_run_load_plan", False)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "tool": "odi.create_load_plan_metadata",
                "status": "error",
                "side_effects": True,
                "will_run_load_plan": False,
                "repository_connection_attempted": True,
                "checks": [{"name": "create_load_plan_metadata_json", "status": "fail", "detail": "ODI load-plan metadata script did not return valid JSON."}],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def load_plan_probe(self, *,
                        approved: bool = False,
                        load_plan_name: str) -> dict:
        """Read-only load-plan metadata probe. It never starts a load plan."""
        plan_name = str(load_plan_name or "").strip()
        if not approved:
            return {
                "tool": "odi.load_plan_probe",
                "status": "blocked_until_approval",
                "side_effects": False,
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "checks": [
                    {
                        "name": "odi_load_plan_probe_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only after the ODI_REPOSITORY_READ_ONLY gate is approved.",
                    }
                ],
            }
        output = self.run_groovy("load_plan_probe.groovy", {"odi.load_plan.name": plan_name})
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", False)
            parsed.setdefault("will_run_load_plan", False)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "tool": "odi.load_plan_probe",
                "status": "error",
                "side_effects": False,
                "will_run_load_plan": False,
                "repository_connection_attempted": True,
                "checks": [{"name": "load_plan_probe_json", "status": "fail", "detail": "ODI load-plan probe did not return valid JSON."}],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def run_load_plan_safe(self, *,
                           approved: bool = False,
                           load_plan_name: str,
                           context: str = "GLOBAL") -> dict:
        """Run one allowlisted dev/TST load plan through a configured ODI remote agent."""
        plan_name = str(load_plan_name or "").strip().upper()
        context_code = str(context or "GLOBAL").strip().upper() or "GLOBAL"
        if not approved:
            return {
                "tool": "odi.run_load_plan_safe",
                "status": "blocked_until_approval",
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_LOAD_PLAN_RUN",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_load_plan_run_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for the controlled dev/TST load-plan smoke.",
                    }
                ],
            }

        if plan_name not in _ODI26_ALLOWED_LOAD_PLANS:
            return {
                "tool": "odi.run_load_plan_safe",
                "status": "blocked_by_allowlist",
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "load_plan": {"name": plan_name},
                "checks": [{"name": "load_plan_allowlist", "status": "fail", "detail": "Only the ODI-26 HR/SH smoke load plan is allowed."}],
            }

        if not re.match(r"^[A-Z0-9_]+$", plan_name) or not re.match(r"^[A-Z0-9_]+$", context_code):
            return {
                "tool": "odi.run_load_plan_safe",
                "status": "blocked_by_validation",
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "checks": [{"name": "load_plan_run_params_valid", "status": "fail", "detail": "Load plan name or context contains unsupported characters."}],
            }

        if not all([ODI_REST_URL, ODI_REST_USER, ODI_REST_PASSWORD]):
            return {
                "tool": "odi.run_load_plan_safe",
                "status": "blocked_until_agent_config",
                "will_run_load_plan": False,
                "repository_connection_attempted": False,
                "load_plan": {"name": plan_name},
                "checks": [
                    {
                        "name": "odi_load_plan_agent_config",
                        "status": "fail",
                        "detail": "ODI remote agent URL/user/password are not configured; load plans require RemoteRuntimeAgentInvoker.",
                    }
                ],
            }

        with groovy_approval_context(approved):
            output = self.run_groovy(
                "run_load_plan_remote.groovy",
                {
                    "odi.load_plan.name": plan_name,
                    "odi.context": context_code,
                },
            )
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("will_run_load_plan", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "tool": "odi.run_load_plan_safe",
                "status": "error",
                "will_run_load_plan": True,
                "repository_connection_attempted": True,
                "checks": [{"name": "run_load_plan_json", "status": "fail", "detail": "ODI load-plan run script did not return valid JSON."}],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def setup_topology(self, data_server: str, schema_name: str, catalog_name: str,
                       logical_schema: str, model_name: str, model_code: str,
                       context: str = "GLOBAL", technology: str = "Oracle",
                       tech_code: str = "ORACLE") -> str:
        """Creates a physical schema, logical schema, context mapping, and model under Topology."""
        params = {
            "odi.data_server": data_server,
            "odi.schema_name": schema_name,
            "odi.catalog_name": catalog_name or schema_name,
            "odi.logical_schema": logical_schema,
            "odi.model_name": model_name,
            "odi.model_code": model_code,
            "odi.context": context,
            "odi.technology": technology,
            "odi.tech_code": tech_code,
        }
        return self.run_groovy("setup_topology.groovy", params)

    def seed_project_folder(self, *,
                            approved: bool = False,
                            project: str = None,
                            folder: str = None,
                            project_name: str = None) -> dict:
        """Create missing ODI project/folder metadata idempotently."""
        if not approved:
            return {
                "tool": "odi.seed_project_folder",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_repository_write_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST idempotent metadata seed.",
                    }
                ],
            }

        params = {
            "odi.project": project or ODI_PROJECT,
            "odi.folder": folder or ODI_FOLDER,
            "odi.project_name": project_name or project or ODI_PROJECT,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("seed_project_folder.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "seed_project_folder_json",
                        "status": "fail",
                        "detail": "ODI project/folder seed did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def seed_model_datastore(self, *,
                             approved: bool = False,
                             model_code: str,
                             model_name: str = None,
                             logical_schema: str = None,
                             table_name: str,
                             resource_name: str = None,
                             columns: list[dict] = None,
                             update_key_name: str | None = None,
                             update_key_columns: list[str] | None = None,
                             technology: str = "Oracle",
                             tech_code: str = "ORACLE") -> dict:
        """Create missing ODI logical schema/model/datastore metadata idempotently."""
        if not approved:
            return {
                "tool": "odi.seed_model_datastore",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_repository_write_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST idempotent model/datastore seed.",
                    }
                ],
            }

        col_parts = []
        for c in columns or []:
            part = c["name"] + ":" + c.get("type", "VARCHAR2")
            if c.get("length"):
                part += ":" + str(c["length"])
            if c.get("scale") is not None and c.get("scale", 0) > 0:
                part += ":" + str(c["scale"])
            col_parts.append(part)

        params = {
            "odi.model_code": model_code,
            "odi.model_name": model_name or model_code,
            "odi.logical_schema": logical_schema or f"LGL_{model_code}",
            "odi.table_name": table_name,
            "odi.resource_name": resource_name or table_name,
            "odi.technology": technology,
            "odi.tech_code": tech_code,
        }
        if col_parts:
            params["odi.columns"] = ",".join(col_parts)
        if update_key_name or update_key_columns:
            if not update_key_name or not update_key_columns:
                raise ValueError("Update key name and columns must be provided together.")
            params["odi.update_key_name"] = update_key_name.strip().upper()
            params["odi.update_key_columns"] = ",".join(
                str(name).strip().upper() for name in update_key_columns
            )
        with groovy_approval_context(approved):
            output = self.run_groovy("seed_model_datastore.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "seed_model_datastore_json",
                        "status": "fail",
                        "detail": "ODI model/datastore seed did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def create_mapping_smoke(self, *,
                             approved: bool = False,
                             mapping_name: str,
                             project: str = None,
                             folder: str = None,
                             src_model: str,
                             src_table: str,
                             tgt_model: str,
                             tgt_table: str) -> dict:
        """Create one absent ODI mapping metadata smoke without delete/run/scenario."""
        if not approved:
            return {
                "tool": "odi.create_mapping_smoke",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_mapping_create_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST absent-only mapping metadata smoke.",
                    }
                ],
            }

        params = {
            "odi.project": project or ODI_PROJECT,
            "odi.folder": folder or ODI_FOLDER,
            "odi.mapping": mapping_name,
            "odi.src_model": src_model,
            "odi.src_table": src_table,
            "odi.tgt_model": tgt_model,
            "odi.tgt_table": tgt_table,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("create_mapping_smoke.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "create_mapping_smoke_json",
                        "status": "fail",
                        "detail": "ODI mapping smoke did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def repair_datastore_metadata(self, *,
                                  approved: bool = False,
                                  model_code: str,
                                  table_name: str) -> dict:
        """Fill missing ODI datastore column metadata needed by mapping components."""
        if not approved:
            return {
                "tool": "odi.repair_datastore_metadata",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_datastore_metadata_repair_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST idempotent datastore metadata repair.",
                    }
                ],
            }

        params = {
            "odi.model_code": model_code,
            "odi.table_name": table_name,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("repair_datastore_metadata.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "repair_datastore_metadata_json",
                        "status": "fail",
                        "detail": "ODI datastore metadata repair did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def seed_physical_topology(self, *,
                               approved: bool = False,
                               data_server: str,
                               schema_name: str,
                               logical_schema: str,
                               env_prefix: str,
                               work_schema_name: str = None,
                               context: str = "GLOBAL",
                               technology: str = "Oracle") -> dict:
        """Create missing ODI Oracle data server/physical schema/context mapping.

        Secrets are not passed as system properties. The Groovy script reads the
        local environment file using env_prefix (for example SOURCE_ORACLE).
        """
        if not approved:
            return {
                "tool": "odi.seed_physical_topology",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_physical_topology_seed_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST idempotent physical topology seed.",
                    }
                ],
            }

        params = {
            "odi.data_server": data_server,
            "odi.schema_name": schema_name,
            "odi.work_schema_name": work_schema_name or schema_name,
            "odi.logical_schema": logical_schema,
            "odi.env_prefix": env_prefix,
            "odi.context": context,
            "odi.technology": technology,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("seed_physical_topology.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "seed_physical_topology_json",
                        "status": "fail",
                        "detail": "ODI physical topology seed did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def repair_mapping_components(self, *,
                                  approved: bool = False,
                                  mapping_name: str,
                                  project: str = None,
                                  folder: str = None,
                                  src_model: str,
                                  src_table: str,
                                  tgt_model: str,
                                  tgt_table: str) -> dict:
        """Wire source/target datastore components only when an existing mapping is empty."""
        if not approved:
            return {
                "tool": "odi.repair_mapping_components",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_CREATE_MAPPING",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_mapping_component_repair_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only for dev/TST empty-mapping component repair.",
                    }
                ],
            }

        params = {
            "odi.project": project or ODI_PROJECT,
            "odi.folder": folder or ODI_FOLDER,
            "odi.mapping": mapping_name,
            "odi.src_model": src_model,
            "odi.src_table": src_table,
            "odi.tgt_model": tgt_model,
            "odi.tgt_table": tgt_table,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("repair_mapping_components.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", True)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": True,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "repair_mapping_components_json",
                        "status": "fail",
                        "detail": "ODI mapping component repair did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def dim_product_scd2_capability_probe(self, *, approved: object = False) -> dict:
        """Read fixed DIM_PRODUCT SCD2 capability metadata behind a strict gate."""
        if approved is not True:
            return {
                "tool": "odi.dim_product_scd2_capability_probe",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "approval": {
                    "action": "ODI_REPOSITORY_READ_ONLY",
                    "required": True,
                    "status": "missing",
                },
            }

        output = self.run_groovy(
            "dim_product_scd2_capability_probe.groovy",
            {"odi.project": ODI_PROJECT, "odi.folder": ODI_FOLDER},
        )
        try:
            from dim_product_odi_scd2_capability_probe import (
                normalize_dim_product_odi_scd2_capability_probe,
            )

            parsed = json.loads(output)
            return normalize_dim_product_odi_scd2_capability_probe(parsed)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return {
                "tool": "odi.dim_product_scd2_capability_probe",
                "status": "error",
                "mode": "normalized_odi_sdk_metadata_only",
                "side_effects": False,
                "repository_connection_attempted": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "error": "ODI capability probe output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def dim_product_scd2_datastore_register(
        self,
        *,
        capability_probe: dict,
        capability_probe_review_status: object,
        registration_contract_review_status: object,
        registrar_implementation_review_status: object,
        approved: object,
        approval_scope: object,
    ) -> dict:
        """Register fixed absent DIM_PRODUCT metadata after every reviewed gate."""
        from dim_product_odi_scd2_datastore_registration import (
            SCRIPT_NAME,
            build_dim_product_odi_scd2_datastore_registration_plan,
            build_dim_product_odi_scd2_datastore_registration_request,
            normalize_dim_product_odi_scd2_datastore_registration,
        )

        plan = build_dim_product_odi_scd2_datastore_registration_plan(
            capability_probe
        )
        request = build_dim_product_odi_scd2_datastore_registration_request(
            plan=plan,
            capability_probe_review_status=capability_probe_review_status,
            registration_contract_review_status=registration_contract_review_status,
            registrar_implementation_review_status=registrar_implementation_review_status,
            approved=approved,
            approval_scope=approval_scope,
        )
        if request.get("status") != "ready_for_dedicated_registrar_execution":
            return request

        with groovy_approval_context(approved):
            output = self.run_groovy(SCRIPT_NAME, {})
        try:
            return normalize_dim_product_odi_scd2_datastore_registration(write_contract.parse_json(output))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return {
                "tool": "odi.dim_product_scd2_datastore_registration",
                "status": "error",
                "mode": "normalized_fixed_absent_only_odi_datastore_registration",
                "side_effects": None,
                "repository_outcome": "unverified",
                "manual_recovery_review_required": True,
                "repository_connection_attempted": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "error": "ODI datastore registration output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def dim_product_scd2_ikm_import(
        self,
        *,
        capability_probe: dict,
        capability_probe_review_status: object,
        import_contract_review_status: object,
        importer_implementation_review_status: object,
        approved: object,
        approval_scope: object,
    ) -> dict:
        """Import one fixed SDK reference IKM after all review gates."""
        from dim_product_odi_scd2_ikm_import import (
            SCRIPT_NAME,
            build_dim_product_odi_scd2_ikm_import_plan,
            build_dim_product_odi_scd2_ikm_import_request,
            normalize_dim_product_odi_scd2_ikm_import,
        )

        plan = build_dim_product_odi_scd2_ikm_import_plan(capability_probe)
        request = build_dim_product_odi_scd2_ikm_import_request(
            plan=plan,
            capability_probe_review_status=capability_probe_review_status,
            import_contract_review_status=import_contract_review_status,
            importer_implementation_review_status=importer_implementation_review_status,
            approved=approved,
            approval_scope=approval_scope,
        )
        if request.get("status") != "ready_for_fixed_ikm_import_execution":
            return request

        with groovy_approval_context(approved):
            output = self.run_groovy(
                SCRIPT_NAME,
                {
                    "odi.oracle_home": self.odi_home,
                    "odi.project": ODI_PROJECT,
                },
            )
        try:
            return normalize_dim_product_odi_scd2_ikm_import(write_contract.parse_json(output))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return {
                "tool": "odi.dim_product_scd2_ikm_import",
                "status": "error",
                "mode": "normalized_fixed_sdk_reference_absent_only_ikm_import",
                "side_effects": None,
                "repository_outcome": "unverified",
                "manual_recovery_review_required": True,
                "repository_connection_attempted": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "error": "ODI IKM import output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def dim_product_scd2_mapping_create(
        self,
        *,
        packaging_readiness: dict,
        mapping_contract_review_status: object,
        mapping_builder_implementation_review_status: object,
        approved: object,
        approval_scope: object,
    ) -> dict:
        """Create the fixed absent DIM_PRODUCT SCD2 mapping after all gates."""
        from dim_product_odi_scd2_mapping_builder import (
            SCRIPT_NAME,
            build_dim_product_odi_scd2_mapping_request,
            normalize_dim_product_odi_scd2_mapping,
        )

        request = build_dim_product_odi_scd2_mapping_request(
            packaging_readiness=packaging_readiness,
            mapping_contract_review_status=mapping_contract_review_status,
            mapping_builder_implementation_review_status=(
                mapping_builder_implementation_review_status
            ),
            approved=approved,
            approval_scope=approval_scope,
        )
        if request.get("status") != "ready_for_dedicated_mapping_builder_execution":
            return request

        params = {"odi.project": ODI_PROJECT, "odi.folder": ODI_FOLDER}
        with groovy_approval_context(approved):
            output = self.run_groovy(SCRIPT_NAME, params)
        try:
            from repository_write_contract import parse_json
            return normalize_dim_product_odi_scd2_mapping(parse_json(output))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return {
                "tool": "odi.dim_product_scd2_mapping_create",
                "status": "error",
                "mode": "normalized_fixed_absent_only_scd2_mapping_creation",
                "side_effects": None,
                "repository_outcome": "unverified",
                "manual_recovery_review_required": True,
                "repository_connection_attempted": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "error": "ODI SCD2 mapping output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def dim_product_audit_role_action(
        self,
        *,
        action: object,
        approved: object = False,
        approval_scope: object = None,
        implementation_review_status: object = None,
        no_active_odi_session_writes_attested: object = False,
        receipt_dir: object = None,
    ) -> dict:
        """Preflight, repair or restore the four fixed DIM_PRODUCT audit roles after all gates."""
        from dim_product_audit_role_repair import SCRIPT_NAME, run_audit_role_action

        def run_script(parameters: dict[str, str]) -> str:
            with groovy_approval_context(approved):
                return self.run_groovy(SCRIPT_NAME, parameters)

        return run_audit_role_action(
            action=action,
            approved=approved,
            approval_scope=approval_scope,
            implementation_review_status=implementation_review_status,
            no_active_odi_session_writes_attested=no_active_odi_session_writes_attested,
            receipt_dir=receipt_dir,
            project=ODI_PROJECT,
            folder=ODI_FOLDER,
            run_script=run_script,
        )

    def dim_product_scd2_mapping_probe(self, *, approved: object = False) -> dict:
        """Inspect the fixed mapping with metadata-only ODI SDK finders."""
        if approved is not True:
            return {
                "tool": "odi.dim_product_scd2_mapping_probe",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
            }
        output = self.run_groovy(
            "dim_product_scd2_mapping_probe.groovy",
            {"odi.project": ODI_PROJECT, "odi.folder": ODI_FOLDER},
        )
        try:
            from dim_product_odi_scd2_mapping_probe import (
                normalize_dim_product_odi_scd2_mapping_probe,
            )

            return normalize_dim_product_odi_scd2_mapping_probe(json.loads(output))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return {
                "tool": "odi.dim_product_scd2_mapping_probe",
                "status": "error",
                "mode": "normalized_fixed_scd2_mapping_metadata",
                "side_effects": False,
                "repository_connection_attempted": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "error": "ODI SCD2 mapping probe output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def dim_product_scd2_scenario_generate(
        self,
        *,
        mapping_probe: dict,
        scenario_probe: dict,
        mapping_builder_review_status: object,
        scenario_generator_review_status: object,
        approved: object,
        approval_scope: object,
    ) -> dict:
        """Generate only the fixed absent SCD2 scenario after separate review."""
        from dim_product_odi_scd2_scenario import (
            MAPPING_NAME,
            SCENARIO_NAME,
            SCENARIO_VERSION,
            build_dim_product_scd2_scenario_generation_readiness,
            normalize_dim_product_scd2_scenario_generation,
        )

        readiness = build_dim_product_scd2_scenario_generation_readiness(
            mapping_probe,
            scenario_probe,
            mapping_builder_review_status=mapping_builder_review_status,
            approved=approved,
            approval_scope=approval_scope,
        )
        if readiness.get("status") != "ready_for_absent_only_scenario_generation":
            return readiness
        if scenario_generator_review_status != "approved":
            return {**readiness, "status": "blocked_scenario_generator_review"}

        params = {
            "odi.project": ODI_PROJECT,
            "odi.folder": ODI_FOLDER,
            "odi.mapping": MAPPING_NAME,
            "odi.scenario.name": SCENARIO_NAME,
            "odi.scenario.version": SCENARIO_VERSION,
        }
        with groovy_approval_context(approved):
            output = self.run_groovy("generate_scenario_metadata.groovy", params)
        try:
            from repository_write_contract import parse_json
            return normalize_dim_product_scd2_scenario_generation(parse_json(output))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            return {
                "tool": "odi.dim_product_scd2_scenario_generation",
                "status": "error",
                "mode": "normalized_fixed_absent_only_scenario_generation",
                "side_effects": None,
                "repository_outcome": "unverified",
                "manual_recovery_review_required": True,
                "repository_connection_attempted": True,
                "raw_rows_included": False,
                "pk_values_included": False,
                "secret_printed": False,
                "error": "ODI scenario generation output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def prove_execution_identity(
        self, *, model_code: str, context_code: str, schema_name: str
    ) -> tuple[dict, object]:
        """Read one topology-bound execution account. The returned status omits that account."""
        from execution_identity import (
            interpret_execution_identity,
            public_blocked,
            validate_identity_request,
        )

        request = validate_identity_request(model_code, context_code, schema_name)
        if request is None:
            return public_blocked("execution_identity_invalid", connected=False), None
        output = self.run_groovy(
            "execution_identity_probe.groovy",
            {
                "odi.execution.model": request[0],
                "odi.execution.context": request[1],
                "odi.execution.schema": request[2],
            },
        )
        return interpret_execution_identity(
            output, model_code=request[0], context_code=request[1], schema_name=request[2]
        )

    def preflight_mapping(self, *, ikm_name: str, src_owner: str | None = None,
                          src_table: str | None = None, context: str = "GLOBAL", approved: bool = False) -> dict:
        """Read exact repository IKM availability and contextual physical source identity."""
        blocked = {"tool": "odi.mapping_preflight", "status": "blocked", "side_effects": False,
                   "repository_connection_attempted": False, "code": "approval_required"}
        if approved is not True:
            return blocked
        try:
            exact = resolve_approved_ikm(ikm_name, "IU", ODI_APPROVED_IKMS, ODI_IKM_PRIORITY)
        except ValueError:
            return {**blocked, "code": "ikm_not_approved"}
        from config import ODI_SOURCE_MODEL_BINDINGS
        from source_model_policy import SourceModelConfigError, trusted_source_model_code
        try:
            expected = trusted_source_model_code(context, src_owner, ODI_SOURCE_MODEL_BINDINGS)
        except SourceModelConfigError:
            return {**blocked, "code": "source_model_config_invalid"}
        try:
            if exact != ikm_name or bool(src_owner) != bool(src_table):
                raise ValueError("Invalid preflight request")
            for identifier in (context, src_owner, src_table):
                if identifier is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", identifier):
                    raise ValueError("Invalid preflight identifier")
            params = {"odi.ikm_name": exact, "odi.context": context}
            if src_owner:
                params.update({"odi.src_owner": src_owner, "odi.src_table": src_table})
            if expected:
                params["odi.source_model_code"] = expected
            with groovy_approval_context(True):
                output = self.run_groovy("mapping_preflight.groovy", params)
            return normalize_repository_preflight(
                json.loads(output), exact, src_owner, src_table, context, expected_model_code=expected,
            )
        except (ValueError, TypeError, RuntimeError, OSError):
            return {**blocked, "code": "repository_lookup_failed", "repository_connection_attempted": True}

    def read_only_probe(self, *,
                        approved: bool = False,
                        project: str = None,
                        folder: str = None,
                        mapping_name: str = None,
                        src_model: str = None,
                        src_table: str = None,
                        tgt_model: str = None,
                        tgt_table: str = None) -> dict:
        """ODI SDK finder/read-only metadata probe.

        This wrapper only calls read_only_probe.groovy. The Groovy script uses
        ODI SDK finders and returns redacted JSON metadata/checks; it must not
        persist, remove, commit, create, update, or delete repository objects.
        """
        if not approved:
            return {
                "tool": "odi.read_only_probe",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_REPOSITORY_READ_ONLY",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_repository_read_only_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only after the ODI_REPOSITORY_READ_ONLY gate is approved.",
                    }
                ],
            }

        params = {
            "odi.project": project or ODI_PROJECT,
            "odi.folder": folder or ODI_FOLDER,
        }
        optional_params = {
            "odi.mapping": mapping_name,
            "odi.src_model": src_model,
            "odi.src_table": src_table,
            "odi.tgt_model": tgt_model,
            "odi.tgt_table": tgt_table,
        }
        params.update({key: val for key, val in optional_params.items() if val})

        output = self.run_groovy("read_only_probe.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", False)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": False,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "read_only_probe_json",
                        "status": "fail",
                        "detail": "ODI read-only probe did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }

    def inspect_mapping_km_options(self, *,
                                   approved: bool = False,
                                   project: str = None,
                                   folder: str = None,
                                   mapping_name: str = None) -> dict:
        """Read current physical KM assignment/options for an ODI mapping.

        This is a read-only metadata probe. It must not update repository
        metadata, generate scenarios, run scenarios, or touch database data.
        """
        if not approved:
            return {
                "tool": "odi.inspect_mapping_km_options",
                "status": "blocked_until_approval",
                "side_effects": False,
                "repository_connection_attempted": False,
                "approval": {
                    "action": "ODI_REPOSITORY_READ_ONLY",
                    "required": True,
                    "status": "missing",
                },
                "checks": [
                    {
                        "name": "odi_repository_read_only_approval",
                        "status": "fail",
                        "detail": "Pass approved=True only after the ODI_REPOSITORY_READ_ONLY gate is approved.",
                    }
                ],
            }

        params = {
            "odi.project": project or ODI_PROJECT,
            "odi.folder": folder or ODI_FOLDER,
        }
        if mapping_name:
            params["odi.mapping"] = mapping_name

        output = self.run_groovy("inspect_mapping_km_options.groovy", params)
        try:
            start = output.find("{")
            if start > 0:
                output = output[start:]
            parsed = json.loads(output)
            parsed.setdefault("side_effects", False)
            parsed.setdefault("repository_connection_attempted", True)
            return parsed
        except Exception as exc:
            return {
                "status": "error",
                "side_effects": False,
                "repository_connection_attempted": True,
                "checks": [
                    {
                        "name": "inspect_mapping_km_options_json",
                        "status": "fail",
                        "detail": "ODI KM options probe did not return valid JSON.",
                    }
                ],
                "error": "ODI Groovy output parsing failed.",
                "error_type": type(exc).__name__,
            }


if __name__ == "__main__":
    orch = OdiOrchestrator()
    print(orch.generate_mapping("MAP_TEST", "ORACLE_HR", "EMPLOYEES", "ORACLE_ODS", "ODS_HR_EMPLOYEES", "NONE"))
