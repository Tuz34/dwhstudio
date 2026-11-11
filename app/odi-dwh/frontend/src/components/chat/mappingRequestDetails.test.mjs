import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mappingRequestDetails } from './mappingRequestDetails.js';
import { buildScd2Handoff, prefillLoadOptions } from '../modals/mappingWizardRequests.js';

// Copied from test_type2_proposal_fixture_has_unqualified_wizard_source_fields.
const proposal = {
  operation: 'CREATE', source_schema: 'HR', source_table: 'ORDERS',
  src_model: 'ORACLE_SOURCE', src_schema: 'HR', src_table: 'ORDERS',
  target_layer: 'EDW', target_schema: 'DWH', target_table: 'EDW_HR_ORDERS',
  tgt_model: 'ORACLE_DWH', tgt_owner: 'DWH', tgt_table: 'EDW_HR_ORDERS',
  mapping_name: 'MAP_HR_ORDERS_TO_EDW', load_type: 'type2',
  key_columns: ['ORDER_ID'],
  stated_fields: ['source_schema', 'source_table', 'target_layer', 'load_type', 'key_columns'],
};

test('groups stated and suggested values and keeps the proposal unchanged', () => {
  const before = structuredClone(proposal);
  assert.deepEqual(mappingRequestDetails(proposal), {
    stated: [
      ['Source schema', 'HR'], ['Source table', 'ORDERS'], ['Layer', 'EDW'],
      ['Load type', 'Type 2 history'], ['Key columns', 'ORDER_ID'],
    ],
    suggested: [
      ['Mapping', 'MAP_HR_ORDERS_TO_EDW'], ['Target schema', 'DWH'],
      ['Target table', 'EDW_HR_ORDERS'],
    ],
    warnings: [],
  });
  assert.deepEqual(proposal, before);
});

test('no-layer LLM values are suggestions and only fixed warnings render', () => {
  assert.deepEqual(mappingRequestDetails({}), { stated: [], suggested: [], warnings: [] });
  const details = mappingRequestDetails({
    source_schema: 'HR', source_table: 'EMPLOYEES', target_schema: 'DWH',
    target_table: 'ODS_HR_EMPLOYEES', stated_fields: [], load_type: 'unrecognized',
    warnings: ['untrusted warning', 'Datamart targets are supported only for Type 2 history.'],
  });
  assert.deepEqual(details.stated, []);
  assert.deepEqual(details.suggested, [
    ['Source schema', 'HR'], ['Source table', 'EMPLOYEES'],
    ['Target schema', 'DWH'], ['Target table', 'ODS_HR_EMPLOYEES'],
  ]);
  assert.deepEqual(details.warnings, ['Datamart targets are supported only for Type 2 history.']);
});

test('Type 2 card shows the fixed placement warning for a stated DM schema', () => {
  const warning = 'Datamart objects are created in DWH with a DM_ prefix in this installation.';
  const details = mappingRequestDetails({
    target_layer: 'DM', target_schema: 'DM', load_type: 'type2',
    stated_fields: ['target_layer', 'target_schema', 'load_type'],
    warnings: [warning],
  });
  assert.deepEqual(details.warnings, [warning]);
});

test('frontend Datamart warning literals mirror the Python constants', async () => {
  const js = await import('../../datamartContract.js');
  const python = readFileSync(new URL('../../../../datamart_contract.py', import.meta.url), 'utf8');
  for (const [name, value] of [
    ['DATAMART_TYPE2_ONLY_WARNING', js.DATAMART_TYPE2_ONLY_WARNING],
    ['DATAMART_PLACEMENT_WARNING', js.DATAMART_PLACEMENT_WARNING],
  ]) {
    const match = python.match(new RegExp(`^${name} = "([^"]+)"$`, 'm'));
    assert.ok(match, `${name} must be a fixed Python literal`);
    assert.equal(value, match[1], name);
  }
});

