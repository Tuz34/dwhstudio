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

export function applyPrefill(form, prefill) {
  const next = { ...emptyScd2Form(), ...form };
  if (!prefill || typeof prefill !== 'object' || Array.isArray(prefill)) return next;
  if (typeof prefill.source_schema === 'string') next.source_owner = upper(prefill.source_schema);
  if (typeof prefill.source_table === 'string') next.source_table = prefill.source_table;
  if (typeof prefill.target_schema === 'string') next.target_owner = upper(prefill.target_schema);
  if (typeof prefill.target_table === 'string') next.target_table = prefill.target_table;
  if (typeof prefill.mapping_name === 'string') next.mapping_name = prefill.mapping_name;
  if (Array.isArray(prefill.key_columns) && prefill.key_columns.every(item => typeof item === 'string')) {
    next.natural_key_text = prefill.key_columns.join(', ');
  }
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

export function projectPostflight(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const counts = payload.counts && typeof payload.counts === 'object' && !Array.isArray(payload.counts)
    ? payload.counts
    : {};
  const ok = payload.status === 'ok';
  const failure = ok ? null : describeFailure(payload);
  return {
    ok,
    success: ok,
    status: typeof payload?.status === 'string' ? payload.status : '',
    counts,
    total: counts.target_total,
    current: counts.target_current,
    closed: counts.rows_with_effective_to_set,
    message: ok ? '' : failure.message,
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
  return {
    ok,
    success: ok,
    status,
    message: ok ? '' : failure.message,
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
