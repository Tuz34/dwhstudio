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
// "not reported". The three proof flags stay false on a passing count witness;
// that is "not proven" / "not checked", not a failed load.
const EVIDENCE_FLAGS = [
  ['source_current_count_equal', 'Source row count equals current row count'],
  ['business_parity_proven', 'Business parity proven'],
  ['physical_noop_proven', 'Physical no-op proven'],
  ['history_overlap_checked', 'History overlap checked'],
  ['stock_contract_valid', 'Stock contract valid'],
];

export const STATUS_OK_SCOPE = 'The count check passed. It does not prove row-level equality.';
export const REVIEW_SENTENCE = 'This operation may have started or partly completed and must be checked before it is tried again.';
export const UNRESOLVED_CLOSE_SENTENCE = 'An operation in this dialog is unresolved and must be checked before it is tried again.';
export const WRITE_IN_FLIGHT_NOTE = 'This request is still running. Do not reload this page. If the page is reloaded anyway, check the evidence before running again.';

const PROOF_FLAGS = new Set(['business_parity_proven', 'physical_noop_proven']);
const UNKNOWN_REPOSITORY = new Set(['unverified', 'commit_outcome_unknown', 'rollback_failed']);

const PREFILL_NOTES = {
  source_owner: 'The suggested source owner was invalid.',
  source_table: 'The suggested source table was invalid.',
  target_owner: 'The suggested target owner was invalid.',
  target_table: 'The suggested target table was invalid.',
  mapping_name: 'The suggested mapping name was invalid.',
  natural_key_text: 'The suggested key was invalid.',
};

// Chat spelling is canonical. The mapping wizard spelling is the alias.
// When both are present and valid, the canonical value is kept.
export const PREFILL_ALIASES = Object.freeze([
  Object.freeze({ field: 'source_owner', canonical: 'source_schema', alias: 'src_schema', owners: SOURCE_OWNERS, uppercase: true }),
  Object.freeze({ field: 'source_table', canonical: 'source_table', alias: 'src_table' }),
  Object.freeze({ field: 'target_owner', canonical: 'target_schema', alias: 'tgt_owner', owners: TARGET_OWNERS, uppercase: true }),
  Object.freeze({ field: 'target_table', canonical: 'target_table', alias: 'tgt_table' }),
]);

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

function suppliedPrefill(prefill, key) {
  if (!Object.prototype.hasOwnProperty.call(prefill, key)) return false;
  const value = prefill[key];
  return value != null && value !== '';
}

function applyPrefillAlias(next, prefill, spec) {
  const canonicalSupplied = suppliedPrefill(prefill, spec.canonical);
  const aliasSupplied = suppliedPrefill(prefill, spec.alias);
  if (!canonicalSupplied && !aliasSupplied) return;
  const options = {
    owners: spec.owners || null,
    uppercase: spec.uppercase === true,
    mapping: spec.mapping === true,
  };
  const canonical = prefill[spec.canonical];
  const alias = prefill[spec.alias];
  if (canonicalSupplied && isSingleIdentifier(canonical, options)) {
    assignPrefillIdentifier(next, spec.field, canonical, options);
    return;
  }
  if (aliasSupplied && isSingleIdentifier(alias, options)) {
    assignPrefillIdentifier(next, spec.field, alias, options);
    return;
  }
  assignPrefillIdentifier(next, spec.field, canonicalSupplied ? canonical : alias, options);
}

export function applyPrefill(form, prefill) {
  const next = { ...emptyScd2Form(), ...form };
  next.prefillNotes = { ...(form?.prefillNotes || {}) };
  if (!prefill || typeof prefill !== 'object' || Array.isArray(prefill)) return next;
  for (const spec of PREFILL_ALIASES) applyPrefillAlias(next, prefill, spec);
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
  if (!gate || gate.phase === 'in_flight' || gate.phase === 'needs_review') return gate;
  if (gate.lockAfterSuccess && gate.completedCount > 0) return { ...gate, confirmed: false };
  if (!checked) return { ...gate, confirmed: false };
  return { ...gate, confirmed: true, confirmToken: gate.confirmToken + 1 };
}

