import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TRUNCATE_WARNING,
  buildRunMappingRequest,
  openRunDialogState,
  runModeWarning,
  selectRunMode,
} from './mappingRunDialog.js';


test('opening the run dialog is non-destructive by default', () => {
  const state = openRunDialogState('MAP_ODI81N_F1');
  assert.deepEqual(state, { mappingName: 'MAP_ODI81N_F1', truncateTargetTable: false });
  assert.equal(runModeWarning(state), null);
  assert.deepEqual(buildRunMappingRequest(state), {
    mapping_name: 'MAP_ODI81N_F1',
    truncate_target_table: false,
    approved: true,
  });
});

test('opening the dialog for another mapping never carries a stale truncate choice', () => {
  const first = selectRunMode(openRunDialogState('MAP_ODI81N_F2'), true);
  assert.equal(first.truncateTargetTable, true);
  const next = openRunDialogState('MAP_ODI81N_F3', first);
  assert.deepEqual(next, { mappingName: 'MAP_ODI81N_F3', truncateTargetTable: false });
});

test('an explicit full-reload choice sends true and shows the warning', () => {
  const state = selectRunMode(openRunDialogState('MAP_ODI81N_F2'), true);
  assert.equal(runModeWarning(state), TRUNCATE_WARNING);
  assert.deepEqual(buildRunMappingRequest(state), {
    mapping_name: 'MAP_ODI81N_F2',
    truncate_target_table: true,
    approved: true,
  });
});

test('switching back to keep rows sends false and clears the warning', () => {
  const state = selectRunMode(selectRunMode(openRunDialogState('MAP_ODI81N_F1'), true), false);
  assert.equal(state.truncateTargetTable, false);
  assert.equal(runModeWarning(state), null);
  assert.equal(buildRunMappingRequest(state).truncate_target_table, false);
});

test('only a literal boolean true enables truncate', () => {
  for (const value of ['true', 1, 'yes', {}, [], null, undefined]) {
    const state = selectRunMode(openRunDialogState('MAP_ODI81N_F1'), value);
    assert.equal(state.truncateTargetTable, false, String(value));
    assert.equal(buildRunMappingRequest(state).truncate_target_table, false, String(value));
  }
  assert.equal(buildRunMappingRequest({ mappingName: 'MAP_X', truncateTargetTable: 'true' }).truncate_target_table, false);
});

test('the request always keeps the literal approval and the exact mapping identity', () => {
  const request = buildRunMappingRequest(openRunDialogState('MAP_ODI81N_F3'));
  assert.equal(request.approved, true);
  assert.deepEqual(Object.keys(request).sort(), ['approved', 'mapping_name', 'truncate_target_table']);
  assert.equal(request.mapping_name, 'MAP_ODI81N_F3');
});

test('a dialog without a mapping name cannot build a request', () => {
  for (const state of [null, undefined, {}, { mappingName: '' }, { mappingName: '   ' }]) {
    assert.throws(() => buildRunMappingRequest(state), /mapping name/);
  }
});
