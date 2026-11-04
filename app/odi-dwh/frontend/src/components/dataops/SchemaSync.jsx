import { useEffect, useMemo, useState } from 'react';
import {
  applySchemaDbChange,
  getSchemaDrift,
  getSchemaImpact,
  getOdiMappingDetail,
  getOdiMappings,
  getSchema,
  getTables,
  getSchemaFindings,
  getSchemaPkParity,
  getSourceTargetFindings,
  preflightSchemaChange,
  reconcileSchemaScenario,
  syncOdiSchemaChange,
} from '../../services/api';
import {
  OPERATION_LABELS,
  WORKFLOW_STEPS,
  findColumnDrift,
  formatColumnOptions,
  getActiveWorkflowStep,
  getSuggestedMappingName,
  groupImpactByMapping,
  summarizeDriftQueue,
  summarizeMappingContext,
  summarizeMappingDetail,
  normalizeTargetTables,
  summarizeRequest,
} from './schemaSyncViewModel';
import {
  EvidencePreview,
  ImpactDrilldown,
  MappingDetailPanel,
  PkParityPanel,
  SchemaFindingsPanel,
  SourceTargetFindingsPanel,
  WorkflowStep,
} from './schemaSyncPanels';

const INPUT =
  'w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 outline-none focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100';
const BUTTON =
  'inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50';

const emptyResults = {
  impact: null,
  preflight: null,
  db: null,
  odi: null,
  scenario: null,
  drift: null,
  findings: null,
  sourceTargetFindings: null,
  pkParity: null,
};

function deriveSourceSchema(tableName) {
  const match = String(tableName || '').match(/^ODS_([^_]+)/i);
  return (match?.[1] || 'HR').toUpperCase();
}