export function acceptWrite(gate, { ready } = {}) {
  if (!gate || gate.phase === 'in_flight') return { accepted: false, reason: 'in_flight', gate };
  if (gate.phase === 'needs_review') return { accepted: false, reason: 'needs_review', gate };
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

export function settleWrite(gate, { ok, review } = {}) {
  if (!gate || gate.phase !== 'in_flight') return gate;
  if (review === true) return { ...gate, phase: 'needs_review', confirmed: false };
  if (ok === true) {
    return { ...gate, phase: 'succeeded', confirmed: false, completedCount: gate.completedCount + 1 };
  }
  return { ...gate, phase: 'failed', confirmed: false };
}

export function replaceWriteGate(current, next) {
  if (current?.phase === 'needs_review') return current;
  return next;
}

export function canRefreshEvidence({ loading = false, gates = [] } = {}) {
  return loading !== true && !writesInFlight(gates);
}

export function writesInFlight(gates) {
  return Array.isArray(gates) && gates.some(gate => gate?.phase === 'in_flight');
}

export function writeButtonState(gate, { ready, idleLabel, pendingLabel, doneLabel, repeatLabel, dialogLocked = false }) {
  const finished = Boolean(gate?.lockAfterSuccess && gate.completedCount > 0);
  const inFlight = gate?.phase === 'in_flight';
  const reviewLocked = gate?.phase === 'needs_review' || dialogLocked === true;
  const repeat = !finished && !reviewLocked && (gate?.completedCount || 0) > 0;
  const armed = Boolean(gate?.confirmed) && gate.confirmToken !== gate.consumedToken;
  const enabled = !finished && !inFlight && !reviewLocked && ready === true && armed;
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

// Definite failures are this closed list only: route, HTTP status, top status.
// Each row is a reviewer "F" outcome proven to return before a target write or
// a scenario start. A hit is still needs-review when the body carries a review
// signal. No other body is a failure because its warning flags are false.
// status '' means the JSON has no top-level status string.
export const DEFINITE_FAILURE_ROWS = Object.freeze([
  // generate — validation and catalog, before the orchestrator writes
  Object.freeze({ route: 'generate', http: 400, status: 'invalid_request' }), // scd2_service.py:58 via :194-196
  Object.freeze({ route: 'generate', http: 400, status: 'blocked_until_approval' }), // scd2_service.py:192-193
  Object.freeze({ route: 'generate', http: 400, status: '' }), // app.py:196-198 via routes/scd2_write.py:37-39
  Object.freeze({ route: 'generate', http: 404, status: 'blocked_source_not_found' }), // scd2_service.py:91-92
  Object.freeze({ route: 'generate', http: 503, status: 'source_metadata_unavailable' }), // scd2_service.py:89-90
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_dm_model_missing' }), // scd2_service.py:163-167, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_target_model_missing' }), // scd2_service.py:168, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_target_model_not_oracle' }), // scd2_service.py:169-170, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_source_model_missing' }), // scd2_service.py:171-172, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_source_datastore_missing' }), // scd2_service.py:173-176, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_project_folder_missing' }), // scd2_service.py:177-178, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_lkm_missing' }), // scd2_service.py:179-180, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_scenario_name_collision' }), // scd2_service.py:181-182, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_mapping_exists_unrelated' }), // scd2_service.py:183-184, :218-221
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_existing_object_mismatch' }), // scd2_service.py:223-228
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_execution_identity' }), // scd2_service.py:242-243, before execute_target_ddl
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_grantee_missing' }), // scd2_target_executor.py:289-293, no DDL
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_target_exists_mismatch' }), // scd2_target_executor.py:298-300, before recover
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_ddl_privilege' }), // scd2_target_executor.py:217-221, :333-340; scd2_service.py:252-259
  Object.freeze({ route: 'generate', http: 409, status: 'blocked_name_in_use' }), // scd2_target_executor.py:222-223, :333-340; scd2_service.py:252-259
  Object.freeze({ route: 'generate', http: 502, status: 'failed' }), // scd2_target_executor.py:224, :333-340; scd2_service.py:252-259
  Object.freeze({ route: 'generate', http: 502, status: 'failed_odi_probe' }), // scd2_service.py:215-216; probe is read-only scd2_odi.py:160-161
  Object.freeze({ route: 'generate', http: 429, status: '' }), // routes/scd2_write.py:32 limiter, before the handler, no SCD2 body
  // run — guards that return before run_scenario
  Object.freeze({ route: 'run', http: 400, status: 'invalid_request' }), // scd2_service.py:363-366
  Object.freeze({ route: 'run', http: 400, status: 'blocked_until_approval' }), // scd2_service.py:361-362
  Object.freeze({ route: 'run', http: 400, status: '' }), // app.py:196-198 via routes/scd2_write.py:56-58
  Object.freeze({ route: 'run', http: 409, status: 'blocked_target_not_scd2' }), // scd2_service.py:377-378
  Object.freeze({ route: 'run', http: 409, status: 'blocked_scenario_name_collision' }), // scd2_service.py:385-386
  Object.freeze({ route: 'run', http: 409, status: 'blocked_scenario_missing' }), // scd2_service.py:387-388
  Object.freeze({ route: 'run', http: 409, status: 'blocked_existing_object_mismatch' }), // scd2_service.py:394-398
  Object.freeze({ route: 'run', http: 409, status: 'blocked_scenario_version_missing' }), // scd2_service.py:400-405
  Object.freeze({ route: 'run', http: 409, status: 'blocked_execution_identity' }), // scd2_service.py:419-420
  Object.freeze({ route: 'run', http: 503, status: 'blocked_agent_preflight' }), // scd2_service.py:372-373; remote_run_contract.py:85-86
  Object.freeze({ route: 'run', http: 503, status: 'target_metadata_unavailable' }), // scd2_service.py:375-376
  Object.freeze({ route: 'run', http: 503, status: 'blocked_count_preflight' }), // scd2_service.py:426-427, before :428
  Object.freeze({ route: 'run', http: 502, status: 'failed_odi_probe' }), // scd2_service.py:382-383; probe is read-only scd2_odi.py:160-161
  Object.freeze({ route: 'run', http: 429, status: '' }), // routes/scd2_write.py:51 limiter, before the handler, no SCD2 body
]);

