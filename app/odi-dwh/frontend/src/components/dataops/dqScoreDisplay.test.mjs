import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { dqScoreDisplay } from './dqScoreDisplay.js';

const UNKNOWN = {
  label: 'Unknown',
  width: '0%',
  bar: 'bg-slate-300',
  text: 'text-slate-500',
};

test('null, absent and malformed scores show neutral Unknown', () => {
  for (const score of [null, undefined, '', '0', '95', NaN, Infinity, -Infinity, true, false, {}, []]) {
    assert.deepEqual(dqScoreDisplay(score), UNKNOWN, String(score));
  }
});

test('a measured zero stays a measured red zero, not Unknown', () => {
  assert.deepEqual(dqScoreDisplay(0), {
    label: '%0',
    width: '0%',
    bar: 'bg-rose-500',
    text: 'text-rose-600',
  });
});

test('measured scores keep the existing colour thresholds', () => {
  const cases = [
    [100, 'bg-emerald-500', 'text-emerald-600'],
    [90.1, 'bg-emerald-500', 'text-emerald-600'],
    [90, 'bg-amber-400', 'text-amber-600'],
    [70.1, 'bg-amber-400', 'text-amber-600'],
    [70, 'bg-rose-500', 'text-rose-600'],
    [50, 'bg-rose-500', 'text-rose-600'],
  ];
  for (const [score, bar, text] of cases) {
    assert.deepEqual(dqScoreDisplay(score), { label: `%${score}`, width: `${score}%`, bar, text });
  }
});

test('the dashboard renders through the helper and no longer coerces a missing score', async () => {
  const source = await readFile(new URL('./DqDashboard.jsx', import.meta.url), 'utf8');
  assert.match(source, /dqScoreDisplay\(d\.score\)/);
  assert.doesNotMatch(source, /d\.score\s*\|\|\s*0/);
  assert.doesNotMatch(source, /%\{score\}/);
});
