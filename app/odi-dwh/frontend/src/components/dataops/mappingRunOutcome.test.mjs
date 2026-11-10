import assert from 'node:assert/strict';
import test from 'node:test';

import {
  HISTORY_CHECK_TEXT,
  MAY_HAVE_RUN_TEXT,
  NOT_REPORTED,
  WITHHELD_ERROR,
  applyRefreshToPanels,
  mappingRunBadge,
  mappingRunOutcomeLines,
  mappingRunToast,
  projectMappingRunOutcome,
  projectRunRefresh,
  reconcileLastRuns,
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

const REQUEST = { mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS', truncate_target_table: false };
const REQUEST_TRUNCATE = { mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS', truncate_target_table: true };

const UNKNOWN_REMOTE = {
  status: 'unknown',
  error_code: 'ScenarioOutcomeUnknown',
  outcome_unknown: true,
  run_may_have_started: true,
  side_effects: true,
  session_status: null,
};

const TIMEOUT_REMOTE = {
  status: 'unknown',
  error_code: 'RemoteTimeout',
  outcome_unknown: true,
  run_may_have_started: true,
  side_effects: true,
};

const OUTCOME_KEYS = [
  'batchId', 'context', 'counts', 'error', 'failedStep', 'generation', 'mapping',
  'outcomeClass', 'overallStatus', 'requestedMapping', 'requestedRunMode', 'runMode',
  'scenarioName', 'scenarioVersion', 'sessionId', 'successful',
];

function assertUnknownWording(outcome, mappingName) {
  const badge = mappingRunBadge(outcome);
  const toast = mappingRunToast(outcome, mappingName);
  const text = [rendered(outcome), badge.label, badge.notice, toast.message].filter(Boolean).join('\n');
  assert.equal(outcome.outcomeClass, 'unknown');
  assert.equal(outcome.successful, false);
  assert.equal(text.includes(MAY_HAVE_RUN_TEXT), true);
  assert.equal(/not successful/i.test(text), false);
  assert.equal(/\bfailed\b/i.test(text), false);
  assert.equal(toast.type, 'warning');
  assert.equal(badge.tone, 'warning');
}

function assertRequested(outcome, mode) {
  const text = rendered(outcome);
  assert.equal(outcome.requestedMapping, 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(outcome.requestedRunMode, mode);
  assert.equal(text.includes(`Requested mapping: MAP_HR_EMPLOYEES_TO_ODS`), true);
  assert.equal(text.includes(`Requested run mode: ${mode}`), true);
  assert.equal(text.includes('Requested mapping: not reported'), false);
  assert.equal(text.includes('Requested run mode: not reported'), false);
}

function rendered(outcome) {
  return mappingRunOutcomeLines(outcome).map(([label, value]) => `${label}: ${value}`).join('\n');
}

test('a success payload is successful only from its status field', () => {
  const outcome = projectMappingRunOutcome({ httpStatus: 200, payload: SUCCESS_PAYLOAD });
  assert.equal(outcome.outcomeClass, 'succeeded');
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
  assert.equal(outcome.outcomeClass, 'failed');
  assert.equal(outcome.successful, false);
  assert.equal(outcome.mapping, 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(outcome.scenarioName, 'MAP_HR_EMPLOYEES_TO_ODS_SCN');
  assert.equal(outcome.scenarioVersion, '001');
  assert.equal(outcome.context, NOT_REPORTED);
  assert.equal(outcome.runMode, 'truncated');
  assert.equal(outcome.overallStatus, 'RUN_FAILED');
  assert.equal(outcome.failedStep, 'run_scenario');
  assert.equal(outcome.error, WITHHELD_ERROR);
  assert.equal(rendered(outcome).includes('redacted short error'), false);
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
  assert.equal(outcome.outcomeClass, 'failed');
  assert.equal(outcome.successful, false);
  assert.equal(outcome.overallStatus, 'failed');
  assert.equal(outcome.failedStep, NOT_REPORTED);
  assert.equal(outcome.error, 'ScenarioRunFailed');
  assert.equal(mappingRunToast(outcome, 'MAP_HR_EMPLOYEES_TO_ODS').message.includes('Scenario run failed'), true);
  assert.equal(mappingRunToast(outcome, 'MAP_HR_EMPLOYEES_TO_ODS').message.includes(MAY_HAVE_RUN_TEXT), false);
  assert.equal(outcome.sessionId, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('ERROR'), false);
});

test('HTTP 200 with an unrecognised status is unknown, including partial and an empty body', () => {
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
    request: REQUEST,
  });
  assertUnknownWording(outcome, REQUEST.mapping_name);
  assert.equal(outcome.overallStatus, 'partial');
  assert.equal(outcome.failedStep, 'generate_scenario');
  assert.equal(outcome.runMode, 'kept rows');
  assert.equal(outcome.error, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('not successful'), false);

  const empty = projectMappingRunOutcome({ httpStatus: 200, payload: {}, request: REQUEST });
  assertUnknownWording(empty, REQUEST.mapping_name);
  assert.equal(empty.overallStatus, NOT_REPORTED);
  assert.equal(mappingRunBadge(empty).label, 'unconfirmed');
  assert.equal(mappingRunToast(empty, REQUEST.mapping_name).type, 'warning');
});

test('a proven success still wins when an error string is also present', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: { ...SUCCESS_PAYLOAD, error: 'Scenario generation failed.', batch_id: 'RUN-OK' },
  });
  assert.equal(outcome.outcomeClass, 'succeeded');
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

test('a thrown network error is unknown and hides the client message', () => {
  const outcome = projectMappingRunOutcome({
    thrown: { message: 'Failed to fetch' },
    request: REQUEST,
  });
  assertUnknownWording(outcome, REQUEST.mapping_name);
  assert.equal(outcome.error, NOT_REPORTED);
  assert.equal(rendered(outcome).includes('Failed to fetch'), false);
  assert.equal(outcome.overallStatus, NOT_REPORTED);
  assert.equal(outcome.batchId, NOT_REPORTED);
  assert.equal(outcome.sessionId, NOT_REPORTED);
  assert.equal(outcome.counts, NOT_REPORTED);
  assert.equal(outcome.failedStep, NOT_REPORTED);
  assertRequested(outcome, 'kept rows');
});

test('missing fields and a body without a status are not reported and not successful', () => {
  const bare = projectMappingRunOutcome({ httpStatus: 200, payload: {} });
  assert.equal(bare.successful, false);
  for (const [, value] of mappingRunOutcomeLines(bare).slice(1)) {
    assert.equal(value, NOT_REPORTED);
  }

  const onlyStatus = projectMappingRunOutcome({
    httpStatus: 200,
    payload: { status: 'SUCCESS' },
    request: REQUEST,
  });
  assertUnknownWording(onlyStatus, REQUEST.mapping_name);
  assert.equal(onlyStatus.overallStatus, NOT_REPORTED);
  assert.equal(rendered(onlyStatus).includes('SUCCESS'), false);
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

test('a session id renders only from a top-level safe token and counts stay unreported', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      status: 'SUCCESS',
      session_id: 'SESS-100',
      row_count: 4,
      counts: { target_total: 3, source_current: 2, customer_id: 123456, rows: [{ id: 1 }] },
    },
  });
  const text = rendered(outcome);
  assert.equal(outcome.sessionId, 'SESS-100');
  assert.equal(outcome.counts, NOT_REPORTED);
  for (const leak of ['customer_id', '123456', 'target_total', 'source_current', 'row_count']) {
    assert.equal(text.includes(leak), false, leak);
  }
  assert.equal(projectMappingRunOutcome({
    httpStatus: 200,
    payload: { status: 'SUCCESS', session_id: 'db.example.local' },
  }).sessionId, NOT_REPORTED);
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
  assert.equal(outcome.error, WITHHELD_ERROR);
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
      error: 'customer_id=123456 at Loader.run(Loader.java:42) host=db.example.local',
    }],
    count: 1,
    limit: 50,
    status: null,
  });
  assert.equal(fromHistory.applied, true);
  assert.equal(fromHistory.outcome.failedStep, 'run_scenario');
  assert.equal(fromHistory.outcome.error, 'ScenarioRunFailed');
  assert.equal(rendered(fromHistory.outcome).includes('customer_id'), false);
  assert.equal(rendered(fromHistory.outcome).includes('db.example.local'), false);
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

