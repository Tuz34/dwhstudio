import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { attachMappingsCap, mappingsCapNotice } from './mappingListCap.js';

const here = dirname(fileURLToPath(import.meta.url));

test('attachMappingsCap keeps a list body and reads truncation headers', () => {
  const rows = [{ MAPPING_NAME: 'MAP_A' }];
  const headers = {
    get(name) {
      const table = {
        'X-ODI-Mappings-Truncated': '1',
        'X-ODI-Mappings-Limit': '500',
      };
      return table[name];
    },
  };
  const attached = attachMappingsCap(rows, headers);
  assert.equal(attached, rows);
  assert.equal(Array.isArray(attached), true);
  assert.equal(attached.truncated, true);
  assert.equal(attached.limit, 500);
  assert.equal(mappingsCapNotice(attached), 'Showing the first 500 mappings');
});

test('mappingsCapNotice is empty when the cap was not hit', () => {
  const rows = [{ MAPPING_NAME: 'MAP_A' }];
  attachMappingsCap(rows, { get: () => null });
  assert.equal(mappingsCapNotice(rows), '');
  assert.equal(mappingsCapNotice([]), '');
});

test('MappingList shows the cap line in the list header', () => {
  const source = readFileSync(join(here, 'MappingList.jsx'), 'utf8');
  assert.ok(source.includes('mappingsCapNotice('));
  const header = source.slice(
    source.indexOf('ODI Mappings'),
    source.indexOf('placeholder="Search mappings..."'),
  );
  assert.ok(header.includes('capNotice'));
});
