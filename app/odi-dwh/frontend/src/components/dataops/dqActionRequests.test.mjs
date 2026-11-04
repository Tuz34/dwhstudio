import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildAutofixAllRequest,
  buildAutofixColumnRequest,
  buildSyncMappingRequest,
} from './dqActionRequests.js';


test('persistent DQ requests carry strict approval and normalized metadata', () => {
  assert.deepEqual(
    buildAutofixColumnRequest('dwh.ods_status', 'status_code'),
    { approved: true, owner: 'DWH', table: 'ODS_STATUS', column: 'STATUS_CODE' },
  );
  assert.deepEqual(
    buildAutofixAllRequest('DWH.ODS_STATUS', ['status_code', 'status_desc']),
    { approved: true, owner: 'DWH', table: 'ODS_STATUS', columns: ['STATUS_CODE', 'STATUS_DESC'] },
  );
  assert.deepEqual(
    buildSyncMappingRequest('DWH.ODS_STATUS'),
    { approved: true, owner: 'DWH', table: 'ODS_STATUS' },
  );
});


test('DQ request builders reject invalid identifiers', () => {
  assert.throws(() => buildAutofixColumnRequest('DWH.BAD-NAME', 'STATUS_CODE'), /Invalid/);
  assert.throws(() => buildAutofixAllRequest('DWH.ODS_STATUS', ['BAD-NAME']), /Invalid/);
  assert.throws(() => buildSyncMappingRequest('OTHER.ODS_STATUS'), /Invalid/);
});