// The five expected proposal shapes match OWNER_SENTENCES in the backend test.
// There are twelve sentence orders, including target-first Type 2 variants.
const ownerProposals = [
  ['a', 'ODI81N_SRC_F1', 'ODS', 'DWH', 'ODS_DWH_ODI81N_SRC_F1',
    'MAP_DWH_ODI81N_SRC_F1_TO_ODS', 'insert_update', ['ID'], false],
  ['b', 'ODI81N_SRC_F2', 'ODS', 'DWH', 'ODS_DWH_ODI81N_SRC_F2',
    'MAP_DWH_ODI81N_SRC_F2_TO_ODS', 'truncate_load', [], false],
  ['c', 'ODS_ODI81N_F4', 'EDW', 'DWH', 'DIM_ODI81N_F4',
    'MAP_DWH_ODS_ODI81N_F4_TO_EDW', 'type2', ['ID'], true],
  ['d', 'ODI81N_SRC_F3', 'EDW', 'DWH', 'DWH_ODI81N_F3',
    'MAP_DWH_ODI81N_SRC_F3_TO_EDW', 'type1', ['ID'], true],
  ['e', 'DWH_ODI81N_F5', 'DM', 'DM', 'DIM_ODI81N_F5',
    null, 'type2', ['ID'], true],
].map(([label, sourceTable, layer, targetOwner, targetTable, mappingName, loadType, keys, statedTarget]) => {
  const proposal = {
    operation: 'CREATE', source_schema: 'DWH', source_table: sourceTable,
    src_model: 'ORACLE_DWH', src_schema: 'DWH', src_table: sourceTable,
    target_layer: layer, target_schema: targetOwner, target_table: targetTable,
    tgt_owner: targetOwner, tgt_table: targetTable, load_type: loadType,
    stated_fields: [
      'source_schema', 'source_table', 'target_layer',
      ...(statedTarget ? ['target_schema', 'target_table'] : []),
      'load_type', ...(keys.length ? ['key_columns'] : []),
    ],
  };
  if (layer !== 'DM') proposal.tgt_model = layer === 'ODS' ? 'ORACLE_ODS' : 'ORACLE_DWH';
  if (mappingName) proposal.mapping_name = mappingName;
  if (keys.length) proposal.key_columns = keys;
  return { label, proposal, sourceTable, targetOwner, targetTable, mappingName, loadType, keys };
});

test('owner proposals and both sentence orders prefill wizard and Type 2 handoff', () => {
  const source = readFileSync(new URL('../modals/MappingWizard.jsx', import.meta.url), 'utf8');
  const fields = ['src_model', 'src_table', 'src_schema', 'tgt_model', 'tgt_owner', 'tgt_table', 'mapping_name'];
  for (const field of fields) {
    assert.match(source, new RegExp(`${field}: initialData\\.${field} \\|\\| emptyForm\\.${field}`));
  }
  assert.match(source, /\.\.\.prefillLoadOptions\(initialData\)/);
  for (const fixture of ownerProposals) {
    const orders = fixture.label === 'c'
      ? ['c', 'c-prime', 'c-permuted', 'c-prime-permuted']
      : ['original', 'permuted'];
    for (const order of orders) {
      const { label, proposal: value, sourceTable, targetOwner, targetTable, mappingName, loadType, keys } = fixture;
      const form = {
        ...Object.fromEntries(fields.map(field => [field, value[field] ||
          (field === 'tgt_model' ? 'DWH' : '')])),
        ...prefillLoadOptions(value),
        needs_skey: false, skey_natural_key: '', description: '', drop_target_if_exists: false,
      };
      const context = `${label}-${order}`;
      assert.equal(form.src_model, 'ORACLE_DWH', context);
      assert.equal(form.src_schema, 'DWH', context);
      assert.equal(form.src_table, sourceTable, context);
      assert.equal(form.tgt_owner, targetOwner, context);
      assert.equal(form.tgt_table, targetTable, context);
      assert.equal(form.mapping_name, mappingName || '', context);
      assert.equal(form.load_type, loadType, context);
      assert.equal(form.key_columns, keys.join(', '), context);
      assert.deepEqual(mappingRequestDetails(value).warnings, [], context);
      if (loadType === 'type2') {
        const handoff = buildScd2Handoff(form).payload.props.mappingRequest;
        assert.equal(handoff.src_model, 'ORACLE_DWH', context);
        assert.equal(handoff.src_schema, 'DWH', context);
        assert.equal(handoff.src_table, sourceTable, context);
        assert.equal(handoff.tgt_owner, targetOwner, context);
        assert.equal(handoff.tgt_table, targetTable, context);
        assert.equal(handoff.mapping_name, mappingName || '', context);
        assert.deepEqual(handoff.key_columns, keys, context);
      }
    }
  }
});