test('a non-2xx body that claims SUCCESS is unknown and does not show SUCCESS', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 409,
    payload: {
      status: 'SUCCESS',
      batch_id: 'RUN-FAKE1',
      scenario: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
      version: '001',
    },
    request: REQUEST,
  });
  const text = rendered(outcome);
  assertUnknownWording(outcome, REQUEST.mapping_name);
  assert.equal(outcome.overallStatus, NOT_REPORTED);
  assert.equal(text.includes('SUCCESS'), false);
  assert.equal(outcome.scenarioName, 'MAP_HR_EMPLOYEES_TO_ODS_SCN');
  assertRequested(outcome, 'kept rows');
});

test('unknown, timeout, and each may-have-run flag use the check-before-retry wording', () => {
  const cases = [
    { httpStatus: 502, payload: { status: 'unknown', remote: UNKNOWN_REMOTE, batch_id: 'RUN-UNK1' } },
    { httpStatus: 504, payload: { status: 'unknown', remote: TIMEOUT_REMOTE, batch_id: 'RUN-TIME1' } },
    { httpStatus: 504, payload: { status: 'failed', remote: { error_code: 'RemoteTimeout', status: 'failed' }, batch_id: 'RUN-TIME2' } },
    { httpStatus: 502, payload: { status: 'failed', remote: { error_code: 'RemoteTimeout' }, batch_id: 'RUN-TIME3' } },
    { httpStatus: 502, payload: { remote: { outcome_unknown: true }, batch_id: 'RUN-UNK2' } },
    { httpStatus: 502, payload: { remote: { run_may_have_started: true }, batch_id: 'RUN-UNK3' } },
    { httpStatus: 502, payload: { remote: { side_effects: true }, batch_id: 'RUN-UNK4' } },
    { httpStatus: 502, payload: { outcome_unknown: true, batch_id: 'RUN-UNK5' } },
    { httpStatus: 502, payload: { run_may_have_started: true, batch_id: 'RUN-UNK6' } },
    { httpStatus: 502, payload: { side_effects: true, batch_id: 'RUN-UNK7' } },
    { httpStatus: 200, payload: { overall_status: 'RUN_UNKNOWN', batch_id: 'RUN-UNK8' } },
    { httpStatus: 502, payload: { status: 'unknown', batch_id: 'RUN-UNK9' } },
  ];
  for (const input of cases) {
    const outcome = projectMappingRunOutcome({ ...input, request: REQUEST });
    assertUnknownWording(outcome, REQUEST.mapping_name);
    assert.equal(rendered(outcome).includes('Failed to fetch'), false);
  }
});

