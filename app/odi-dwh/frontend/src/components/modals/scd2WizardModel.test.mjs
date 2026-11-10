import assert from 'node:assert/strict';
import test from 'node:test';

import { shouldDismissModal } from '../ui/modalFocus.js';
import { buildScd2Handoff } from './mappingWizardRequests.js';
import {
  acceptWrite,
  applyPrefill,
  buildGenerateRequest,
  buildPlanRequest,
  buildPostflightParams,
  buildRunRequest,
  canGenerate,
  canRefreshEvidence,
  classifyWriteOutcome,
  closeDialogDecision,
  createWriteGate,
  DEFINITE_FAILURE_ROWS,
  dialogWriteDecision,
  emptyScd2Form,
  PREFILL_ALIASES,
  projectApiError,
  projectGenerate,
  projectPostflight,
  projectPlan,
  projectRejected,
  projectRun,
  replaceWriteGate,
  REVIEW_SENTENCE,
  setWriteConfirmation,
  UNRESOLVED_CLOSE_SENTENCE,
  settleWrite,
  stageSucceeded,
  STATUS_OK_SCOPE,
  validateScd2Form,
  writeButtonState,
  writesInFlight,
} from './scd2WizardModel.js';

// Copied from tests/scd2_support.py REQUEST.
const REQUEST = {
  source_owner: 'DWH',
  source_table: 'ODS_CUSTOMERS',
  natural_key: ['CUST_ID'],
  tracked_columns: ['CUST_NAME', 'CUST_CITY'],
  target_owner: 'DM',
  target_table: 'DIM_CUSTOMER',
  mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
};

// Copied from tests/test_scd2_service.py RUN_BODY, plus the route's literal approval.
const RUN_BODY = {
  approved: true,
  source_owner: 'DWH',
  source_table: 'ODS_CUSTOMERS',
  target_owner: 'DM',
  target_table: 'DIM_CUSTOMER',
  mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
  natural_key: ['CUST_ID'],
};

function customerForm(overrides = {}) {
  return {
    ...emptyScd2Form(),
    source_owner: 'dwh',
    source_table: 'ods_customers',
    target_owner: 'dm',
    target_table: 'dim_customer',
    mapping_name: 'map_ods_cust_to_dim_cust',
    natural_key_text: 'cust_id',
    tracked_mode: 'list',
    tracked_text: 'cust_name, cust_city',
    ...overrides,
  };
}

// Shape from test_plan_preview_from_request_columns_is_connectionless and build_scd2_plan.
const PLANNED = {
  status: 'planned',
  metadata_source: 'request',
  plan: {
    mode: 'scd2_static_plan',
    status: 'planned',
    source: { owner: 'DWH', table: 'ODS_CUSTOMERS' },
    target: { owner: 'DM', table: 'DIM_CUSTOMER' },
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    scenario_name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
    scenario_version: '001',
    natural_key: ['CUST_ID'],
    tracked_attributes: ['CUST_NAME', 'CUST_CITY'],
    target_columns: [
      { name: 'CUSTOMER_SK', data_type: 'NUMBER', nullable: false, scd_role: 'SURROGATE_KEY' },
      { name: 'CUST_ID', data_type: 'NUMBER(10)', nullable: false, scd_role: 'NATURAL_KEY' },
      { name: 'CUST_NAME', data_type: 'VARCHAR2(50)', nullable: false, scd_role: 'ADD_ROW_ON_CHANGE' },
      { name: 'CUST_CITY', data_type: 'VARCHAR2(30)', nullable: true, scd_role: 'ADD_ROW_ON_CHANGE' },
      { name: 'EFFECTIVE_FROM', data_type: 'DATE', nullable: false, scd_role: 'START_TIMESTAMP' },
      { name: 'EFFECTIVE_TO', data_type: 'DATE', nullable: false, scd_role: 'END_TIMESTAMP' },
      { name: 'CURRENT_FLAG', data_type: 'VARCHAR2(1)', nullable: false, scd_role: 'CURRENT_RECORD_FLAG' },
    ],
    connection_attempted: false,
    requires_user_approval: true,
  },
  ddl: {
    create_sequence: 'CREATE SEQUENCE "DM"."SEQ_DIM_CUSTOMER" START WITH 1 INCREMENT BY 1 NOCYCLE',
    create_table: 'CREATE TABLE "DM"."DIM_CUSTOMER" (\n  "CUSTOMER_SK" NUMBER NOT NULL\n)',
    grants: [],
  },
  odi_expressions: { CUSTOMER_SK: 'DM.SEQ_DIM_CUSTOMER.NEXTVAL' },
  write_operations_attempted: [],
};

// Shape from test_generate_happy_path_runs_steps_in_order.
const GENERATED = {
  status: 'generated',
  mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
  scenario_name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
  scenario_version: '001',
  target: { owner: 'DM', table: 'DIM_CUSTOMER' },
  steps: [
    { step: 'odi_probe', status: 'passed' },
    { step: 'execution_identity', status: 'passed' },
    { step: 'target_ddl', status: 'created' },
    { step: 'odi_datastore_register', status: 'passed' },
    { step: 'odi_mapping_create', status: 'passed' },
    { step: 'odi_scenario_generate', status: 'passed' },
  ],
};

