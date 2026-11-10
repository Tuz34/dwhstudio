/**
 * DWH Studio — Centralized API Service
 * All /api/* calls are routed from here.
 * The CSRF token is added automatically (compatible with Flask-WTF).
 */

function getCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

const HTTP_ERROR_MESSAGES = {
  400: 'Request was rejected.',
  401: 'Authentication required.',
  403: 'This operation is not allowed.',
  404: 'Requested resource was not found.',
  409: 'Request conflicts with the current state.',
  429: 'Too many requests. Try again shortly.',
};

function buildFetchOptions(options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = { ...(options.headers || {}) };
  let body = options.body;

  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers['X-CSRFToken'] = getCsrfToken();
    if (body && typeof body === 'object') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
  }
  return { ...options, method, headers, body };
}

// SCD2 error bodies are status codes and count-only evidence. Other routes keep
// the generic message and do not attach a response body.
const SCD2_ERROR_KEYS = [
  'status', 'error', 'error_code', 'error_type',
  'mapping_name', 'scenario_name', 'scenario_version', 'scenario', 'target',
  'steps', 'mismatches', 'available_versions',
  'required_model_code', 'required_topology_steps', 'required_km', 'required_action',
  'preflight', 'postflight', 'counts',
  'manual_review_required', 'postflight_checks_passed', 'scenario_execution_completed',
  'outcome_unknown', 'known_side_effects',
  'raw_rows_included', 'pk_values_included', 'secret_printed',
  'metadata_source', 'plan', 'ddl', 'warnings', 'blockers',
  'stock_contract_valid', 'source_current_count_equal', 'mode',
];

function scd2ErrorPayload(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const safe = {};
  for (const key of SCD2_ERROR_KEYS) {
    if (Object.prototype.hasOwnProperty.call(payload, key)) safe[key] = payload[key];
  }
  return Object.keys(safe).length > 0 ? safe : null;
}

async function responseError(res, { captureScd2Error = false } = {}) {
  let errorType;
  let scd2Error = null;
  const contentType = res.headers?.get?.('content-type') || '';
  if (contentType.includes('application/json') && typeof res.json === 'function') {
    try {
      const payload = await res.json();
      if (typeof payload?.error_type === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(payload.error_type)) {
        errorType = payload.error_type;
      }
      if (captureScd2Error) scd2Error = scd2ErrorPayload(payload);
    } catch {
      // Response bodies are deliberately not surfaced to the user.
    }
  }

  const message = HTTP_ERROR_MESSAGES[res.status] || (res.status >= 500 ? 'Service request failed.' : 'Request failed.');
  const error = new Error(message);
  error.status = res.status;
  if (errorType) error.errorType = errorType;
  if (scd2Error) error.scd2 = scd2Error;
  return error;
}

