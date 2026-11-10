const LOAD_LABELS = {
  insert_update: 'Insert / update',
  truncate_load: 'Truncate and reload',
  type1: 'Type 1',
  type2: 'Type 2 history',
};

export function mappingRequestDetails(request = {}) {
  const details = [];
  if (request.target_layer) details.push(['Layer', request.target_layer]);
  if (LOAD_LABELS[request.load_type]) details.push(['Load type', LOAD_LABELS[request.load_type]]);
  if (Array.isArray(request.key_columns) && request.key_columns.length) {
    details.push(['Key columns', request.key_columns.join(', ')]);
  }
  return details;
}
