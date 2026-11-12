export const MAPPINGS_TRUNCATED_HEADER = 'X-ODI-Mappings-Truncated';
export const MAPPINGS_LIMIT_HEADER = 'X-ODI-Mappings-Limit';

export function attachMappingsCap(data, headers) {
  if (!Array.isArray(data)) return data;
  const get = headers && typeof headers.get === 'function' ? headers.get.bind(headers) : null;
  data.truncated = Boolean(get && get(MAPPINGS_TRUNCATED_HEADER) === '1');
  data.limit = Number((get && get(MAPPINGS_LIMIT_HEADER)) || 0);
  return data;
}

export function mappingsCapNotice(list) {
  if (!Array.isArray(list) || !list.truncated) return '';
  const limit = Number(list.limit);
  if (!Number.isFinite(limit) || limit < 1) return '';
  return `Showing the first ${limit} mappings`;
}
