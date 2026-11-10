// Projection of a mapping-run response for the DataOps last-run panel.
// Only allowlisted fields are returned. A missing value stays "not reported".

export const NOT_REPORTED = 'not reported';

// Shown for the unknown class. Must not say "failed" or "not successful".
export const MAY_HAVE_RUN_TEXT =
  'The scenario may have started or completed. Check the run before running it again.';

// Replacement for any error text that is not an allowlisted sentence or code.
export const WITHHELD_ERROR = 'The server reported an error; details are withheld.';

// Added when that replacement is all the operator has and the response carried
// no batch id they can refresh. routes/mapping_write.py validation bodies and
// routes/error_responses.py redacted 500s omit batch_id.
export const HISTORY_CHECK_TEXT =
  'Check the run history for this mapping before trying again.';

// Fixed panel copy. The server warning sentence is not shown.
export const HISTORY_NOT_SAVED_TEXT =
  'The run history record for this run could not be saved. The outcome shown here is what the server reported.';

const SUCCESS_STATUSES = new Set(['SUCCESS', 'SUCCEEDED']);

// Closed set from remote_run_contract.ERRORS.
const REMOTE_ERROR_CODES = new Set([
  'ApprovalRequired',
  'InvalidRunIdentity',
  'AgentConfigurationUnavailable',
  'AgentUnavailable',
  'ScenarioOwnershipUnavailable',
  'RepositoryCleanupFailed',
  'ScenarioRunFailed',
  'ScenarioOutcomeUnknown',
  'RemoteOutputInvalid',
  'RemoteProcessFailed',
  'RemoteTimeout',
]);

// Closed `failed` set. Classification matches the whole shape
// (HTTP status + status + flags + sentence or code), not one field.
// `hasUncertaintyEvidence` runs before any class. A contradictory body is
// `unknown`. Anything that is not a proven success and not in this set is
// `unknown`.
//
// Sources for the shapes:
// - routes/mapping_write.py:208-221 and app.py:196-199 (validation / approval 400)
// - mapping_run_service.py:58-80 (validation sentences)
// - mapping_run_service.py:50-55,266-267 (409 fixed-mapping gate)
// - mapping_run_service.py:227-260 (409 IKM option unsupported)
// - mapping_run_service.py:292-296 (500 "Scenario option update failed.")
// - mapping_run_service.py:303-307 (400 "Scenario generation failed.")
// - mapping_run_service.py:276-280 and remote_run_contract.py:32-37,85-100
//   (health block: status blocked, remote.status blocked, three flags false;
//   any error_code, including RemoteTimeout, because the health probe is pre-run)
// - odi_automation/remote_agent_health.groovy:18-40 (health never starts a scenario)
// - mapping_run_service.py:314-323 and remote_run_contract.py:96-100
//   (502 run block and 502 resolved ERROR)
// - odi_automation/run_scenario_remote.groovy:78-135 (run-block codes, before start)
// - mapping_run_service.py:209-213,314-318 (persisted overall_status RUN_FAILED)
// - routes/error_responses.py:9-16 (redacted 500 is NOT in this set)

// Pre-run 400 sentences. The body is `{error}` with no status.
const VALIDATION_ERRORS = [
  'mapping_name is required.',
  'Invalid mapping_name.',
  'approved:true is required for ODI scenario run.',
  'truncate_target_table must be selected as true or false before running a scenario.',
  'Invalid scenario_version.',
  'Invalid context.',
];

// mapping_run_service.py:51-54
const FIXED_GATE_STATUS = 'blocked_fixed_mapping_cutover_pending';
const FIXED_GATE_ERROR = 'The fixed DIM_PRODUCT mapping cannot run through the generic mapping run path until a separately reviewed cutover proof replaces this gate.';

// mapping_run_service.py:295 and :306. Different HTTP statuses.
const OPTION_UPDATE_ERROR = 'Scenario option update failed.';
const GENERATION_ERROR = 'Scenario generation failed.';

// routes/error_responses.py:12 — known sentence, but not a proven pre-run failure.
const REDACTED_RUN_ERROR = 'mapping run failed.';

