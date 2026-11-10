import assert from 'node:assert/strict';
import test from 'node:test';

import {
  NOT_REPORTED,
  mappingRunOutcomeLines,
  projectMappingRunOutcome,
  projectRunRefresh,
} from './mappingRunOutcome.js';

// Shapes copied from mapping_run_service.py and the backend tests named below.
const DONE_REMOTE = {
  tool: 'odi.remote_scenario_run',
  status: 'passed',
  stage: 'completed',
  error_code: null,
  agent_contact_attempted: true,
  repository_connection_attempted: true,
  run_may_have_started: true,
  side_effects: true,
  outcome_unknown: false,
  session_status: 'DONE',
  raw_rows_included: false,
  pk_values_included: false,
  secret_printed: false,
};

const FAILED_REMOTE = {
  tool: 'odi.remote_scenario_run',
  status: 'failed',
  stage: 'run',
  error_code: 'ScenarioRunFailed',
  agent_contact_attempted: true,
  repository_connection_attempted: true,
  run_may_have_started: true,
  side_effects: true,
  outcome_unknown: false,
  session_status: 'ERROR',
  raw_rows_included: false,
  pk_values_included: false,
  secret_printed: false,
};

// test_odi87_fixed_mapping_run_gate.BLOCKED_PAYLOAD
const GATE_ERROR = 'The fixed DIM_PRODUCT mapping cannot run through the generic mapping run path until a separately reviewed cutover proof replaces this gate.';
const GATE_PAYLOAD = {
  status: 'blocked_fixed_mapping_cutover_pending',
  error: GATE_ERROR,
};

// test_other_mapping_full_path_is_unchanged / MappingRunSuccess
const SUCCESS_PAYLOAD = {
  status: 'SUCCESS',
  batch_id: 'RUN-ABCDEF123456',
  scenario: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
  version: '003',
  context: 'DEVELOPMENT',
  truncate_target_table: true,
  remote: DONE_REMOTE,
};

const OUTCOME_KEYS = [
  'batchId', 'context', 'counts', 'error', 'failedStep', 'mapping',
  'overallStatus', 'runMode', 'scenarioName', 'scenarioVersion',
  'sessionId', 'successful',
];

function rendered(outcome) {
  return mappingRunOutcomeLines(outcome).map(([label, value]) => `${label}: ${value}`).join('\n');
}

test('a success payload is successful only from its status field', () => {
  const outcome = projectMappingRunOutcome({ httpStatus: 200, payload: SUCCESS_PAYLOAD });
  assert.equal(outcome.successful, true);
  assert.equal(outcome.mapping, NOT_REPORTED);
  assert.equal(outcome.scenarioName, 'MAP_HR_EMPLOYEES_TO_ODS_SCN');
  assert.equal(outcome.scenarioVersion, '003');
  assert.equal(outcome.context, 'DEVELOPMENT');
  assert.equal(outcome.runMode, 'truncated');
  assert.equal(outcome.overallStatus, 'SUCCESS');
  assert.equal(outcome.failedStep, NOT_REPORTED);
  assert.equal(outcome.error, NOT_REPORTED);
  assert.equal(outcome.batchId, 'RUN-ABCDEF123456');
  assert.equal(outcome.sessionId, NOT_REPORTED);
  assert.equal(outcome.counts, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('DONE'), false);
  assert.equal(rendered(outcome).includes('odi.remote_scenario_run'), false);
});

test('kept rows are shown only for a literal false truncate flag', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: { ...SUCCESS_PAYLOAD, truncate_target_table: false, version: '001', context: 'GLOBAL' },
  });
  assert.equal(outcome.runMode, 'kept rows');
  assert.equal(projectMappingRunOutcome({
    httpStatus: 200,
    payload: { ...SUCCESS_PAYLOAD, truncate_target_table: 'false' },
  }).runMode, NOT_REPORTED);
});

test('a failed step comes from current_step on the saved run record', () => {
  // tests/test_api_endpoints.py mapping run history fixture, plus the step the service persists.
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      batch_id: 'RUN-001',
      mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
      scenario_name: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
      scenario_version: '001',
      overall_status: 'RUN_FAILED',
      current_step: 'run_scenario',
      truncate_target_table: true,
      started_at: '2026-06-18T19:44:00',
      updated_at: '2026-06-18T19:44:05',
      duration_seconds: 5,
      error: 'redacted short error',
    },
  });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.mapping, 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(outcome.scenarioName, 'MAP_HR_EMPLOYEES_TO_ODS_SCN');
  assert.equal(outcome.scenarioVersion, '001');
  assert.equal(outcome.context, NOT_REPORTED);
  assert.equal(outcome.runMode, 'truncated');
  assert.equal(outcome.overallStatus, 'RUN_FAILED');
  assert.equal(outcome.failedStep, 'run_scenario');
  assert.equal(outcome.error, 'redacted short error');
  assert.equal(outcome.batchId, 'RUN-001');
  assert.equal(rendered(outcome).includes('2026-06-18'), false);
  assert.equal(rendered(outcome).includes('duration'), false);
});