// Counts from test_run_executes_scenario_and_returns_count_only_postflight.
const RUN_COMPLETED = {
  status: 'run_completed',
  scenario: { name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN', version: '001', context: 'GLOBAL' },
  steps: [
    { step: 'remote_agent_health', status: 'passed' },
    { step: 'odi_scenario_run', status: 'passed', session_status: 'DONE' },
  ],
  postflight: {
    status: 'ok',
    counts: {
      target_total: 5,
      target_current: 5,
      max_effective_from: '2026-11-04',
      rows_with_effective_to_set: 0,
      duplicate_current_groups: 0,
      invalid_date_order_rows: 0,
      source_count: 5,
      invalid_current_flag_rows: 0,
      invalid_open_end_rows: 0,
      invalid_closed_end_rows: 0,
      null_natural_key_rows: 0,
    },
  },
  raw_rows_included: false,
};


test('plan, generate and run bodies match the SCD2 contract', () => {
  const form = customerForm();
  assert.deepEqual(buildPlanRequest(form), REQUEST);
  assert.equal('approved' in buildPlanRequest(form), false);
  assert.equal('source_columns' in buildPlanRequest(form), false);

  assert.deepEqual(buildGenerateRequest(form), { ...REQUEST, approved: true });
  assert.deepEqual(buildRunRequest(form), RUN_BODY);
  assert.deepEqual(buildRunRequest(form, { scenario_version: '001' }), {
    ...RUN_BODY,
    scenario_version: '001',
  });
  assert.deepEqual(buildPostflightParams(form), {
    owner: 'DM',
    table: 'DIM_CUSTOMER',
    natural_key: 'CUST_ID',
    source_owner: 'DWH',
    source_table: 'ODS_CUSTOMERS',
  });
});


test('literal approval is boolean true only on generate and run', () => {
  const form = { ...customerForm(), approved: 'true' };
  const plan = buildPlanRequest(form);
  const generated = buildGenerateRequest(form);
  const run = buildRunRequest({ ...customerForm(), approved: 1 });

  assert.equal('approved' in plan, false);
  assert.equal(generated.approved, true);
  assert.equal(typeof generated.approved, 'boolean');
  assert.notEqual(generated.approved, 'true');
  assert.equal(run.approved, true);
  assert.equal(typeof run.approved, 'boolean');
  assert.throws(() => buildRunRequest(customerForm(), { scenario_version: '1 2' }), /invalid_scenario_version/);
});


test('empty required fields block a request', () => {
  const missing = validateScd2Form(emptyScd2Form());
  assert.equal(missing.ok, false);
  for (const key of ['source_owner', 'source_table', 'target_owner', 'target_table', 'mapping_name', 'natural_key_text', 'tracked_text']) {
    assert.ok(missing.errors[key], key);
  }
  assert.throws(() => buildPlanRequest(emptyScd2Form()), /Required SCD2 fields are missing/);

  const trackedAll = validateScd2Form(customerForm({ tracked_mode: 'all', tracked_text: '' }));
  assert.equal(trackedAll.ok, true);
  assert.equal(trackedAll.request.tracked_columns, 'all');

  const sameTable = validateScd2Form(customerForm({ target_owner: 'DWH', target_table: 'ODS_CUSTOMERS' }));
  assert.equal(sameTable.ok, false);
  assert.match(sameTable.errors.target_table, /different tables/);

  const tooLong = validateScd2Form(customerForm({ mapping_name: 'M'.repeat(32) }));
  assert.equal(tooLong.ok, false);
  assert.match(tooLong.errors.mapping_name, /31 characters/);
});


test('optional contract fields are sent only when set', () => {
  const body = buildPlanRequest(customerForm({
    overwrite_text: 'credit_limit',
    sequence_name: 'seq_dim_customer',
    grant_select_to_app_user: true,
  }));
  assert.deepEqual(body.overwrite_columns, ['CREDIT_LIMIT']);
  assert.equal(body.sequence_name, 'SEQ_DIM_CUSTOMER');
  assert.equal(body.grant_select_to_app_user, true);
  assert.equal('sequence_name' in buildPlanRequest(customerForm()), false);
  assert.equal('grant_select_to_app_user' in buildPlanRequest(customerForm()), false);
});


test('prefill maps known keys and ignores unknown keys', () => {
  const form = applyPrefill(emptyScd2Form(), {
    source_schema: 'hr',
    source_table: 'ODS_CUSTOMERS',
    target_schema: 'DM',
    target_table: 'DIM_CUSTOMER',
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    key_columns: ['CUST_ID'],
    source_owner: 'DWH',
    tracked_columns: ['CUST_NAME'],
    approved: true,
    drop_target_if_exists: true,
  });

  assert.equal(form.source_owner, 'HR');
  assert.equal(form.source_table, 'ODS_CUSTOMERS');
  assert.equal(form.target_owner, 'DM');
  assert.equal(form.target_table, 'DIM_CUSTOMER');
  assert.equal(form.mapping_name, 'MAP_ODS_CUST_TO_DIM_CUST');
  assert.equal(form.natural_key_text, 'CUST_ID');
  assert.equal(form.tracked_text, '');
  assert.equal(form.grant_select_to_app_user, false);
  assert.equal(form.approved, undefined);
  assert.equal(form.drop_target_if_exists, undefined);
  assert.equal(applyPrefill(emptyScd2Form(), { key_columns: 'CUST_ID' }).natural_key_text, '');
});


test('a plan blocker disables generate and a warning does not', () => {
  const ready = projectPlan(PLANNED);
  assert.equal(ready.success, true);
  assert.equal(canGenerate(ready), true);
  assert.equal(ready.ddl.create_table.startsWith('CREATE TABLE "DM"."DIM_CUSTOMER"'), true);
  assert.deepEqual(ready.validityColumns.map(column => column.name), ['EFFECTIVE_FROM', 'EFFECTIVE_TO', 'CURRENT_FLAG']);
  assert.equal(ready.columns[0].scd_role, 'SURROGATE_KEY');
  assert.deepEqual(ready.write_operations_attempted, []);

  // Statuses from test_plan_preview_error_statuses.
  for (const payload of [
    { status: 'invalid_request', error_code: 'target_owner_not_allowed' },
    { status: 'invalid_request', error_code: 'natural_key_not_in_source' },
    { status: 'blocked_source_not_found' },
    { status: 'source_metadata_unavailable' },
  ]) {
    const blocked = projectPlan(payload);
    assert.equal(blocked.success, false, payload.status);
    assert.equal(canGenerate(blocked), false, payload.status);
  }

  const listed = projectPlan({ ...PLANNED, blockers: ['review_blocked'] });
  assert.equal(listed.success, false);
  assert.equal(canGenerate(listed), false);
  assert.ok(listed.blockers.includes('review_blocked'));

  const warned = projectPlan({
    ...PLANNED,
    warnings: ['Check the DDL preview.'],
    grants_skipped_by_config: true,
  });
  assert.equal(canGenerate(warned), true);
  assert.deepEqual(warned.warnings, ['Check the DDL preview.', 'grants_skipped_by_config']);
});


test('generate and run success follow the server status and scenario object', () => {
  const generated = projectGenerate(GENERATED);
  assert.equal(generated.success, true);
  assert.equal(generated.status, 'generated');
  assert.equal(generated.mapping_name, 'MAP_ODS_CUST_TO_DIM_CUST');
  assert.equal(generated.scenario_name, 'MAP_ODS_CUST_TO_DIM_CUST_SCN');
  assert.equal(generated.scenario_version, '001');

  // test_dm_model_missing_blocks_before_any_write_with_topology_steps
  const blocked = projectGenerate({
    status: 'blocked_dm_model_missing',
    required_model_code: 'TGT_DM',
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    scenario_name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
    scenario_version: '001',
  });
  assert.equal(blocked.success, false);
  assert.match(blocked.message, /blocked_dm_model_missing/);

  const completed = projectRun(RUN_COMPLETED);
  assert.equal(completed.success, true);
  assert.deepEqual(completed.scenario, {
    name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
    version: '001',
    context: 'GLOBAL',
  });
  assert.equal(completed.session_status, 'DONE');
  assert.equal(completed.evidence.total, 5);
  assert.equal(completed.evidence.current, 5);
  assert.equal(completed.evidence.closed, 0);
  assert.equal(completed.evidence.counts.max_effective_from, '2026-11-04');
});


test('a failed payload is not shown as success', () => {
  // test_run_failure_still_reports_postflight_counts: HTTP 502, status run_failed, postflight ok.
  const failedRun = projectRun({
    status: 'run_failed',
    scenario: { name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN', version: '001', context: 'GLOBAL' },
    postflight: {
      status: 'ok',
      counts: { target_total: 5, target_current: 5, rows_with_effective_to_set: 0 },
    },
    steps: [{ step: 'odi_scenario_run', status: 'failed', session_status: 'ERROR' }],
  });
  assert.equal(failedRun.ok, false);
  assert.equal(failedRun.success, false);
  assert.equal(failedRun.session_status, 'ERROR');
  assert.equal(failedRun.evidence.success, true);
  assert.equal(failedRun.evidence.total, 5);

  assert.equal(projectRun({
    status: 'run_completed',
    scenario: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
    postflight: { status: 'ok', counts: { target_total: 5, target_current: 5, rows_with_effective_to_set: 0 } },
  }).success, false);

  assert.equal(projectGenerate({ status: 'generated' }).success, false);
  assert.equal(projectGenerate({ status: 'blocked_until_approval' }).success, false);

  const apiError = projectApiError({
    message: 'Request was rejected.',
    status: 400,
    scd2: { status: 'invalid_request', error_code: 'target_owner_not_allowed' },
  });
  assert.equal(apiError.success, false);
  assert.equal(apiError.message, 'invalid_request — target_owner_not_allowed');
  assert.equal(apiError.message.includes('Request was rejected'), false);

  const approval = projectApiError({
    message: 'Request was rejected.',
    scd2: { error: 'approved:true is required for SCD2 generation.' },
  });
  assert.equal(approval.success, false);
  assert.equal(approval.message, 'approved:true is required for SCD2 generation.');

  const evidence = projectPostflight({ status: 'invalid_stock_contract', error_code: 'invalid_identifier', counts: {} });
  assert.equal(evidence.success, false);
  assert.match(evidence.message, /invalid_stock_contract/);
});


function arm(gate) {
  return setWriteConfirmation(gate, true);
}

function button(gate, labels) {
  return writeButtonState(gate, { ready: true, ...labels });
}

const GENERATE_LABELS = { idleLabel: 'Generate', pendingLabel: 'Generating...', doneLabel: 'Generated' };
const RUN_LABELS = { idleLabel: 'Run', pendingLabel: 'Running...', doneLabel: 'Ran', repeatLabel: 'Run again' };


test('a second generate or run is blocked after success', () => {
  let generate = arm(createWriteGate({ lockAfterSuccess: true }));
  generate = settleWrite(acceptWrite(generate, { ready: true }).gate, { ok: true });
  assert.equal(generate.confirmed, false);
  const finished = button(arm(generate), GENERATE_LABELS);
  assert.equal(finished.enabled, false);
  assert.equal(finished.label, 'Generated');
  assert.equal(acceptWrite(arm(generate), { ready: true }).reason, 'finished');

  let run = arm(createWriteGate({ lockAfterSuccess: false }));
  run = settleWrite(acceptWrite(run, { ready: true }).gate, { ok: true });
  const disarmed = button(run, RUN_LABELS);
  assert.equal(disarmed.enabled, false);
  assert.equal(disarmed.label, 'Run again');
  assert.equal(disarmed.notice, 'This scenario already ran once in this dialog.');
  assert.equal(acceptWrite(run, { ready: true }).reason, 'confirmation');
  const rerun = acceptWrite(arm(run), { ready: true });
  assert.equal(rerun.accepted, true);
});


test('a failed generate or run needs a new confirmation before retry', () => {
  for (const lockAfterSuccess of [true, false]) {
    let gate = arm(createWriteGate({ lockAfterSuccess }));
    gate = settleWrite(acceptWrite(gate, { ready: true }).gate, { ok: false });
    assert.equal(gate.confirmed, false);
    assert.equal(acceptWrite(gate, { ready: true }).reason, 'confirmation');
    assert.equal(button(gate, lockAfterSuccess ? GENERATE_LABELS : RUN_LABELS).enabled, false);
    assert.equal(acceptWrite(arm(gate), { ready: true }).accepted, true);
  }
});


test('an in-flight write blocks another submit, dismiss, and step changes', () => {
  let gate = arm(createWriteGate({ lockAfterSuccess: true }));
  const started = acceptWrite(gate, { ready: true });
  assert.equal(started.accepted, true);
  assert.equal(started.gate.phase, 'in_flight');
  assert.equal(acceptWrite(started.gate, { ready: true }).reason, 'in_flight');
  assert.equal(writesInFlight([started.gate]), true);
  assert.equal(shouldDismissModal(writesInFlight([started.gate])), false);
  const duringWrite = dialogWriteDecision(started.gate, createWriteGate());
  for (const source of ['dismiss', 'confirm-button']) {
    const blocked = duringWrite.close({ source });
    assert.equal(blocked.close, false, source);
    assert.equal(blocked.needsConfirm, false, source);
    assert.equal(blocked.message, '', source);
    const pure = closeDialogDecision({ locked: false, source, inFlight: true });
    assert.equal(pure.close, false, source);
    assert.equal(pure.needsConfirm, false, source);
  }
  assert.equal(button(started.gate, GENERATE_LABELS).enabled, false);
  assert.equal(button(started.gate, GENERATE_LABELS).inFlight, true);
  const settled = settleWrite(started.gate, { ok: false });
  assert.equal(writesInFlight([settled]), false);
  assert.equal(shouldDismissModal(writesInFlight([settled])), true);
  assert.equal(shouldDismissModal(), true);
  assert.equal(shouldDismissModal(false), true);
});


test('a non-2xx response is a failed stage even when the body looks successful', () => {
  const body = {
    status: 'generated',
    scenario_version: '001',
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    scenario_name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
  };
  assert.equal(projectGenerate(body).success, true);
  const rejected = projectRejected({ status: 502, message: 'Service request failed.', scd2: body }, { route: 'generate' });
  assert.equal(rejected.success, false);
  assert.equal(rejected.ok, false);
  assert.equal(rejected.needsReview, true);
  assert.equal(rejected.outcome, 'needs_review');
  assert.equal(rejected.rejected, false);
  assert.equal(stageSucceeded(rejected), false);
  assert.equal(rejected.scenario_version, '');
  assert.match(rejected.message, /HTTP 502/);
  assert.match(rejected.message, /generated/);
  assert.match(rejected.message, /may have started or partly completed/);
  assert.equal(rejected.message.includes('Service request failed'), false);

  const detailed = projectRejected({
    status: 502,
    scd2: { status: 'run_failed', error_code: 'scenario_failed', error: 'session ERROR' },
  }, { route: 'run' });
  assert.equal(detailed.needsReview, true);
  assert.equal(detailed.outcome, 'needs_review');
  assert.match(detailed.message, /HTTP 502/);
  assert.match(detailed.message, /run_failed/);
  assert.match(detailed.message, /scenario_failed/);
  assert.match(detailed.message, /session ERROR/);
  assert.equal(stageSucceeded(detailed), false);

  const network = projectRejected(new Error('Failed to fetch'), { route: 'generate' });
  assert.equal(network.success, false);
  assert.equal(network.needsReview, true);
  assert.equal(network.http_status, null);
  assert.match(network.message, /Failed to fetch/);
});


test('status ok with parity false is a mismatch warning', () => {
  const mismatch = projectPostflight({
    status: 'ok',
    source_current_count_equal: false,
    business_parity_proven: false,
    physical_noop_proven: false,
    history_overlap_checked: false,
    stock_contract_valid: true,
    counts: { source_count: 6, target_current: 5, target_total: 5, rows_with_effective_to_set: 0 },
  });
  assert.equal(mismatch.success, false);
  assert.equal(mismatch.warning, true);
  assert.equal(mismatch.message, 'Source and current counts do not match.');
  assert.equal(mismatch.parity.find(flag => flag.key === 'source_current_count_equal').label, 'Source row count equals current row count');
  assert.equal(mismatch.parity.find(flag => flag.key === 'source_current_count_equal').text, 'false');
  assert.equal(mismatch.parity.find(flag => flag.key === 'business_parity_proven').text, 'not proven by this check');
  assert.equal(mismatch.parity.find(flag => flag.key === 'physical_noop_proven').text, 'not proven by this check');
  assert.equal(mismatch.parity.find(flag => flag.key === 'history_overlap_checked').text, 'not checked by this check');
  assert.equal(mismatch.statusScope, 'The count check passed. It does not prove row-level equality.');
  assert.equal(stageSucceeded(mismatch), false);

  const matched = projectPostflight({
    status: 'ok',
    source_current_count_equal: true,
    business_parity_proven: false,
    physical_noop_proven: false,
    history_overlap_checked: false,
    stock_contract_valid: true,
    counts: { source_count: 5, target_current: 5, target_total: 5, rows_with_effective_to_set: 0 },
  });
  assert.equal(matched.success, true);
  assert.equal(matched.warning, false);
  assert.equal(matched.parity.find(flag => flag.key === 'source_current_count_equal').label, 'Source row count equals current row count');
  assert.equal(matched.parity.find(flag => flag.key === 'source_current_count_equal').text, 'true');
  assert.equal(matched.parity.find(flag => flag.key === 'business_parity_proven').text, 'not proven by this check');
  assert.equal(matched.parity.find(flag => flag.key === 'physical_noop_proven').text, 'not proven by this check');
  assert.equal(matched.parity.find(flag => flag.key === 'history_overlap_checked').text, 'not checked by this check');
  assert.equal(matched.statusScope, 'The count check passed. It does not prove row-level equality.');
  for (const flag of matched.parity) {
    if (['business_parity_proven', 'physical_noop_proven', 'history_overlap_checked'].includes(flag.key)) {
      assert.equal(flag.text === 'false', false, flag.key);
    }
  }
});


test('a missing parity flag is not reported', () => {
  const evidence = projectPostflight({
    status: 'ok',
    counts: { target_total: 5, target_current: 5, rows_with_effective_to_set: 0 },
  });
  assert.equal(evidence.success, true);
  assert.equal(evidence.warning, false);
  for (const key of [
    'source_current_count_equal',
    'business_parity_proven',
    'physical_noop_proven',
    'history_overlap_checked',
    'stock_contract_valid',
  ]) {
    assert.equal(evidence.parity.find(flag => flag.key === key).text, 'not reported', key);
  }
  const explicitNull = projectPostflight({ status: 'ok', source_current_count_equal: null, counts: {} });
  assert.equal(explicitNull.parity.find(flag => flag.key === 'source_current_count_equal').text, 'not reported');
  assert.equal(explicitNull.success, true);
});


test('a malformed prefill key is dropped instead of split', () => {
  const badKeys = ['CUST ID', 'A,B', 'A;B', '"CUST_ID"', "'CUST_ID'"];
  for (const bad of badKeys) {
    const form = applyPrefill(customerForm(), { key_columns: [bad] });
    assert.equal(form.natural_key_text, '', bad);
    assert.equal(form.prefillNotes.natural_key_text, 'The suggested key was invalid.', bad);
    const validated = validateScd2Form(form);
    assert.equal(validated.ok, false, bad);
    assert.deepEqual(validated.request.natural_key, [], bad);
  }

  const mixed = applyPrefill(customerForm(), { key_columns: ['CUST_ID', 'A,B'] });
  assert.equal(mixed.natural_key_text, '');
  assert.equal(mixed.prefillNotes.natural_key_text, 'The suggested key was invalid.');

  for (const bad of ['CUST ID', 'A,B', 'A;B', '"ODS_CUSTOMERS"']) {
    const form = applyPrefill(emptyScd2Form(), { source_table: bad, mapping_name: 'MAP NAME', target_table: bad });
    assert.equal(form.source_table, '', bad);
    assert.equal(form.target_table, '', bad);
    assert.equal(form.mapping_name, '');
    assert.equal(form.prefillNotes.source_table, 'The suggested source table was invalid.');
    assert.equal(form.prefillNotes.mapping_name, 'The suggested mapping name was invalid.');
    assert.equal(form.prefillNotes.target_table, 'The suggested target table was invalid.');
  }

  const valid = applyPrefill(emptyScd2Form(), { key_columns: ['cust_id', 'LOC_ID'], source_table: 'ODS_CUSTOMERS' });
  assert.equal(valid.natural_key_text, 'cust_id, LOC_ID');
  assert.equal(valid.prefillNotes.natural_key_text, undefined);
  assert.equal(valid.source_table, 'ODS_CUSTOMERS');
});


test('proof flags say proven or checked, and never the raw word false', () => {
  const proven = projectPostflight({
    status: 'ok',
    business_parity_proven: true,
    physical_noop_proven: true,
    history_overlap_checked: true,
    counts: { target_total: 5, target_current: 5, rows_with_effective_to_set: 0 },
  });
  assert.equal(proven.parity.find(flag => flag.key === 'business_parity_proven').text, 'proven');
  assert.equal(proven.parity.find(flag => flag.key === 'physical_noop_proven').text, 'proven');
  assert.equal(proven.parity.find(flag => flag.key === 'history_overlap_checked').text, 'checked');
  assert.equal(proven.statusScope, STATUS_OK_SCOPE);

  const absent = projectPostflight({ status: 'ok', counts: {} });
  for (const key of ['business_parity_proven', 'physical_noop_proven', 'history_overlap_checked']) {
    assert.equal(absent.parity.find(flag => flag.key === key).text, 'not reported', key);
  }
  assert.equal(projectPostflight({ status: 'invalid_stock_contract', counts: {} }).statusScope, '');
});


function lockedAfter(error) {
  const view = projectRejected(error, { write: true });
  let gate = arm(createWriteGate({ lockAfterSuccess: true }));
  gate = settleWrite(acceptWrite(gate, { ready: true }).gate, { review: view.needsReview === true, ok: view.success === true });
  return { view, gate };
}


test('unknown, partial, and transport failures need review and stay locked', () => {
  const cases = [
    {
      name: 'run_unknown',
      error: {
        status: 502,
        message: 'Service request failed.',
        scd2: {
          status: 'run_unknown',
          outcome_unknown: true,
          manual_review_required: true,
          scenario_execution_completed: null,
          steps: [{ step: 'odi_scenario_run', outcome_unknown: true, remote: { run_may_have_started: true } }],
        },
      },
      status: /run_unknown/,
    },
    {
      name: 'run_failed manual review',
      error: {
        status: 502,
        scd2: {
          status: 'run_failed',
          manual_review_required: true,
          scenario_execution_completed: false,
          steps: [{ step: 'odi_scenario_run', status: 'failed', side_effects: true, remote: { run_may_have_started: true } }],
        },
      },
      status: /run_failed/,
    },
    {
      name: 'generate partial side effects',
      error: {
        status: 502,
        scd2: {
          status: 'partial_failure_target_ddl',
          known_side_effects: true,
          manual_review_required: true,
          outcome_unknown: false,
          steps: [{ step: 'target_ddl', status: 'partial_failure', sequence_created: true, target_created: false }],
        },
      },
      status: /partial_failure_target_ddl/,
    },
    {
      name: 'network',
      error: new Error('Failed to fetch'),
      status: /Failed to fetch/,
    },
    {
      name: 'timeout',
      error: new Error('timeout'),
      status: /timeout/,
    },
  ];

  for (const item of cases) {
    const { view, gate } = lockedAfter(item.error);
    assert.equal(view.needsReview, true, item.name);
    assert.equal(view.success, false, item.name);
    assert.equal(view.outcome, 'needs_review', item.name);
    assert.match(view.message, item.status, item.name);
    assert.match(view.message, /may have started or partly completed/, item.name);
    assert.equal(view.message.trim().toLowerCase() === 'failed', false, item.name);
    assert.equal(view.message.includes(REVIEW_SENTENCE), true, item.name);
    const again = acceptWrite(arm(gate), { ready: true });
    assert.equal(again.accepted, false, item.name);
    assert.equal(again.reason, 'needs_review', item.name);
    assert.equal(setWriteConfirmation(gate, true).confirmed, false, item.name);
    assert.equal(canRefreshEvidence({ loading: false, gates: [gate] }), true, item.name);
    assert.equal(writesInFlight([gate]), false, item.name);
    assert.equal(shouldDismissModal(writesInFlight([gate])), true, item.name);
    assert.equal(button(gate, GENERATE_LABELS).enabled, false, item.name);
  }
});


test('a definite failure can be retried after a fresh confirmation', () => {
  const view = projectRejected({
    status: 409,
    scd2: {
      status: 'blocked_target_not_scd2',
      manual_review_required: false,
      outcome_unknown: false,
      steps: [{ step: 'odi_scenario_run', status: 'blocked', side_effects: false, remote: { run_may_have_started: false } }],
    },
  }, { write: true });
  assert.equal(view.needsReview, false);
  assert.equal(view.outcome, 'failed');
  assert.equal(view.rejected, true);
  assert.match(view.message, /blocked_target_not_scd2/);
  assert.equal(view.message.includes(REVIEW_SENTENCE), false);

  let gate = settleWrite(acceptWrite(arm(createWriteGate()), { ready: true }).gate, { ok: false });
  assert.equal(acceptWrite(gate, { ready: true }).reason, 'confirmation');
  assert.equal(acceptWrite(arm(gate), { ready: true }).accepted, true);

  const cleanRun = projectRun({
    status: 'run_failed',
    manual_review_required: false,
    outcome_unknown: false,
    known_side_effects: false,
    scenario_execution_completed: false,
    scenario: { name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN', version: '001', context: 'GLOBAL' },
    steps: [{ step: 'odi_scenario_run', status: 'blocked', side_effects: false, remote: { run_may_have_started: false } }],
    postflight: { status: 'ok', counts: { target_total: 1, target_current: 1, rows_with_effective_to_set: 0 } },
  });
  assert.equal(cleanRun.needsReview, true);
  assert.equal(cleanRun.outcome, 'needs_review');
  assert.equal(cleanRun.success, false);
});


test('a completed run with postflight failure and a may-have flag need review', () => {
  const postflightFailed = projectRejected({
    status: 502,
    scd2: {
      status: 'run_completed_postflight_failed',
      manual_review_required: true,
      scenario_execution_completed: true,
      scenario: { name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN', version: '001', context: 'GLOBAL' },
      postflight: { status: 'error', counts: {} },
    },
  }, { write: true });
  assert.equal(postflightFailed.needsReview, true);
  assert.match(postflightFailed.message, /run_completed_postflight_failed/);
  assert.equal(postflightFailed.evidence.status, 'error');

  const mayHaveOnly = projectRejected({
    status: 502,
    scd2: {
      status: 'run_failed',
      steps: [{ step: 'odi_scenario_run', remote: { run_may_have_started: true } }],
    },
  }, { write: true });
  assert.equal(mayHaveOnly.needsReview, true);

  const unknownOnly = projectGenerate({
    status: 'partial_failure_scenario_generate',
    outcome_unknown: true,
    known_side_effects: true,
    manual_review_required: true,
  });
  assert.equal(unknownOnly.needsReview, true);
  assert.equal(unknownOnly.success, false);
  assert.match(unknownOnly.message, /partial_failure_scenario_generate/);

  const generated = projectGenerate({
    ...GENERATED,
    outcome_unknown: false,
    known_side_effects: true,
    manual_review_required: false,
    steps: [{ step: 'odi_scenario_run', status: 'passed', side_effects: true, remote: { run_may_have_started: true } }],
  });
  assert.equal(generated.success, true);
  assert.equal(generated.outcome, 'succeeded');
  assert.equal(generated.needsReview, false);
});


test('a fresh plan does not unlock a write that needs review', () => {
  const { gate } = lockedAfter({ status: 502, scd2: { status: 'run_unknown', manual_review_required: true } });
  const replacement = createWriteGate({ lockAfterSuccess: true });
  assert.equal(replaceWriteGate(gate, replacement), gate);
  assert.equal(replaceWriteGate(createWriteGate(), replacement), replacement);
});


test('prefill accepts both hand-off spellings and lets a valid canonical value win', () => {
  assert.deepEqual(PREFILL_ALIASES.map(spec => [spec.field, spec.canonical, spec.alias]), [
    ['source_owner', 'source_schema', 'src_schema'],
    ['source_table', 'source_table', 'src_table'],
    ['target_owner', 'target_schema', 'tgt_owner'],
    ['target_table', 'target_table', 'tgt_table'],
  ]);

  const wizard = applyPrefill(emptyScd2Form(), {
    src_schema: 'hr',
    src_table: 'ODS_CUSTOMERS',
    tgt_owner: 'dm',
    tgt_table: 'DIM_CUSTOMER',
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    key_columns: ['CUST_ID'],
    src_model: 'ORACLE_DWH',
    load_type: 'type2',
  });
  assert.equal(wizard.source_owner, 'HR');
  assert.equal(wizard.source_table, 'ODS_CUSTOMERS');
  assert.equal(wizard.target_owner, 'DM');
  assert.equal(wizard.target_table, 'DIM_CUSTOMER');
  assert.equal(wizard.src_model, undefined);
  assert.equal(wizard.load_type, undefined);

  const conflict = applyPrefill(emptyScd2Form(), {
    source_schema: 'HR',
    src_schema: 'DWH',
    source_table: 'ODS_CUSTOMERS',
    src_table: 'OTHER_TABLE',
    target_schema: 'DM',
    tgt_owner: 'DWH',
    target_table: 'DIM_CUSTOMER',
    tgt_table: 'OTHER_TARGET',
  });
  assert.equal(conflict.source_owner, 'HR');
  assert.equal(conflict.source_table, 'ODS_CUSTOMERS');
  assert.equal(conflict.target_owner, 'DM');
  assert.equal(conflict.target_table, 'DIM_CUSTOMER');

  const aliasFillsInvalidCanonical = applyPrefill(emptyScd2Form(), {
    source_schema: 'NOPE',
    src_schema: 'SH',
    target_schema: '',
    tgt_owner: 'DM',
  });
  assert.equal(aliasFillsInvalidCanonical.source_owner, 'SH');
  assert.equal(aliasFillsInvalidCanonical.prefillNotes.source_owner, undefined);
  assert.equal(aliasFillsInvalidCanonical.target_owner, 'DM');

  const invalid = applyPrefill(emptyScd2Form(), {
    src_schema: 'SYS',
    src_table: 'BAD NAME',
    tgt_owner: 'HR',
    tgt_table: 'A,B',
    source_schema: 'NOT_AN_OWNER',
    target_table: '"DIM_CUSTOMER"',
  });
  assert.equal(invalid.source_owner, '');
  assert.equal(invalid.source_table, '');
  assert.equal(invalid.target_owner, '');
  assert.equal(invalid.target_table, '');
  assert.equal(invalid.prefillNotes.source_owner, 'The suggested source owner was invalid.');
  assert.equal(invalid.prefillNotes.source_table, 'The suggested source table was invalid.');
  assert.equal(invalid.prefillNotes.target_owner, 'The suggested target owner was invalid.');
  assert.equal(invalid.prefillNotes.target_table, 'The suggested target table was invalid.');
});


test('the mapping wizard hand-off fills the Type 2 identity fields', () => {
  const action = buildScd2Handoff({
    src_model: 'ORACLE_DWH',
    src_table: 'ODS_CUSTOMERS',
    src_schema: 'DWH',
    tgt_model: 'ORACLE_DM',
    tgt_owner: 'DM',
    tgt_table: 'DIM_CUSTOMER',
    needs_skey: false,
    skey_natural_key: '',
    description: 'Synthetic mapping',
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    drop_target_if_exists: false,
    load_type: 'type2',
    key_columns: 'cust_id',
  });
  const request = action.payload.props.mappingRequest;
  assert.equal(request.src_schema, 'DWH');
  assert.equal(request.src_table, 'ODS_CUSTOMERS');
  assert.equal(request.tgt_owner, 'DM');
  assert.equal(request.tgt_table, 'DIM_CUSTOMER');
  const form = applyPrefill(emptyScd2Form(), request);
  assert.equal(form.source_owner, 'DWH');
  assert.equal(form.source_table, 'ODS_CUSTOMERS');
  assert.equal(form.target_owner, 'DM');
  assert.equal(form.target_table, 'DIM_CUSTOMER');
  assert.equal(form.mapping_name, 'MAP_ODS_CUST_TO_DIM_CUST');
  assert.equal(form.natural_key_text, 'CUST_ID');
  assert.equal(form.load_type, undefined);
  assert.equal(form.drop_target_if_exists, undefined);
  const identity = validateScd2Form(form);
  assert.deepEqual(Object.keys(identity.errors), ['tracked_text']);
  const ready = validateScd2Form({ ...form, tracked_mode: 'all' });
  assert.equal(ready.ok, true);
  assert.equal(ready.request.source_owner, 'DWH');
  assert.equal(ready.request.source_table, 'ODS_CUSTOMERS');
  assert.equal(ready.request.target_owner, 'DM');
  assert.equal(ready.request.target_table, 'DIM_CUSTOMER');
  assert.equal(ready.request.mapping_name, 'MAP_ODS_CUST_TO_DIM_CUST');
  assert.deepEqual(ready.request.natural_key, ['CUST_ID']);
});


const QUIET_FLAGS = {
  outcome_unknown: false,
  manual_review_required: false,
  known_side_effects: false,
};

function quietStatus(status, extra = {}) {
  return { status, ...QUIET_FLAGS, ...extra };
}

function assertWriteClass(row) {
  const outcome = classifyWriteOutcome({
    route: row.route,
    httpStatus: row.http,
    payload: row.payload,
    resolved: row.resolved === true,
  });
  assert.equal(outcome, row.expected, row.name);
  if (row.resolved === true) {
    const view = row.route === 'run' ? projectRun(row.payload) : projectGenerate(row.payload);
    assert.equal(view.outcome, row.expected, `${row.name} view`);
    assert.equal(view.success, row.expected === 'succeeded', `${row.name} success`);
    return;
  }
  const error = row.payload == null
    ? { status: row.http, message: row.name }
    : { status: row.http, message: row.name, scd2: row.payload };
  const view = projectRejected(error, { route: row.route });
  assert.equal(view.outcome, row.expected, `${row.name} rejected`);
  if (row.expected === 'needs_review') {
    assert.equal(view.needsReview, true, row.name);
    assert.equal(view.rejected, false, row.name);
    assert.equal(view.message.includes(REVIEW_SENTENCE), true, row.name);
  } else if (row.expected === 'failed') {
    assert.equal(view.needsReview, false, row.name);
    assert.equal(view.rejected, true, row.name);
    assert.equal(view.message.includes(REVIEW_SENTENCE), false, row.name);
  }
}

test('every reviewed generate and run outcome uses the closed failure table', () => {
  const approval = { error: 'approved:true is required for SCD2 generation.' };
  const redacted500 = { error: 'SCD2 generation failed.', error_type: 'RuntimeError' };
  const generatedReject = {
    status: 'generated',
    scenario_version: '001',
    mapping_name: 'MAP_ODS_CUST_TO_DIM_CUST',
    scenario_name: 'MAP_ODS_CUST_TO_DIM_CUST_SCN',
    ...QUIET_FLAGS,
  };
  const generateBlocked = [
    'blocked_dm_model_missing',
    'blocked_target_model_missing',
    'blocked_target_model_not_oracle',
    'blocked_source_model_missing',
    'blocked_source_datastore_missing',
    'blocked_project_folder_missing',
    'blocked_lkm_missing',
    'blocked_scenario_name_collision',
    'blocked_mapping_exists_unrelated',
    'blocked_existing_object_mismatch',
    'blocked_execution_identity',
    'blocked_grantee_missing',
    'blocked_target_exists_mismatch',
  ];
  const runBlocked = [
    'blocked_target_not_scd2',
    'blocked_scenario_name_collision',
    'blocked_scenario_missing',
    'blocked_existing_object_mismatch',
    'blocked_scenario_version_missing',
    'blocked_execution_identity',
  ];
  const runUnavailable = ['blocked_agent_preflight', 'target_metadata_unavailable', 'blocked_count_preflight'];
  const partials = [
    'partial_failure_target_ddl',
    'partial_failure_target_grants',
    'partial_failure_ikm_import',
    'partial_failure_datastore_register',
    'partial_failure_mapping_create',
    'partial_failure_scenario_generate',
  ];
  const rows = [
    {
      name: 'generate 200 generated',
      route: 'generate',
      http: 200,
      resolved: true,
      expected: 'succeeded',
      payload: {
        ...GENERATED,
        ...QUIET_FLAGS,
        steps: GENERATED.steps.map(step => (
          step.step === 'target_ddl' ? { ...step, target_created: true, grants_applied: 2 } : step
        )),
      },
    },
    {
      name: 'run 200 run_completed',
      route: 'run',
      http: 200,
      resolved: true,
      expected: 'succeeded',
      payload: {
        ...RUN_COMPLETED,
        outcome_unknown: false,
        manual_review_required: false,
        scenario_execution_completed: true,
        steps: [
          { step: 'remote_agent_health', status: 'passed' },
          { step: 'odi_scenario_run', status: 'passed', session_status: 'DONE', side_effects: true, run_may_have_started: true },
        ],
        postflight: { ...RUN_COMPLETED.postflight, source_current_count_equal: true, stock_contract_valid: true },
      },
    },
    { name: 'generate 400 invalid_request', route: 'generate', http: 400, expected: 'failed', payload: quietStatus('invalid_request', { error_code: 'target_owner_not_allowed' }) },
    { name: 'generate 400 blocked_until_approval', route: 'generate', http: 400, expected: 'failed', payload: quietStatus('blocked_until_approval') },
    { name: 'generate 400 approval error', route: 'generate', http: 400, expected: 'failed', payload: approval },
    { name: 'generate 404 blocked_source_not_found', route: 'generate', http: 404, expected: 'failed', payload: quietStatus('blocked_source_not_found') },
    { name: 'generate 503 source_metadata_unavailable', route: 'generate', http: 503, expected: 'failed', payload: quietStatus('source_metadata_unavailable') },
    ...generateBlocked.map(status => ({
      name: `generate 409 ${status}`,
      route: 'generate',
      http: 409,
      expected: 'failed',
      payload: quietStatus(status),
    })),
    { name: 'generate 502 failed_odi_probe', route: 'generate', http: 502, expected: 'failed', payload: quietStatus('failed_odi_probe') },
    { name: 'generate 409 blocked_ddl_privilege', route: 'generate', http: 409, expected: 'failed', payload: quietStatus('blocked_ddl_privilege') },
    { name: 'generate 409 blocked_name_in_use', route: 'generate', http: 409, expected: 'failed', payload: quietStatus('blocked_name_in_use') },
    { name: 'generate 502 failed', route: 'generate', http: 502, expected: 'failed', payload: quietStatus('failed') },
    { name: 'generate 429', route: 'generate', http: 429, expected: 'failed', payload: null },
    { name: 'generate 502 failed_odi_probe review', route: 'generate', http: 502, expected: 'needs_review', payload: quietStatus('failed_odi_probe', { outcome_unknown: true, manual_review_required: true }) },
    { name: 'generate 409 blocked_ddl_privilege review', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_ddl_privilege', { outcome_unknown: true, manual_review_required: true }) },
    { name: 'generate 409 blocked_name_in_use review', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_name_in_use', { outcome_unknown: true, manual_review_required: true }) },
    { name: 'generate 502 failed review', route: 'generate', http: 502, expected: 'needs_review', payload: quietStatus('failed', { outcome_unknown: true, manual_review_required: true }) },
    { name: 'generate blocked_target_incomplete sequence', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_target_incomplete', { outcome_unknown: true, manual_review_required: true }) },
    { name: 'generate blocked_target_incomplete grants', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_target_incomplete', { grants_applied: 4, grants_failed: 1 }) },
    // Flags are false and the applied counter is zero. A flags-false check classifies this as failed.
    { name: 'flags-false blocked_target_incomplete', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_target_incomplete', { grants_applied: 0, grants_failed: 1 }) },
    ...partials.map(status => ({
      name: `generate 502 ${status}`,
      route: 'generate',
      http: 502,
      expected: 'needs_review',
      payload: quietStatus(status),
    })),
    { name: 'run 400 invalid_request', route: 'run', http: 400, expected: 'failed', payload: quietStatus('invalid_request') },
    { name: 'run 400 blocked_until_approval', route: 'run', http: 400, expected: 'failed', payload: quietStatus('blocked_until_approval') },
    { name: 'run 400 approval error', route: 'run', http: 400, expected: 'failed', payload: approval },
    ...runBlocked.map(status => ({
      name: `run 409 ${status}`,
      route: 'run',
      http: 409,
      expected: 'failed',
      payload: quietStatus(status),
    })),
    ...runUnavailable.map(status => ({
      name: `run 503 ${status}`,
      route: 'run',
      http: 503,
      expected: 'failed',
      payload: quietStatus(status),
    })),
    { name: 'run 502 failed_odi_probe', route: 'run', http: 502, expected: 'failed', payload: quietStatus('failed_odi_probe') },
    { name: 'run 429', route: 'run', http: 429, expected: 'failed', payload: null },
    { name: 'run 502 run_unknown', route: 'run', http: 502, expected: 'needs_review', payload: { status: 'run_unknown', scenario_execution_completed: null, manual_review_required: true, steps: [{ run_may_have_started: true }] } },
    { name: 'run 502 run_failed', route: 'run', http: 502, expected: 'needs_review', payload: { status: 'run_failed', scenario_execution_completed: false, manual_review_required: true, steps: [{ run_may_have_started: true }] } },
    { name: 'run 502 run_completed_postflight_failed', route: 'run', http: 502, expected: 'needs_review', payload: { status: 'run_completed_postflight_failed', scenario_execution_completed: true, manual_review_required: true, steps: [{ run_may_have_started: true }] } },
    // No warning flags. The old check treated these as definite failures.
    { name: 'flags-false generate 500', route: 'generate', http: 500, expected: 'needs_review', payload: redacted500 },
    { name: 'flags-false run 500', route: 'run', http: 500, expected: 'needs_review', payload: redacted500 },
    { name: 'flags-false generate 502 generated', route: 'generate', http: 502, expected: 'needs_review', payload: generatedReject },
    { name: 'flags-false run 502 generated', route: 'run', http: 502, expected: 'needs_review', payload: generatedReject },
    { name: 'near-miss manual_review_required', route: 'run', http: 409, expected: 'needs_review', payload: quietStatus('blocked_target_not_scd2', { manual_review_required: true }) },
    { name: 'near-miss grants_applied', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_grantee_missing', { grants_applied: 4 }) },
    { name: 'near-miss nested grants_applied', route: 'generate', http: 409, expected: 'needs_review', payload: quietStatus('blocked_grantee_missing', { steps: [{ grants_applied: 4 }] }) },
    { name: 'near-miss wrong http', route: 'generate', http: 500, expected: 'needs_review', payload: quietStatus('blocked_dm_model_missing') },
  ];

  for (const row of rows) assertWriteClass(row);

  for (const row of DEFINITE_FAILURE_ROWS) {
    const payload = row.status
      ? quietStatus(row.status)
      : (row.http === 429 ? null : approval);
    assertWriteClass({
      name: `table ${row.route} ${row.http} ${row.status || 'no-status'}`,
      route: row.route,
      http: row.http,
      payload,
      expected: 'failed',
    });
    if (!row.status) continue;
    assertWriteClass({
      name: `table ${row.route} ${row.status} manual_review_required`,
      route: row.route,
      http: row.http,
      payload: quietStatus(row.status, { manual_review_required: true }),
      expected: 'needs_review',
    });
    assertWriteClass({
      name: `table ${row.route} ${row.status} grants_applied`,
      route: row.route,
      http: row.http,
      payload: quietStatus(row.status, { grants_applied: 4 }),
      expected: 'needs_review',
    });
    assertWriteClass({
      name: `table ${row.route} ${row.status} wrong http`,
      route: row.route,
      http: row.http === 409 ? 400 : 409,
      payload: quietStatus(row.status),
      expected: 'needs_review',
    });
  }

  for (const route of ['generate', 'run']) {
    const timeout = projectRejected(Object.assign(new Error('timeout'), { status: undefined }), { route });
    assert.equal(timeout.outcome, 'needs_review', route);
    assert.equal(timeout.http_status, null, route);
  }

  const rateLimit = projectRejected({ status: 429, message: 'Too many requests. Try again shortly.' }, { route: 'generate' });
  assert.equal(rateLimit.outcome, 'failed');
  let gate = settleWrite(acceptWrite(arm(createWriteGate()), { ready: true }).gate, { ok: false, review: false });
  assert.equal(acceptWrite(arm(gate), { ready: true }).accepted, true);
});


test('a needs-review gate locks both writes, every step, and close', () => {
  let generate = arm(createWriteGate({ lockAfterSuccess: true }));
  generate = settleWrite(acceptWrite(generate, { ready: true }).gate, { ok: true });
  let run = arm(createWriteGate());
  run = settleWrite(acceptWrite(run, { ready: true }).gate, { review: true });
  const runView = { needsReview: true, detail: 'run_unknown', status: 'run_unknown', message: REVIEW_SENTENCE };

  const afterRun = dialogWriteDecision(generate, run);
  const planned = afterRun.afterPlan(createWriteGate({ lockAfterSuccess: true }), createWriteGate());
  assert.equal(planned.reset, false);
  assert.equal(planned.generateGate, generate);
  assert.equal(planned.runGate, run);
  const reconfirmed = setWriteConfirmation(planned.generateGate, true);
  const generateAttempt = dialogWriteDecision(planned.generateGate, planned.runGate).accept(reconfirmed, { ready: true });
  assert.equal(generateAttempt.accepted, false);
  assert.equal(generateAttempt.reason, 'needs_review');
  assert.equal(canRefreshEvidence({ loading: false, gates: [planned.generateGate, planned.runGate] }), true);

  let generateReview = arm(createWriteGate({ lockAfterSuccess: true }));
  generateReview = settleWrite(acceptWrite(generateReview, { ready: true }).gate, { review: true });
  const runArmed = arm(createWriteGate());
  assert.equal(acceptWrite(runArmed, { ready: true }).accepted, true);
  const runAttempt = dialogWriteDecision(generateReview, runArmed).accept(runArmed, { ready: true });
  assert.equal(runAttempt.accepted, false);
  assert.equal(runAttempt.reason, 'needs_review');
  const keptGenerate = dialogWriteDecision(generateReview, runArmed).afterPlan(
    createWriteGate({ lockAfterSuccess: true }),
    createWriteGate(),
  );
  assert.equal(keptGenerate.reset, false);
  assert.equal(keptGenerate.generateGate, generateReview);

  for (const step of [0, 1, 2, 3]) {
    const banners = afterRun.banners({ step, generateView: null, runView });
    assert.equal(banners.length, 1, `step ${step}`);
    assert.equal(banners[0].detail, 'run_unknown', `step ${step}`);
  }
  const generateBanners = dialogWriteDecision(generateReview, createWriteGate());
  for (const step of [0, 1, 2, 3]) {
    assert.equal(generateBanners.banners({ step, generateView: { needsReview: true, detail: 'partial_failure_target_ddl' }, runView: null }).length, 1, `generate step ${step}`);
  }

  const open = dialogWriteDecision(createWriteGate({ lockAfterSuccess: true }), createWriteGate());
  for (const step of [0, 1, 2, 3]) {
    assert.equal(open.banners({ step, generateView: null, runView: null }).length, 0, `clear step ${step}`);
  }
  const unlocked = open.close({ source: 'dismiss' });
  assert.equal(unlocked.close, true);
  assert.equal(unlocked.needsConfirm, false);
  assert.equal(unlocked.message, '');
  assert.equal(closeDialogDecision({ locked: false, source: 'dismiss' }).close, true);

  for (const times of [1, 2, 10]) {
    const decision = dialogWriteDecision(generate, run);
    for (let attempt = 1; attempt <= times; attempt += 1) {
      const result = decision.close({ source: 'dismiss' });
      assert.equal(result.close, false, `dismiss x${times} #${attempt}`);
      assert.equal(result.needsConfirm, true, `dismiss x${times} #${attempt}`);
      assert.equal(result.message, UNRESOLVED_CLOSE_SENTENCE, `dismiss x${times} #${attempt}`);
      const pure = closeDialogDecision({ locked: true, source: 'dismiss' });
      assert.equal(pure.close, false, `pure dismiss x${times} #${attempt}`);
      assert.equal(pure.needsConfirm, true, `pure dismiss x${times} #${attempt}`);
      assert.equal(pure.message, UNRESOLVED_CLOSE_SENTENCE, `pure dismiss x${times} #${attempt}`);
    }
  }
  // The old second-dismiss path passed confirmed:true. That flag is not a source.
  const armedDismiss = afterRun.close({ source: 'dismiss', confirmed: true });
  assert.equal(armedDismiss.close, false);
  assert.equal(armedDismiss.needsConfirm, true);
  assert.equal(afterRun.close({ confirmed: true }).close, false);

  const confirmed = afterRun.close({ source: 'confirm-button' });
  assert.equal(confirmed.close, true);
  assert.equal(confirmed.needsConfirm, false);
  assert.equal(confirmed.message, '');
  const pureConfirm = closeDialogDecision({ locked: true, source: 'confirm-button' });
  assert.equal(pureConfirm.close, true);
  assert.equal(pureConfirm.needsConfirm, false);

  let flying = arm(createWriteGate({ lockAfterSuccess: true }));
  flying = acceptWrite(flying, { ready: true }).gate;
  const lockedAndFlying = dialogWriteDecision(generate, flying);
  for (const source of ['dismiss', 'confirm-button']) {
    const blocked = lockedAndFlying.close({ source });
    assert.equal(blocked.close, false, source);
    assert.equal(blocked.needsConfirm, false, source);
    const pure = closeDialogDecision({ locked: true, source, inFlight: true });
    assert.equal(pure.close, false, source);
    assert.equal(pure.needsConfirm, false, source);
  }
});
