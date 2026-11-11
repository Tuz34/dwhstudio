import assert from 'node:assert/strict';
import test from 'node:test';

import {
  TARGET_OWNERS,
  applyPrefill,
  emptyScd2Form,
  validateScd2Form,
} from './scd2WizardModel.js';

test('Datamart chat proposal prefills a valid DWH and DM_ Type 2 target', () => {
  const form = applyPrefill(emptyScd2Form(), {
    source_schema: 'HR',
    source_table: 'ORDERS',
    target_layer: 'DM',
    target_schema: 'DWH',
    tgt_owner: 'DWH',
    target_table: 'DM_HR_ORDERS',
    tgt_table: 'DM_HR_ORDERS',
    mapping_name: 'MAP_HR_ORDERS_TO_DM',
    key_columns: ['ORDER_ID'],
  });
  assert.equal(form.target_owner, 'DWH');
  assert.equal(form.target_table, 'DM_HR_ORDERS');
  assert.equal(form.mapping_name, 'MAP_HR_ORDERS_TO_DM');
  assert.equal(form.natural_key_text, 'ORDER_ID');
  assert.deepEqual(form.prefillNotes, {});
  const validated = validateScd2Form({ ...form, tracked_mode: 'all' });
  assert.equal(validated.ok, true);
  assert.equal(validated.request.target_owner, 'DWH');
  assert.equal(validated.request.target_table, 'DM_HR_ORDERS');
});

test('explicit legacy DM proposal remains selectable and prefills unchanged', () => {
  assert.deepEqual(TARGET_OWNERS, ['DWH', 'DM']);
  const form = applyPrefill(emptyScd2Form(), {
    source_schema: 'HR', source_table: 'ORDERS',
    target_schema: 'DM', tgt_owner: 'DM',
    target_table: 'DIM_ORDERS', mapping_name: 'MAP_HR_ORDERS_TO_DM',
    key_columns: ['ORDER_ID'],
  });
  assert.equal(form.target_owner, 'DM');
  assert.equal(form.target_table, 'DIM_ORDERS');
  assert.equal(validateScd2Form({ ...form, tracked_mode: 'all' }).ok, true);
});

test('target-owner validation identifies DWH as the Datamart default', () => {
  const validation = validateScd2Form({
    ...emptyScd2Form(), source_owner: 'HR', source_table: 'ORDERS',
    target_owner: 'SYS', target_table: 'DM_HR_ORDERS',
    mapping_name: 'MAP_HR_ORDERS_TO_DM', natural_key_text: 'ORDER_ID',
    tracked_mode: 'all',
  });
  assert.equal(validation.errors.target_owner,
    'Target owner must be DWH for Datamart defaults; DM is available for an explicit target.');
});