function JsonBlock({ title, value }) {
  if (!value) return null;
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-950 text-slate-100">
      <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
        <span className="text-xs font-bold uppercase text-slate-300">{title}</span>
      </div>
      <pre className="max-h-72 overflow-auto p-3 text-xs leading-5">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function deriveSchemaSyncRecovery(steps, results, error) {
  if (error) {
    const failedStep = Object.entries(steps).find(([, state]) => state === 'error')?.[0] || 'current step';
    return {
      tone: 'error',
      title: 'Workflow stopped',
      message: `Workflow stopped at ${failedStep}. Review the evidence, then rerun that step when ready.`,
      nextAction: 'Fix the failed step first; do not continue the workflow blindly.',
    };
  }
  if (results.preflight?.allowed_to_apply === false || steps.preflight === 'error') {
    return {
      tone: 'blocked',
      title: 'Preflight blocked',
      message: 'Preflight blocked DB apply. No write steps should run.',
      nextAction: 'Review the preflight response before changing the request.',
    };
  }
  if (steps.db === 'done' && steps.odi !== 'done') {
    return {
      tone: 'warning',
      title: 'Partial state',
      message: 'DB change completed. ODI metadata sync is still pending.',
      nextAction: 'Run Sync ODI, then reconcile the scenario after ODI read-back is aligned.',
    };
  }
  if (steps.odi === 'done' && steps.scenario !== 'done') {
    return {
      tone: 'warning',
      title: 'Partial state',
      message: 'ODI metadata synced. Scenario reconcile is still pending.',
      nextAction: 'Run Scenario before treating the workflow as complete.',
    };
  }
  if (steps.db === 'done' && steps.odi === 'done' && steps.scenario === 'done') {
    return {
      tone: 'ok',
      title: 'Aligned',
      message: 'DB, ODI, and scenario metadata are aligned for this workflow.',
      nextAction: 'Run Drift Check for final read-back confidence.',
    };
  }
  if (steps.impact === 'done' || steps.preflight === 'done') {
    return {
      tone: 'info',
      title: 'Ready for controlled apply',
      message: 'Read-only evidence is available; write steps still require approval.',
      nextAction: 'Confirm approval only when the impact and preflight results look correct.',
    };
  }
  return {
    tone: 'idle',
    title: 'Not started',
    message: 'Start with read-only Impact. Nothing is written until Apply DB or Sync ODI.',
    nextAction: 'Run Impact, then Preflight.',
  };
}

function derivePreflightOutcome(preflight) {
  if (!preflight) {
    return {
      tone: 'idle',
      title: 'No preflight yet',
      message: 'Run Preflight before direct write actions.',
    };
  }
  const classification = preflight?.risk?.classification || 'review_required';
  const labels = {
    safe_widening: {
      tone: 'ok',
      title: 'Allowed',
      message: 'Preflight classified this as safe widening. Approval is still required before DB apply.',
    },
    blocked_narrowing: {
      tone: 'blocked',
      title: 'Blocked',
      message: 'Preflight blocked this narrowing request. DB apply must remain disabled.',
    },
    no_op: {
      tone: 'info',
      title: 'Already current',
      message: 'The DB definition already matches the requested change.',
    },
    narrowing_requires_override: {
      tone: 'warning',
      title: 'Review required',
      message: 'Narrowing may fit current data, but still requires explicit review before apply.',
    },
    type_change_requires_review: {
      tone: 'warning',
      title: 'Review required',
      message: 'Type changes outside safe widening require explicit review before apply.',
    },
    review_required: {
      tone: 'warning',
      title: 'Review required',
      message: 'Rename or non-standard change requires impact review before apply.',
    },
  };
  return labels[classification] || labels.review_required;
}

function deriveDriftSummary(drift) {
  if (!drift) {
    return {
      tone: 'idle',
      title: 'Not checked',
      message: 'Run Drift Check after DB/ODI sync or whenever read-back confidence is needed.',
      count: null,
    };
  }
  const rows = Array.isArray(drift.drift) ? drift.drift : [];
  const driftCount = Number.isFinite(Number(drift.drift_count)) ? Number(drift.drift_count) : rows.length;
  if (driftCount === 0) {
    return {
      tone: 'ok',
      title: 'No drift',
      message: 'No DB-to-ODI drift reported for this owner.',
      count: 0,
    };
  }
  return {
    tone: 'warning',
    title: 'Drift found',
    message: `${driftCount} DB-to-ODI drift item${driftCount === 1 ? '' : 's'} reported for owner ${drift.owner || 'selected owner'}. Review evidence before treating the workflow as aligned.`,
    count: driftCount,
  };
}

function StatusPill({ tone, children }) {
  const palette = {
    idle: 'bg-slate-100 text-slate-600',
    info: 'bg-cyan-100 text-cyan-700',
    warning: 'bg-amber-100 text-amber-800',
    blocked: 'bg-rose-100 text-rose-700',
    error: 'bg-rose-100 text-rose-700',
    ok: 'bg-emerald-100 text-emerald-700',
  };
  return (
    <span className={`rounded-md px-2 py-1 text-[11px] font-black uppercase ${palette[tone] || palette.idle}`}>
      {children}
    </span>
  );
}

function StatusPanel({ title, message, nextAction, tone }) {
  const palette = {
    idle: 'border-slate-200 bg-white text-slate-700',
    info: 'border-cyan-200 bg-cyan-50 text-cyan-800',
    warning: 'border-amber-200 bg-amber-50 text-amber-800',
    blocked: 'border-rose-200 bg-rose-50 text-rose-800',
    error: 'border-rose-200 bg-rose-50 text-rose-800',
    ok: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  };
  return (
    <div className={`rounded-lg border p-4 ${palette[tone] || palette.idle}`}>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-black uppercase tracking-wide">Current Status</span>
        <StatusPill tone={tone}>{title}</StatusPill>
      </div>
      <p className="text-sm font-semibold leading-5">{message}</p>
      <p className="mt-2 text-xs font-semibold leading-5 opacity-80">{nextAction}</p>
    </div>
  );
}

function Field({ label, children, className = '' }) {
  return (
    <label className={`space-y-1 ${className}`}>
      <span className="text-xs font-bold uppercase text-slate-500">{label}</span>
      {children}
    </label>
  );
}

function SummaryList({ rows }) {
  return (
    <div className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
      {rows.map(([label, value]) => (
        <div key={label} className="grid grid-cols-[120px_minmax(0,1fr)] gap-3 px-3 py-2 text-sm">
          <span className="text-xs font-black uppercase text-slate-400">{label}</span>
          <span className="min-w-0 break-all font-semibold text-slate-700">{value || '-'}</span>
        </div>
      ))}
    </div>
  );
}

function DriftAlert({ drift, owner, driftCount, loading }) {
  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-500">
        Checking DB-to-ODI drift...
      </div>
    );
  }
  if (drift) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-semibold leading-5 text-amber-800">
        <span className="font-black uppercase">Mismatch found:</span> DB is {drift.db_type || '-'}, ODI is {drift.odi_type || '-'} for this column.
      </div>
    );
  }
  if (Number(driftCount) > 0) {
    return (
      <div className="rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-2 text-xs font-semibold leading-5 text-cyan-800">
        {driftCount} drift item{driftCount === 1 ? '' : 's'} exists for owner {owner}; selected column is not one of them.
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold leading-5 text-emerald-800">
      No DB-to-ODI drift reported for owner {owner}.
    </div>
  );
}

