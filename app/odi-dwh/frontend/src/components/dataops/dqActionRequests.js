const IDENTIFIER = /^[A-Z][A-Z0-9_]*$/;

function targetParts(targetTable) {
  const parts = String(targetTable || '').trim().toUpperCase().split('.');
  const owner = parts.length === 2 ? parts[0] : 'DWH';
  const table = parts.length === 2 ? parts[1] : parts[0];
  if (parts.length > 2 || owner !== 'DWH' || !IDENTIFIER.test(table)) {
    throw new Error('Invalid DQ target identifier.');
  }
  return { owner, table };
}

function columnName(value) {
  const column = String(value || '').trim().toUpperCase();
  if (!IDENTIFIER.test(column)) throw new Error('Invalid DQ column identifier.');
  return column;
}

export function buildAutofixColumnRequest(targetTable, column) {
  return { approved: true, ...targetParts(targetTable), column: columnName(column) };
}

export function buildAutofixAllRequest(targetTable, columns) {
  if (!Array.isArray(columns) || columns.length === 0) throw new Error('Invalid DQ column list.');
  return { approved: true, ...targetParts(targetTable), columns: columns.map(columnName) };
}

export function buildSyncMappingRequest(targetTable) {
  return { approved: true, ...targetParts(targetTable) };
}
