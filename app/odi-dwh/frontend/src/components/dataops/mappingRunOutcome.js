// Projection of a mapping-run response for the DataOps last-run panel.
// Only allowlisted fields are returned. A missing value stays "not reported".

export const NOT_REPORTED = 'not reported';

const SUCCESS_STATUSES = new Set(['SUCCESS', 'SUCCEEDED']);

// Closed set from remote_run_contract.ERRORS. Anything else stays hidden.
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

const COUNT_KEYS = [
  'row_count',
  'insert_count',
  'update_count',
  'delete_count',
  'source_count',
  'target_count',
  'record_count',
  'loaded_count',
];

const UNSAFE_TEXT = /password\s*[:=]|passwd\s*[:=]|pwd\s*[:=]|secret\s*[:=]|token\s*[:=]|jdbc:oracle|\bdsn\s*[:=]|connect_string|connection_string/i;
const TOKEN = /^[A-Za-z0-9_.:-]{1,128}$/;
const COUNT_KEY = /^[A-Za-z][A-Za-z0-9_]{0,40}$/;

function plainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function sentence(value, max = 500) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text || text.length > max || /[\r\n]/.test(text) || UNSAFE_TEXT.test(text)) return null;
  return text;
}

function token(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!TOKEN.test(text) || UNSAFE_TEXT.test(text)) return null;
  return text;
}

function asPayload(value) {
  return plainObject(value) ? value : null;
}

function statusTokens(payload) {
  if (!payload) return [];
  const found = [];
  for (const key of ['overall_status', 'status']) {
    const text = token(payload[key]);
    if (text) found.push(text);
  }
  return found;
}

function displayStatus(payload) {
  if (!payload) return NOT_REPORTED;
  return token(payload.overall_status) || token(payload.status) || NOT_REPORTED;
}

function httpFailed(httpStatus) {
  return typeof httpStatus === 'number'
    && (!Number.isInteger(httpStatus) || httpStatus < 200 || httpStatus >= 300);
}

function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function projectSession(payload) {
  if (!payload) return NOT_REPORTED;
  for (const key of ['session_id', 'odi_session_id', 'session_number']) {
    const value = payload[key];
    if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 999999999) {
      return String(value);
    }
    const text = token(value);
    if (text) return text;
  }
  return NOT_REPORTED;
}

function projectCounts(payload) {
  if (!payload) return NOT_REPORTED;
  const parts = [];
  for (const key of COUNT_KEYS) {
    if (finiteNumber(payload[key])) parts.push(`${key}=${payload[key]}`);
  }
  if (plainObject(payload.counts)) {
    for (const key of Object.keys(payload.counts).sort()) {
      if (!COUNT_KEY.test(key) || !finiteNumber(payload.counts[key])) continue;
      parts.push(`${key}=${payload.counts[key]}`);
    }
  }
  return parts.length ? parts.join(', ') : NOT_REPORTED;
}

function projectError(payload, thrown) {
  const fromPayload = payload ? sentence(payload.error) : null;
  if (fromPayload) return fromPayload;
  const code = payload && plainObject(payload.remote) ? payload.remote.error_code : null;
  if (typeof code === 'string' && REMOTE_ERROR_CODES.has(code)) return code;
  if (!payload && thrown && typeof thrown === 'object') {
    const message = sentence(thrown.message);
    if (message) return message;
  }
  return NOT_REPORTED;
}

export function projectMappingRunOutcome({ httpStatus = null, payload = null, thrown = null } = {}) {
  const body = asPayload(payload);
  const statuses = statusTokens(body);
  const successful = !httpFailed(httpStatus)
    && statuses.length > 0
    && statuses.every((value) => SUCCESS_STATUSES.has(value.toUpperCase()));

  return {
    successful,
    mapping: token(body?.mapping_name) || NOT_REPORTED,
    scenarioName: token(body?.scenario_name) || token(body?.scenario) || NOT_REPORTED,
    scenarioVersion: token(body?.scenario_version) || token(body?.version) || NOT_REPORTED,
    context: token(body?.context) || NOT_REPORTED,
    runMode: body?.truncate_target_table === true
      ? 'truncated'
      : body?.truncate_target_table === false
        ? 'kept rows'
        : NOT_REPORTED,
    overallStatus: displayStatus(body),
    failedStep: successful ? NOT_REPORTED : (token(body?.current_step) || NOT_REPORTED),
    error: projectError(body, thrown),
    batchId: token(body?.batch_id) || NOT_REPORTED,
    sessionId: projectSession(body),
    counts: projectCounts(body),
  };
}

export function mappingRunOutcomeLines(outcome) {
  return [
    ['Result', outcome.successful ? 'successful' : 'not successful'],
    ['Mapping', outcome.mapping],
    ['Scenario', outcome.scenarioName],
    ['Version', outcome.scenarioVersion],
    ['Context', outcome.context],
    ['Run mode', outcome.runMode],
    ['Overall status', outcome.overallStatus],
    ['Failed step', outcome.failedStep],
    ['Error', outcome.error],
    ['Batch', outcome.batchId],
    ['Session', outcome.sessionId],
    ['Counts', outcome.counts],
  ];
}

function sameBatch(record, batchId) {
  return token(record?.batch_id) === batchId;
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

function keepShownError(current, next) {
  if (!current || current.error === NOT_REPORTED) return next.error;
  // Persisted state reduces the public error to this prefix. Keep the HTTP text.
  if (next.error === NOT_REPORTED || next.error === 'Operation failed.' || next.error.startsWith('Operation failed. ')) {
    return current.error;
  }
  return next.error;
}

export function projectRunRefresh(current, response) {
  const record = selectSameRun(current, response);
  if (!record) return { applied: false, outcome: current };
  const next = projectMappingRunOutcome({ httpStatus: 200, payload: record });
  return {
    applied: true,
    outcome: { ...next, error: keepShownError(current, next) },
  };
}