// mapping_run_service.py:245-247. Option token is the only interpolation.
const IKM_ERROR = /^The mapping's IKM does not expose the [A-Z][A-Z0-9_]{0,63} option\. Run with truncate_target_table=false or change the IKM\.$/;

// Run block codes from the reviewer's table, emitted before the scenario starts
// (run_scenario_remote.groovy:78-135; remote_run_contract.py blocked branch).
const RUN_BLOCK_CODES = new Set([
  'InvalidRunIdentity',
  'AgentConfigurationUnavailable',
  'AgentUnavailable',
  'ScenarioOwnershipUnavailable',
  'RepositoryCleanupFailed',
]);

// remote_run_contract.py:96-98. Flags are started / side_effects / not unknown.
const RESOLVED_ERROR = {
  httpStatus: 502,
  status: 'failed',
  sessionStatus: 'ERROR',
  errorCode: 'ScenarioRunFailed',
};

// Saved state read back with HTTP 200. Definite finished result.
const PERSISTED_RUN_FAILED = 'RUN_FAILED';

// `error` is rendered only when it is exactly one of these, or an allowlisted
// remote.error_code. Anything else is replaced with WITHHELD_ERROR.
const ALLOWED_ERRORS = new Set([
  ...VALIDATION_ERRORS,
  FIXED_GATE_ERROR,
  OPTION_UPDATE_ERROR,
  GENERATION_ERROR,
  REDACTED_RUN_ERROR,
]);

// Steps and status tokens. Not the backend name contract below.
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_]{0,80}$/;
// Mapping, scenario and context.
// app.py:172 `_IDENTIFIER_RE` = `^[A-Z][A-Z0-9_]*$`, applied after uppercasing
// in `_valid_identifier` (app.py:188-189).
// odi_orchestrator.py:1209 `re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", v)` and
// odi_automation/run_scenario_remote.groovy:87-88 `/[A-Z][A-Z0-9_]{0,127}/`
// cap that class at 128 characters. Display uses that exact remote pattern.
const BACKEND_IDENTIFIER = /^[A-Z][A-Z0-9_]{0,127}$/;
// Version.
// app.py:173 `_VERSION_RE` = `^[A-Za-z0-9_.-]+$` (`_valid_version`, app.py:192-193).
// odi_orchestrator.py:1210 and run_scenario_remote.groovy:88 use the same class
// with a length bound: `[A-Za-z0-9_.-]{1,12}`. Display uses that exact pattern,
// so `001.2` is shown and a longer value stays "not reported".
const BACKEND_VERSION = /^[A-Za-z0-9_.-]{1,12}$/;
const BATCH = /^RUN-[A-Z0-9]{2,32}$/;
const SESSION = /^(?:[0-9]{1,12}|[A-Za-z][A-Za-z0-9_-]{0,63})$/;
const UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/;
const ERROR_TYPE = /^[A-Za-z][A-Za-z0-9_]{0,79}$/;

function plainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function safeIdentifier(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!IDENTIFIER.test(text)) return null;
  return text;
}

function isSuccessWord(value) {
  return typeof value === 'string' && SUCCESS_STATUSES.has(value.trim().toUpperCase());
}

function isUnknownWord(value) {
  if (typeof value !== 'string') return false;
  const text = value.trim().toUpperCase();
  return text === 'UNKNOWN' || text === 'RUN_UNKNOWN';
}

function statusTokens(payload) {
  if (!payload) return [];
  const found = [];
  for (const key of ['overall_status', 'status']) {
    const text = safeIdentifier(payload[key]);
    if (text) found.push(text);
  }
  return found;
}

function resolvedHttp(httpStatus) {
  return Number.isInteger(httpStatus) && httpStatus >= 200 && httpStatus < 300;
}

function remoteOf(body) {
  return plainObject(body?.remote) ? body.remote : null;
}

// Health and run blocks require the three remote flags false. A true flag on
// the body or the remote is a different outcome (the scenario may have started).
function blockFlagsClear(body, remote) {
  return ['run_may_have_started', 'side_effects', 'outcome_unknown'].every((key) => (
    remote?.[key] === false && body?.[key] !== true
  ));
}

