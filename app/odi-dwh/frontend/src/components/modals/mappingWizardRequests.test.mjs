import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  AUDIT_PRESERVATION_REJECTED_MESSAGE,
  LOAD_TYPES,
  buildMappingRequest,
  buildScd2Handoff,
  getLoadOptionError,
  getMappingOutcome,
  prefillLoadOptions,
  selectWizardFailureMessage,
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

test('a typed DM_ target cannot continue with a classic load', () => {
  const warning = 'Datamart targets are supported only for Type 2 history.';
  for (const loadType of ['truncate_load', 'insert_update', 'type1']) {
    const typed = { ...form, tgt_owner: 'DWH', tgt_table: '  dm_x  ', load_type: loadType };
    assert.equal(getLoadOptionError(typed), warning, loadType);
    assert.throws(() => buildMappingRequest(typed), { message: warning }, loadType);
  }
  const type2 = { ...form, tgt_owner: 'DWH', tgt_table: 'DM_X', load_type: 'type2' };
  assert.equal(getLoadOptionError(type2), null);
  assert.equal(buildScd2Handoff(type2).payload.props.mappingRequest.tgt_table, 'DM_X');
});

test('classic load guards use the final Unicode-trimmed target segment', () => {
  const warning = 'Datamart targets are supported only for Type 2 history.';
  const spellings = [
    ['DWH. DM_X', true], ['DWH.  DM_X', true], ['DWH.\tDM_X', true],
    ['DWH.\u00a0DM_X', true], ['dwh. dM_x', true], [' \tDWH. DM_X', true],
    ['DWH.\u2003DM_X', true], ['ODS_DM_X', false], ['XDM_Y', false],
    ['DWH.ODS_X', false],
  ];
  for (const [target, isDatamart] of spellings) {
    for (const loadType of ['truncate_load', 'insert_update', 'type1']) {
      const typed = { ...form, tgt_table: target, load_type: loadType };
      if (isDatamart) {
        assert.equal(getLoadOptionError(typed), warning, `${target}/${loadType}`);
        assert.throws(() => buildMappingRequest(typed), { message: warning }, `${target}/${loadType}`);
      } else {
        assert.equal(getLoadOptionError(typed), null, `${target}/${loadType}`);
        assert.equal(buildMappingRequest(typed).params.tgt_table, target, `${target}/${loadType}`);
      }
    }
  }
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

test('only a typed empty-form DWH schema selects the internal source model', () => {
  const typed = buildMappingRequest({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: 'DWH', src_schema_typed: true,
  });
  assert.equal(typed.params.src_model, 'ORACLE_DWH');
  const lower = buildMappingRequest({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: ' dwh ', src_schema_typed: true,
  });
  assert.equal(lower.params.src_model, 'ORACLE_DWH');
  for (const schema of ['ODS', 'AUDIT_USER', 'DWH_FLAT', 'HR']) {
    const request = buildMappingRequest({
      ...form, src_model: 'ORACLE_SOURCE', src_schema: schema, src_schema_typed: true,
    });
    assert.equal(request.params.src_model, 'ORACLE_SOURCE', schema);
  }
  const untouched = buildMappingRequest({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: 'DWH',
  });
  assert.equal(untouched.params.src_model, 'ORACLE_SOURCE');
});

test('a prefilled source model and the Type 2 handoff stay exactly as received', () => {
  const prefilled = buildMappingRequest({
    ...form,
    src_model: 'ORACLE_SOURCE',
    src_schema: 'DWH',
    src_model_from_prefill: true,
    src_schema_typed: true,
  });
  assert.equal(prefilled.params.src_model, 'ORACLE_SOURCE');
  const handoff = buildScd2Handoff({
    ...form,
    src_model: 'ORACLE_SOURCE',
    src_schema: 'DWH',
    load_type: 'type2',
    src_model_from_prefill: true,
    src_schema_typed: true,
  });
  assert.equal(handoff.payload.props.mappingRequest.src_model, 'ORACLE_SOURCE');
  const typedHandoff = buildScd2Handoff({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: 'DWH', load_type: 'type2', src_schema_typed: true,
  });
  assert.equal(typedHandoff.payload.props.mappingRequest.src_model, 'ORACLE_SOURCE');
});

test('a touched source model and an external schema stay as entered', () => {
  const touched = buildMappingRequest({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: 'DWH', src_model_touched: true, src_schema_typed: true,
  });
  assert.equal(touched.params.src_model, 'ORACLE_SOURCE');
  const external = buildMappingRequest({
    ...form, src_model: 'ORACLE_SOURCE', src_schema: 'HR', src_schema_typed: true,
  });
  assert.equal(external.params.src_model, 'ORACLE_SOURCE');
});

test('only the source model field marks the model as touched', () => {
  const source = readFileSync(new URL('./MappingWizard.jsx', import.meta.url), 'utf8');
  assert.equal(source.split('src_model_touched').length - 1, 1);
  assert.match(source, /src_model: e\.target\.value, src_model_touched: true/);
  assert.match(source, /src_model_from_prefill: typeof initialData\.src_model === 'string'/);
  assert.match(source, /src_schema: e\.target\.value, src_schema_typed: true/);
  assert.match(source, /const requestedSourceModel = resolveWizardSourceModel\(form\)/);
});

const MAPPING_STEP_TEXT = 'The target table step finished, but ODI mapping creation did not complete. Check the ODI error details and retry.';

test('audit preservation rejection uses its sentence and other failures keep the old text', () => {
  const rejected = {
    overall_status: 'FAILED',
    error_code: 'audit_preservation_rejected',
    error: 'mapping generation failed.',
    error_type: 'OperationError',
  };
  assert.equal(selectWizardFailureMessage(rejected, rejected.error), AUDIT_PRESERVATION_REJECTED_MESSAGE);
  assert.equal(
    AUDIT_PRESERVATION_REJECTED_MESSAGE,
    "The target table's audit column types are not supported for audit preservation. Nothing was created.",
  );
  const other = {
    overall_status: 'FAILED',
    error: 'mapping generation failed.',
    error_type: 'OperationError',
  };
  assert.equal(selectWizardFailureMessage(other, other.error), MAPPING_STEP_TEXT);
  assert.equal(selectWizardFailureMessage({ overall_status: 'SUCCESS' }, ''), '');
});

test('Type 2 handoff payloads equal the a645d5a buildScd2Handoff literals', () => {
  const untouched = {
    src_model: 'ORACLE_SOURCE',
    src_table: 'ODI81N_SRC_F1',
    src_schema: 'DWH',
    src_schema_typed: true,
    tgt_model: 'ORACLE_ODS',
    tgt_owner: 'DWH',
    tgt_table: 'ODS_ODI81N_F1',
    needs_skey: false,
    skey_natural_key: '',
    description: 'Untouched model',
    mapping_name: '',
    drop_target_if_exists: false,
    load_type: 'type2',
    key_columns: 'ENTITY_ID',
  };
  assert.deepEqual(buildScd2Handoff(untouched), {
    type: 'OPEN_MODAL',
    payload: {
      type: 'scd2',
      props: {
        mappingRequest: {
          src_model: 'ORACLE_SOURCE',
          src_table: 'ODI81N_SRC_F1',
          src_schema: 'DWH',
          tgt_model: 'ORACLE_ODS',
          tgt_owner: 'DWH',
          tgt_table: 'ODS_ODI81N_F1',
          needs_skey: false,
          skey_natural_key: '',
          description: 'Untouched model',
          mapping_name: '',
          drop_target_if_exists: false,
          load_type: 'type2',
          key_columns: ['ENTITY_ID'],
        },
      },
    },
  });
  const prefilled = {
    src_model: 'ORACLE_SOURCE',
    src_model_from_prefill: true,
    src_schema_typed: true,
    src_table: 'ORDERS',
    src_schema: 'DWH',
    tgt_model: 'DWH',
    tgt_owner: 'DWH',
    tgt_table: 'DWH_ORDERS',
    needs_skey: true,
    skey_natural_key: 'ORDER_ID',
    description: 'Prefilled source model',
    mapping_name: 'MAP_ORDERS_T2',
    drop_target_if_exists: true,
    load_type: 'type2',
    key_columns: 'ORDER_ID, LINE_ID',
  };
  assert.deepEqual(buildScd2Handoff(prefilled), {
    type: 'OPEN_MODAL',
    payload: {
      type: 'scd2',
      props: {
        mappingRequest: {
          src_model: 'ORACLE_SOURCE',
          src_table: 'ORDERS',
          src_schema: 'DWH',
          tgt_model: 'DWH',
          tgt_owner: 'DWH',
          tgt_table: 'DWH_ORDERS',
          needs_skey: true,
          skey_natural_key: 'ORDER_ID',
          description: 'Prefilled source model',
          mapping_name: 'MAP_ORDERS_T2',
          drop_target_if_exists: true,
          load_type: 'type2',
          key_columns: ['ORDER_ID', 'LINE_ID'],
        },
      },
    },
  });
  const typed = {
    src_model: 'MDL_SRC_ORACLE_HR',
    src_model_touched: true,
    src_schema_typed: true,
    src_table: 'EMPLOYEES',
    src_schema: 'HR',
    tgt_model: 'ORACLE_DWH',
    tgt_owner: 'DWH',
    tgt_table: 'DWH_EMPLOYEES',
    needs_skey: false,
    skey_natural_key: '',
    description: 'Typed source model',
    mapping_name: 'MAP_EMP_T2',
    drop_target_if_exists: false,
    load_type: 'type2',
    key_columns: 'EMPLOYEE_ID',
  };
  assert.deepEqual(buildScd2Handoff(typed), {
    type: 'OPEN_MODAL',
    payload: {
      type: 'scd2',
      props: {
        mappingRequest: {
          src_model: 'MDL_SRC_ORACLE_HR',
          src_table: 'EMPLOYEES',
          src_schema: 'HR',
          tgt_model: 'ORACLE_DWH',
          tgt_owner: 'DWH',
          tgt_table: 'DWH_EMPLOYEES',
          needs_skey: false,
          skey_natural_key: '',
          description: 'Typed source model',
          mapping_name: 'MAP_EMP_T2',
          drop_target_if_exists: false,
          load_type: 'type2',
          key_columns: ['EMPLOYEE_ID'],
        },
      },
    },
  });
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
