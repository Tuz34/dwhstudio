import assert from 'node:assert/strict';
import test from 'node:test';

import { layerMatches, sameTable, tableMeta, uniqueImpactRows } from './impactAnalysisModel.js';


test('impact model normalizes ODI schema aliases and table identity', () => {
  assert.equal(sameTable('SRC_HR.EMPLOYEES', 'HR.EMPLOYEES'), true);
  assert.deepEqual(
    tableMeta('SRC_HR.EMPLOYEES'),
    {
      table: 'EMPLOYEES',
      schema: 'HR',
      system: 'ORACLE',
      badge: 'bg-emerald-50 text-emerald-700 border-emerald-100',
    },
  );
});


test('a missing layer matches only the unfiltered list', () => {
  assert.equal(layerMatches('', ''), true);
  assert.equal(layerMatches(undefined, ''), true);
  assert.equal(layerMatches('', 'ODS'), false);
  assert.equal(layerMatches('EDW', 'EDW'), true);
  assert.equal(layerMatches('EDW', 'ODS'), false);
  assert.equal(layerMatches('DM', 'DM'), true);
});


test('impact model removes duplicate relation rows without dropping distinct directions', () => {
  const rows = [
    { related_table: 'HR.EMPLOYEES', mapping_name: 'MAP_A', direction: 'up' },
    { related_table: 'SRC_HR.EMPLOYEES', mapping_name: 'MAP_A', direction: 'up' },
    { related_table: 'HR.EMPLOYEES', mapping_name: 'MAP_A', direction: 'down' },
  ];

  assert.equal(uniqueImpactRows(rows).length, 2);
});