const EFFECT_KEY_RE = /(applied|created|changed)/i;
const MAY_HAVE_RE = /may_have_/i;
const PARTIAL_STATUS_RE = /^partial_failure(?:_|$)/;
const UNKNOWN_TOP_STATUS = new Set(['run_unknown', 'outcome_unknown']);

function topLevelUncertainty(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return false;
  if (payload.outcome_unknown === true) return true;
  if (payload.manual_review_required === true) return true;
  if (payload.manual_recovery_review_required === true) return true;
  if (Object.prototype.hasOwnProperty.call(payload, 'scenario_execution_completed') && payload.scenario_execution_completed == null) {
    return true;
  }
  return false;
}

// Positive evidence that a write may have started or changed something.
// False, missing, and zero are not evidence of a clean failure.
function responseHasReviewSignal(value, seen = new Set()) {
  if (!value || typeof value !== 'object') return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.some(item => responseHasReviewSignal(item, seen));
  if (value.outcome_unknown === true) return true;
  if (value.manual_review_required === true) return true;
  if (value.manual_recovery_review_required === true) return true;
  if (value.known_side_effects === true) return true;
  const status = typeof value.status === 'string' ? value.status : '';
  if (PARTIAL_STATUS_RE.test(status) || UNKNOWN_TOP_STATUS.has(status)) return true;
  if (Object.prototype.hasOwnProperty.call(value, 'scenario_execution_completed') && value.scenario_execution_completed == null) return true;
  if (Object.prototype.hasOwnProperty.call(value, 'side_effects') && value.side_effects == null) return true;
  if (typeof value.repository_outcome === 'string' && UNKNOWN_REPOSITORY.has(value.repository_outcome)) return true;
  for (const [key, item] of Object.entries(value)) {
    if (MAY_HAVE_RE.test(key) && item === true) return true;
    if (EFFECT_KEY_RE.test(key) && typeof item === 'number' && item > 0) return true;
    if (EFFECT_KEY_RE.test(key) && item === true) return true;
    if (item && typeof item === 'object' && responseHasReviewSignal(item, seen)) return true;
  }
  return false;
}

