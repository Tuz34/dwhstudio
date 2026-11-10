import assert from 'node:assert/strict';
import test from 'node:test';

import { getSchema, request, runMapping } from './api.js';


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


test('request still drops a JSON error body unless capture is requested', async () => {
  installDocumentStub();
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    headers: { get: () => 'application/json' },
    json: async () => ({
      status: 'blocked_fixed_mapping_cutover_pending',
      error: 'gate message',
    }),
  });

  await assert.rejects(
    request('/api/example', { method: 'POST', body: { approved: true } }),
    (error) => {
      assert.equal(error.message, 'Request conflicts with the current state.');
      assert.equal(error.status, 409);
      assert.equal(error.body, undefined);
      assert.equal(error.message.includes('gate message'), false);
      return true;
    },
  );
});


test('runMapping keeps the gate JSON on the error without putting it in the message', async () => {
  installDocumentStub();
  const gate = {
    status: 'blocked_fixed_mapping_cutover_pending',
    error: 'The fixed DIM_PRODUCT mapping cannot run through the generic mapping run path until a separately reviewed cutover proof replaces this gate.',
  };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/mapping/run');
    assert.equal(options.captureErrorBody, undefined);
    assert.equal(options.body, '{"mapping_name":"MAP_X","truncate_target_table":false,"approved":true}');
    return {
      ok: false,
      status: 409,
      headers: { get: () => 'application/json' },
      json: async () => gate,
    };
  };

  await assert.rejects(
    runMapping({ mapping_name: 'MAP_X', truncate_target_table: false, approved: true }),
    (error) => {
      assert.equal(error.message, 'Request conflicts with the current state.');
      assert.equal(error.status, 409);
      assert.deepEqual(error.body, gate);
      assert.equal(String(error).includes(gate.error), false);
      return true;
    },
  );
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
