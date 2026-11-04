export const WORKFLOW_STEPS = [
  {
    id: 'impact',
    label: 'Impact',
    actionLabel: 'Run Impact',
    icon: 'fa-magnifying-glass',
    description: 'Read-only check for where this object is used.',
    kind: 'read',
  },
  {
    id: 'preflight',
    label: 'Preflight',
    actionLabel: 'Run Preflight',
    icon: 'fa-shield-halved',
    description: 'Read-only safety decision before any write.',
    kind: 'read',
  },
  {
    id: 'db',
    label: 'Apply DB',
    actionLabel: 'Apply DB Change',
    icon: 'fa-database',
    description: 'Approved physical DB change for the target object.',
    kind: 'write',
  },
  {
    id: 'odi',
    label: 'Sync ODI',
    actionLabel: 'Sync ODI Metadata',
    icon: 'fa-rotate',
    description: 'Approved ODI datastore metadata sync.',
    kind: 'write',
  },
  {
    id: 'scenario',
    label: 'Scenario',
    actionLabel: 'Reconcile Scenario',
    icon: 'fa-file-circle-check',
    description: 'Metadata reconcile only. It does not run the scenario.',
    kind: 'metadata',
  },
  {
    id: 'drift',
    label: 'Verify Drift',
    actionLabel: 'Run Drift Check',
    icon: 'fa-code-compare',
    description: 'Read-back check for DB-to-ODI alignment.',
    kind: 'read',
  },
];

export const OPERATION_LABELS = {
  modify_type: 'Modify column type',
  rename_column: 'Rename column',
  rename_table: 'Rename table',
};

export function getStepActionLabel(stepId) {
  return WORKFLOW_STEPS.find(step => step.id === stepId)?.actionLabel || 'Run Step';
}

export function getActiveWorkflowStep(steps, results = {}) {
  if (steps.preflight === 'done' && results.preflight?.allowed_to_apply === false) {
    return 'blocked';
  }

  for (const step of WORKFLOW_STEPS) {
    if (steps[step.id] !== 'done') {
      return step.id;
    }
  }

  return 'complete';
}

export function summarizeRequest(form) {
  const isRenameTable = form.op === 'rename_table';
  const isModifyType = form.op === 'modify_type';
  const object = isRenameTable
    ? `${form.owner}.${form.table}`
    : `${form.owner}.${form.table}.${form.column}`;
  const target = isModifyType ? form.new_type : form.new_name;

  return [
    ['Operation', OPERATION_LABELS[form.op] || form.op],
    ['Object', object],
    ['Target', target || '-'],
    ['ODI Model', form.model_code],
    ['Mapping', form.mapping_name],
    ['Scenario Version', form.scenario_version],
  ];
}

export function normalizeTargetTables(rows, owner = 'DWH') {
  const requestedOwner = String(owner || '').toUpperCase();
  return (Array.isArray(rows) ? rows : [])
    .map(row => {
      const fullName = String(row?.original || row?.name || '').replace(/^ORACLE:\s*/i, '').trim().toUpperCase();
      const [rowOwner, ...tableParts] = fullName.split('.');
      return {
        owner: String(row?.schema || rowOwner || '').toUpperCase(),
        table: tableParts.join('.').toUpperCase(),
        fullName,
      };
    })
    .filter(row => row.owner === requestedOwner && row.table)
    .sort((a, b) => a.table.localeCompare(b.table));
}

export function formatColumnOptions(columns) {
  return (Array.isArray(columns) ? columns : [])
    .map(row => {
      const name = String(row?.COLUMN_NAME || row?.column_name || '').toUpperCase();
      const dataType = String(row?.DATA_TYPE || row?.data_type || row?.FULL_DATA_TYPE || '').toUpperCase();
      const nullable = String(row?.NULLABLE || row?.nullable || '').toUpperCase();
      return {
        name,
        dataType,
        nullable,
        label: dataType ? `${name} - ${dataType}` : name,
      };
    })
    .filter(row => row.name);
}

