import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPrefill,
  buildGenerateRequest,
  buildPlanRequest,
  buildPostflightParams,
  buildRunRequest,
  canGenerate,
  emptyScd2Form,
  projectApiError,
  projectGenerate,
  projectPlan,
  projectPostflight,
  projectRun,
  validateScd2Form,
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