test('anomalous error text and arbitrary count keys are withheld', () => {
  const leak = 'customer_id=123456 at Loader.run(Loader.java:42) host=db.example.local';
  const outcome = projectMappingRunOutcome({
    httpStatus: 500,
    payload: {
      error: leak,
      error_type: 'host=db.example.local',
      counts: { customer_id: 123456 },
      row_count: 9,
      insert_count: 8,
      status: 'RUN_FAILED',
      batch_id: 'RUN-LEAK1',
    },
    request: REQUEST_TRUNCATE,
  });
  const text = rendered(outcome);
  assertUnknownWording(outcome, REQUEST_TRUNCATE.mapping_name);
  assert.equal(outcome.error, WITHHELD_ERROR);
  assert.equal(outcome.error.includes(HISTORY_CHECK_TEXT), false);
  assert.equal(outcome.counts, NOT_REPORTED);
  for (const secret of ['customer_id', '123456', 'Loader.java', 'db.example.local', 'row_count', 'insert_count']) {
    assert.equal(text.includes(secret), false, secret);
  }
  assertRequested(outcome, 'truncated');

  const typed = projectMappingRunOutcome({
    httpStatus: 500,
    payload: { error: leak, error_type: 'ValueError', status: 'RUN_FAILED' },
  });
  assert.equal(typed.outcomeClass, 'unknown');
  assert.equal(typed.error, `${WITHHELD_ERROR} ${HISTORY_CHECK_TEXT} error_type=ValueError`);
  assert.equal(rendered(typed).includes('customer_id'), false);
  assert.equal(rendered(typed).includes('Loader.java'), false);

  const coded = projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'failed', remote: { error_code: leak }, batch_id: 'RUN-LEAK2' },
  });
  assert.equal(coded.outcomeClass, 'unknown');
  assert.equal(coded.error, WITHHELD_ERROR);
  assert.equal(rendered(coded).includes('customer_id'), false);
});