export function findColumnDrift(driftResult, owner, table, column) {
  const rows = Array.isArray(driftResult?.drift) ? driftResult.drift : [];
  const target = {
    owner: String(owner || '').toUpperCase(),
    table: String(table || '').toUpperCase(),
    column: String(column || '').toUpperCase(),
  };
  return rows.find(row => (
    String(row?.owner || '').toUpperCase() === target.owner
    && String(row?.table || '').toUpperCase() === target.table
    && String(row?.column || '').toUpperCase() === target.column
  )) || null;
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean).map(value => String(value).toUpperCase()))].sort();
}

export function groupImpactByMapping(impact) {
  const rows = Array.isArray(impact?.mappings) ? impact.mappings : [];
  const grouped = new Map();
  rows.forEach(row => {
    const mappingName = String(row?.mapping_name || row?.MAPPING_NAME || 'UNKNOWN_MAPPING').toUpperCase();
    if (!grouped.has(mappingName)) grouped.set(mappingName, []);
    grouped.get(mappingName).push(row);
  });
  return [...grouped.entries()]
    .map(([mappingName, mappingRows]) => ({
      mappingName,
      upstreamTables: uniqueSorted(
        mappingRows
          .filter(row => String(row?.direction || '').toLowerCase() === 'upstream')
          .map(row => row?.related_table)
      ),
      downstreamTables: uniqueSorted(
        mappingRows
          .filter(row => String(row?.direction || '').toLowerCase() === 'downstream')
          .map(row => row?.related_table)
      ),
      rows: mappingRows,
    }))
    .sort((a, b) => a.mappingName.localeCompare(b.mappingName));
}

export function getSuggestedMappingName(impact) {
  return groupImpactByMapping(impact)[0]?.mappingName || '';
}

export function summarizeMappingContext(mappings, mappingName) {
  const targetName = String(mappingName || '').toUpperCase();
  const match = (Array.isArray(mappings) ? mappings : []).find(row => (
    String(row?.MAPPING_NAME || row?.mapping_name || '').toUpperCase() === targetName
  ));
  if (!match) {
    return { project: '-', folder: '-', targetTable: '-' };
  }
  return {
    project: String(match.PROJECT_CODE || match.project_code || match.PROJECT_NAME || match.project_name || match.project || '-').toUpperCase() || '-',
    folder: String(match.FOLDER_NAME || match.folder_name || '-').toUpperCase() || '-',
    targetTable: String(match.TGT_TABLE || match.target_table || '-').toUpperCase() || '-',
  };
}

export function summarizeMappingDetail(detail, selectedColumn = '') {
  const targetColumn = String(selectedColumn || '').toUpperCase();
  const columns = Array.isArray(detail?.columns) ? detail.columns : [];
  const selectedColumns = columns
    .filter(row => !targetColumn || String(row?.tgt || row?.target_column || '').toUpperCase() === targetColumn)
    .map(row => ({
      target: String(row?.tgt || row?.target_column || '').toUpperCase(),
      source: String(row?.src || row?.expression || ''),
      type: String(row?.type || row?.data_type || '').toUpperCase(),
      expression: Boolean(row?.is_exp || row?.is_expression),
    }))
    .filter(row => row.target);

  return {
    sources: uniqueSorted((Array.isArray(detail?.sources) ? detail.sources : []).map(row => row?.name)),
    target: String(detail?.target?.name || detail?.TGT_TABLE || detail?.target || ''),
    selectedColumns,
    expressionCount: columns.filter(row => Boolean(row?.is_exp || row?.is_expression)).length,
    columnCount: columns.length,
  };
}

export function summarizeDriftQueue(driftResult) {
  const rows = Array.isArray(driftResult?.drift) ? driftResult.drift : [];
  return rows
    .map(row => {
      const owner = String(row?.owner || '').toUpperCase();
      const table = String(row?.table || '').toUpperCase();
      const column = String(row?.column || '').toUpperCase();
      const dbType = String(row?.db_type || '').toUpperCase() || '-';
      const odiType = String(row?.odi_type || '').toUpperCase() || '-';
      return {
        owner,
        table,
        column,
        label: `${table}.${column}`,
        detail: `DB ${dbType} / ODI ${odiType}`,
      };
    })
    .filter(row => row.owner && row.table && row.column)
    .sort((a, b) => a.label.localeCompare(b.label));
}