// Live finished success. remote_run_contract.py accepts `passed` only with
// session DONE, no error code, and outcome_unknown false.
function isDefiniteDoneSuccessBody(body) {
  const remote = remoteOf(body);
  if (!remote) return false;
  if (typeof body.status !== 'string' || body.status.trim().toUpperCase() !== 'SUCCESS') return false;
  if (body.overall_status != null && !isSuccessWord(body.overall_status)) return false;
  if (body.outcome_unknown === true) return false;
  if (typeof remote.status !== 'string' || remote.status.trim() !== 'passed') return false;
  if (remote.session_status !== 'DONE') return false;
  if (remote.error_code != null) return false;
  if (remote.outcome_unknown !== false) return false;
  return true;
}

// Resolved scenario failure. The session check is load-bearing: without it a
// DONE or missing session would be classified failed.
function isDefiniteResolvedErrorBody(body) {
  const remote = remoteOf(body);
  if (!remote) return false;
  if (body.status !== RESOLVED_ERROR.status || remote.status !== RESOLVED_ERROR.status) return false;
  if (remote.session_status !== RESOLVED_ERROR.sessionStatus) return false;
  if (remote.error_code !== RESOLVED_ERROR.errorCode) return false;
  if (remote.run_may_have_started !== true || remote.side_effects !== true) return false;
  if (remote.outcome_unknown !== false || body.outcome_unknown === true) return false;
  return true;
}

// True when the body contradicts a definite outcome. Start and side-effect
// flags are expected on the two finished shapes and nowhere else.
function hasUncertaintyEvidence(body) {
  if (!plainObject(body)) return false;
  const remote = remoteOf(body);
  const places = remote ? [body, remote] : [body];
  for (const place of places) {
    if (isUnknownWord(place.status) || isUnknownWord(place.overall_status)) return true;
    if (place.outcome_unknown === true) return true;
  }
  const started = body.run_may_have_started === true
    || body.side_effects === true
    || remote?.run_may_have_started === true
    || remote?.side_effects === true;
  if (!started) return false;
  return !isDefiniteDoneSuccessBody(body) && !isDefiniteResolvedErrorBody(body);
}

function contradictsMemberStatus(body, expectedStatus) {
  const statusSet = body.status != null;
  const overallSet = body.overall_status != null;
  if (expectedStatus == null) return statusSet || overallSet;
  if (body.status !== expectedStatus) return true;
  return overallSet;
}

function contradictsMemberRemote(body, allowsRemote) {
  return !allowsRemote && remoteOf(body) != null;
}

function acceptsPreRunMember(body, expectedStatus, allowsRemote) {
  return hasUncertaintyEvidence(body) === false
    && !contradictsMemberStatus(body, expectedStatus)
    && !contradictsMemberRemote(body, allowsRemote);
}

function isProvenSuccess(httpStatus, body) {
  return resolvedHttp(httpStatus) && isDefiniteDoneSuccessBody(body);
}

// Saved state read of a finished success: overall_status SUCCEEDED, no remote.
function isPersistedSucceeded(httpStatus, body) {
  if (!resolvedHttp(httpStatus) || body.overall_status !== 'SUCCEEDED') return false;
  if (remoteOf(body)) return false;
  if (hasUncertaintyEvidence(body)) return false;
  if (body.run_may_have_started === true || body.side_effects === true || body.outcome_unknown === true) {
    return false;
  }
  if (body.status != null && !isSuccessWord(body.status)) return false;
  return true;
}

function isValidationFailure(httpStatus, body) {
  return httpStatus === 400
    && VALIDATION_ERRORS.includes(body.error)
    && acceptsPreRunMember(body, null, false);
}

function isFixedGate(httpStatus, body) {
  return httpStatus === 409
    && body.error === FIXED_GATE_ERROR
    && acceptsPreRunMember(body, FIXED_GATE_STATUS, false);
}

function isIkmFailure(httpStatus, body) {
  return httpStatus === 409
    && typeof body.error === 'string'
    && IKM_ERROR.test(body.error)
    && acceptsPreRunMember(body, 'ikm_option_unsupported', false);
}

function isOptionUpdateFailure(httpStatus, body) {
  return httpStatus === 500
    && body.error === OPTION_UPDATE_ERROR
    && acceptsPreRunMember(body, null, false);
}