test('refresh for a closed, superseded, or different mapping panel is dropped', () => {
  const started = projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'failed', remote: FAILED_REMOTE, batch_id: 'RUN-001' },
    request: REQUEST,
    generation: 1,
  });
  const response = {
    mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
    state: {
      batch_id: 'RUN-001',
      mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
      scenario_name: 'MAP_HR_EMPLOYEES_TO_ODS_SCN',
      current_step: 'run_scenario',
      overall_status: 'RUN_FAILED',
      error: 'Operation failed.',
    },
  };

  const still = projectRunRefresh(started, response, { ...started });
  assert.equal(still.applied, true);
  assert.equal(still.reason, 'applied');
  assert.equal(still.outcome.failedStep, 'run_scenario');
  assert.equal(still.outcome.requestedMapping, 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(still.outcome.requestedRunMode, 'kept rows');
  assert.equal(still.outcome.generation, 1);

  const closed = projectRunRefresh(started, response, null);
  assert.equal(closed.applied, false);
  assert.equal(closed.reason, 'closed');
  assert.equal(closed.outcome, null);

  const newer = projectMappingRunOutcome({
    httpStatus: 200,
    payload: SUCCESS_PAYLOAD,
    request: REQUEST_TRUNCATE,
    generation: 2,
  });
  const superseded = projectRunRefresh(started, response, newer);
  assert.equal(superseded.applied, false);
  assert.equal(superseded.reason, 'superseded');
  assert.equal(superseded.outcome, newer);

  const mismatch = projectRunRefresh(started, {
    mapping_name: 'MAP_OTHER',
    state: {
      batch_id: 'RUN-001',
      mapping_name: 'MAP_OTHER',
      overall_status: 'SUCCEEDED',
      current_step: 'run_scenario',
    },
  }, started);
  assert.equal(mismatch.applied, false);
  assert.equal(mismatch.reason, 'mismatch');
  assert.equal(mismatch.outcome, started);

  const panels = { MAP_HR_EMPLOYEES_TO_ODS: newer };
  const dropped = applyRefreshToPanels(panels, 'MAP_HR_EMPLOYEES_TO_ODS', started, response);
  assert.equal(dropped.applied, false);
  assert.equal(dropped.panels.MAP_HR_EMPLOYEES_TO_ODS, newer);

  const closedPanels = applyRefreshToPanels({}, 'MAP_HR_EMPLOYEES_TO_ODS', started, response);
  assert.equal(closedPanels.applied, false);
  assert.deepEqual(closedPanels.panels, {});
});

test('requested mapping and run mode are shown on success and every failure class', () => {
  const cases = [
    ['succeeded', { httpStatus: 200, payload: SUCCESS_PAYLOAD, request: REQUEST_TRUNCATE }, 'truncated'],
    ['failed', { httpStatus: 409, payload: GATE_PAYLOAD, request: REQUEST }, 'kept rows'],
    ['failed', {
      httpStatus: 502,
      payload: { status: 'failed', remote: FAILED_REMOTE, batch_id: 'RUN-001' },
      request: REQUEST,
    }, 'kept rows'],
    ['failed', {
      httpStatus: 500,
      payload: { error: 'Scenario option update failed.', batch_id: 'RUN-OPT1' },
      request: REQUEST_TRUNCATE,
    }, 'truncated'],
    ['failed', {
      httpStatus: 400,
      payload: { error: 'Scenario generation failed.', batch_id: 'RUN-GEN1' },
      request: REQUEST,
    }, 'kept rows'],
    ['unknown', { httpStatus: 409, payload: { status: 'SUCCESS' }, request: REQUEST }, 'kept rows'],
    ['unknown', {
      httpStatus: 502,
      payload: { status: 'unknown', remote: UNKNOWN_REMOTE, batch_id: 'RUN-UNK1' },
      request: REQUEST_TRUNCATE,
    }, 'truncated'],
    ['unknown', {
      httpStatus: 504,
      payload: { status: 'unknown', remote: TIMEOUT_REMOTE, batch_id: 'RUN-TIME1' },
      request: REQUEST,
    }, 'kept rows'],
    ['unknown', { thrown: { message: 'Failed to fetch' }, request: REQUEST_TRUNCATE }, 'truncated'],
  ];
  for (const [expected, input, mode] of cases) {
    const outcome = projectMappingRunOutcome(input);
    assert.equal(outcome.outcomeClass, expected, JSON.stringify(input.payload || input.thrown));
    assertRequested(outcome, mode);
    if (expected === 'unknown') assertUnknownWording(outcome, REQUEST.mapping_name);
  }
});