test('an HTTP failure body names the remote error code and not a session id', () => {
  // execute_mapping_run remote failure: {status, remote, batch_id}. The step is not in this body.
  const outcome = projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'failed', remote: FAILED_REMOTE, batch_id: 'RUN-001' },
  });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.overallStatus, 'failed');
  assert.equal(outcome.failedStep, NOT_REPORTED);
  assert.equal(outcome.error, 'ScenarioRunFailed');
  assert.equal(outcome.sessionId, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('ERROR'), false);
});

test('HTTP 200 with a non-success status is not successful', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      status: 'partial',
      mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
      scenario: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
      version: '001',
      context: 'GLOBAL',
      truncate_target_table: false,
      current_step: 'generate_scenario',
      batch_id: 'RUN-PARTIAL1',
    },
  });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.overallStatus, 'partial');
  assert.equal(outcome.failedStep, 'generate_scenario');
  assert.equal(outcome.runMode, 'kept rows');
  assert.equal(outcome.error, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('not successful'), true);
});

test('a success status still wins when an error string is also present', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: { status: 'SUCCESS', error: 'Scenario generation failed.', batch_id: 'RUN-OK' },
  });
  assert.equal(outcome.successful, true);
  assert.equal(outcome.error, 'Scenario generation failed.');
});

test('the 409 gate response shows its message and is not successful', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 409,
    payload: GATE_PAYLOAD,
    thrown: { message: 'Request conflicts with the current state.', status: 409 },
  });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.overallStatus, 'blocked_fixed_mapping_cutover_pending');
  assert.equal(outcome.error, GATE_ERROR);
  assert.equal(outcome.batchId, NOT_REPORTED);
  assert.equal(outcome.mapping, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('Request conflicts with the current state.'), false);
});

test('the IKM conflict message is the error text', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 409,
    payload: {
      status: 'ikm_option_unsupported',
      option: 'TRUNCATE_TARGET_TABLE',
      error: "The mapping's IKM does not expose the TRUNCATE_TARGET_TABLE option. Run with truncate_target_table=false or change the IKM.",
      batch_id: 'RUN-IKM1',
    },
  });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.overallStatus, 'ikm_option_unsupported');
  assert.equal(outcome.error.includes('TRUNCATE_TARGET_TABLE'), true);
  assert.equal(outcome.batchId, 'RUN-IKM1');
  assert.equal(rendered(outcome).includes('option:'), false);
});

test('a thrown network error has no run fields', () => {
  const outcome = projectMappingRunOutcome({ thrown: { message: 'Failed to fetch' } });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.error, 'Failed to fetch');
  assert.equal(outcome.overallStatus, NOT_REPORTED);
  assert.equal(outcome.batchId, NOT_REPORTED);
  assert.equal(outcome.sessionId, NOT_REPORTED);
  assert.equal(outcome.counts, NOT_REPORTED);
  assert.equal(outcome.failedStep, NOT_REPORTED);
});

test('missing fields and a body without a status are not reported and not successful', () => {
  const bare = projectMappingRunOutcome({ httpStatus: 200, payload: {} });
  assert.equal(bare.successful, false);
  for (const [, value] of mappingRunOutcomeLines(bare).slice(1)) {
    assert.equal(value, NOT_REPORTED);
  }

  const onlyStatus = projectMappingRunOutcome({ httpStatus: 200, payload: { status: 'SUCCESS' } });
  assert.equal(onlyStatus.successful, true);
  assert.equal(onlyStatus.mapping, NOT_REPORTED);
  assert.equal(onlyStatus.scenarioName, NOT_REPORTED);
  assert.equal(onlyStatus.scenarioVersion, NOT_REPORTED);
  assert.equal(onlyStatus.context, NOT_REPORTED);
  assert.equal(onlyStatus.runMode, NOT_REPORTED);
  assert.equal(onlyStatus.batchId, NOT_REPORTED);
  assert.equal(onlyStatus.sessionId, NOT_REPORTED);
  assert.equal(onlyStatus.counts, NOT_REPORTED);
  assert.equal(onlyStatus.failedStep, NOT_REPORTED);
});