function isGenerationFailure(httpStatus, body) {
  return httpStatus === 400
    && body.error === GENERATION_ERROR
    && acceptsPreRunMember(body, null, false);
}

function isHealthBlock(httpStatus, body) {
  const remote = remoteOf(body);
  if ((httpStatus !== 400 && httpStatus !== 503) || !remote) return false;
  if (!acceptsPreRunMember(body, 'blocked', true) || remote.status !== 'blocked') return false;
  if (!blockFlagsClear(body, remote) || remote.session_status != null) return false;
  // Any code, including RemoteTimeout. The health probe does not start a run.
  return typeof remote.error_code === 'string' && remote.error_code.length > 0;
}

function isRunBlock(httpStatus, body) {
  const remote = remoteOf(body);
  if (httpStatus !== 502 || !remote) return false;
  if (!acceptsPreRunMember(body, 'blocked', true) || remote.status !== 'blocked') return false;
  if (!blockFlagsClear(body, remote) || remote.session_status != null) return false;
  return RUN_BLOCK_CODES.has(remote.error_code);
}

function isResolvedRunError(httpStatus, body) {
  return httpStatus === RESOLVED_ERROR.httpStatus && isDefiniteResolvedErrorBody(body);
}

function isPersistedRunFailed(httpStatus, body) {
  if (!resolvedHttp(httpStatus) || body.overall_status !== PERSISTED_RUN_FAILED) return false;
  // Saved-state shape only: the state endpoint does not return remote or flags.
  if (remoteOf(body)) return false;
  if (body.run_may_have_started === true || body.side_effects === true || body.outcome_unknown === true) {
    return false;
  }
  if (isUnknownWord(body.status) || isUnknownWord(body.overall_status)) return false;
  if (statusTokens(body).some(isSuccessWord)) return false;
  return true;
}

function isClosedFailure(httpStatus, body) {
  return isValidationFailure(httpStatus, body)
    || isFixedGate(httpStatus, body)
    || isIkmFailure(httpStatus, body)
    || isOptionUpdateFailure(httpStatus, body)
    || isGenerationFailure(httpStatus, body)
    || isHealthBlock(httpStatus, body)
    || isRunBlock(httpStatus, body)
    || isResolvedRunError(httpStatus, body)
    || isPersistedRunFailed(httpStatus, body);
}

// Uncertainty is decided before any class. A proven success is only the
// finished DONE shape or a saved SUCCEEDED with no remote. `failed` is only
// the closed set above. Every other body, including a redacted 500, 2xx {},
// 2xx partial, a success claim that is not the finished shape, a non-2xx
// SUCCESS, remote unknown, HTTP 504, and a missing body, is `unknown`.
function classifyOutcome(httpStatus, body) {
  if (!body) return 'unknown';
  if (hasUncertaintyEvidence(body)) return 'unknown';
  if (isProvenSuccess(httpStatus, body) || isPersistedSucceeded(httpStatus, body)) return 'succeeded';
  if (isClosedFailure(httpStatus, body)) return 'failed';
  return 'unknown';
}

function displayStatus(body, outcomeClass) {
  if (!body) return NOT_REPORTED;
  const tokens = statusTokens(body);
  if (outcomeClass === 'succeeded') return tokens.find(isSuccessWord) || NOT_REPORTED;
  const visible = tokens.filter((value) => {
    if (isSuccessWord(value)) return false;
    // The unknown class must not be labelled with a failure word.
    if (outcomeClass === 'unknown' && /fail/i.test(value)) return false;
    return true;
  });
  return visible[0] || NOT_REPORTED;
}

function projectBackendIdentifier(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return BACKEND_IDENTIFIER.test(text) ? text : null;
}

function projectVersion(value) {
  if (typeof value === 'number' && Number.isInteger(value) && value >= 0) {
    const text = String(value);
    return BACKEND_VERSION.test(text) ? text : null;
  }
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return BACKEND_VERSION.test(text) ? text : null;
}

function projectBatch(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (BATCH.test(text) || UUID.test(text)) return text;
  return null;
}

