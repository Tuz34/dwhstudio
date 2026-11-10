import test from 'node:test';
import assert from 'node:assert/strict';
import { mappingRequestDetails } from './mappingRequestDetails.js';

test('shows the stated layer, load type and keys without changing the request', () => {
  const request = {
    target_layer: 'EDW',
    load_type: 'type2',
    key_columns: ['CUSTOMER_ID', 'REGION_ID'],
  };
  assert.deepEqual(mappingRequestDetails(request), [
    ['Layer', 'EDW'],
    ['Load type', 'Type 2 history'],
    ['Key columns', 'CUSTOMER_ID, REGION_ID'],
  ]);
  assert.deepEqual(request.key_columns, ['CUSTOMER_ID', 'REGION_ID']);
});

test('omits unstated and unknown details', () => {
  assert.deepEqual(mappingRequestDetails({}), []);
  assert.deepEqual(mappingRequestDetails({ load_type: 'unrecognized', key_columns: [] }), []);
});
