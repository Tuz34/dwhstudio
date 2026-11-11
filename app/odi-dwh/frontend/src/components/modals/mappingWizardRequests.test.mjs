import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  LOAD_TYPES,
  buildMappingRequest,
  buildScd2Handoff,
  getLoadOptionError,
  getMappingOutcome,
  prefillLoadOptions,
} from './mappingWizardRequests.js';

const form = {
  src_model: 'ORACLE_DWH',
  src_table: 'SOURCE_TABLE',
  src_schema: 'DWH',
  tgt_model: 'ORACLE_ODS',
  tgt_owner: 'DWH',
  tgt_table: 'ODS_TARGET_TABLE',
  needs_skey: false,
  skey_natural_key: '',
  description: 'Synthetic mapping',
  mapping_name: '',
  drop_target_if_exists: false,
  load_type: 'insert_update',
  key_columns: 'entity_id, source_id',
};

const commonParams = {
  src_model: 'ORACLE_DWH',
  src_table: 'SOURCE_TABLE',
  src_schema: 'DWH',
  tgt_model: 'ORACLE_ODS',
  tgt_owner: 'DWH',
  tgt_table: 'ODS_TARGET_TABLE',
  needs_skey: false,
  skey_natural_key: '',
  description: 'Synthetic mapping',
  mapping_name: 'MAP_SOURCE_TABLE_TO_ODS',
  drop_target_if_exists: false,
  replace_existing_mapping: false,
  operation: 'CREATE',
};

test('the four load type choices have their exact labels', () => {
  assert.deepEqual(LOAD_TYPES, [
    { value: 'insert_update', label: 'Insert / update (needs key columns)' },
    { value: 'truncate_load', label: 'Truncate and reload' },
    { value: 'type1', label: 'Type 1 overwrite (needs key columns)' },
    { value: 'type2', label: 'Type 2 history' },
  ]);
});

for (const loadType of ['insert_update', 'type1']) {
  test(`${loadType} sends the exact classic IU request with a primary update key`, () => {
    assert.deepEqual(buildMappingRequest({ ...form, load_type: loadType }), {
      approved: true,
      engine: 'classic',
      params: {
        ...commonParams,
        integration_type: 'IU',
        update_key_columns: ['ENTITY_ID', 'SOURCE_ID'],
        update_key_kind: 'primary',
      },
    });
  });
}

test('truncate_load sends CONTROL_APPEND without keys, even after switching from IU', () => {
  assert.deepEqual(buildMappingRequest({ ...form, load_type: 'truncate_load' }), {
    approved: true,
    engine: 'classic',
    params: {
      ...commonParams,
      integration_type: 'CONTROL_APPEND',
      update_key_columns: [],
      update_key_kind: 'unique',
    },
  });
});

test('only the existing rebuild toggle permits target drop and mapping replacement', () => {
  const request = buildMappingRequest({
    ...form,
    mapping_name: 'MAP_EXPLICIT',
    key_columns: 'entity_id, ENTITY_ID',
    drop_target_if_exists: true,
  });
  assert.equal(request.params.mapping_name, 'MAP_EXPLICIT');
  assert.deepEqual(request.params.update_key_columns, ['ENTITY_ID']);
  assert.equal(request.params.drop_target_if_exists, true);
  assert.equal(request.params.replace_existing_mapping, true);
  assert.equal(request.approved, true);
});

test('missing or unknown load type is never inferred from table names', () => {
  assert.deepEqual(prefillLoadOptions({ src_table: 'DIM_HISTORY', tgt_table: 'ODS_HISTORY' }), {
    load_type: '', key_columns: '',
  });
  assert.deepEqual(prefillLoadOptions({ load_type: 'merge', key_columns: ['id'] }), {
    load_type: '', key_columns: 'ID',
  });
  assert.match(getLoadOptionError({ ...form, load_type: '' }), /Choose a load type/);
  assert.throws(() => buildMappingRequest({ ...form, load_type: '' }), /Choose a load type/);
  assert.throws(() => buildMappingRequest({ ...form, load_type: 'other' }), /Choose a load type/);
});

test('prefill accepts only named load types and array key columns', () => {
  assert.deepEqual(prefillLoadOptions({ load_type: 'type1', key_columns: [' entity_id ', 'source_id'] }), {
    load_type: 'type1', key_columns: 'ENTITY_ID, SOURCE_ID',
  });
  assert.deepEqual(prefillLoadOptions({ load_type: 'TYPE1', key_columns: 'entity_id' }), {
    load_type: '', key_columns: '',
  });
  assert.deepEqual(prefillLoadOptions({ load_type: 'type2', key_columns: ['id'] }), {
    load_type: 'type2', key_columns: 'ID',
  });
});