function projectSession(payload) {
  if (!payload) return NOT_REPORTED;
  for (const key of ['session_id', 'odi_session_id', 'session_number']) {
    const value = payload[key];
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 999999999) {
      return String(value);
    }
    if (typeof value !== 'string') continue;
    const text = value.trim();
    if (SESSION.test(text) || UUID.test(text)) return text;
  }
  return NOT_REPORTED;
}

function allowedErrorText(value) {
  return typeof value === 'string' && (ALLOWED_ERRORS.has(value) || IKM_ERROR.test(value));
}

function allowedRemoteCode(payload) {
  const code = plainObject(payload?.remote) ? payload.remote.error_code : null;
  return typeof code === 'string' && REMOTE_ERROR_CODES.has(code) ? code : null;
}

function errorTypeSuffix(payload, thrown) {
  const raw = payload?.error_type ?? thrown?.errorType;
  if (typeof raw === 'string' && ERROR_TYPE.test(raw)) return ` error_type=${raw}`;
  return '';
}

function withheldError(payload, thrown) {
  return `${WITHHELD_ERROR}${errorTypeSuffix(payload, thrown)}`;
}

function projectError(payload, thrown) {
  if (!payload) return NOT_REPORTED;
  const hasError = Object.prototype.hasOwnProperty.call(payload, 'error')
    && payload.error != null
    && payload.error !== '';
  if (hasError) {
    if (allowedErrorText(payload.error)) return payload.error;
    return withheldError(payload, thrown);
  }
  const code = allowedRemoteCode(payload);
  if (code) return code;
  const rawCode = plainObject(payload.remote) ? payload.remote.error_code : null;
  if (rawCode != null && rawCode !== '') return withheldError(payload, thrown);
  return NOT_REPORTED;
}

function errorForClass(error, outcomeClass, payload, thrown) {
  if (outcomeClass !== 'unknown' || error === NOT_REPORTED) return error;
  if (/\bfailed\b/i.test(error) || /not successful/i.test(error) || /fail/i.test(error)) {
    return withheldError(payload, thrown);
  }
  return error;
}

function finishError(error, payload) {
  if (typeof error !== 'string' || !error.startsWith(WITHHELD_ERROR)) return error;
  if (projectBatch(payload?.batch_id)) return error;
  if (error.includes(HISTORY_CHECK_TEXT)) return error;
  const suffix = error.slice(WITHHELD_ERROR.length);
  return `${WITHHELD_ERROR} ${HISTORY_CHECK_TEXT}${suffix}`;
}

function requestedFields(request) {
  if (!request || typeof request !== 'object') {
    return { requestedMapping: null, requestedRunMode: null };
  }
  const mapping = typeof request.mapping_name === 'string'
    ? request.mapping_name.split('\0').join('').replace(/[\r\n]/g, '').trim().slice(0, 128)
    : '';
  if (!mapping || (request.truncate_target_table !== true && request.truncate_target_table !== false)) {
    return { requestedMapping: mapping || null, requestedRunMode: null };
  }
  return {
    requestedMapping: mapping,
    requestedRunMode: request.truncate_target_table === true ? 'truncated' : 'kept rows',
  };
}

export function projectMappingRunOutcome({
  httpStatus = null,
  payload = null,
  thrown = null,
  request = null,
  generation = null,
} = {}) {
  const body = plainObject(payload) ? payload : null;
  const outcomeClass = classifyOutcome(httpStatus, body);
  const requested = requestedFields(request);
  const serverMode = body?.truncate_target_table === true
    ? 'truncated'
    : body?.truncate_target_table === false
      ? 'kept rows'
      : NOT_REPORTED;

  return {
    outcomeClass,
    successful: outcomeClass === 'succeeded',
    // Only a top-level boolean true. It does not change the class.
    historyNotSaved: body?.state_persist_failed === true,
    generation: Number.isInteger(generation) ? generation : null,
    requestedMapping: requested.requestedMapping,
    requestedRunMode: requested.requestedRunMode,
    mapping: projectBackendIdentifier(body?.mapping_name) || NOT_REPORTED,
    scenarioName: projectBackendIdentifier(body?.scenario_name) || projectBackendIdentifier(body?.scenario) || NOT_REPORTED,
    scenarioVersion: projectVersion(body?.scenario_version) || projectVersion(body?.version) || NOT_REPORTED,
    context: projectBackendIdentifier(body?.context) || NOT_REPORTED,
    runMode: serverMode,
    overallStatus: displayStatus(body, outcomeClass),
    failedStep: outcomeClass === 'succeeded'
      ? NOT_REPORTED
      : (safeIdentifier(body?.current_step) || NOT_REPORTED),
    error: finishError(errorForClass(projectError(body, thrown), outcomeClass, body, thrown), body),
    batchId: projectBatch(body?.batch_id) || NOT_REPORTED,
    sessionId: projectSession(body),
    // MappingRunSuccess / MappingRunFailure define no count fields.
    counts: NOT_REPORTED,
  };
}