test('an option-update failure shows the public error and the batch only', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 500,
    payload: { error: 'Scenario option update failed.', batch_id: 'RUN-OPT1' },
    thrown: { message: 'Service request failed.', status: 500 },
  });
  assert.equal(outcome.successful, false);
  assert.equal(outcome.overallStatus, NOT_REPORTED);
  assert.equal(outcome.failedStep, NOT_REPORTED);
  assert.equal(outcome.error, 'Scenario option update failed.');
  assert.equal(outcome.batchId, 'RUN-OPT1');
  assert.equal(rendered(outcome).includes('Service request failed.'), false);
});

test('counts render only finite numbers and a session id only from the top level', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      status: 'SUCCESS',
      session_id: 'SESS-100',
      row_count: 4,
      counts: { target_total: 3, source_current: 2, rows: [{ id: 1 }] },
    },
  });
  assert.equal(outcome.sessionId, 'SESS-100');
  assert.equal(outcome.counts, 'row_count=4, source_current=2, target_total=3');
});

test('raw data and secrets are not rendered', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      ...SUCCESS_PAYLOAD,
      password: 'password=top-secret',
      log: 'jdbc:oracle:thin:@secret-host/service',
      sql: 'select * from hr.employees',
      rows: [{ NAME: 'Alice Raw' }],
      validated_params: { mapping_name: 'MAP_FROM_PARAMS', password: 'password=from-params' },
      completed_steps: ['update_km', 'password=step'],
      remote: {
        ...DONE_REMOTE,
        session_id: 'SESS-SECRET-99',
        stdout: 'leaked row Alice Raw',
      },
      error: 'password=hidden-in-error',
    },
  });
  const text = rendered(outcome);
  assert.deepEqual(Object.keys(outcome).sort(), OUTCOME_KEYS);
  for (const secret of [
    'password=top-secret',
    'password=from-params',
    'password=step',
    'password=hidden-in-error',
    'jdbc:oracle',
    'Alice Raw',
    'select * from hr.employees',
    'SESS-SECRET-99',
    'MAP_FROM_PARAMS',
    'leaked row',
    'odi.remote_scenario_run',
  ]) {
    assert.equal(text.includes(secret), false, secret);
  }
  assert.equal(outcome.error, NOT_REPORTED);
  assert.equal(outcome.sessionId, NOT_REPORTED);
});

test('refresh applies only the saved record with the same batch id', () => {
  const current = projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'failed', remote: FAILED_REMOTE, batch_id: 'RUN-001' },
  });
  const refreshed = projectRunRefresh(current, {
    mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
    state: {
      batch_id: 'RUN-001',
      mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
      scenario_name: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
      scenario_version: '001',
      context: 'GLOBAL',
      truncate_target_table: false,
      current_step: 'run_scenario',
      overall_status: 'RUN_FAILED',
      error: 'Operation failed.',
    },
  });
  assert.equal(refreshed.applied, true);
  assert.equal(refreshed.outcome.failedStep, 'run_scenario');
  assert.equal(refreshed.outcome.mapping, 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(refreshed.outcome.overallStatus, 'RUN_FAILED');
  assert.equal(refreshed.outcome.error, 'ScenarioRunFailed');
  assert.equal(refreshed.outcome.successful, false);

  const other = projectRunRefresh(current, {
    state: { batch_id: 'RUN-OTHER', current_step: 'update_km', overall_status: 'RUN_FAILED' },
  });
  assert.equal(other.applied, false);
  assert.equal(other.outcome, current);

  const fromHistory = projectRunRefresh(current, {
    runs: [{
      batch_id: 'RUN-001',
      mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
      scenario_name: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
      scenario_version: '001',
      overall_status: 'RUN_FAILED',
      current_step: 'run_scenario',
      truncate_target_table: false,
      error: 'redacted short error',
    }],
    count: 1,
    limit: 50,
    status: null,
  });
  assert.equal(fromHistory.applied, true);
  assert.equal(fromHistory.outcome.failedStep, 'run_scenario');
  assert.equal(fromHistory.outcome.error, 'redacted short error');
  assert.equal(fromHistory.outcome.counts, NOT_REPORTED);
});

test('a gate response without a batch id is not replaced by saved state', () => {
  const current = projectMappingRunOutcome({ httpStatus: 409, payload: GATE_PAYLOAD });
  const refreshed = projectRunRefresh(current, {
    state: {
      batch_id: 'RUN-OLD',
      mapping_name: 'MAP_ODS_PRODUCTS_TO_DIM_PRODUCT',
      overall_status: 'SUCCEEDED',
      current_step: 'run_scenario',
    },
  });
  assert.equal(refreshed.applied, false);
  assert.equal(refreshed.outcome.error, GATE_ERROR);
  assert.equal(refreshed.outcome.successful, false);
});
