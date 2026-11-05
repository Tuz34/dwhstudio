import assert from 'node:assert/strict';
import test from 'node:test';

import { findPendingDmlQuery, isDmlAction } from './dmlActions.js';

test('backend insert, update and delete actions are DML in any case', () => {
  for (const action of ['insert', 'update', 'delete', 'INSERT', 'Update', ' delete ', 'dml']) {
    assert.equal(isDmlAction(action), true, action);
  }
});

test('read-only, DDL, missing and non-string actions are not pending DML', () => {
  for (const action of ['select', 'ddl', '', undefined, null, 1, {}, ['insert']]) {
    assert.equal(isDmlAction(action), false, String(action));
  }
});

test('the first non-executed DML query becomes pending', () => {
  const select = { action: 'select', sql: 'S' };
  const done = { action: 'update', sql: 'U0', auto_executed: true };
  const del = { action: 'delete', sql: 'D', label: 'Remove' };
  const ins = { action: 'insert', sql: 'I' };
  assert.equal(findPendingDmlQuery([select, done, del, ins]), del);
});

test('no DML or no usable query list gives no pending query', () => {
  assert.equal(findPendingDmlQuery([{ action: 'select' }, { action: 'ddl' }]), undefined);
  assert.equal(findPendingDmlQuery([null, 'x']), undefined);
  assert.equal(findPendingDmlQuery(undefined), undefined);
});
