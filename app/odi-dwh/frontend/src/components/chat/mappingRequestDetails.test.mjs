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

test('backend proposal prefills the wizard and hands unqualified source to Type 2', () => {
  const source = readFileSync(new URL('../modals/MappingWizard.jsx', import.meta.url), 'utf8');
  const fields = ['src_model', 'src_table', 'src_schema', 'tgt_model', 'tgt_owner', 'tgt_table', 'mapping_name'];
  for (const field of fields) {
    assert.match(source, new RegExp(`${field}: initialData\\.${field} \\|\\| emptyForm\\.${field}`));
  }
  assert.match(source, /\.\.\.prefillLoadOptions\(initialData\)/);
  const form = {
    ...Object.fromEntries(fields.map(field => [field, proposal[field]])),
    ...prefillLoadOptions(proposal),
    needs_skey: false, skey_natural_key: '', description: '', drop_target_if_exists: false,
  };
  assert.equal(form.src_schema, 'HR');
  assert.equal(form.src_table, 'ORDERS');
  assert.equal(form.tgt_table, 'EDW_HR_ORDERS');
  assert.equal(form.load_type, 'type2');
  assert.equal(form.key_columns, 'ORDER_ID');
  const handoff = buildScd2Handoff(form).payload.props.mappingRequest;
  assert.equal(handoff.src_schema, 'HR');
  assert.equal(handoff.src_table, 'ORDERS');
  assert.deepEqual(handoff.key_columns, ['ORDER_ID']);
});