export function hasReviewSignal(payload) {
  return responseHasReviewSignal(payload);
}

function topStatus(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return '';
  if (!Object.prototype.hasOwnProperty.call(payload, 'status')) return '';
  if (typeof payload.status !== 'string') return null;
  return payload.status;
}

function matchesDefiniteFailure(route, httpStatus, payload) {
  if (!Number.isInteger(httpStatus)) return false;
  const status = topStatus(payload);
  if (status == null) return false;
  const listed = DEFINITE_FAILURE_ROWS.some(row => (
    row.http === httpStatus
    && row.status === status
    && (route == null || row.route === route)
  ));
  if (!listed) return false;
  if (responseHasReviewSignal(payload)) return false;
  return true;
}

function isResolvedSuccess(route, payload, resolved) {
  if (resolved !== true || topLevelUncertainty(payload)) return false;
  const status = typeof payload?.status === 'string' ? payload.status : '';
  if (route === 'generate') {
    const scenarioVersion = typeof payload?.scenario_version === 'string' ? payload.scenario_version : '';
    return status === 'generated' && scenarioVersion.length > 0;
  }
  if (route === 'run') {
    return status === 'run_completed' && Boolean(scenarioObject(payload?.scenario)) && projectPostflight(payload?.postflight)?.ok === true;
  }
  return false;
}

// succeeded | failed | needs_review. `failed` is only a closed-table hit
// with no review signal. Every other non-success is needs_review.
export function classifyWriteOutcome({ route = null, httpStatus = null, payload = null, resolved = false } = {}) {
  if (isResolvedSuccess(route, payload, resolved)) return 'succeeded';
  if (matchesDefiniteFailure(route, httpStatus, payload)) return 'failed';
  return 'needs_review';
}

function bannerView(view) {
  if (view?.needsReview) return view;
  return { needsReview: true, detail: '', message: REVIEW_SENTENCE };
}

// Unresolved close depends on (locked, source). Escape, the backdrop, and X are
// `dismiss` and never close a locked dialog, no matter how many times they repeat.
// Only `confirm-button` closes it. An in-flight write blocks both sources.
export function closeDialogDecision({ locked = false, source = 'dismiss', inFlight = false } = {}) {
  if (inFlight === true) return { close: false, needsConfirm: false, message: '' };
  if (locked === true && source !== 'confirm-button') {
    return { close: false, needsConfirm: true, message: UNRESOLVED_CLOSE_SENTENCE };
  }
  return { close: true, needsConfirm: false, message: '' };
}

// One decision for both write gates. Either needs_review locks Generate and Run.
export function dialogWriteDecision(generateGate, runGate) {
  const locked = generateGate?.phase === 'needs_review' || runGate?.phase === 'needs_review';
  return {
    locked,
    accept(gate, { ready } = {}) {
      if (locked) return { accepted: false, reason: 'needs_review', gate };
      return acceptWrite(gate, { ready });
    },
    afterPlan(nextGenerate, nextRun) {
      if (locked) return { generateGate, runGate, reset: false };
      return {
        generateGate: replaceWriteGate(generateGate, nextGenerate),
        runGate: replaceWriteGate(runGate, nextRun),
        reset: true,
      };
    },
    close({ source = 'dismiss' } = {}) {
      return closeDialogDecision({
        locked,
        source,
        inFlight: writesInFlight([generateGate, runGate]),
      });
    },
    banners({ step = 0, generateView = null, runView = null } = {}) {
      if (locked) {
        const items = [];
        if (generateGate?.phase === 'needs_review') items.push(bannerView(generateView));
        if (runGate?.phase === 'needs_review') items.push(bannerView(runView));
        return items;
      }
      if (step === 2 && generateView?.needsReview) return [generateView];
      if (step === 3 && runView?.needsReview) return [runView];
      return [];
    },
  };
}

