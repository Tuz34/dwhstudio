import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  applyDetailResponse,
  applyListResponse,
  beginDetailRequest,
  initialSelection,
} from './mappingSelection.js';

const here = dirname(fileURLToPath(import.meta.url));

function tracking(overrides = {}) {
  return {
    ...initialSelection(),
    listedNames: ['A', 'B'],
    detail: { MAPPING_NAME: 'A' },
    requestedName: 'A',
    latestSeq: 1,
    nextSeq: 2,
    runDialog: { mappingName: 'B' },
    runStatus: { A: 'running', B: 'error' },
    ...overrides,
  };
}

test('refresh keeps A while detail B is pending, then a late B response is ignored', () => {
  let state = tracking();
  const pending = beginDetailRequest(state, 'B');
  state = pending.state;
  assert.equal(state.requestedName, 'B');
  assert.equal(state.detail.MAPPING_NAME, 'A');
  assert.equal(pending.requestSeq, state.latestSeq);

  state = applyListResponse(state, ['A']);
  assert.equal(state.cleared, true);
  assert.equal(state.detail, null);
  assert.equal(state.runDialog, null);
  assert.equal(state.runStatus.A, 'running');
  assert.equal(state.runStatus.B, undefined);
  assert.equal(state.requestedName, 'B');

  const late = applyDetailResponse(state, {
    requestSeq: pending.requestSeq,
    mappingName: 'B',
    detail: { MAPPING_NAME: 'B' },
  });
  assert.equal(late, state);
  assert.equal(late.detail, null);
});

test('mirror: pending A is dropped when the refreshed list keeps only B', () => {
  let state = tracking({
    detail: { MAPPING_NAME: 'B' },
    requestedName: 'B',
    runDialog: { mappingName: 'A' },
    runStatus: { A: 'running', B: 'done' },
  });
  const pending = beginDetailRequest(state, 'A');
  state = pending.state;
  state = applyListResponse(state, ['B']);
  assert.equal(state.detail, null);
  assert.equal(state.runDialog, null);
  assert.equal(state.runStatus.B, 'done');
  assert.equal(state.runStatus.A, undefined);
  const late = applyDetailResponse(state, {
    requestSeq: pending.requestSeq,
    mappingName: 'A',
    detail: { MAPPING_NAME: 'A' },
  });
  assert.equal(late.detail, null);
});

test('mirror: a pending mapping that is still listed is not cleared', () => {
  let state = tracking({
    detail: { MAPPING_NAME: 'B' },
    requestedName: 'B',
    runDialog: { mappingName: 'A' },
    runStatus: { A: 'running' },
  });
  const pending = beginDetailRequest(state, 'A');
  state = pending.state;
  const next = applyListResponse(state, ['A']);
  assert.equal(next.cleared, false);
  assert.equal(next.detail, state.detail);
  assert.equal(next.runDialog, state.runDialog);
  assert.equal(next.runStatus, state.runStatus);
  const applied = applyDetailResponse(next, {
    requestSeq: pending.requestSeq,
    mappingName: 'A',
    detail: { MAPPING_NAME: 'A' },
  });
  assert.equal(applied.detail.MAPPING_NAME, 'A');
});

test('mirror: an older detail response is ignored and the latest one applies', () => {
  let state = initialSelection();
  state = { ...state, listedNames: ['A', 'B'] };
  const first = beginDetailRequest(state, 'A');
  const second = beginDetailRequest(first.state, 'B');
  state = applyListResponse(second.state, ['A', 'B']);
  assert.equal(state.cleared, false);
  const lateFirst = applyDetailResponse(state, {
    requestSeq: first.requestSeq,
    mappingName: 'A',
    detail: { MAPPING_NAME: 'A', from: 'late' },
  });
  assert.equal(lateFirst, state);
  const current = applyDetailResponse(state, {
    requestSeq: second.requestSeq,
    mappingName: 'B',
    detail: { MAPPING_NAME: 'B' },
  });
  assert.equal(current.detail.MAPPING_NAME, 'B');
});

test('mirror: cancelling the latest request ignores the response that was in flight', () => {
  let state = tracking();
  const pending = beginDetailRequest(state, 'B');
  const cancelled = beginDetailRequest(pending.state, null);
  state = applyListResponse(cancelled.state, ['A', 'B']);
  assert.equal(state.cleared, false);
  const late = applyDetailResponse(state, {
    requestSeq: pending.requestSeq,
    mappingName: 'B',
    detail: { MAPPING_NAME: 'B' },
  });
  assert.equal(late, state);
});

test('MappingList applies the selection decisions synchronously', () => {
  const source = readFileSync(join(here, 'MappingList.jsx'), 'utf8');
  const loadDetail = source.slice(
    source.indexOf('const loadDetail = useCallback'),
    source.indexOf('const loadMappings = useCallback'),
  );
  const requestAt = loadDetail.indexOf('beginDetailRequest(');
  const awaitAt = loadDetail.indexOf('await getOdiMappingDetail');
  assert.ok(requestAt >= 0 && requestAt < awaitAt);
  assert.ok(loadDetail.indexOf('selectedNameRef.current =') < awaitAt);
  assert.ok(loadDetail.includes('applyDetailResponse('));
  const loadMappings = source.slice(
    source.indexOf('const loadMappings = useCallback'),
    source.indexOf('}, [loadDetail, showToast]);'),
  );
  assert.ok(loadMappings.includes('applyListResponse('));
  assert.ok(loadMappings.includes('selectedNameRef.current'));
  assert.equal(source.includes('selectedNameRef.current = detail?.MAPPING_NAME'), false);
});