test('IU and Type 1 reject missing or malformed key columns', () => {
  for (const loadType of ['insert_update', 'type1']) {
    assert.throws(() => buildMappingRequest({ ...form, load_type: loadType, key_columns: '' }), /at least one/);
    assert.throws(() => buildMappingRequest({ ...form, load_type: loadType, key_columns: 'id, ' }), /valid comma-separated/);
    assert.throws(() => buildMappingRequest({ ...form, load_type: loadType, key_columns: 'bad-name' }), /valid comma-separated/);
    assert.throws(() => buildMappingRequest({ ...form, load_type: loadType, key_columns: `A${'B'.repeat(128)}` }), /valid comma-separated/);
    assert.throws(() => buildMappingRequest({ ...form, load_type: loadType, key_columns: 'A,B,C,D,E,F,G,H,I' }), /no more than 8/);
  }
});

test('Type 2 creates only the exact SCD2 modal handoff with current form values', () => {
  const action = buildScd2Handoff({ ...form, load_type: 'type2' });
  assert.deepEqual(action, {
    type: 'OPEN_MODAL',
    payload: {
      type: 'scd2',
      props: {
        mappingRequest: {
          src_model: 'ORACLE_DWH',
          src_table: 'SOURCE_TABLE',
          src_schema: 'DWH',
          tgt_model: 'ORACLE_ODS',
          tgt_owner: 'DWH',
          tgt_table: 'ODS_TARGET_TABLE',
          needs_skey: false,
          skey_natural_key: '',
          description: 'Synthetic mapping',
          mapping_name: '',
          drop_target_if_exists: false,
          load_type: 'type2',
          key_columns: ['ENTITY_ID', 'SOURCE_ID'],
        },
      },
    },
  });
  assert.throws(() => buildMappingRequest({ ...form, load_type: 'type2' }), /SCD2 wizard/);
  assert.throws(() => buildScd2Handoff(form), /Choose Type 2/);
});

test('an untouched internal source schema sends the internal source model', () => {
  for (const schema of ['DWH', 'dwh', ' ODS ', 'AUDIT_USER', 'DWH_FLAT']) {
    const request = buildMappingRequest({ ...form, src_model: 'ORACLE_SOURCE', src_schema: schema });
    assert.equal(request.params.src_model, 'ORACLE_DWH', schema);
    const handoff = buildScd2Handoff({
      ...form, src_model: 'ORACLE_SOURCE', src_schema: schema, load_type: 'type2',
    });
    assert.equal(handoff.payload.props.mappingRequest.src_model, 'ORACLE_DWH', schema);
  }
});

test('a touched source model and an external schema stay as entered', () => {
  const touched = buildMappingRequest({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: 'DWH', src_model_touched: true,
  });
  assert.equal(touched.params.src_model, 'ORACLE_SOURCE');
  const external = buildMappingRequest({ ...form, src_model: 'ORACLE_SOURCE', src_schema: 'HR' });
  assert.equal(external.params.src_model, 'ORACLE_SOURCE');
});

test('only the source model field marks the model as touched', () => {
  const source = readFileSync(new URL('./MappingWizard.jsx', import.meta.url), 'utf8');
  assert.equal(source.split('src_model_touched').length - 1, 1);
  assert.match(source, /src_model: e\.target\.value, src_model_touched: true/);
  assert.match(source, /const requestedSourceModel = resolveWizardSourceModel\(form\)/);
});

test('result summary uses only actual response fields', () => {
  assert.deepEqual(getMappingOutcome({
    params: { integration_type: 'IU', ikm_name: 'IKM Oracle Merge' },
    steps: { create_target_table: { key: { status: 'CREATED', kind: 'primary', columns: ['ENTITY_ID'] } } },
  }), {
    integrationType: 'IU', ikm: 'IKM Oracle Merge', key: 'CREATED primary (ENTITY_ID)',
  });
  assert.deepEqual(getMappingOutcome({ params: {}, steps: { create_target_table: {} } }), {
    integrationType: null, ikm: null, key: null,
  });
  assert.deepEqual(getMappingOutcome(null), { integrationType: null, ikm: null, key: null });
});