test('a panel whose mapping is no longer in the list is removed', () => {
  const keep = { batchId: 'RUN-001' };
  const panels = { MAP_KEEP: keep, MAP_GONE: { batchId: 'RUN-002' } };
  const next = reconcileLastRuns(panels, ['MAP_KEEP', 'MAP_OTHER']);
  assert.deepEqual(Object.keys(next), ['MAP_KEEP']);
  assert.equal(next.MAP_KEEP, keep);
  assert.equal(reconcileLastRuns(panels, ['MAP_KEEP', 'MAP_GONE']), panels);
});

function blockedRemote(errorCode, overrides = {}) {
  return {
    status: 'blocked',
    error_code: errorCode,
    run_may_have_started: false,
    side_effects: false,
    outcome_unknown: false,
    session_status: null,
    ...overrides,
  };
}

function assertFailedClass(outcome) {
  assert.equal(outcome.outcomeClass, 'failed');
  assert.equal(outcome.successful, false);
  const badge = mappingRunBadge(outcome);
  const toast = mappingRunToast(outcome, 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(badge.label, 'not successful');
  assert.equal(badge.tone, 'failure');
  assert.equal(badge.notice, null);
  assert.equal(toast.type, 'error');
  assert.equal(toast.message.includes(MAY_HAVE_RUN_TEXT), false);
  assert.equal(rendered(outcome).includes(MAY_HAVE_RUN_TEXT), false);
}

test('a redacted 500 is unknown in the panel, badge and toast', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 500,
    payload: { error: 'mapping run failed.', error_type: 'RuntimeError' },
    thrown: { message: 'Service request failed.', status: 500 },
    request: REQUEST,
  });
  assertUnknownWording(outcome, REQUEST.mapping_name);
  assert.equal(mappingRunBadge(outcome).label, 'unconfirmed');
  assert.equal(mappingRunToast(outcome, REQUEST.mapping_name).message, MAY_HAVE_RUN_TEXT);
  assert.equal(outcome.batchId, NOT_REPORTED);
  assert.equal(outcome.error, `${WITHHELD_ERROR} ${HISTORY_CHECK_TEXT} error_type=RuntimeError`);
  const text = rendered(outcome);
  assert.equal(text.includes('mapping run failed.'), false);
  assert.equal(text.includes('Service request failed.'), false);
  assert.equal(text.includes(HISTORY_CHECK_TEXT), true);
});

test('a withheld message with no batch id says to check run history', () => {
  const outcome = projectMappingRunOutcome({
    httpStatus: 400,
    payload: { error: 'Invalid context!' },
    request: REQUEST,
  });
  assertUnknownWording(outcome, REQUEST.mapping_name);
  assert.equal(outcome.batchId, NOT_REPORTED);
  assert.equal(outcome.error, `${WITHHELD_ERROR} ${HISTORY_CHECK_TEXT}`);
  assert.equal(rendered(outcome).includes('Invalid context!'), false);
});