function resultText(outcome) {
  if (outcome?.outcomeClass === 'succeeded') return 'successful';
  if (outcome?.outcomeClass === 'unknown') return MAY_HAVE_RUN_TEXT;
  return 'not successful';
}

export function mappingRunOutcomeLines(outcome) {
  const lines = [['Result', resultText(outcome)]];
  if (outcome?.requestedMapping) lines.push(['Requested mapping', outcome.requestedMapping]);
  if (outcome?.requestedRunMode) lines.push(['Requested run mode', outcome.requestedRunMode]);
  lines.push(
    ['Mapping', outcome.mapping],
    ['Scenario', outcome.scenarioName],
    ['Version', outcome.scenarioVersion],
    ['Context', outcome.context],
    ['Run mode', outcome.runMode],
    ['Overall status', outcome.overallStatus],
    [outcome?.outcomeClass === 'unknown' ? 'Step' : 'Failed step', outcome.failedStep],
    ['Error', outcome.error],
    ['Batch', outcome.batchId],
    ['Session', outcome.sessionId],
    ['Counts', outcome.counts],
  );
  return lines;
}

export function mappingRunBadge(outcome) {
  const historyNotice = outcome?.historyNotSaved === true ? HISTORY_NOT_SAVED_TEXT : null;
  if (outcome?.outcomeClass === 'succeeded') {
    return { label: 'successful', tone: 'success', notice: null, historyNotice };
  }
  if (outcome?.outcomeClass === 'unknown') {
    return { label: 'unconfirmed', tone: 'warning', notice: MAY_HAVE_RUN_TEXT, historyNotice };
  }
  return { label: 'not successful', tone: 'failure', notice: null, historyNotice };
}

export function mappingRunToast(outcome, mappingName) {
  if (outcome?.outcomeClass === 'succeeded') {
    const name = typeof mappingName === 'string' && mappingName.trim()
      ? mappingName.trim()
      : outcome.requestedMapping;
    if (outcome.historyNotSaved === true) {
      return {
        type: 'success',
        message: `${name} scenario ran successfully, but its history record could not be saved.`,
      };
    }
    const mode = outcome.requestedRunMode === 'truncated' ? 'with target truncate' : 'without target truncate';
    return { type: 'success', message: `${name} scenario ran successfully ${mode}.` };
  }
  if (outcome?.outcomeClass === 'unknown') {
    return { type: 'warning', message: MAY_HAVE_RUN_TEXT };
  }
  const reason = outcome?.error && outcome.error !== NOT_REPORTED
    ? outcome.error
    : outcome?.overallStatus && outcome.overallStatus !== NOT_REPORTED
      ? outcome.overallStatus
      : '';
  return { type: 'error', message: reason ? `Scenario run failed: ${reason}` : 'Scenario run failed' };
}

function sameBatch(record, batchId) {
  if (!batchId || batchId === NOT_REPORTED) return false;
  return projectBatch(record?.batch_id) === batchId;
}

function selectSameRun(current, response) {
  if (!current || current.batchId === NOT_REPORTED || !plainObject(response)) return null;
  if (plainObject(response.state) && sameBatch(response.state, current.batchId)) return response.state;
  if (Array.isArray(response.runs)) {
    return response.runs.find((run) => sameBatch(run, current.batchId)) || null;
  }
  if (sameBatch(response, current.batchId)) return response;
  return null;
}