function reviewCopy(parts) {
  const detail = parts.filter(Boolean).join(' — ');
  const message = detail ? `${detail}. ${REVIEW_SENTENCE}` : REVIEW_SENTENCE;
  return { detail, message };
}

export function projectGenerate(payload) {
  const status = typeof payload?.status === 'string' ? payload.status : '';
  const scenarioVersion = typeof payload?.scenario_version === 'string' ? payload.scenario_version : '';
  const outcome = classifyWriteOutcome({ route: 'generate', httpStatus: 200, payload, resolved: true });
  const needsReview = outcome === 'needs_review';
  const ok = outcome === 'succeeded';
  if (!ok) {
    const failure = describeFailure(payload);
    const review = needsReview ? reviewCopy([status, typeof payload?.error_code === 'string' ? payload.error_code : '']) : null;
    const message = review ? review.message : failure.message;
    return {
      ok: false,
      success: false,
      needsReview,
      outcome: needsReview ? 'needs_review' : 'failed',
      status,
      detail: review ? review.detail : '',
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
    needsReview: false,
    outcome: 'succeeded',
    status,
    detail: '',
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

function evidenceFlagText(key, value) {
  if (key === 'history_overlap_checked') {
    return value === true ? 'checked' : 'not checked by this check';
  }
  if (PROOF_FLAGS.has(key)) {
    return value === true ? 'proven' : 'not proven by this check';
  }
  return String(value);
}

function evidenceFlag(payload, key, label) {
  if (!payload || !Object.prototype.hasOwnProperty.call(payload, key) || payload[key] == null) {
    return { key, label, reported: false, value: null, text: 'not reported' };
  }
  return { key, label, reported: true, value: payload[key], text: evidenceFlagText(key, payload[key]) };
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
    statusScope: payload.status === 'ok' ? STATUS_OK_SCOPE : '',
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
  const outcomeClass = classifyWriteOutcome({ route: 'run', httpStatus: 200, payload, resolved: true });
  const needsReview = outcomeClass === 'needs_review';
  const ok = outcomeClass === 'succeeded';
  const failure = ok ? null : describeFailure(payload);
  const review = needsReview
    ? reviewCopy([status, typeof payload?.error_code === 'string' ? payload.error_code : ''])
    : null;
  const message = ok
    ? ''
    : (review ? review.message : (evidence?.warning && evidence.message ? evidence.message : failure.message));
  return {
    ok,
    success: ok,
    needsReview,
    outcome: ok ? 'succeeded' : outcomeClass,
    status,
    detail: review ? review.detail : '',
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

export function projectRejected(error, { write = false, route = null } = {}) {
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
  const writeRoute = route === 'generate' || route === 'run' ? route : null;
  const isWrite = write === true || writeRoute != null;
  const needsReview = isWrite && classifyWriteOutcome({
    route: writeRoute,
    httpStatus,
    payload: body,
    resolved: false,
  }) === 'needs_review';
  if (needsReview) {
    const fallback = typeof error?.message === 'string' && error.message ? error.message : '';
    const review = reviewCopy(parts.length ? parts : [fallback]);
    return {
      ok: false,
      success: false,
      rejected: false,
      needsReview: true,
      warning: true,
      outcome: 'needs_review',
      http_status: httpStatus,
      status: serverStatus,
      error_code: errorCode,
      error: serverError,
      detail: review.detail,
      message: review.message,
      blockers: [review.message],
      warnings: [],
      mapping_name: '',
      scenario_name: '',
      scenario_version: '',
      target: null,
      scenario: scenarioObject(body?.scenario),
      session_status: sessionStatus(body?.steps),
      evidence: body?.postflight ? projectPostflight(body.postflight) : null,
    };
  }
  const message = parts.join(' — ')
    || (typeof error?.message === 'string' && error.message ? error.message : 'Request failed.');
  return {
    ok: false,
    success: false,
    rejected: true,
    needsReview: false,
    warning: false,
    outcome: 'failed',
    http_status: httpStatus,
    status: serverStatus,
    error_code: errorCode,
    error: serverError,
    detail: '',
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