test('each closed failed shape is failed, and a near-miss of each is unknown', () => {
  const validation = [
    'mapping_name is required.',
    'Invalid mapping_name.',
    'approved:true is required for ODI scenario run.',
    'truncate_target_table must be selected as true or false before running a scenario.',
    'Invalid scenario_version.',
    'Invalid context.',
  ];
  for (const error of validation) {
    const outcome = projectMappingRunOutcome({ httpStatus: 400, payload: { error }, request: REQUEST });
    assertFailedClass(outcome);
    assert.equal(outcome.error, error);
    assert.equal(outcome.error.includes(HISTORY_CHECK_TEXT), false);
    const near = projectMappingRunOutcome({ httpStatus: 500, payload: { error }, request: REQUEST });
    assertUnknownWording(near, REQUEST.mapping_name);
  }

  const gate = projectMappingRunOutcome({ httpStatus: 409, payload: GATE_PAYLOAD, request: REQUEST });
  assertFailedClass(gate);
  assert.equal(gate.error, GATE_ERROR);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 400,
    payload: GATE_PAYLOAD,
    request: REQUEST,
  }), REQUEST.mapping_name);

  const ikmError = "The mapping's IKM does not expose the TRUNCATE_TARGET_TABLE option. Run with truncate_target_table=false or change the IKM.";
  const ikm = projectMappingRunOutcome({
    httpStatus: 409,
    payload: { status: 'ikm_option_unsupported', error: ikmError, batch_id: 'RUN-IKM1' },
    request: REQUEST,
  });
  assertFailedClass(ikm);
  assert.equal(ikm.error, ikmError);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 400,
    payload: { status: 'ikm_option_unsupported', error: ikmError },
    request: REQUEST,
  }), REQUEST.mapping_name);

  const optionUpdate = projectMappingRunOutcome({
    httpStatus: 500,
    payload: { error: 'Scenario option update failed.', batch_id: 'RUN-OPT1' },
    request: REQUEST,
  });
  assertFailedClass(optionUpdate);
  assert.equal(optionUpdate.error, 'Scenario option update failed.');
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 400,
    payload: { error: 'Scenario option update failed.' },
    request: REQUEST,
  }), REQUEST.mapping_name);

  const generation = projectMappingRunOutcome({
    httpStatus: 400,
    payload: { error: 'Scenario generation failed.', batch_id: 'RUN-GEN1' },
    request: REQUEST,
  });
  assertFailedClass(generation);
  assert.equal(generation.error, 'Scenario generation failed.');
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 500,
    payload: { error: 'Scenario generation failed.', batch_id: 'RUN-GEN1' },
    request: REQUEST,
  }), REQUEST.mapping_name);

  const healthCodes = [
    [400, 'ApprovalRequired'],
    [503, 'AgentConfigurationUnavailable'],
    [503, 'AgentUnavailable'],
    [503, 'RemoteProcessFailed'],
    [503, 'RemoteOutputInvalid'],
    [503, 'RemoteTimeout'],
  ];
  for (const [httpStatus, errorCode] of healthCodes) {
    const outcome = projectMappingRunOutcome({
      httpStatus,
      payload: { status: 'blocked', remote: blockedRemote(errorCode) },
      request: REQUEST,
    });
    assertFailedClass(outcome);
    assert.equal(outcome.error, errorCode);
    assert.equal(outcome.overallStatus, 'blocked');
  }
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 503,
    payload: { status: 'blocked', remote: blockedRemote('RemoteTimeout', { run_may_have_started: true }) },
    request: REQUEST,
  }), REQUEST.mapping_name);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 500,
    payload: { status: 'blocked', remote: blockedRemote('ApprovalRequired') },
    request: REQUEST,
  }), REQUEST.mapping_name);

  for (const errorCode of [
    'InvalidRunIdentity',
    'AgentConfigurationUnavailable',
    'AgentUnavailable',
    'ScenarioOwnershipUnavailable',
    'RepositoryCleanupFailed',
  ]) {
    const outcome = projectMappingRunOutcome({
      httpStatus: 502,
      payload: { status: 'blocked', remote: blockedRemote(errorCode), batch_id: 'RUN-BLOCK1' },
      request: REQUEST,
    });
    assertFailedClass(outcome);
    assert.equal(outcome.error, errorCode);
  }
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'blocked', remote: blockedRemote('InvalidRunIdentity', { side_effects: true }) },
    request: REQUEST,
  }), REQUEST.mapping_name);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'blocked', remote: blockedRemote('RemoteTimeout') },
    request: REQUEST,
  }), REQUEST.mapping_name);

  const resolved = projectMappingRunOutcome({
    httpStatus: 502,
    payload: { status: 'failed', remote: FAILED_REMOTE, batch_id: 'RUN-001' },
    request: REQUEST,
  });
  assertFailedClass(resolved);
  assert.equal(resolved.error, 'ScenarioRunFailed');
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 500,
    payload: { status: 'failed', remote: FAILED_REMOTE, batch_id: 'RUN-001' },
    request: REQUEST,
  }), REQUEST.mapping_name);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 502,
    payload: {
      status: 'failed',
      remote: { ...FAILED_REMOTE, outcome_unknown: true },
      batch_id: 'RUN-001',
    },
    request: REQUEST,
  }), REQUEST.mapping_name);

  const persisted = projectMappingRunOutcome({
    httpStatus: 200,
    payload: { overall_status: 'RUN_FAILED', batch_id: 'RUN-001', current_step: 'run_scenario' },
    request: REQUEST,
  });
  assertFailedClass(persisted);
  assert.equal(persisted.overallStatus, 'RUN_FAILED');
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 500,
    payload: { overall_status: 'RUN_FAILED', batch_id: 'RUN-001' },
    request: REQUEST,
  }), REQUEST.mapping_name);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 200,
    payload: { overall_status: 'RUN_FAILED', outcome_unknown: true, batch_id: 'RUN-001' },
    request: REQUEST,
  }), REQUEST.mapping_name);
  assertUnknownWording(projectMappingRunOutcome({
    httpStatus: 200,
    payload: { status: 'failed', batch_id: 'RUN-BARE1' },
    request: REQUEST,
  }), REQUEST.mapping_name);
});

