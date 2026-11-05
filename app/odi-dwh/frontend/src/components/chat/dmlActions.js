// /api/ask returns each write as its own action; 'dml' is kept for older responses.
const DML_ACTIONS = new Set(['insert', 'update', 'delete', 'dml']);

export function isDmlAction(action) {
  return typeof action === 'string' && DML_ACTIONS.has(action.trim().toLowerCase());
}

export function findPendingDmlQuery(queries) {
  if (!Array.isArray(queries)) return undefined;
  return queries.find(query => isDmlAction(query?.action) && !query.auto_executed);
}
