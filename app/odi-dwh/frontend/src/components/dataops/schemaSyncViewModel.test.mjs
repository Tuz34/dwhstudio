import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findColumnDrift,
  formatColumnOptions,
  getActiveWorkflowStep,
  getStepActionLabel,
  groupImpactByMapping,
  getSuggestedMappingName,
  summarizeDriftQueue,
  summarizeMappingContext,
  summarizeMappingDetail,
  normalizeTargetTables,
  summarizeRequest,
} from './schemaSyncViewModel.js';

test('identifies preflight as the next guided action after impact completes', () => {
  const steps = {
    impact: 'done',
    preflight: 'idle',
    db: 'idle',
    odi: 'idle',
    scenario: 'idle',
    drift: 'idle',
  };

  assert.equal(getActiveWorkflowStep(steps, {}), 'preflight');
  assert.equal(getStepActionLabel('preflight'), 'Run Preflight');
});

test('moves to DB apply only after preflight allows the change', () => {
  const steps = {
    impact: 'done',
    preflight: 'done',
    db: 'idle',
    odi: 'idle',
    scenario: 'idle',
    drift: 'idle',
  };
  const results = {
    preflight: { allowed_to_apply: true },
  };

  assert.equal(getActiveWorkflowStep(steps, results), 'db');
});

test('keeps DB apply blocked when preflight rejects the request', () => {
  const steps = {
    impact: 'done',
    preflight: 'done',
    db: 'idle',
    odi: 'idle',
    scenario: 'idle',
    drift: 'idle',
  };
  const results = {
    preflight: { allowed_to_apply: false },
  };

  assert.equal(getActiveWorkflowStep(steps, results), 'blocked');
});

test('summarizes rename table requests without column text', () => {
  const summary = summarizeRequest({
    owner: 'DWH',
    model_code: 'TGT_DWH',
    table: 'ODS_HR_EMPLOYEES',
    column: 'FIRST_NAME',
    op: 'rename_table',
    new_name: 'ODS_HR_EMPLOYEES_V2',
    mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
    scenario_version: 'V44A',
  });

  assert.deepEqual(summary, [
    ['Operation', 'Rename table'],
    ['Object', 'DWH.ODS_HR_EMPLOYEES'],
    ['Target', 'ODS_HR_EMPLOYEES_V2'],
    ['ODI Model', 'TGT_DWH'],
    ['Mapping', 'MAP_HR_EMPLOYEES_TO_ODS'],
    ['Scenario Version', 'V44A'],
  ]);
});

test('normalizes target table dropdown options from table API rows', () => {
  const rows = [
    { original: 'HR.EMPLOYEES', schema: 'HR', platform: 'SOURCE' },
    { original: 'DWH.ODS_HR_EMPLOYEES', schema: 'DWH', platform: 'ORACLE' },
    { original: 'DWH.ODS_HR_JOBS', schema: 'DWH', platform: 'ORACLE' },
  ];

  assert.deepEqual(normalizeTargetTables(rows, 'DWH'), [
    { owner: 'DWH', table: 'ODS_HR_EMPLOYEES', fullName: 'DWH.ODS_HR_EMPLOYEES' },
    { owner: 'DWH', table: 'ODS_HR_JOBS', fullName: 'DWH.ODS_HR_JOBS' },
  ]);
});

test('formats column dropdown options with current data type', () => {
  const columns = [
    { COLUMN_NAME: 'FIRST_NAME', DATA_TYPE: 'VARCHAR2(30)', NULLABLE: 'Y' },
    { COLUMN_NAME: 'LAST_NAME', DATA_TYPE: 'VARCHAR2(25)', NULLABLE: 'N' },
  ];

  assert.deepEqual(formatColumnOptions(columns), [
    { name: 'FIRST_NAME', dataType: 'VARCHAR2(30)', nullable: 'Y', label: 'FIRST_NAME - VARCHAR2(30)' },
    { name: 'LAST_NAME', dataType: 'VARCHAR2(25)', nullable: 'N', label: 'LAST_NAME - VARCHAR2(25)' },
  ]);
});

test('finds selected DB-to-ODI drift for the active column', () => {
  const drift = {
    drift: [
      { owner: 'DWH', table: 'ODS_HR_EMPLOYEES', column: 'FIRST_NAME', db_type: 'VARCHAR2(30)', odi_type: 'VARCHAR2(20)' },
    ],
  };

  assert.deepEqual(findColumnDrift(drift, 'DWH', 'ODS_HR_EMPLOYEES', 'FIRST_NAME'), drift.drift[0]);
  assert.equal(findColumnDrift(drift, 'DWH', 'ODS_HR_EMPLOYEES', 'LAST_NAME'), null);
});