test('dotted versions and backend identifiers are displayed only inside the remote contract', () => {
  const mapping = `M${'A'.repeat(89)}`;
  const scenario = `S${'C'.repeat(93)}`;
  const context = `C${'X'.repeat(127)}`;
  assert.equal(mapping.length, 90);
  assert.equal(scenario.length, 94);
  assert.equal(context.length, 128);
  const outcome = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      ...SUCCESS_PAYLOAD,
      mapping_name: mapping,
      scenario,
      version: '001.2',
      context,
    },
  });
  assert.equal(outcome.outcomeClass, 'succeeded');
  assert.equal(outcome.mapping, mapping);
  assert.equal(outcome.scenarioName, scenario);
  assert.equal(outcome.scenarioVersion, '001.2');
  assert.equal(outcome.context, context);
  assert.equal(rendered(outcome).includes(mapping), true);
  assert.equal(rendered(outcome).includes('001.2'), true);

  const hidden = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      status: 'SUCCESS',
      mapping_name: `M${'A'.repeat(128)}`,
      scenario_name: 'MAP_HR.EMPLOYEES',
      scenario: 'MAP_HR-EMPLOYEES',
      scenario_version: '1234567890123',
      version: '001/2',
      context: 'GLOBAL/DEV',
      remote: DONE_REMOTE,
    },
  });
  assert.equal(`M${'A'.repeat(128)}`.length, 129);
  assert.equal(hidden.outcomeClass, 'succeeded');
  assert.equal(hidden.mapping, NOT_REPORTED);
  assert.equal(hidden.scenarioName, NOT_REPORTED);
  assert.equal(hidden.scenarioVersion, NOT_REPORTED);
  assert.equal(hidden.context, NOT_REPORTED);
  const hiddenText = rendered(hidden);
  assert.equal(hiddenText.includes('MAP_HR.EMPLOYEES'), false);
  assert.equal(hiddenText.includes('MAP_HR-EMPLOYEES'), false);
  assert.equal(hiddenText.includes('GLOBAL/DEV'), false);
  assert.equal(hiddenText.includes('1234567890123'), false);
});

