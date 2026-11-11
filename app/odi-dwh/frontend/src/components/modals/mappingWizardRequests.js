import { DATAMART_TYPE2_ONLY_WARNING } from '../../datamartContract.js';

const IDENTIFIER = /^[A-Z][A-Z0-9_]{0,127}$/;
const MAX_KEY_COLUMNS = 8;

// Authority: mapping_generator.CLASSIC_DWH_SOURCE_OWNER. The drift test fails
// when that constant changes. Only this owner is proposed as ORACLE_DWH.
export const DEFAULT_SOURCE_MODEL = 'ORACLE_SOURCE';
export const INTERNAL_SOURCE_MODEL = 'ORACLE_DWH';
export const INTERNAL_SOURCE_OWNERS = Object.freeze(['DWH']);

export function resolveWizardSourceModel(form = {}) {
  if (form.src_model_from_prefill === true || form.src_model_touched === true) {
    return form.src_model;
  }
  const schema = String(form.src_schema || '').trim().toUpperCase();
  const model = form.src_model;
  const emptyDefault = model == null || model === '' || model === DEFAULT_SOURCE_MODEL;
  if (form.src_schema_typed === true && emptyDefault && INTERNAL_SOURCE_OWNERS.includes(schema)) {
    return INTERNAL_SOURCE_MODEL;
  }
  return model;
}

export const LOAD_TYPES = Object.freeze([
  { value: 'insert_update', label: 'Insert / update (needs key columns)' },
  { value: 'truncate_load', label: 'Truncate and reload' },
  { value: 'type1', label: 'Type 1 overwrite (needs key columns)' },
  { value: 'type2', label: 'Type 2 history' },
]);

const VALID_LOAD_TYPES = new Set(LOAD_TYPES.map(({ value }) => value));

export function prefillLoadOptions(initialData = {}) {
  return {
    load_type: VALID_LOAD_TYPES.has(initialData.load_type) ? initialData.load_type : '',
    key_columns: Array.isArray(initialData.key_columns) &&
      initialData.key_columns.every(column => typeof column === 'string')
      ? initialData.key_columns.map(column => column.trim().toUpperCase()).join(', ')
      : '',
  };
}

export function parseKeyColumns(value) {
  if (typeof value !== 'string') throw new Error('Enter comma-separated key columns.');
  if (!value.trim()) return [];
  const columns = value.split(',').map(column => column.trim().toUpperCase());
  if (columns.some(column => !IDENTIFIER.test(column))) {
    throw new Error('Key columns must be valid comma-separated identifiers.');
  }
  const uniqueColumns = [...new Set(columns)];
  if (uniqueColumns.length > MAX_KEY_COLUMNS) {
    throw new Error(`Enter no more than ${MAX_KEY_COLUMNS} key columns.`);
  }
  return uniqueColumns;
}

export function validateLoadOptions(form) {
  const loadType = form.load_type;
  if (!VALID_LOAD_TYPES.has(loadType)) throw new Error('Choose a load type.');
  if (loadType !== 'type2' && String(form.tgt_table ?? '').trim().toUpperCase().startsWith('DM_')) {
    throw new Error(DATAMART_TYPE2_ONLY_WARNING);
  }
  const keyColumns = loadType === 'truncate_load' ? [] : parseKeyColumns(form.key_columns);
  if (['insert_update', 'type1'].includes(loadType) && keyColumns.length === 0) {
    throw new Error('Enter at least one key column for this load type.');
  }
  return { loadType, keyColumns };
}

export function getLoadOptionError(form) {
  try {
    validateLoadOptions(form);
    return null;
  } catch (error) {
    return error.message;
  }
}

export function mappingName(form) {
  return form.mapping_name || `MAP_${form.src_table.replace(/\W/g, '_').toUpperCase()}_TO_ODS`;
}