test('groups impact rows mapping-first for drilldown display', () => {
  const impact = {
    mappings: [
      { mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS', direction: 'downstream', related_table: 'ODS_HR_EMPLOYEES' },
      { mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS', direction: 'upstream', related_table: 'EMPLOYEES' },
      { mapping_name: 'MAP_HR_JOBS_TO_ODS', direction: 'upstream', related_table: 'JOBS' },
    ],
  };

  assert.deepEqual(groupImpactByMapping(impact), [
    {
      mappingName: 'MAP_HR_EMPLOYEES_TO_ODS',
      upstreamTables: ['EMPLOYEES'],
      downstreamTables: ['ODS_HR_EMPLOYEES'],
      rows: impact.mappings.slice(0, 2),
    },
    {
      mappingName: 'MAP_HR_JOBS_TO_ODS',
      upstreamTables: ['JOBS'],
      downstreamTables: [],
      rows: impact.mappings.slice(2),
    },
  ]);
});

test('suggests the first affected mapping name from impact groups', () => {
  assert.equal(getSuggestedMappingName({
    mappings: [
      { mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS', direction: 'upstream', related_table: 'EMPLOYEES' },
    ],
  }), 'MAP_HR_EMPLOYEES_TO_ODS');
  assert.equal(getSuggestedMappingName({ mappings: [] }), '');
});

test('summarizes mapping catalog context for project and folder display', () => {
  const mappings = [
    { MAPPING_NAME: 'MAP_HR_EMPLOYEES_TO_ODS', FOLDER_NAME: 'FLD_CORE_MAPPINGS', PROJECT_CODE: 'ORA_DWH', TGT_TABLE: 'ODS_HR_EMPLOYEES' },
  ];

  assert.deepEqual(summarizeMappingContext(mappings, 'map_hr_employees_to_ods'), {
    project: 'ORA_DWH',
    folder: 'FLD_CORE_MAPPINGS',
    targetTable: 'ODS_HR_EMPLOYEES',
  });
});

test('summarizes mapping detail around selected target column', () => {
  const detail = {
    sources: [{ component: 'EMPLOYEES', name: 'SRC_HR.EMPLOYEES', role: 'Ana kaynak' }],
    target: { component: 'ODS_HR_EMPLOYEES', name: 'TGT_DWH.ODS_HR_EMPLOYEES' },
    columns: [
      { tgt: 'FIRST_NAME', src: 'EMPLOYEES.FIRST_NAME', type: 'VARCHAR2(30)', is_exp: false },
      { tgt: 'SOURCE_SYSTEM', src: "'HR'", type: 'CHAR(2)', is_exp: true },
    ],
  };

  assert.deepEqual(summarizeMappingDetail(detail, 'FIRST_NAME'), {
    sources: ['SRC_HR.EMPLOYEES'],
    target: 'TGT_DWH.ODS_HR_EMPLOYEES',
    selectedColumns: [{ target: 'FIRST_NAME', source: 'EMPLOYEES.FIRST_NAME', type: 'VARCHAR2(30)', expression: false }],
    expressionCount: 1,
    columnCount: 2,
  });
});

test('summarizes drift queue as selectable table-column items', () => {
  const drift = {
    drift: [
      { owner: 'DWH', table: 'ODS_HR_EMPLOYEES', column: 'FIRST_NAME', db_type: 'VARCHAR2(30)', odi_type: 'VARCHAR2(20)' },
      { owner: 'DWH', table: 'ODS_HR_JOBS', column: 'JOB_TITLE', db_type: 'VARCHAR2(35)', odi_type: 'VARCHAR2(25)' },
    ],
  };

  assert.deepEqual(summarizeDriftQueue(drift), [
    {
      owner: 'DWH',
      table: 'ODS_HR_EMPLOYEES',
      column: 'FIRST_NAME',
      label: 'ODS_HR_EMPLOYEES.FIRST_NAME',
      detail: 'DB VARCHAR2(30) / ODI VARCHAR2(20)',
    },
    {
      owner: 'DWH',
      table: 'ODS_HR_JOBS',
      column: 'JOB_TITLE',
      label: 'ODS_HR_JOBS.JOB_TITLE',
      detail: 'DB VARCHAR2(35) / ODI VARCHAR2(25)',
    },
  ]);
});