function DriftQueue({ items, onSelect }) {
  if (!items.length) {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-800">
        No drift queue items for this owner.
      </div>
    );
  }
  return (
    <div className="max-h-44 space-y-2 overflow-auto pr-1">
      {items.map(item => (
        <button
          key={`${item.owner}.${item.table}.${item.column}`}
          type="button"
          onClick={() => onSelect(item)}
          className="w-full rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-left hover:bg-amber-100"
        >
          <p className="break-all text-xs font-black text-amber-900">{item.label}</p>
          <p className="mt-1 text-[11px] font-semibold text-amber-700">{item.detail}</p>
        </button>
      ))}
    </div>
  );
}

export function SchemaSync() {
  const [form, setForm] = useState({
    owner: 'DWH',
    model_code: 'TGT_DWH',
    table: 'ODS_HR_EMPLOYEES',
    column: 'FIRST_NAME',
    op: 'modify_type',
    new_type: 'VARCHAR2(30)',
    new_name: '',
    mapping_name: 'MAP_HR_EMPLOYEES_TO_ODS',
    scenario_version: '001',
    project: '',
    folder: '',
  });
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [tableOptions, setTableOptions] = useState([]);
  const [columnOptions, setColumnOptions] = useState([]);
  const [metadataLoading, setMetadataLoading] = useState({ tables: true, columns: true, drift: true, findings: true, sourceCompare: true });
  const [metadataError, setMetadataError] = useState('');
  const [mappingCatalog, setMappingCatalog] = useState([]);
  const [mappingDetail, setMappingDetail] = useState(null);
  const [mappingDetailLoading, setMappingDetailLoading] = useState(true);
  const [mappingDetailError, setMappingDetailError] = useState('');
  const [pkParityLoading, setPkParityLoading] = useState(false);
  const [results, setResults] = useState(emptyResults);
  const [steps, setSteps] = useState({
    impact: 'idle',
    preflight: 'idle',
    db: 'idle',
    odi: 'idle',
    scenario: 'idle',
    drift: 'idle',
  });

  const recovery = useMemo(() => deriveSchemaSyncRecovery(steps, results, error), [steps, results, error]);
  const preflightOutcome = useMemo(() => derivePreflightOutcome(results.preflight), [results.preflight]);
  const driftSummary = useMemo(() => deriveDriftSummary(results.drift), [results.drift]);
  const activeStep = useMemo(() => getActiveWorkflowStep(steps, results), [steps, results]);
  const requestSummary = useMemo(() => summarizeRequest(form), [form]);
  const selectedDrift = useMemo(
    () => findColumnDrift(results.drift, form.owner, form.table, form.column),
    [results.drift, form.owner, form.table, form.column]
  );
  const impactGroups = useMemo(() => groupImpactByMapping(results.impact), [results.impact]);
  const driftQueue = useMemo(() => summarizeDriftQueue(results.drift), [results.drift]);
  const mappingContext = useMemo(() => summarizeMappingContext(mappingCatalog, form.mapping_name), [mappingCatalog, form.mapping_name]);
  const mappingDetailSummary = useMemo(() => summarizeMappingDetail(mappingDetail, form.column), [mappingDetail, form.column]);
  const sourceSchema = useMemo(() => deriveSourceSchema(form.table), [form.table]);

  useEffect(() => {
    let cancelled = false;
    getOdiMappings()
      .then(data => {
        if (!cancelled) setMappingCatalog(Array.isArray(data) ? data : []);
      })
      .catch(() => {
        if (!cancelled) setMappingCatalog([]);
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      getTables({ schema: form.owner }),
      getSchemaDrift(form.owner),
      getSchemaFindings(form.owner),
    ]).then(([tablesResult, driftResult, findingsResult]) => {
      if (cancelled) return;
      if (tablesResult.status === 'fulfilled') {
        const options = normalizeTargetTables(tablesResult.value, form.owner);
        setTableOptions(options);
        if (options.length > 0) {
          setMetadataLoading(prev => ({ ...prev, columns: true }));
        }
        setForm(prev => {
          if (options.length > 0 && !options.some(option => option.table === prev.table)) {
            return { ...prev, table: options[0].table };
          }
          return prev;
        });
      } else {
        setMetadataError(tablesResult.reason?.message || 'Table list unavailable.');
      }
      if (driftResult.status === 'fulfilled') {
        setResults(prev => ({ ...prev, drift: driftResult.value }));
        setSteps(prev => ({ ...prev, drift: 'done' }));
      } else {
        setMetadataError(driftResult.reason?.message || 'Drift check unavailable.');
      }
      if (findingsResult.status === 'fulfilled') {
        setResults(prev => ({ ...prev, findings: findingsResult.value }));
      } else {
        setMetadataError(findingsResult.reason?.message || 'Schema findings unavailable.');
      }
    }).finally(() => {
      if (!cancelled) setMetadataLoading(prev => ({ ...prev, tables: false, drift: false, findings: false }));
    });
    return () => { cancelled = true; };
  }, [form.owner]);

  useEffect(() => {
    if (!form.owner || !sourceSchema) return undefined;
    let cancelled = false;
    getSourceTargetFindings(sourceSchema, form.owner)
      .then(data => {
        if (!cancelled) setResults(prev => ({ ...prev, sourceTargetFindings: data }));
      })
      .catch(err => {
        if (!cancelled) {
          setResults(prev => ({ ...prev, sourceTargetFindings: null }));
          setMetadataError(err.message || 'Source compare unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setMetadataLoading(prev => ({ ...prev, sourceCompare: false }));
      });
    return () => { cancelled = true; };
  }, [form.owner, sourceSchema]);

  useEffect(() => {
    if (!form.owner || !form.table) return undefined;
    let cancelled = false;
    getSchema(`${form.owner}.${form.table}`)
      .then(data => {
        if (cancelled) return;
        const options = formatColumnOptions(data);
        setColumnOptions(options);
        setForm(prev => {
          if (options.length > 0 && !options.some(option => option.name === prev.column)) {
            return { ...prev, column: options[0].name, new_type: options[0].dataType || prev.new_type };
          }
          return prev;
        });
      })
      .catch(err => {
        if (!cancelled) {
          setColumnOptions([]);
          setMetadataError(err.message || 'Column list unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setMetadataLoading(prev => ({ ...prev, columns: false }));
      });
    return () => { cancelled = true; };
  }, [form.owner, form.table]);

  useEffect(() => {
    if (!form.mapping_name) return undefined;
    let cancelled = false;
    getOdiMappingDetail(form.mapping_name)
      .then(data => {
        if (!cancelled) setMappingDetail(data || null);
      })
      .catch(err => {
        if (!cancelled) {
          setMappingDetail(null);
          setMappingDetailError(err.message || 'Mapping detail unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setMappingDetailLoading(false);
      });
    return () => { cancelled = true; };
  }, [form.mapping_name]);

  const set = (key, value) => {
    const nextValue = key === 'op' ? value : value.toUpperCase();
    setForm(prev => ({ ...prev, [key]: nextValue }));
    if (key === 'owner') {
      setMetadataLoading(prev => ({ ...prev, tables: true, columns: true, drift: true, findings: true, sourceCompare: true }));
      setMetadataError('');
    }
    if (key === 'table') {
      setMetadataLoading(prev => ({ ...prev, columns: true, sourceCompare: true }));
      setMetadataError('');
    }
    if (key === 'mapping_name') {
      setMappingDetailLoading(true);
      setMappingDetailError('');
    }
    setConfirmed(false);
    setResults(prev => ({
      ...emptyResults,
      drift: prev.drift,
      findings: prev.findings,
      sourceTargetFindings: prev.sourceTargetFindings,
    }));
    setSteps(prev => ({
      impact: 'idle',
      preflight: 'idle',
      db: 'idle',
      odi: 'idle',
      scenario: 'idle',
      drift: prev.drift === 'done' ? 'done' : 'idle',
    }));
    setError('');
  };

  const isRenameTable = form.op === 'rename_table';
  const isModifyType = form.op === 'modify_type';
  const objectRef = useMemo(() => {
    const base = `${form.owner}.${form.table}`;
    return isRenameTable || !form.column ? base : `${base}.${form.column}`;
  }, [form.owner, form.table, form.column, isRenameTable]);

  const basePayload = () => ({
    owner: form.owner,
    table: form.table,
    column: isRenameTable ? undefined : form.column,
    op: form.op,
    new_type: isModifyType ? form.new_type : undefined,
    new_name: isModifyType ? undefined : form.new_name,
  });

  const odiPayload = () => ({
    approved: true,
    model_code: form.model_code,
    table: form.table,
    column: isRenameTable ? undefined : form.column,
    op: form.op,
    new_type: isModifyType ? form.new_type : undefined,
    new_name: isModifyType ? undefined : form.new_name,
  });

  const scenarioPayload = () => ({
    approved: true,
    mapping_name: form.mapping_name,
    scenario_version: form.scenario_version,
    project: form.project || undefined,
    folder: form.folder || undefined,
  });

  const runStep = async (name, fn) => {
    setError('');
    setBusy(name);
    setSteps(prev => ({ ...prev, [name]: 'running' }));
    try {
      const value = await fn();
      setResults(prev => ({ ...prev, [name]: value }));
      if (name === 'odi' && (
        value?.applied === false
        || (typeof value?.status === 'string' && value.status.toUpperCase().endsWith('_FAILED'))
      )) {
        setError(typeof value?.reason === 'string' && value.reason.trim()
          ? value.reason
          : 'ODI metadata sync failed.');
        setSteps(prev => ({ ...prev, [name]: 'error' }));
        return value;
      }
      setSteps(prev => ({ ...prev, [name]: 'done' }));
      return value;
    } catch (err) {
      setError(err.message || String(err));
      setSteps(prev => ({ ...prev, [name]: 'error' }));
      throw err;
    } finally {
      setBusy('');
    }
  };

  const actions = {
    impact: () => runStep('impact', () => getSchemaImpact(objectRef)),
    preflight: () => runStep('preflight', () => preflightSchemaChange(basePayload())),
    db: () => runStep('db', () => applySchemaDbChange({ ...basePayload(), approved: true })),
    odi: () => runStep('odi', () => syncOdiSchemaChange(odiPayload())),
    scenario: () => runStep('scenario', () => reconcileSchemaScenario(scenarioPayload())),
    drift: () => runStep('drift', () => getSchemaDrift(form.owner)),
  };

  const runImpactWithMappingSuggestion = async () => {
    const impact = await actions.impact();
    const suggested = getSuggestedMappingName(impact);
    if (suggested) {
      setForm(prev => ({ ...prev, mapping_name: suggested }));
    }
    return impact;
  };

  const selectDriftItem = (item) => {
    setForm(prev => ({
      ...prev,
      owner: item.owner,
      table: item.table,
      column: item.column,
      op: 'modify_type',
      new_type: prev.new_type,
    }));
    setConfirmed(false);
    setResults(prev => ({
      ...emptyResults,
      drift: prev.drift,
      findings: prev.findings,
      sourceTargetFindings: prev.sourceTargetFindings,
    }));
    setSteps(prev => ({
      impact: 'idle',
      preflight: 'idle',
      db: 'idle',
      odi: 'idle',
      scenario: 'idle',
      drift: prev.drift === 'done' ? 'done' : 'idle',
    }));
    setError('');
  };

  const preflightAllowed = steps.preflight === 'done' && results.preflight?.allowed_to_apply === true;
  const canRun = {
    impact: !busy,
    preflight: !busy,
    db: confirmed && !busy && preflightAllowed && steps.db !== 'done',
    odi: confirmed && !busy && steps.db === 'done' && steps.odi !== 'done',
    scenario: confirmed && !busy && steps.odi === 'done' && steps.scenario !== 'done',
    drift: !busy,
  };

  const runReadOnlyChecks = async () => {
    await runImpactWithMappingSuggestion();
    const preflight = await actions.preflight();
    if (preflight?.allowed_to_apply === false) {
      setError('Preflight blocked the DB apply step.');
    }
  };

  const runPkParity = async () => {
    setError('');
    setPkParityLoading(true);
    try {
      const report = await getSchemaPkParity(sourceSchema, form.owner);
      setResults(prev => ({ ...prev, pkParity: report }));
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setPkParityLoading(false);
    }
  };

  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-cyan-200 bg-white p-5 shadow-sm">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-black text-slate-900">Schema Sync</h2>
              <StatusPill tone={recovery.tone}>{recovery.title}</StatusPill>
            </div>
            <p className="max-w-3xl text-sm font-semibold leading-6 text-slate-600">
              Guided DB-to-ODI alignment. Start with read-only checks, then apply write steps only after preflight and approval.
            </p>
          </div>
          <button
            className={`${BUTTON} border border-cyan-200 bg-cyan-50 text-cyan-700 hover:bg-cyan-100`}
            disabled={!!busy}
            onClick={runReadOnlyChecks}
          >
            <i className="fa-solid fa-shield-halved" />
            Run Read-Only Checks
          </button>
        </div>
      </section>

      <div className="grid gap-5 xl:grid-cols-[380px_minmax(0,1fr)_320px]">
        <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-black text-slate-800">Change Request</h3>
              <p className="text-xs font-semibold uppercase text-slate-400">Target-side controlled path</p>
            </div>
            <i className="fa-solid fa-pen-to-square rounded-lg bg-cyan-50 p-2 text-cyan-600" />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Owner">
              <input className={INPUT} value={form.owner} onChange={e => set('owner', e.target.value)} />
            </Field>
            <Field label="ODI Model">
              <input className={INPUT} value={form.model_code} onChange={e => set('model_code', e.target.value)} />
            </Field>
            <Field label="Table" className="col-span-2">
              <select className={INPUT} value={form.table} onChange={e => set('table', e.target.value)} disabled={metadataLoading.tables}>
                {tableOptions.length === 0 && <option value={form.table}>{form.table}</option>}
                {tableOptions.map(option => (
                  <option key={option.fullName} value={option.table}>{option.fullName}</option>
                ))}
              </select>
            </Field>
            <Field label="Operation" className="col-span-2">
              <select className={INPUT} value={form.op} onChange={e => set('op', e.target.value)}>
                {Object.entries(OPERATION_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </Field>
            {!isRenameTable && (
              <Field label="Column" className="col-span-2">
                <select className={INPUT} value={form.column} onChange={e => {
                  const selected = columnOptions.find(option => option.name === e.target.value);
                  set('column', e.target.value);
                  if (selected?.dataType) set('new_type', selected.dataType);
                }} disabled={metadataLoading.columns}>
                  {columnOptions.length === 0 && <option value={form.column}>{form.column}</option>}
                  {columnOptions.map(option => (
                    <option key={option.name} value={option.name}>{option.label}</option>
                  ))}
                </select>
              </Field>
            )}
            {isModifyType ? (
              <Field label="New Type" className="col-span-2">
                <input className={INPUT} value={form.new_type} onChange={e => set('new_type', e.target.value)} />
              </Field>
            ) : (
              <Field label="New Name" className="col-span-2">
                <input className={INPUT} value={form.new_name} onChange={e => set('new_name', e.target.value)} />
              </Field>
            )}
            <Field label="Mapping" className="col-span-2">
              <input className={INPUT} value={form.mapping_name} onChange={e => set('mapping_name', e.target.value)} />
            </Field>
            <Field label="Scenario Version">
              <input className={INPUT} value={form.scenario_version} onChange={e => set('scenario_version', e.target.value)} />
            </Field>
            <Field label="Project">
              <input className={INPUT} value={form.project} onChange={e => set('project', e.target.value)} placeholder="Optional" />
            </Field>
            <Field label="Folder" className="col-span-2">
              <input className={INPUT} value={form.folder} onChange={e => set('folder', e.target.value)} placeholder="Optional" />
            </Field>
          </div>

          <label className="mt-4 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm font-semibold text-amber-800">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={e => setConfirmed(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-amber-300"
            />
            <span>I approve the write steps after reviewing impact and preflight evidence.</span>
          </label>
          <div className="mt-3 space-y-2">
            <DriftAlert
              drift={selectedDrift}
              owner={form.owner}
              driftCount={driftSummary.count}
              loading={metadataLoading.drift}
            />
            {metadataError && (
              <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700">
                {metadataError}
              </div>
            )}
          </div>
        </section>

        <section className="space-y-4">
          <StatusPanel {...recovery} />
          {error && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm font-semibold text-rose-700">
              {error}
            </div>
          )}
          <div className="space-y-3">
            {WORKFLOW_STEPS.map(step => (
              <WorkflowStep
                key={step.id}
                step={step}
                state={steps[step.id]}
                active={activeStep === step.id}
                disabled={!canRun[step.id]}
                onRun={step.id === 'impact' ? runImpactWithMappingSuggestion : actions[step.id]}
              />
            ))}
          </div>
        </section>

        <aside className="space-y-4">
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-black text-slate-800">Request Summary</h3>
              <i className="fa-solid fa-circle-info text-slate-400" />
            </div>
            <SummaryList rows={requestSummary} />
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-black text-slate-800">Drift Queue</h3>
              <i className="fa-solid fa-triangle-exclamation text-amber-400" />
            </div>
            <DriftQueue items={driftQueue} onSelect={selectDriftItem} />
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-black text-slate-800">Schema Findings</h3>
              <i className="fa-solid fa-code-compare text-amber-400" />
            </div>
            <SchemaFindingsPanel report={results.findings} loading={metadataLoading.findings} owner={form.owner} />
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-black text-slate-800">Source Compare</h3>
              <i className="fa-solid fa-table-columns text-cyan-500" />
            </div>
            <SourceTargetFindingsPanel report={results.sourceTargetFindings} loading={metadataLoading.sourceCompare} />
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-black text-slate-800">PK Parity</h3>
              <i className="fa-solid fa-key text-cyan-500" />
            </div>
            <PkParityPanel report={results.pkParity} loading={pkParityLoading} onRun={runPkParity} />
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-black text-slate-800">Impact Drilldown</h3>
              <i className="fa-solid fa-diagram-project text-slate-400" />
            </div>
            <ImpactDrilldown
              groups={impactGroups}
              selectedMappingName={form.mapping_name}
              onSelectMapping={mappingName => {
                setForm(prev => ({ ...prev, mapping_name: mappingName }));
                setConfirmed(false);
              }}
            />
          </div>
          <MappingDetailPanel
            context={mappingContext}
            detailSummary={mappingDetailSummary}
            loading={mappingDetailLoading}
            error={mappingDetailError}
          />
          <EvidencePreview preflightOutcome={preflightOutcome} driftSummary={driftSummary} />
        </aside>
      </div>

      <details className="rounded-lg border border-slate-200 bg-white shadow-sm">
        <summary className="cursor-pointer px-4 py-3 text-sm font-black text-slate-700">
          Advanced Evidence
        </summary>
        <div className="grid gap-4 border-t border-slate-100 p-4 xl:grid-cols-2">
          <JsonBlock title="Impact" value={results.impact} />
          <JsonBlock title="Preflight" value={results.preflight} />
          <JsonBlock title="DB Apply" value={results.db} />
          <JsonBlock title="ODI Sync" value={results.odi} />
          <JsonBlock title="Scenario Reconcile" value={results.scenario} />
          <JsonBlock title="Drift Check" value={results.drift} />
        </div>
      </details>
    </div>
  );
}