export function buildMappingRequest(form) {
  const { loadType, keyColumns } = validateLoadOptions(form);
  if (loadType === 'type2') throw new Error('Type 2 history must use the SCD2 wizard.');
  const incremental = loadType === 'insert_update' || loadType === 'type1';
  const rebuild = form.drop_target_if_exists === true;
  return {
    approved: true,
    engine: 'classic',
    params: {
      src_model: resolveWizardSourceModel(form),
      src_table: form.src_table,
      src_schema: form.src_schema,
      tgt_model: form.tgt_model,
      tgt_owner: form.tgt_owner,
      tgt_table: form.tgt_table,
      needs_skey: form.needs_skey === true,
      skey_natural_key: form.skey_natural_key,
      description: form.description,
      mapping_name: mappingName(form),
      drop_target_if_exists: rebuild,
      replace_existing_mapping: rebuild,
      operation: 'CREATE',
      integration_type: incremental ? 'IU' : 'CONTROL_APPEND',
      update_key_columns: keyColumns,
      update_key_kind: incremental ? 'primary' : 'unique',
    },
  };
}

export function buildScd2Handoff(form) {
  const { loadType, keyColumns } = validateLoadOptions(form);
  if (loadType !== 'type2') throw new Error('Choose Type 2 history for the SCD2 wizard.');
  return {
    type: 'OPEN_MODAL',
    payload: {
      type: 'scd2',
      props: {
        mappingRequest: {
          src_model: form.src_model,
          src_table: form.src_table,
          src_schema: form.src_schema,
          tgt_model: form.tgt_model,
          tgt_owner: form.tgt_owner,
          tgt_table: form.tgt_table,
          needs_skey: form.needs_skey === true,
          skey_natural_key: form.skey_natural_key,
          description: form.description,
          mapping_name: form.mapping_name,
          drop_target_if_exists: form.drop_target_if_exists === true,
          load_type: loadType,
          key_columns: keyColumns,
        },
      },
    },
  };
}

const stripHelpLinks = (text = '') => String(text).replace(/\s*Help:\s*https?:\/\/\S+/gi, '').trim();

export function getFriendlyFailure(text = '') {
  const cleaned = stripHelpLinks(text);
  const lower = cleaned.toLowerCase();

  if (cleaned.includes('ORA-01031')) {
    return 'The target table already exists, but this connection is not allowed to drop it. Retry with "Drop target table if it exists" turned off, or ask a DBA to drop the table or grant the required permission.';
  }

  if (cleaned.includes('ORA-00955')) {
    return 'The target table or another database object with this name already exists. Retry without dropping the table, or choose a different target table name.';
  }

  if (cleaned.includes('ODI-10178')) {
    return 'ODI could not open the repository connection. Check the local ODI connection setup, then retry.';
  }

  if (lower.includes('datastore')) {
    return 'The target table step finished, but ODI table registration did not complete. Check the ODI repository connection and retry.';
  }

  if (lower.includes('mapping')) {
    return 'The target table step finished, but ODI mapping creation did not complete. Check the ODI error details and retry.';
  }

  if (cleaned) {
    return 'The run stopped before completion. Review the technical details or retry after fixing the highlighted step.';
  }

  return 'The run stopped before completion.';
}

export const AUDIT_PRESERVATION_REJECTED_CODE = 'audit_preservation_rejected';
export const AUDIT_PRESERVATION_REJECTED_MESSAGE =
  "The target table's audit column types are not supported for audit preservation. Nothing was created.";

export function selectWizardFailureMessage(result, text) {
  if (result?.error_code === AUDIT_PRESERVATION_REJECTED_CODE) {
    return AUDIT_PRESERVATION_REJECTED_MESSAGE;
  }
  if (!text) return '';
  return getFriendlyFailure(text);
}

export function getMappingOutcome(result) {
  const params = result?.params || result?.validated_params;
  const key = result?.steps?.create_target_table?.key || result?.target_table_result?.key;
  return {
    integrationType: typeof params?.integration_type === 'string' ? params.integration_type : null,
    ikm: typeof params?.ikm_name === 'string' ? params.ikm_name : null,
    key: key && typeof key.status === 'string'
      ? [key.status, key.kind,
        Array.isArray(key.columns) && key.columns.length ? `(${key.columns.join(', ')})` : null]
        .filter(Boolean).join(' ')
      : null,
  };
}
