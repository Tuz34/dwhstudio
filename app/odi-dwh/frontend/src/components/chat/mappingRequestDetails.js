import { DATAMART_PLACEMENT_WARNING, DATAMART_TYPE2_ONLY_WARNING } from '../../datamartContract.js';

const LOAD_LABELS = {
  insert_update: 'Insert / update',
  truncate_load: 'Truncate and reload',
  type1: 'Type 1',
  type2: 'Type 2 history',
};
const WARNINGS = new Set([
  DATAMART_TYPE2_ONLY_WARNING,
  DATAMART_PLACEMENT_WARNING,
  'Mapping name is longer than 31 characters; Type 2 requires a shorter name.',
]);
const STATED_FIELDS = new Set([
  'source_schema', 'source_table', 'target_layer', 'target_schema',
  'target_table', 'mapping_name', 'load_type', 'key_columns',
]);

export function mappingRequestDetails(request = {}) {
  const value = request && typeof request === 'object' ? request : {};
  const statedFields = new Set(
    Array.isArray(value.stated_fields)
      ? value.stated_fields.filter(field => STATED_FIELDS.has(field))
      : []
  );
  const rows = [
    ['mapping_name', 'Mapping', value.mapping_name],
    ['source_schema', 'Source schema', value.source_schema || value.src_schema],
    ['source_table', 'Source table', value.source_table || value.src_table],
    ['target_layer', 'Layer', value.target_layer],
    ['target_schema', 'Target schema', value.target_schema || value.tgt_owner],
    ['target_table', 'Target table', value.target_table || value.tgt_table],
    ['load_type', 'Load type', LOAD_LABELS[value.load_type]],
    ['key_columns', 'Key columns', Array.isArray(value.key_columns) && value.key_columns.length
      ? value.key_columns.join(', ') : null],
  ];
  const details = { stated: [], suggested: [], warnings: [] };
  for (const [field, label, text] of rows) {
    if (text) details[statedFields.has(field) ? 'stated' : 'suggested'].push([label, text]);
  }
  if (Array.isArray(value.warnings)) {
    details.warnings = value.warnings.filter(warning => WARNINGS.has(warning));
  }
  return details;
}