export async function request(url, options = {}) {
  const captureScd2Error = options.captureScd2Error === true;
  const fetchOptions = { ...options };
  delete fetchOptions.captureScd2Error;
  const built = buildFetchOptions(fetchOptions);
  const res = await fetch(url, built);

  if (!res.ok) {
    throw await responseError(res, { captureScd2Error });
  }

  const text = await res.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

export async function requestBlob(url, options = {}) {
  const res = await fetch(url, buildFetchOptions(options));
  if (!res.ok) throw await responseError(res);
  return res.blob();
}

// ── GET helpers ─────────────────────────────────────────────────────────────
export const apiGet  = (url, params) => {
  const u = params ? `${url}?${new URLSearchParams(params)}` : url;
  return request(u, { method: 'GET' });
};

// ── POST helpers ─────────────────────────────────────────────────────────────
export const apiPost = (url, body) =>
  request(url, { method: 'POST', body });

// ── DELETE helpers ────────────────────────────────────────────────────────────
export const apiDelete = (url, body) =>
  request(url, { method: 'DELETE', body });

// ── Specific API calls ────────────────────────────────────────────────────────

// Status
export const getStatus       = ()        => apiGet('/api/status');

// Auth
export const getAuthStatus   = ()        => apiGet('/api/auth/status');
export const loginAuth       = (body)    => apiPost('/api/auth/login', body);
export const logoutAuth      = ()        => apiPost('/api/auth/logout', {});
export const getSecurityAudit = (limit = 100) => apiGet('/api/security/audit', { limit });

// Tables & Schema
export const getTables       = (params) => apiGet('/api/tables', params);
export const getSchema       = (name)    => apiGet(`/api/schema/${encodeURIComponent(name)}`);
export const getSchemaImpact = (objectRef) => apiGet('/api/schema/impact', { object: objectRef });
export const preflightSchemaChange = (body) => apiPost('/api/schema/change/preflight', body);
export const applySchemaDbChange = (body) => apiPost('/api/schema/change/apply-db', body);
export const syncOdiSchemaChange = (body) => apiPost('/api/schema/change/sync-odi', body);
export const reconcileSchemaScenario = (body) => apiPost('/api/schema/change/reconcile-scenario', body);
export const getSchemaDrift  = (owner = 'DWH') => apiGet('/api/schema/drift', { owner });
export const getSchemaFindings = (owner = 'DWH') => apiGet('/api/schema/findings', { owner });
export const getSchemaFindingsExportUrl = (owner = 'DWH') =>
  `/api/schema/findings/export?${new URLSearchParams({ owner })}`;
export const getSourceTargetFindings = (sourceSchema = 'HR', targetOwner = 'DWH') =>
  apiGet('/api/schema/source-target/findings', { source_schema: sourceSchema, target_owner: targetOwner });
export const getSchemaPkParity = (sourceSchema = 'HR', targetOwner = 'DWH', maxKeys = 100000) =>
  apiGet('/api/schema/pk-parity', { source_schema: sourceSchema, target_owner: targetOwner, max_keys: maxKeys });
export const getSourceSchema = (name)    => apiGet(`/api/source/schema/${encodeURIComponent(name)}`);
export const getPreview      = (name)    => apiGet(`/api/preview/${encodeURIComponent(name)}`);
export const getSourcePreview = (name)   => apiGet(`/api/source/preview/${encodeURIComponent(name)}`);

// AI Chat
export const askAi           = (body)    => apiPost('/api/ask', body);
export const summarizeLocal  = (body)    => apiPost('/api/local-analyst/summarize', body);
export const executeQuery    = (body)    => apiPost('/api/execute', body);
export const exportExcel     = (body)    => requestBlob('/api/export', { method: 'POST', body });
export const clearHistory    = ()        => apiPost('/api/clear', {});

// ODI
export const getOdiSessions  = ()        => apiGet('/api/odi/sessions');
export const getOdiScenarios = ()        => apiGet('/api/odi/scenarios');
export const getOdiKms       = ()        => apiGet('/api/odi/kms');
export const getOdiMappings  = ()        => apiGet('/api/odi/mappings');
export const getOdiMappingDetail = (name) => apiGet('/api/odi/mapping_detail', { mapping_name: name });
export const getOdiSessionSuggestion = (body) => apiPost('/api/odi/session_suggestion', body);

// Mapping CRUD
export const analyzePrompt   = (body)    => apiPost('/api/mapping/analyze-prompt', body);
export const preflightMappingTarget = (body) => apiPost('/api/mapping/target-preflight', body);
export const generateMapping = (body)    => apiPost('/api/mapping/generate', body);
export const updateMapping   = (body)    => apiPost('/api/mapping/update', body);
export const deleteMapping   = (body)    => apiPost('/api/mapping/delete', body);
export const runMapping      = (body)    => apiPost('/api/mapping/run', body);
export const validateExpression = (body) => apiPost('/api/mapping/validate-expression', body);
export const getMappingState = (name) => apiGet(`/api/mapping/state/${encodeURIComponent(name)}`);
export const getMappingRuns  = ({ limit = 50, status } = {}) => {
  const params = { limit };
  if (status) params.status = status;
  return apiGet('/api/mapping/runs', params);
};

// SCD2 (Type 2) builder: plan is a preview; generate and run need approved: true.
// Error JSON is attached as error.scd2 so the wizard can show the server code.
function scd2Post(url, body) {
  return request(url, { method: 'POST', body, captureScd2Error: true });
}

export const planScd2      = (body)   => scd2Post('/api/scd2/plan', body);
export const generateScd2  = (body)   => scd2Post('/api/scd2/generate', body);
export const runScd2       = (body)   => scd2Post('/api/scd2/run', body);
export const getScd2Postflight = (params) => {
  const url = params ? `/api/scd2/postflight?${new URLSearchParams(params)}` : '/api/scd2/postflight';
  return request(url, { method: 'GET', captureScd2Error: true });
};


// Mapping Snapshots
export const takeSnapshot    = (body)    => apiPost('/api/mapping/snapshot', body);
export const getSnapshots    = (name, limit = 10) =>
  apiGet(`/api/mapping/snapshots/${encodeURIComponent(name)}`, { limit });
export const compareSnapshots = (name, params) =>
  apiGet(`/api/mapping/compare/${encodeURIComponent(name)}`, params);

// Alerts
export const getAlerts       = ()        => apiGet('/api/alerts');
export const acknowledgeAlerts = (body)  => apiPost('/api/alerts/acknowledge', body);
export const checkAlerts     = (body = { freshness_days: 2 }) => apiPost('/api/alerts/check', body);

// Monitoring
export const getDqScores     = (params)  => apiGet('/api/monitoring/dq-scores', params);
export const validateTable   = (body)    => apiPost('/api/quality/validate', body);
export const getIncrementalPreflight = (specName = 'SH_SALES_ODS') =>
  apiGet('/api/incremental/preflight', { spec_name: specName });
export const executeIncrementalAppend = (body) => apiPost('/api/incremental/execute', body);
export const autofixColumn   = (body)    => apiPost('/api/quality/autofix-column', body);
export const autofixAllColumns = (body)  => apiPost('/api/quality/autofix-all-columns', body);
export const syncOdiMapping    = (body)  => apiPost('/api/quality/sync-mapping', body);
export const reconcileTable  = (body)    => apiPost('/api/quality/reconcile', body);
export const getFreshness    = ()        => apiGet('/api/monitoring/freshness');

// RAG
export const getRag          = ()        => apiGet('/api/rag_knowledge');
export const saveRag         = (body)    => apiPost('/api/rag_knowledge', body);
export const deleteRag       = (body)    => apiDelete('/api/rag_knowledge', body);

// Lineage
export const getImpact       = (table, direction='both') =>
  apiGet(`/api/lineage/impact/${encodeURIComponent(table)}`, { direction });
export const getUsageDetails = (table, mappingName) =>
  apiGet(`/api/lineage/usage/${encodeURIComponent(table)}`, { mapping_name: mappingName });

// LangGraph
export const getLangGraphInfo = ()       => apiGet('/api/mapping/graph-info');

// Project Hub
export const getProjectHub   = ()        => apiGet('/api/project_hub');