function safeMappingName(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim().toUpperCase();
  return BACKEND_IDENTIFIER.test(text) ? text : null;
}

function returnedMappingMatches(current, response, record, mappingName) {
  const reported = [];
  if (typeof response?.mapping_name === 'string' && response.mapping_name.trim()) {
    const name = safeMappingName(response.mapping_name);
    if (!name) return false;
    reported.push(name);
  }
  if (typeof record?.mapping_name === 'string' && record.mapping_name.trim()) {
    const name = safeMappingName(record.mapping_name);
    if (!name) return false;
    reported.push(name);
  }
  if (reported.length === 0 || reported.some((name) => name !== reported[0])) return false;

  const wanted = [safeMappingName(mappingName), safeMappingName(current?.requestedMapping)];
  if (current?.mapping && current.mapping !== NOT_REPORTED) wanted.push(safeMappingName(current.mapping));
  const expected = wanted.filter(Boolean);
  if (expected.length === 0) return true;
  return expected.every((name) => name === reported[0]);
}

function sameRunPanel(started, current) {
  if (started === current) return true;
  return started.generation === current.generation
    && started.batchId === current.batchId
    && started.requestedMapping === current.requestedMapping
    && started.requestedRunMode === current.requestedRunMode;
}

function requestFromOutcome(current) {
  if (typeof current?.requestedMapping !== 'string' || !current.requestedMapping) return null;
  if (current.requestedRunMode !== 'truncated' && current.requestedRunMode !== 'kept rows') return null;
  return {
    mapping_name: current.requestedMapping,
    truncate_target_table: current.requestedRunMode === 'truncated',
  };
}

function keepShownError(current, next) {
  if (!current || current.error === NOT_REPORTED) return next.error;
  // Saved state rewrites the public error to "Operation failed." or drops it.
  // Keep the allowlisted HTTP text instead of a withheld replacement.
  if (
    next.error === NOT_REPORTED
    || next.error === WITHHELD_ERROR
    || next.error.startsWith(`${WITHHELD_ERROR} `)
    || next.error === 'Operation failed.'
    || next.error.startsWith('Operation failed. ')
  ) {
    return current.error;
  }
  return next.error;
}

export function decideRunRefresh({ started, current, response, mappingName = null } = {}) {
  if (!started || !current) {
    return { applied: false, reason: 'closed', outcome: current ?? null };
  }
  if (!sameRunPanel(started, current)) {
    return { applied: false, reason: 'superseded', outcome: current };
  }
  const record = selectSameRun(current, response);
  if (!record || !returnedMappingMatches(current, response, record, mappingName)) {
    return { applied: false, reason: 'mismatch', outcome: current };
  }
  const next = projectMappingRunOutcome({
    httpStatus: 200,
    payload: record,
    request: requestFromOutcome(current),
    generation: current.generation,
  });
  return {
    applied: true,
    reason: 'applied',
    outcome: {
      ...next,
      error: keepShownError(current, next),
      requestedMapping: current.requestedMapping,
      requestedRunMode: current.requestedRunMode,
      generation: current.generation,
    },
  };
}

export function projectRunRefresh(started, response, panelNow = started) {
  return decideRunRefresh({
    started,
    current: panelNow,
    response,
    mappingName: started?.requestedMapping || null,
  });
}

export function applyRefreshToPanels(panels, name, started, response) {
  const source = panels && typeof panels === 'object' ? panels : {};
  const current = Object.prototype.hasOwnProperty.call(source, name) ? source[name] : null;
  const decision = decideRunRefresh({ started, current, response, mappingName: name });
  if (!decision.applied) return { ...decision, panels: source };
  return { ...decision, panels: { ...source, [name]: decision.outcome } };
}

export function reconcileLastRuns(lastRuns, mappingNames) {
  const source = lastRuns && typeof lastRuns === 'object' ? lastRuns : {};
  const keep = new Set(Array.isArray(mappingNames) ? mappingNames : []);
  let removed = false;
  const next = {};
  for (const [name, outcome] of Object.entries(source)) {
    if (keep.has(name)) next[name] = outcome;
    else removed = true;
  }
  return removed ? next : source;
}
