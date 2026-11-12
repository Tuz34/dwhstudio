export function cleanName(value) {
  return String(value || '').trim();
}

export function splitTables(value) {
  return cleanName(value)
    .split(',')
    .map(item => cleanName(item))
    .filter(item => item && item !== '?');
}

export function shortName(value) {
  const text = cleanName(value);
  return text.split('.').pop() || text;
}

const ODI_SCHEMA_TO_DB_SCHEMA = {
  SRC_HR: 'HR',
  SRC_SH: 'SH',
  TGT_DWH: 'DWH',
  LGCL_ORACLE_DWH_UNIT: 'DWH',
};

export const LAYER_OPTIONS = ['', 'ODS', 'EDW', 'DM'];

export function layerMatches(mappingLayer, selectedLayer) {
  const selected = cleanName(selectedLayer).toUpperCase();
  if (!selected) return true;
  return cleanName(mappingLayer).toUpperCase() === selected;
}

function normalizeSchema(schema) {
  const key = cleanName(schema).toUpperCase();
  return ODI_SCHEMA_TO_DB_SCHEMA[key] || schema;
}

export function sameTable(a, b) {
  return shortName(a).toUpperCase() === shortName(b).toUpperCase();
}

export function tableRole(mapping, tableName) {
  if (sameTable(mapping.TGT_TABLE, tableName)) return 'Target';
  if (splitTables(mapping.SRC_TABLE).some(table => sameTable(table, tableName))) return 'Source';
  return 'Used';
}

export function purposeText(role, mapping) {
  if (role === 'Target') return `${mapping.MAPPING_NAME} produces this target table.`;
  if (role === 'Source') return `${mapping.MAPPING_NAME} uses this source table.`;
  return `${mapping.MAPPING_NAME} uses this as an expression, filter, or helper component.`;
}

export function displayRole(value) {
  const normalized = cleanName(value).toUpperCase();
  const word = (...codes) => String.fromCharCode(...codes);
  const sourceWord = word(75, 65, 89, 78, 65, 75);
  const roleMap = {
    [`ANA ${sourceWord}`]: 'MAIN SOURCE',
    [sourceWord]: 'SOURCE',
    [word(72, 69, 68, 69, 70)]: 'TARGET',
    [word(70, 73, 76, 84, 82, 69)]: 'FILTER',
    [`YARDIMCI ${sourceWord}`]: 'HELPER SOURCE',
  };
  return roleMap[normalized] || value || 'SOURCE';
}

export function displayType(value, fallback = 'N/A') {
  const text = cleanName(value);
  if (!text || text === '()') return fallback;
  if (/^\(\d/.test(text)) return `Length ${text.replace(/[()]/g, '')}`;
  return text;
}

export function impactLabel(direction) {
  return direction === 'up' ? 'SOURCE INPUT' : 'TARGET OUTPUT';
}

export function impactTitle(direction) {
  return direction === 'up'
    ? 'Selected table is produced from this related source table.'
    : 'Selected table is used as a source to produce this target table.';
}

export function tableMeta(value) {
  const text = cleanName(value);
  const parts = text.split('.');
  const rawSchema = parts.length > 1 ? parts.slice(0, -1).join('.') : '';
  const schema = normalizeSchema(rawSchema);
  const table = shortName(text);
  const upper = text.toUpperCase();
  const schemaUpper = schema.toUpperCase();
  const tableUpper = table.toUpperCase();

  if (upper.startsWith('FLAT_FILE')) {
    return { table, schema, system: 'FILE', badge: 'bg-slate-100 text-slate-600 border-slate-200' };
  }
  if (
    schemaUpper === 'HR' ||
    schemaUpper === 'SH' ||
    schemaUpper === 'DWH' ||
    upper.startsWith('DWH.') ||
    upper.startsWith('HR.') ||
    upper.startsWith('SH.') ||
    upper.startsWith('SRC_HR.') ||
    upper.startsWith('SRC_SH.') ||
    upper.startsWith('TGT_DWH.') ||
    upper.startsWith('LGCL_ORACLE_DWH_UNIT.') ||
    upper.startsWith('ODS.') ||
    tableUpper.startsWith('ODS_') ||
    tableUpper.startsWith('SKEY_') ||
    tableUpper.startsWith('DWH_')
  ) {
    return { table, schema: schema || 'DWH', system: 'ORACLE', badge: 'bg-emerald-50 text-emerald-700 border-emerald-100' };
  }

  return { table, schema, system: 'ORACLE', badge: 'bg-emerald-50 text-emerald-700 border-emerald-100' };
}

export function uniqueImpactRows(rows) {
  const seen = new Set();
  return rows.filter(row => {
    const key = `${shortName(row.related_table).toUpperCase()}|${row.mapping_name}|${row.direction}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
