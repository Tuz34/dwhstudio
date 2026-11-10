// Projection of a mapping-run response for the DataOps last-run panel.
// Only allowlisted fields are returned. A missing value stays "not reported".

export const NOT_REPORTED = 'not reported';

// Shown for the unknown class. Must not say "failed" or "not successful".
export const MAY_HAVE_RUN_TEXT =
  'The scenario may have started or completed. Check the run before running it again.';

// Replacement for any error text that is not an allowlisted sentence or code.
export const WITHHELD_ERROR = 'The server reported an error; details are withheld.';

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

// Fixed sentences emitted for POST /api/mapping/run. `error` is rendered only
// when it is exactly one of these, or an allowlisted remote.error_code.
// Anything else is replaced with WITHHELD_ERROR.
//
// Sources:
// - routes/mapping_write.py api_mapping_run:
//   "mapping_name is required.", "Invalid mapping_name.", and the
//   validation_error strings from mapping_run_service.py below.
// - app.py _approval_error, called by that route with action "ODI scenario run":
//   "approved:true is required for ODI scenario run."
// - mapping_run_service.py parse_required_bool / validate_mapping_run_options:
//   "truncate_target_table must be selected as true or false before running a scenario.",
//   "Invalid scenario_version.", "Invalid context."
// - mapping_run_service.py _CUTOVER_GATE_ERROR (409 gate constant).
// - mapping_run_service.py _failure_outcome public_error values:
//   "Scenario option update failed.", "Scenario generation failed."
// - mapping_run_service.py _unsupported_option_outcome. The option token is the
//   only interpolation and matches [A-Z][A-Z0-9_]{0,63} (_unsupported_ikm_option).
// - routes/error_responses.py redacted_error via redacted_write_error("mapping run"):
//   "mapping run failed."
const ALLOWED_ERRORS = new Set([
  'mapping_name is required.',
  'Invalid mapping_name.',
  'approved:true is required for ODI scenario run.',
  'truncate_target_table must be selected as true or false before running a scenario.',
  'Invalid scenario_version.',
  'Invalid context.',
  'The fixed DIM_PRODUCT mapping cannot run through the generic mapping run path until a separately reviewed cutover proof replaces this gate.',
  'Scenario option update failed.',
  'Scenario generation failed.',
  'mapping run failed.',
]);

const IKM_ERROR = /^The mapping's IKM does not expose the [A-Z][A-Z0-9_]{0,63} option\. Run with truncate_target_table=false or change the IKM\.$/;

// Identifier, UUID-like value, or short token. Dots and free text stay hidden.
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_]{0,80}$/;
const VERSION = /^(?:[0-9]{1,6}|[A-Za-z][A-Za-z0-9_]{0,31})$/;
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

function isDefiniteFailureWord(value, remote = false) {
  if (typeof value !== 'string') return false;
  const text = value.trim();
  if (!IDENTIFIER.test(text) || isSuccessWord(text) || isUnknownWord(text)) return false;
  // remote_run_contract uses "passed" for a completed healthy run.
  if (remote && text.toLowerCase() === 'passed') return false;
  return true;
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

function flagTrue(body, remote, key) {
  return body?.[key] === true || remote?.[key] === true;
}

function resolvedHttp(httpStatus) {
  return Number.isInteger(httpStatus) && httpStatus >= 200 && httpStatus < 300;
}

// `succeeded` is only a resolved 2xx whose status says success.
// `failed` is a definite failure or block the server reported.
// `unknown` covers remote status unknown, RUN_UNKNOWN, RemoteTimeout, HTTP 504,
// outcome_unknown, and run_may_have_started / side_effects when the server did
// not also report a definite result. No response body is unknown too.
// run_may_have_started does not override a definite success or failure: the
// remote contract sets it for a finished DONE session and a finished ERROR session.
function classifyOutcome(httpStatus, body) {
  if (!body) return 'unknown';

  const remote = plainObject(body.remote) ? body.remote : null;
  const top = statusTokens(body);
  const remoteStatus = typeof remote?.status === 'string' ? remote.status.trim() : '';
  const outcomeUnknown = flagTrue(body, remote, 'outcome_unknown');
  const mayHaveRun = flagTrue(body, remote, 'run_may_have_started') || flagTrue(body, remote, 'side_effects');
  const remoteTimeout = httpStatus === 504
    || body.error_code === 'RemoteTimeout'
    || remote?.error_code === 'RemoteTimeout';
  const statusUnknown = top.some(isUnknownWord) || isUnknownWord(remoteStatus);

  if (outcomeUnknown || remoteTimeout || statusUnknown) return 'unknown';

  const resolved = resolvedHttp(httpStatus);
  const successClaim = top.some(isSuccessWord) || isSuccessWord(remoteStatus);
  if (!resolved && successClaim) return 'unknown';

  const remotePassed = remoteStatus === '' || isSuccessWord(remoteStatus) || remoteStatus.toLowerCase() === 'passed';
  if (resolved && top.length > 0 && top.every(isSuccessWord) && remotePassed) return 'succeeded';

  if (top.some((value) => isDefiniteFailureWord(value)) || isDefiniteFailureWord(remoteStatus, true)) {
    return 'failed';
  }
  if (mayHaveRun) return 'unknown';
  return 'failed';
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

function projectVersion(value) {
  if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 999999) {
    return String(value);
  }
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return VERSION.test(text) ? text : null;
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
    generation: Number.isInteger(generation) ? generation : null,
    requestedMapping: requested.requestedMapping,
    requestedRunMode: requested.requestedRunMode,
    mapping: safeIdentifier(body?.mapping_name) || NOT_REPORTED,
    scenarioName: safeIdentifier(body?.scenario_name) || safeIdentifier(body?.scenario) || NOT_REPORTED,
    scenarioVersion: projectVersion(body?.scenario_version) || projectVersion(body?.version) || NOT_REPORTED,
    context: safeIdentifier(body?.context) || NOT_REPORTED,
    runMode: serverMode,
    overallStatus: displayStatus(body, outcomeClass),
    failedStep: outcomeClass === 'succeeded'
      ? NOT_REPORTED
      : (safeIdentifier(body?.current_step) || NOT_REPORTED),
    error: errorForClass(projectError(body, thrown), outcomeClass, body, thrown),
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
  if (outcome?.outcomeClass === 'succeeded') {
    return { label: 'successful', tone: 'success', notice: null };
  }
  if (outcome?.outcomeClass === 'unknown') {
    return { label: 'unconfirmed', tone: 'warning', notice: MAY_HAVE_RUN_TEXT };
  }
  return { label: 'not successful', tone: 'failure', notice: null };
}

export function mappingRunToast(outcome, mappingName) {
  if (outcome?.outcomeClass === 'succeeded') {
    const mode = outcome.requestedRunMode === 'truncated' ? 'with target truncate' : 'without target truncate';
    const name = typeof mappingName === 'string' && mappingName.trim()
      ? mappingName.trim()
      : outcome.requestedMapping;
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
  const text = safeIdentifier(value);
  return text ? text.toUpperCase() : null;
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
