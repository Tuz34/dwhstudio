/**
 * Pure request and response helpers for the Type 2 (SCD2) wizard.
 * Field names follow scd2_builder.validate_request. `approved: true` is added
 * only by the generate and run builders, as a boolean, never from prefill.
 */

const IDENTIFIER_RE = /^[A-Z0-9_$#]{1,30}$/;
const MAPPING_NAME_RE = /^[A-Z0-9_$#]{1,31}$/;
const MAPPING_NAME_MAX = 31;
const SCENARIO_VERSION_RE = /^[A-Za-z0-9._-]{1,12}$/;

export const SOURCE_OWNERS = ['DWH', 'DM', 'HR', 'SH'];
export const TARGET_OWNERS = ['DWH', 'DM'];

const VALIDITY_NAMES = new Set(['EFFECTIVE_FROM', 'EFFECTIVE_TO', 'CURRENT_FLAG']);
const VALIDITY_ROLES = new Set(['START_TIMESTAMP', 'END_TIMESTAMP', 'CURRENT_RECORD_FLAG']);
const COUNT_LABELS = {
  target_total: 'Total rows',
  target_current: 'Current rows',
  rows_with_effective_to_set: 'Closed rows',
};
const COUNT_ORDER = ['target_total', 'target_current', 'rows_with_effective_to_set'];

// Flags returned by scd2_postflight.collect_stock_postflight. A missing flag is
// "not reported". `*_proven` false means the check was not proven, not a mismatch.
const EVIDENCE_FLAGS = [
  ['source_current_count_equal', 'Source equals current'],
  ['business_parity_proven', 'Business parity proven'],
  ['physical_noop_proven', 'Physical no-op proven'],
  ['history_overlap_checked', 'History overlap checked'],
  ['stock_contract_valid', 'Stock contract valid'],
];

const PREFILL_NOTES = {
  source_owner: 'The suggested source owner was invalid.',
  source_table: 'The suggested source table was invalid.',
  target_owner: 'The suggested target owner was invalid.',
  target_table: 'The suggested target table was invalid.',
  mapping_name: 'The suggested mapping name was invalid.',
  natural_key_text: 'The suggested key was invalid.',
};

export function emptyScd2Form() {
  return {
    source_owner: '',
    source_table: '',
    target_owner: '',
    target_table: '',
    mapping_name: '',
    natural_key_text: '',
    tracked_mode: 'list',
    tracked_text: '',
    overwrite_text: '',
    sequence_name: '',
    grant_select_to_app_user: false,
    prefillNotes: {},
  };
}

function upper(value) {
  return String(value ?? '').trim().toUpperCase();
}

function parseColumnList(text) {
  return String(text ?? '')
    .split(/[\s,]+/)
    .map(item => item.trim().toUpperCase())
    .filter(Boolean);
}

function identifierError(value, label) {
  if (!value) return `${label} is required.`;
  if (value.length <= 30 && /^[A-Z0-9_$#]+$/.test(value) && value.includes('$')) {
    return `${label} cannot contain $.`;
  }
  if (!IDENTIFIER_RE.test(value) || value.includes('$')) {
    return `${label} must be an Oracle identifier.`;
  }
  return '';
}

function listError(values, { required, label }) {
  if (!values.length) return required ? `${label} is required.` : '';
  const seen = new Set();
  for (const value of values) {
    const problem = identifierError(value, label);
    if (problem) return problem;
    if (seen.has(value)) return `${label} must be unique.`;
    seen.add(value);
  }
  return '';
}

function mappingNameError(value) {
  if (!value) return 'Mapping name is required.';
  if (value.length > MAPPING_NAME_MAX && /^[A-Z0-9_$#]+$/.test(value)) {
    return 'Mapping name must be 31 characters or fewer.';
  }
  if (!MAPPING_NAME_RE.test(value) || value.includes('$')) {
    return 'Mapping name must be an Oracle identifier.';
  }
  return '';
}

function overlap(left, right) {
  const other = new Set(right);
  return left.filter(name => other.has(name));
}

function isSingleIdentifier(value, { mapping = false, owners = null } = {}) {
  if (typeof value !== 'string' || value.length === 0 || value !== value.trim()) return false;
  const token = value.toUpperCase();
  const pattern = mapping ? MAPPING_NAME_RE : IDENTIFIER_RE;
  if (!pattern.test(token) || token.includes('$')) return false;
  if (owners && !owners.includes(token)) return false;
  return true;
}

function assignPrefillIdentifier(next, field, value, options = {}) {
  if (value == null || value === '') return;
  if (!isSingleIdentifier(value, options)) {
    next[field] = '';
    next.prefillNotes[field] = PREFILL_NOTES[field];
    return;
  }
  next[field] = options.uppercase ? value.toUpperCase() : value;
  delete next.prefillNotes[field];
}

function assignPrefillKey(next, columns) {
  if (!Array.isArray(columns) || columns.length === 0) return;
  if (!columns.every(item => isSingleIdentifier(item))) {
    next.natural_key_text = '';
    next.prefillNotes.natural_key_text = PREFILL_NOTES.natural_key_text;
    return;
  }
  next.natural_key_text = columns.join(', ');
  delete next.prefillNotes.natural_key_text;
}

export function applyPrefill(form, prefill) {
  const next = { ...emptyScd2Form(), ...form };
  next.prefillNotes = { ...(form?.prefillNotes || {}) };
  if (!prefill || typeof prefill !== 'object' || Array.isArray(prefill)) return next;
  if ('source_schema' in prefill) assignPrefillIdentifier(next, 'source_owner', prefill.source_schema, { owners: SOURCE_OWNERS, uppercase: true });
  if ('source_table' in prefill) assignPrefillIdentifier(next, 'source_table', prefill.source_table);
  if ('target_schema' in prefill) assignPrefillIdentifier(next, 'target_owner', prefill.target_schema, { owners: TARGET_OWNERS, uppercase: true });
  if ('target_table' in prefill) assignPrefillIdentifier(next, 'target_table', prefill.target_table);
  if ('mapping_name' in prefill) assignPrefillIdentifier(next, 'mapping_name', prefill.mapping_name, { mapping: true });
  if ('key_columns' in prefill) assignPrefillKey(next, prefill.key_columns);
  return next;
}

export function validateScd2Form(form) {
  const errors = {};
  const sourceOwner = upper(form?.source_owner);
  const sourceTable = upper(form?.source_table);
  const targetOwner = upper(form?.target_owner);
  const targetTable = upper(form?.target_table);
  const mappingName = upper(form?.mapping_name);
  const naturalKey = parseColumnList(form?.natural_key_text);
  const trackedMode = form?.tracked_mode === 'all' ? 'all' : form?.tracked_mode === 'list' ? 'list' : '';
  const tracked = trackedMode === 'all' ? [] : parseColumnList(form?.tracked_text);
  const overwrite = parseColumnList(form?.overwrite_text);
  const sequenceName = upper(form?.sequence_name);

  if (!sourceOwner) errors.source_owner = 'Source owner is required.';
  else if (!SOURCE_OWNERS.includes(sourceOwner)) errors.source_owner = 'Source owner must be DWH, DM, HR, or SH.';

  const sourceTableError = identifierError(sourceTable, 'Source table');
  if (sourceTableError) errors.source_table = sourceTableError;

  if (!targetOwner) errors.target_owner = 'Target owner is required.';
  else if (!TARGET_OWNERS.includes(targetOwner)) errors.target_owner = 'Target owner must be DWH or DM.';

  const targetTableError = identifierError(targetTable, 'Target table');
  if (targetTableError) errors.target_table = targetTableError;

  const nameError = mappingNameError(mappingName);
  if (nameError) errors.mapping_name = nameError;

  const naturalKeyError = listError(naturalKey, { required: true, label: 'Natural key' });
  if (naturalKeyError) errors.natural_key_text = naturalKeyError;

  if (!trackedMode) errors.tracked_text = 'Tracked columns are required.';
  else if (trackedMode === 'list') {
    const trackedError = listError(tracked, { required: true, label: 'Tracked column' });
    if (trackedError) errors.tracked_text = trackedError;
  }

  const overwriteError = listError(overwrite, { required: false, label: 'Overwrite column' });
  if (overwriteError) errors.overwrite_text = overwriteError;

  if (sequenceName) {
    const sequenceError = identifierError(sequenceName, 'Sequence name');
    if (sequenceError) errors.sequence_name = sequenceError;
  }

  if (typeof form?.grant_select_to_app_user !== 'boolean') {
    errors.grant_select_to_app_user = 'Grant option must be true or false.';
  }

  if (!errors.natural_key_text && !errors.tracked_text && trackedMode === 'list' && overlap(tracked, naturalKey).length) {
    errors.tracked_text = 'Tracked columns overlap the natural key.';
  }
  if (!errors.natural_key_text && !errors.overwrite_text && overlap(overwrite, naturalKey).length) {
    errors.overwrite_text = 'Overwrite columns overlap the natural key.';
  }
  if (!errors.tracked_text && !errors.overwrite_text && trackedMode === 'list' && overlap(tracked, overwrite).length) {
    errors.tracked_text = 'Tracked columns overlap overwrite columns.';
  }
  if (!errors.source_owner && !errors.source_table && !errors.target_owner && !errors.target_table
    && sourceOwner === targetOwner && sourceTable === targetTable) {
    errors.target_table = 'Source and target must be different tables.';
  }

  const request = {
    source_owner: sourceOwner,
    source_table: sourceTable,
    target_owner: targetOwner,
    target_table: targetTable,
    mapping_name: mappingName,
    natural_key: naturalKey,
    tracked_columns: trackedMode === 'all' ? 'all' : tracked,
  };
  if (overwrite.length) request.overwrite_columns = overwrite;
  if (sequenceName && !errors.sequence_name) request.sequence_name = sequenceName;
  if (form?.grant_select_to_app_user === true) request.grant_select_to_app_user = true;

  return { ok: Object.keys(errors).length === 0, errors, request };
}

function requireValid(form) {
  const validation = validateScd2Form(form);
  if (!validation.ok) {
    const error = new Error('Required SCD2 fields are missing.');
    error.errors = validation.errors;
    throw error;
  }
  return validation.request;
}

export function buildPlanRequest(form) {
  return requireValid(form);
}

export function buildGenerateRequest(form) {
  return { ...requireValid(form), approved: true };
}

export function buildRunRequest(form, extras = {}) {
  const plan = requireValid(form);
  const body = {
    approved: true,
    source_owner: plan.source_owner,
    source_table: plan.source_table,
    target_owner: plan.target_owner,
    target_table: plan.target_table,
    mapping_name: plan.mapping_name,
    natural_key: plan.natural_key,
  };
  const version = extras.scenario_version == null ? '' : String(extras.scenario_version).trim();
  if (version) {
    if (!SCENARIO_VERSION_RE.test(version)) {
      throw new Error('invalid_scenario_version');
    }
    body.scenario_version = version;
  }
  return body;
}

export function buildPostflightParams(form) {
  const plan = requireValid(form);
  return {
    owner: plan.target_owner,
    table: plan.target_table,
    natural_key: plan.natural_key.join(','),
    source_owner: plan.source_owner,
    source_table: plan.source_table,
  };
}

export function describeFailure(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { message: 'Request failed.', blockers: ['Request failed.'] };
  }
  const status = typeof payload.status === 'string' ? payload.status : '';
  const errorCode = typeof payload.error_code === 'string' ? payload.error_code : '';
  const error = typeof payload.error === 'string' ? payload.error : '';
  const errorType = typeof payload.error_type === 'string' ? payload.error_type : '';
  const parts = [status, errorCode, error, errorType].filter(Boolean);
  const message = parts.join(' — ') || 'Request failed.';
  const blockers = [];
  if (status && !['planned', 'generated', 'run_completed', 'ok'].includes(status)) {
    blockers.push(errorCode ? `${status}: ${errorCode}` : status);
  }
  if (Array.isArray(payload.blockers)) {
    for (const item of payload.blockers) {
      if (typeof item === 'string' && item && !blockers.includes(item)) blockers.push(item);
    }
  }
  if (!blockers.length) blockers.push(message);
  return { message, blockers };
}

function stringList(value) {
  return Array.isArray(value) ? value.filter(item => typeof item === 'string' && item) : [];
}

function collectWarnings(payload) {
  const warnings = stringList(payload?.warnings);
  if (payload?.grants_skipped_by_config === true) warnings.push('grants_skipped_by_config');
  if (payload?.grants_skipped_owner_is_grantee === true) warnings.push('grants_skipped_owner_is_grantee');
  return warnings;
}

function planColumns(plan) {
  if (!Array.isArray(plan?.target_columns)) return [];
  return plan.target_columns.filter(column => column && typeof column === 'object').map(column => ({
    name: String(column.name || ''),
    data_type: String(column.data_type || ''),
    nullable: Boolean(column.nullable),
    scd_role: String(column.scd_role || ''),
  }));
}

function normalizeDdl(ddl) {
  if (!ddl || typeof ddl !== 'object') return null;
  return {
    create_table: typeof ddl.create_table === 'string' ? ddl.create_table : '',
    create_sequence: typeof ddl.create_sequence === 'string' ? ddl.create_sequence : '',
    grants: Array.isArray(ddl.grants) ? ddl.grants.filter(item => typeof item === 'string') : [],
  };
}

export function projectPlan(payload) {
  const planned = payload?.status === 'planned' && payload.plan && typeof payload.plan === 'object';
  const columns = planned ? planColumns(payload.plan) : [];
  const validityColumns = columns.filter(column => VALIDITY_NAMES.has(column.name) || VALIDITY_ROLES.has(column.scd_role));
  const explicitBlockers = stringList(payload?.blockers);
  const failure = planned ? null : describeFailure(payload);
  const blockers = planned ? explicitBlockers : [...failure.blockers, ...explicitBlockers.filter(item => !failure.blockers.includes(item))];
  const ok = Boolean(planned && blockers.length === 0);
  return {
    ok,
    success: ok,
    blockers,
    warnings: planned ? collectWarnings(payload) : [],
    message: ok ? '' : (failure?.message || blockers.join('; ')),
    columns,
    validityColumns,
    ddl: normalizeDdl(payload?.ddl),
    metadata_source: planned ? String(payload.metadata_source || '') : '',
    mapping_name: planned ? String(payload.plan.mapping_name || '') : '',
    scenario_name: planned ? String(payload.plan.scenario_name || '') : '',
    scenario_version: planned ? String(payload.plan.scenario_version || '') : '',
    natural_key: planned && Array.isArray(payload.plan.natural_key) ? payload.plan.natural_key : [],
    tracked_attributes: planned && Array.isArray(payload.plan.tracked_attributes) ? payload.plan.tracked_attributes : [],
    write_operations_attempted: Array.isArray(payload?.write_operations_attempted) ? payload.write_operations_attempted : [],
  };
}

export function canGenerate(view) {
  return Boolean(view?.ok && view?.success && Array.isArray(view.blockers) && view.blockers.length === 0);
}

export function stageSucceeded(view) {
  return view?.success === true && view?.rejected !== true && view?.warning !== true;
}

export function createWriteGate({ lockAfterSuccess = false } = {}) {
  return {
    phase: 'idle',
    confirmed: false,
    confirmToken: 0,
    consumedToken: 0,
    completedCount: 0,
    lockAfterSuccess: lockAfterSuccess === true,
  };
}

export function setWriteConfirmation(gate, checked) {
  if (!gate || gate.phase === 'in_flight') return gate;
  if (gate.lockAfterSuccess && gate.completedCount > 0) return { ...gate, confirmed: false };
  if (!checked) return { ...gate, confirmed: false };
  return { ...gate, confirmed: true, confirmToken: gate.confirmToken + 1 };
}

export function acceptWrite(gate, { ready } = {}) {
  if (!gate || gate.phase === 'in_flight') return { accepted: false, reason: 'in_flight', gate };
  if (gate.lockAfterSuccess && gate.completedCount > 0) return { accepted: false, reason: 'finished', gate };
  if (ready !== true) return { accepted: false, reason: 'not_ready', gate };
  if (!gate.confirmed || gate.confirmToken === gate.consumedToken) {
    return { accepted: false, reason: 'confirmation', gate };
  }
  return {
    accepted: true,
    reason: 'accepted',
    gate: {
      ...gate,
      phase: 'in_flight',
      confirmed: false,
      consumedToken: gate.confirmToken,
    },
  };
}

export function settleWrite(gate, { ok } = {}) {
  if (!gate || gate.phase !== 'in_flight') return gate;
  if (ok === true) {
    return { ...gate, phase: 'succeeded', confirmed: false, completedCount: gate.completedCount + 1 };
  }
  return { ...gate, phase: 'failed', confirmed: false };
}

export function writesInFlight(gates) {
  return Array.isArray(gates) && gates.some(gate => gate?.phase === 'in_flight');
}

export function writeButtonState(gate, { ready, idleLabel, pendingLabel, doneLabel, repeatLabel }) {
  const finished = Boolean(gate?.lockAfterSuccess && gate.completedCount > 0);
  const inFlight = gate?.phase === 'in_flight';
  const repeat = !finished && (gate?.completedCount || 0) > 0;
  const armed = Boolean(gate?.confirmed) && gate.confirmToken !== gate.consumedToken;
  const enabled = !finished && !inFlight && ready === true && armed;
  let label = idleLabel;
  if (inFlight) label = pendingLabel;
  else if (finished) label = doneLabel;
  else if (repeat) label = repeatLabel || idleLabel;
  let notice = '';
  if ((gate?.completedCount || 0) === 1) notice = 'This scenario already ran once in this dialog.';
  else if ((gate?.completedCount || 0) > 1) notice = `This scenario already ran ${gate.completedCount} times in this dialog.`;
  if (finished) notice = '';
  return { enabled, label, finished, inFlight, repeat, notice };
}

export function projectGenerate(payload) {
  const status = typeof payload?.status === 'string' ? payload.status : '';
  const scenarioVersion = typeof payload?.scenario_version === 'string' ? payload.scenario_version : '';
  const ok = status === 'generated' && scenarioVersion.length > 0;
  if (!ok) {
    const failure = describeFailure(payload);
    const message = status === 'generated' ? 'generated response is missing scenario_version.' : failure.message;
    return {
      ok: false,
      success: false,
      status,
      message,
      mapping_name: '',
      scenario_name: '',
      scenario_version: '',
      target: null,
    };
  }
  return {
    ok: true,
    success: true,
    status,
    message: '',
    mapping_name: String(payload.mapping_name || ''),
    scenario_name: String(payload.scenario_name || ''),
    scenario_version: scenarioVersion,
    target: payload.target && typeof payload.target === 'object' ? payload.target : null,
  };
}

function scenarioObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const name = value.name;
  const version = value.version;
  const context = value.context;
  if (typeof name !== 'string' || !name || typeof version !== 'string' || !version || typeof context !== 'string' || !context) {
    return null;
  }
  return { name, version, context };
}

function sessionStatus(steps) {
  if (!Array.isArray(steps)) return '';
  const runStep = steps.find(step => step && step.step === 'odi_scenario_run');
  return typeof runStep?.session_status === 'string' ? runStep.session_status : '';
}

function evidenceFlag(payload, key, label) {
  if (!payload || !Object.prototype.hasOwnProperty.call(payload, key) || payload[key] == null) {
    return { key, label, reported: false, value: null, text: 'not reported' };
  }
  return { key, label, reported: true, value: payload[key], text: String(payload[key]) };
}

export function projectEvidenceFlags(payload) {
  const flags = EVIDENCE_FLAGS.map(([key, label]) => evidenceFlag(payload, key, label));
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    for (const key of Object.keys(payload)) {
      if (EVIDENCE_FLAGS.some(([name]) => name === key)) continue;
      if (!/mismatch|_equal$/i.test(key)) continue;
      flags.push(evidenceFlag(payload, key, key));
    }
  }
  return flags;
}

function evidenceMismatch(flags) {
  return flags.some(flag => {
    if (!flag.reported) return false;
    if (flag.key === 'source_current_count_equal' || /_equal$/i.test(flag.key)) return flag.value === false;
    if (/mismatch/i.test(flag.key)) return flag.value === true;
    return false;
  });
}

export function projectPostflight(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const counts = payload.counts && typeof payload.counts === 'object' && !Array.isArray(payload.counts)
    ? payload.counts
    : {};
  const parity = projectEvidenceFlags(payload);
  const mismatch = evidenceMismatch(parity);
  const statusOk = payload.status === 'ok';
  const warning = Boolean(statusOk && mismatch);
  const ok = statusOk && !mismatch;
  const failure = statusOk ? null : describeFailure(payload);
  return {
    ok,
    success: ok,
    warning,
    status: typeof payload?.status === 'string' ? payload.status : '',
    counts,
    total: counts.target_total,
    current: counts.target_current,
    closed: counts.rows_with_effective_to_set,
    message: warning ? 'Source and current counts do not match.' : (ok ? '' : failure.message),
    parity,
  };
}

export function postflightRows(evidence) {
  const counts = evidence?.counts && typeof evidence.counts === 'object' ? evidence.counts : {};
  const keys = [
    ...COUNT_ORDER.filter(key => Object.prototype.hasOwnProperty.call(counts, key)),
    ...Object.keys(counts).filter(key => !COUNT_ORDER.includes(key)),
  ];
  return keys.map(key => ({
    key,
    label: COUNT_LABELS[key] || key,
    value: counts[key],
  }));
}

export function projectRun(payload) {
  const status = typeof payload?.status === 'string' ? payload.status : '';
  const scenario = scenarioObject(payload?.scenario);
  const evidence = projectPostflight(payload?.postflight);
  const session = sessionStatus(payload?.steps);
  const ok = status === 'run_completed' && Boolean(scenario) && evidence?.ok === true;
  const failure = ok ? null : describeFailure(payload);
  const message = ok ? '' : (evidence?.warning && evidence.message ? evidence.message : failure.message);
  return {
    ok,
    success: ok,
    status,
    message,
    scenario,
    session_status: session,
    evidence,
  };
}

export function payloadFromApiError(error) {
  const body = error?.scd2;
  if (body && typeof body === 'object' && !Array.isArray(body)) return body;
  return null;
}

export function projectRejected(error) {
  const httpStatus = Number.isInteger(error?.status) ? error.status : null;
  const body = payloadFromApiError(error);
  const serverStatus = typeof body?.status === 'string' ? body.status : '';
  const errorCode = typeof body?.error_code === 'string' ? body.error_code : '';
  const serverError = typeof body?.error === 'string' ? body.error : '';
  const parts = [];
  if (httpStatus != null) parts.push(`HTTP ${httpStatus}`);
  if (serverStatus) parts.push(serverStatus);
  if (errorCode) parts.push(errorCode);
  if (serverError) parts.push(serverError);
  const message = parts.join(' — ')
    || (typeof error?.message === 'string' && error.message ? error.message : 'Request failed.');
  return {
    ok: false,
    success: false,
    rejected: true,
    warning: false,
    http_status: httpStatus,
    status: serverStatus,
    error_code: errorCode,
    error: serverError,
    message,
    blockers: [message],
    warnings: [],
    mapping_name: '',
    scenario_name: '',
    scenario_version: '',
    target: null,
    scenario: null,
    session_status: '',
    evidence: null,
  };
}

export function projectApiError(error) {
  const body = payloadFromApiError(error);
  if (body) {
    const failure = describeFailure(body);
    return {
      ok: false,
      success: false,
      status: typeof body.status === 'string' ? body.status : '',
      error_code: typeof body.error_code === 'string' ? body.error_code : '',
      error: typeof body.error === 'string' ? body.error : '',
      message: failure.message,
      blockers: failure.blockers,
      warnings: [],
    };
  }
  const message = typeof error?.message === 'string' && error.message ? error.message : 'Request failed.';
  return {
    ok: false,
    success: false,
    status: '',
    error_code: '',
    error: '',
    message,
    blockers: [message],
    warnings: [],
  };
}
