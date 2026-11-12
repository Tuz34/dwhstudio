import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { DATAMART_TYPE2_ONLY_WARNING } from '../datamartContract.js';
import { request } from './api.js';
import {
  AUDIT_PRESERVATION_REJECTED_CODE,
  AUDIT_PRESERVATION_REJECTED_MESSAGE,
  DATAMART_REQUIRES_TYPE2_CODE,
  refusedRequestMessage,
} from '../refusedRequestContract.js';


function installDocumentStub() {
  globalThis.document = {
    querySelector: () => ({ content: 'test-csrf-token' }),
  };
}

function rejectWith(status, payload) {
  installDocumentStub();
  globalThis.fetch = async () => ({
    ok: false,
    status,
    headers: { get: () => 'application/json' },
    json: async () => payload,
  });
}

function pythonStringConstant(source, name) {
  const match = source.match(new RegExp(`^${name} = (\\([\\s\\S]*?\\n\\)|"[^"\\n]*")`, 'm'));
  assert.ok(match, `${name} must be a fixed Python literal`);
  return [...match[1].matchAll(/"([^"\n]*)"/g)].map(part => part[1]).join('');
}


test('an allowlisted 400 code shows the client sentence, not the server text', async () => {
  rejectWith(400, {
    error_code: DATAMART_REQUIRES_TYPE2_CODE,
    error: 'password=secret should not render',
  });
  await assert.rejects(
    request('/api/mapping/target-preflight', { method: 'POST', body: { params: {} } }),
    (error) => {
      assert.equal(error.message, DATAMART_TYPE2_ONLY_WARNING);
      assert.equal(error.status, 400);
      assert.equal(error.message.includes('secret'), false);
      assert.equal(error.message.includes('password'), false);
      return true;
    },
  );
});

test('audit preservation 400 uses its own fixed sentence', async () => {
  rejectWith(400, {
    error_code: AUDIT_PRESERVATION_REJECTED_CODE,
    error: 'Audit preservation column type is not supported.',
  });
  await assert.rejects(
    request('/api/example', { method: 'POST', body: {} }),
    (error) => {
      assert.equal(error.message, AUDIT_PRESERVATION_REJECTED_MESSAGE);
      assert.equal(error.message.includes('Audit preservation column type'), false);
      return true;
    },
  );
});

test('an unknown 400 code and a 400 without a code stay generic', async () => {
  rejectWith(400, { error_code: 'not_a_real_code', error: 'internal detail must stay hidden' });
  await assert.rejects(
    request('/api/example', { method: 'POST', body: {} }),
    (error) => {
      assert.equal(error.message, 'Request was rejected.');
      assert.equal(error.message.includes('internal detail'), false);
      return true;
    },
  );
  rejectWith(400, { error: 'internal detail must stay hidden' });
  await assert.rejects(
    request('/api/example', { method: 'POST', body: {} }),
    (error) => {
      assert.equal(error.message, 'Request was rejected.');
      assert.equal(error.message.includes('internal detail'), false);
      return true;
    },
  );
});

test('refused-request sentences drift-check the Python constants', () => {
  const datamart = readFileSync(new URL('../../../datamart_contract.py', import.meta.url), 'utf8');
  const errors = readFileSync(new URL('../../../routes/error_responses.py', import.meta.url), 'utf8');
  assert.equal(DATAMART_REQUIRES_TYPE2_CODE, pythonStringConstant(datamart, 'DATAMART_REQUIRES_TYPE2_CODE'));
  assert.equal(DATAMART_TYPE2_ONLY_WARNING, pythonStringConstant(datamart, 'DATAMART_TYPE2_ONLY_WARNING'));
  assert.equal(
    refusedRequestMessage(DATAMART_REQUIRES_TYPE2_CODE),
    DATAMART_TYPE2_ONLY_WARNING,
  );
  assert.equal(
    AUDIT_PRESERVATION_REJECTED_CODE,
    pythonStringConstant(errors, '_AUDIT_PRESERVATION_REJECTED'),
  );
  assert.equal(
    AUDIT_PRESERVATION_REJECTED_MESSAGE,
    pythonStringConstant(errors, '_AUDIT_PRESERVATION_SENTENCE'),
  );
  assert.equal(refusedRequestMessage('not_a_real_code'), null);
  assert.equal(refusedRequestMessage(undefined), null);
});