test('a contradictory body is unknown and a dropped session guard would misclassify it', () => {
  const ikmError = "The mapping's IKM does not expose the TRUNCATE_TARGET_TABLE option. Run with truncate_target_table=false or change the IKM.";
  const cases = [
    ['validation started', {
      httpStatus: 400,
      payload: { error: 'Invalid context.', run_may_have_started: true },
    }],
    ['validation foreign status', {
      httpStatus: 400,
      payload: { error: 'Invalid context.', status: 'blocked' },
    }],
    ['validation remote', {
      httpStatus: 400,
      payload: { error: 'Invalid context.', remote: { status: 'blocked' } },
    }],
    ['gate outcome unknown', {
      httpStatus: 409,
      payload: { ...GATE_PAYLOAD, outcome_unknown: true },
    }],
    ['gate status unknown', {
      httpStatus: 409,
      payload: { ...GATE_PAYLOAD, status: 'unknown' },
    }],
    ['gate remote', {
      httpStatus: 409,
      payload: { ...GATE_PAYLOAD, remote: { status: 'blocked' } },
    }],
    ['ikm outcome unknown', {
      httpStatus: 409,
      payload: { status: 'ikm_option_unsupported', error: ikmError, outcome_unknown: true },
    }],
    ['ikm status unknown', {
      httpStatus: 409,
      payload: { status: 'unknown', error: ikmError, outcome_unknown: true },
    }],
    ['option status unknown', {
      httpStatus: 500,
      payload: { error: 'Scenario option update failed.', status: 'unknown', batch_id: 'RUN-OPT1' },
    }],
    ['option outcome unknown', {
      httpStatus: 500,
      payload: { error: 'Scenario option update failed.', outcome_unknown: true, batch_id: 'RUN-OPT1' },
    }],
    ['option foreign status', {
      httpStatus: 500,
      payload: { error: 'Scenario option update failed.', status: 'blocked', batch_id: 'RUN-OPT1' },
    }],
    ['generation status unknown', {
      httpStatus: 400,
      payload: { error: 'Scenario generation failed.', status: 'unknown', batch_id: 'RUN-GEN1' },
    }],
    ['generation outcome unknown', {
      httpStatus: 400,
      payload: { error: 'Scenario generation failed.', outcome_unknown: true, batch_id: 'RUN-GEN1' },
    }],
    ['success claim with ERROR session', {
      httpStatus: 200,
      payload: {
        status: 'SUCCESS',
        remote: {
          ...DONE_REMOTE,
          session_status: 'ERROR',
          error_code: 'ScenarioRunFailed',
          run_may_have_started: true,
          side_effects: true,
          outcome_unknown: false,
        },
      },
    }],
    ['saved RUN_FAILED with remote started', {
      httpStatus: 200,
      payload: {
        overall_status: 'RUN_FAILED',
        remote: {
          status: 'blocked',
          run_may_have_started: true,
          side_effects: true,
          outcome_unknown: false,
        },
        batch_id: 'RUN-001',
      },
    }],
    ['saved RUN_FAILED with remote and clear flags', {
      httpStatus: 200,
      payload: {
        overall_status: 'RUN_FAILED',
        remote: blockedRemote('ApprovalRequired'),
        batch_id: 'RUN-001',
      },
    }],
    // Full success except session_status. Fails if the DONE guard is removed.
    ['DONE session guard', {
      httpStatus: 200,
      payload: {
        status: 'SUCCESS',
        remote: { ...DONE_REMOTE, session_status: 'ERROR' },
      },
    }],
    // Full resolved ERROR except session_status. Fails if that guard is removed.
    ['resolved ERROR session guard', {
      httpStatus: 502,
      payload: {
        status: 'failed',
        remote: { ...FAILED_REMOTE, session_status: 'DONE' },
        batch_id: 'RUN-001',
      },
    }],
  ];
  for (const [label, input] of cases) {
    const outcome = projectMappingRunOutcome({ ...input, request: REQUEST });
    assert.equal(outcome.outcomeClass, 'unknown', label);
    assertUnknownWording(outcome, REQUEST.mapping_name);
  }

  const claimed = projectMappingRunOutcome({
    httpStatus: 200,
    payload: {
      status: 'SUCCESS',
      remote: {
        ...DONE_REMOTE,
        session_status: 'ERROR',
        error_code: 'ScenarioRunFailed',
      },
    },
    request: REQUEST,
  });
  assert.equal(claimed.overallStatus, NOT_REPORTED);
  const claimedText = rendered(claimed);
  assert.equal(claimedText.includes('SUCCESS'), false);
  assert.equal(claimedText.includes('ScenarioRunFailed'), false);

  const savedOk = projectMappingRunOutcome({
    httpStatus: 200,
    payload: { overall_status: 'SUCCEEDED', batch_id: 'RUN-001', current_step: 'run_scenario' },
    request: REQUEST,
  });
  assert.equal(savedOk.outcomeClass, 'succeeded');
  assert.equal(savedOk.successful, true);
  assert.equal(savedOk.overallStatus, 'SUCCEEDED');
});
