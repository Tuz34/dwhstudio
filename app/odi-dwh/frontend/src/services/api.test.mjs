import assert from 'node:assert/strict';
import test from 'node:test';

import { apiPost, getOdiMappings, getSchema, planScd2, request, runMapping } from './api.js';


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
      assert.equal(error.scd2, undefined);
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
    assert.equal(options.captureScd2Error, undefined);
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
      assert.equal(error.scd2, undefined);
      assert.equal(String(error).includes(gate.error), false);
      return true;
    },
  );
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
      && error.status === 400
      && error.scd2.status === 'invalid_request'
      && error.scd2.error_code === 'target_owner_not_allowed'
      && error.scd2.detail === undefined
      && error.body === undefined
      && !error.message.includes('internal-proxy-detail'),
  );
  assert.equal(seenOptions.captureScd2Error, undefined);
  assert.equal(seenOptions.captureErrorBody, undefined);
});


test('a plain apiPost keeps the generic error, status, errorType and parsed return', async () => {
  installDocumentStub();
  let seenOptions;
  globalThis.fetch = async (_url, options) => {
    seenOptions = options;
    return {
      ok: false,
      status: 403,
      headers: { get: () => 'application/json' },
      json: async () => ({
        error: 'internal-proxy-detail',
        error_type: 'PermissionDenied',
        status: 'denied',
      }),
    };
  };

  await assert.rejects(
    apiPost('/api/example', { approved: true }),
    (error) => {
      assert.equal(error.message, 'This operation is not allowed.');
      assert.equal(error.status, 403);
      assert.equal(error.errorType, 'PermissionDenied');
      assert.equal(error.body, undefined);
      assert.equal(error.scd2, undefined);
      assert.equal(error.message.includes('internal-proxy-detail'), false);
      assert.equal(error.message.includes('PermissionDenied'), false);
      return true;
    },
  );
  assert.equal(seenOptions.captureErrorBody, undefined);
  assert.equal(seenOptions.captureScd2Error, undefined);
  assert.equal(seenOptions.body, '{"approved":true}');

  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    text: async () => '{"ok":true,"n":1}',
  });
  assert.deepEqual(await apiPost('/api/example', {}), { ok: true, n: 1 });
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


test('getOdiMappings keeps a list body and attaches truncation headers', async () => {
  installDocumentStub();
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    headers: {
      get(name) {
        const table = {
          'X-ODI-Mappings-Truncated': '1',
          'X-ODI-Mappings-Limit': '500',
        };
        return table[name];
      },
    },
    text: async () => '[{"MAPPING_NAME":"MAP_A"}]',
  });

  const data = await getOdiMappings();

  assert.equal(Array.isArray(data), true);
  assert.equal(data.length, 1);
  assert.equal(data[0].MAPPING_NAME, 'MAP_A');
  assert.equal(data.truncated, true);
  assert.equal(data.limit, 500);
});
