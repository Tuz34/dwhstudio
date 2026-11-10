import assert from 'node:assert/strict';
import test from 'node:test';

import { getSchema, planScd2, request } from './api.js';


function installDocumentStub() {
  globalThis.document = {
    querySelector: () => ({ content: 'test-csrf-token' }),
  };
}


test('request never exposes an arbitrary non-JSON error body', async () => {
  installDocumentStub();
  globalThis.fetch = async () => ({
    ok: false,
    status: 500,
    headers: { get: () => 'text/html' },
    text: async () => '<html>internal proxy detail</html>',
  });

  await assert.rejects(
    request('/api/example'),
    error => error.message === 'Service request failed.' && !error.message.includes('proxy detail'),
  );
});


test('request does not mutate caller options or object body', async () => {
  installDocumentStub();
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      status: 200,
      text: async () => '{"ok":true}',
    };
  };
  const body = { approved: true };
  const options = { method: 'POST', body };

  const result = await request('/api/example', options);

  assert.deepEqual(result, { ok: true });
  assert.equal(options.body, body);
  assert.deepEqual(body, { approved: true });
  assert.equal(captured.options.body, '{"approved":true}');
  assert.equal(captured.options.headers['X-CSRFToken'], 'test-csrf-token');
});


test('SCD2 errors keep the server status and code off the generic message', async () => {
  installDocumentStub();
  let seenOptions;
  globalThis.fetch = async (_url, options) => {
    seenOptions = options;
    return {
      ok: false,
      status: 400,
      headers: { get: () => 'application/json' },
      json: async () => ({
        status: 'invalid_request',
        error_code: 'target_owner_not_allowed',
        detail: 'internal-proxy-detail',
      }),
    };
  };

  await assert.rejects(
    planScd2({ source_owner: 'SYS' }),
    error => error.message === 'Request was rejected.'
      && error.scd2.status === 'invalid_request'
      && error.scd2.error_code === 'target_owner_not_allowed'
      && error.scd2.detail === undefined
      && !error.message.includes('internal-proxy-detail'),
  );
  assert.equal(seenOptions.captureScd2Error, undefined);
});


test('dynamic schema path segments are encoded', async () => {
  installDocumentStub();
  let requestedUrl = '';
  globalThis.fetch = async url => {
    requestedUrl = url;
    return { ok: true, status: 200, text: async () => '{}' };
  };

  await getSchema('DWH.TABLE/with space');

  assert.equal(requestedUrl, '/api/schema/DWH.TABLE%2Fwith%20space');
});
