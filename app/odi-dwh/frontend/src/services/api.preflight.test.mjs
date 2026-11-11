import assert from 'node:assert/strict';
import test from 'node:test';

import { visiblePreflightError } from './api.js';

test('a source-model preflight message replaces the generic client error', () => {
  const error = Object.assign(new Error('Requested resource was not found.'), {
    status: 404,
    body: { error: 'Source table not found through source model ORACLE_SOURCE' },
  });
  const visible = visiblePreflightError(error);
  assert.equal(visible.message, 'Source table not found through source model ORACLE_SOURCE');
  assert.equal(visible.status, 404);
});

test('connection text in any other preflight body stays hidden', () => {
  const error = Object.assign(new Error('Requested resource was not found.'), {
    status: 500,
    body: { error: 'password=secret host=10.1.2.3', error_type: 'RuntimeError' },
  });
  const visible = visiblePreflightError(error);
  assert.equal(visible, error);
  assert.equal(visible.message, 'Requested resource was not found.');
  assert.doesNotMatch(visible.message, /secret|10\.1\.2\.3/);
});
