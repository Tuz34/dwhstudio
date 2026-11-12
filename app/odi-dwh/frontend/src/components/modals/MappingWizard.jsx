import { useState } from 'react';
import { Modal } from '../ui/Modal';
import { generateMapping, preflightMappingTarget } from '../../services/api';
import { useDispatch } from '../../store/AppContext';
import clsx from 'clsx';
import {
  LOAD_TYPES,
  buildMappingRequest,
  buildScd2Handoff,
  getFriendlyFailure,
  getLoadOptionError,
  getMappingOutcome,
  mappingName,
  neutralSkipDetail,
  prefillLoadOptions,
  resolveWizardSourceModel,
  selectWizardFailureMessage,
  validateLoadOptions,
  wizardHeadlineDetail,
  wizardStepTone,
} from './mappingWizardRequests';

const STEPS = ['Source', 'Target', 'Options', 'Confirm'];
const TERMINAL_FAILURES = new Set(['failed', 'error', 'dq_failed', 'run_failed']);

const STEP_LABELS = {
  init: 'Start request',
  parse_prompt: 'Read request',
  validate: 'Validate setup',
  preview: 'Confirm run',
  create_table: 'Prepare target table',
  register_datastores: 'Register ODI tables',
  create_mapping: 'Build ODI mapping',
  create_skey: 'Create surrogate key',
  skip_skey: 'Skip surrogate key',
  post_steps: 'Save metadata',
  lineage: 'Save lineage',
  tests: 'Create quality checks',
  snapshot: 'Take snapshot',
  finalize: 'Finish run',
};

const STATUS_LABELS = {
  SUCCESS: 'Done',
  CREATED: 'Created',
  RECONCILED: 'Updated',
  ALREADY_IN_SYNC: 'Already ready',
  AUTO_APPROVED: 'Approved',
  SKIPPED: 'Skipped',
  WAITING: 'Waiting',
  WARNING: 'Needs review',
  FAILED: 'Stopped',
  ERROR: 'Stopped',
};

const stripHelpLinks = (text = '') => String(text).replace(/\s*Help:\s*https?:\/\/\S+/gi, '').trim();

const getStepLabel = (step) => STEP_LABELS[step] || String(step || 'Step').replace(/_/g, ' ');

const getStatusLabel = (status) => STATUS_LABELS[String(status || '').toUpperCase()] || String(status || 'State');

const getFriendlyStepDetail = (entry = {}, result = {}) => {
  const step = entry.step;
  const status = String(entry.status || '').toUpperCase();
  const detail = stripHelpLinks(entry.detail || '');

  if (status === 'SKIPPED') return neutralSkipDetail(entry.detail);

  if (status === 'FAILED' || status === 'ERROR') {
    const selected = selectWizardFailureMessage(result, detail || result.error);
    if (result?.error_code === 'audit_preservation_rejected') return selected;
    return getFriendlyFailure(detail || result.error);
  }

  if (step === 'init') return 'The mapping request started.';
  if (step === 'parse_prompt') return 'Using the values entered in this form.';
  if (step === 'validate') return 'The source and target table settings are valid.';
  if (step === 'preview') return 'The run was approved to continue.';
  if (step === 'create_table') {
    if (status === 'CREATED') return 'The target table was created and database access grants were processed.';
    if (status === 'RECONCILED') return 'The target table already existed, so missing columns were added.';
    if (status === 'ALREADY_IN_SYNC') return 'The target table already existed and matched the expected structure.';
    return 'The target table was checked.';
  }
  if (step === 'register_datastores') return 'ODI source and target table definitions were prepared.';
  if (step === 'create_mapping') return 'The ODI mapping was created.';
  if (step === 'create_skey') return 'Surrogate key objects were prepared.';
  if (step === 'skip_skey') return 'No surrogate key was requested for this mapping.';
  if (step === 'lineage') return 'Lineage metadata was saved.';
  if (step === 'tests') return 'Quality checks were generated.';
  if (step === 'snapshot') return 'A mapping snapshot was recorded.';
  if (step === 'finalize') {
    return TERMINAL_FAILURES.has(String(result?.overall_status || '').toLowerCase())
      ? 'The run stopped before all steps were completed.'
      : 'The run completed.';
  }

  return detail || 'Step completed.';
};

const emptyForm = {
  src_model: 'ORACLE_SOURCE',
  src_table: '',
  src_schema: '',
  tgt_model: 'DWH',
  tgt_owner: 'DWH',
  tgt_table: '',
  needs_skey: false,
  skey_natural_key: '',
  description: '',
  mapping_name: '',
  drop_target_if_exists: false,
  load_type: '',
  key_columns: '',
};

export function MappingWizard({ open, onClose, initialData = {} }) {
  const dispatch = useDispatch();
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const [form, setForm] = useState({
    ...emptyForm,
    src_model: initialData.src_model || emptyForm.src_model,
    src_table: initialData.src_table || emptyForm.src_table,
    src_schema: initialData.src_schema || emptyForm.src_schema,
    tgt_model: initialData.tgt_model || emptyForm.tgt_model,
    tgt_owner: initialData.tgt_owner || emptyForm.tgt_owner,
    tgt_table: initialData.tgt_table || emptyForm.tgt_table,
    needs_skey: initialData.needs_skey ?? emptyForm.needs_skey,
    skey_natural_key: initialData.skey_natural_key || emptyForm.skey_natural_key,
    description: initialData.description || emptyForm.description,
    mapping_name: initialData.mapping_name || emptyForm.mapping_name,
    src_model_from_prefill: typeof initialData.src_model === 'string' && initialData.src_model.trim() !== '',
    ...prefillLoadOptions(initialData),
  });

  const set = (key, val) => setForm(prev => ({ ...prev, [key]: val }));

  const isStepValid = () => {
    if (step === 0) return form.src_table.trim().length > 0;
    if (step === 1) return form.tgt_table.trim().length > 0;
    if (step === 2) return getLoadOptionError(form) === null;
    return true;
  };

  const handleNext = () => {
    if (step === 2 && form.load_type === 'type2') {
      const handoff = buildScd2Handoff(form);
      handleClose();
      dispatch(handoff);
      return;
    }
    setStep(previous => previous + 1);
  };

  const handleGenerate = async () => {
    setLoading(true);
    setError(null);
    try {
      const request = buildMappingRequest(form);
      const payload = request.params;

      const preflight = await preflightMappingTarget({ params: payload });
      if (payload.drop_target_if_exists && preflight.exists && preflight.has_rows) {
        const userConfirmedRebuild = window.confirm(
          `${preflight.target} contains data.\n\n` +
          'This will delete and recreate the target table and replace an existing mapping.\n\n' +
          'Choose OK only if this rebuild is intentional.'
        );
        if (!userConfirmedRebuild) {
          setLoading(false);
          return;
        }
      }

      const data = await generateMapping({ ...request, approved: true });
      const hasGraphResult = Array.isArray(data.log) || data.overall_status || data.current_step;
      if (data.error && !hasGraphResult) throw new Error(data.error);

      setResult(data);
      const failed = TERMINAL_FAILURES.has(String(data.overall_status || '').toLowerCase());
      dispatch({
        type: 'SHOW_TOAST',
        payload: {
          message: failed ? 'Mapping stopped with errors.' : 'Mapping created.',
          type: failed ? 'error' : 'success',
        }
      });
      setStep(4);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setStep(0);
    setResult(null);
    setError(null);
    setForm(emptyForm);
    onClose();
  };

  const inputCls = 'w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400';
  const resultFailed = TERMINAL_FAILURES.has(String(result?.overall_status || '').toLowerCase()) || Boolean(result?.error);
  const resultSteps = Array.isArray(result?.log) ? result.log : [];
  const primaryError = wizardHeadlineDetail(result, resultSteps);
  const friendlyError = selectWizardFailureMessage(result, primaryError);
  const outcome = getMappingOutcome(result);
  const selectedLoadType = LOAD_TYPES.find(({ value }) => value === form.load_type);
  const requestedSourceModel = resolveWizardSourceModel(form);

  return (
    <Modal open={open} onClose={handleClose} title="Mapping Wizard" size="lg">
      {step < 4 && (
        <div className="flex items-center gap-0 mb-6">
          {STEPS.map((s, i) => (
            <div key={i} className="flex items-center flex-1">
              <div
                className={clsx(
                  'w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold border-2 shrink-0 transition-all',
                  i < step ? 'bg-indigo-600 border-indigo-600 text-white' :
                    i === step ? 'bg-white border-indigo-600 text-indigo-600' :
                      'bg-white border-slate-200 text-slate-400'
                )}
              >
                {i < step ? <i className="fa-solid fa-check text-[10px]" /> : i + 1}
              </div>
              <div className="text-xs ml-1.5 font-medium hidden sm:block" style={{ color: i <= step ? '#4f46e5' : '#94a3b8' }}>
                {s}
              </div>
              {i < STEPS.length - 1 && (
                <div className={clsx('h-px flex-1 mx-2', i < step ? 'bg-indigo-400' : 'bg-slate-200')} />
              )}
            </div>
          ))}
        </div>
      )}

      {step === 0 && (
        <div className="space-y-4">
          <p className="text-sm text-slate-500">Select the Oracle source table.</p>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Source Model</label>
              <input
                value={form.src_model}
                onChange={e => setForm(prev => ({
                  ...prev, src_model: e.target.value, src_model_touched: true,
                }))}
                className={inputCls}
              />
            </div>
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Schema (optional)</label>
              <input value={form.src_schema} onChange={e => setForm(prev => ({ ...prev, src_schema: e.target.value, src_schema_typed: true }))} placeholder="HR, SH..." className={inputCls} />
            </div>
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Source Table *</label>
            <input value={form.src_table} onChange={e => set('src_table', e.target.value)} placeholder="EMPLOYEES, SALES, CUSTOMERS..." className={inputCls} autoFocus />
          </div>
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <p className="text-sm text-slate-500">Define the Oracle DWH target table.</p>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Target Owner</label>
              <input value={form.tgt_owner} onChange={e => set('tgt_owner', e.target.value)} className={inputCls} />
            </div>
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Target Model</label>
              <input value={form.tgt_model} onChange={e => set('tgt_model', e.target.value)} className={inputCls} />
            </div>
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Target Table *</label>
            <input
              value={form.tgt_table}
              onChange={e => set('tgt_table', e.target.value)}
              placeholder={`ODS_${(form.src_schema || 'SCHEMA').toUpperCase()}_${(form.src_table || 'TABLE').toUpperCase()}`}
              className={inputCls}
              autoFocus
            />
            <p className="text-xs text-slate-400 mt-1">Rule: ODS_&lt;SCHEMA&gt;_&#123;TABLE&#125; (e.g. ODS_SALES_CUSTOMER)</p>
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">MAPPING NAME (optional)</label>
            <input
              value={form.mapping_name}
              onChange={e => set('mapping_name', e.target.value)}
              placeholder={`MAP_${(form.src_table || 'TABLE').toUpperCase()}_TO_ODS`}
              className={inputCls}
            />
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-5">
          <p className="text-sm text-slate-500">Advanced options and metadata.</p>
          <div>
            <label htmlFor="mapping-load-type" className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Load type *</label>
            <select
              id="mapping-load-type"
              value={form.load_type}
              onChange={event => set('load_type', event.target.value)}
              className={inputCls}
            >
              <option value="">Choose a load type</option>
              {LOAD_TYPES.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
            </select>
            {form.load_type !== 'truncate_load' && form.load_type && (
              <div className="mt-4">
                <label htmlFor="mapping-key-columns" className="text-xs font-semibold text-slate-500 uppercase mb-1 block">
                  Key columns {form.load_type === 'type2' ? '(optional)' : '*'}
                </label>
                <input
                  id="mapping-key-columns"
                  value={form.key_columns}
                  onChange={event => set('key_columns', event.target.value)}
                  placeholder="ENTITY_ID, SOURCE_ID"
                  className={inputCls}
                />
                <p className="text-xs text-slate-400 mt-1">Separate column names with commas.</p>
              </div>
            )}
            {getLoadOptionError(form) && (
              <p className="text-xs text-rose-600 mt-2">{getLoadOptionError(form)}</p>
            )}
          </div>
          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <label className="flex items-center gap-3 cursor-pointer select-none">
              <div
                onClick={() => set('needs_skey', !form.needs_skey)}
                className={clsx(
                  'w-11 h-6 rounded-full relative transition-colors cursor-pointer',
                  form.needs_skey ? 'bg-indigo-600' : 'bg-slate-300'
                )}
              >
                <div
                  className={clsx(
                    'absolute top-1 w-4 h-4 bg-white rounded-full shadow transition-transform',
                    form.needs_skey ? 'translate-x-6' : 'translate-x-1'
                  )}
                />
              </div>
              <span className="font-medium text-slate-700 text-sm">Create Surrogate Key (SKEY)</span>
            </label>
            {form.needs_skey && (
              <div className="mt-4">
                <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Natural Key Column</label>
                <input value={form.skey_natural_key} onChange={e => set('skey_natural_key', e.target.value)} placeholder="EMPLOYEE_ID, CUST_ID..." className={inputCls} />
              </div>
            )}

            <label className="flex items-center gap-3 mt-4 pt-4 border-t border-slate-200 cursor-pointer select-none">
              <div
                onClick={() => set('drop_target_if_exists', !form.drop_target_if_exists)}
                className={clsx(
                  'w-11 h-6 rounded-full relative transition-colors cursor-pointer shrink-0',
                  form.drop_target_if_exists ? 'bg-rose-500' : 'bg-slate-300'
                )}
              >
                <div
                  className={clsx(
                    'absolute top-1 w-4 h-4 bg-white rounded-full shadow transition-transform',
                    form.drop_target_if_exists ? 'translate-x-6' : 'translate-x-1'
                  )}
                />
              </div>
              <div className="flex flex-col">
                <span className="font-medium text-rose-700 text-sm">Drop target table if it exists (delete and recreate)</span>
                <span className="text-[10px] text-slate-500 mt-0.5">
                  Default behavior reuses the table. If metadata already matches, this step is marked ready;
                  if columns are missing, the wizard adds them with ALTER. Turning this on also replaces an existing mapping.
                </span>
              </div>
            </label>
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Description</label>
            <textarea value={form.description} onChange={e => set('description', e.target.value)} rows={3} className={inputCls + ' resize-none'} placeholder="Mapping description..." />
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="space-y-4">
          <div className="bg-indigo-50 rounded-xl border border-indigo-200 p-5 space-y-3">
            <h4 className="font-bold text-indigo-800 flex items-center gap-2">
              <i className="fa-solid fa-wand-magic-sparkles" />
              Mapping Summary
            </h4>
            {[
              ['Mapping Name', mappingName(form)],
              ['Source', `${requestedSourceModel}.${form.src_schema ? form.src_schema + '.' : ''}${form.src_table}`],
              ['Target', `${form.tgt_owner}.${form.tgt_table}`],
              ['Load type', selectedLoadType?.label || '-'],
              ['Key columns', validateLoadOptions(form).keyColumns.join(', ') || '-'],
              ['SKEY', form.needs_skey ? `Natural Key: ${form.skey_natural_key}` : '-'],
            ].map(([label, val]) => (
              <div key={label} className="flex gap-3 text-sm">
                <span className="text-indigo-600 font-medium w-28 shrink-0">{label}</span>
                <span className="text-slate-700 font-mono">{val}</span>
              </div>
            ))}
          </div>
          {error && (
            <div className="bg-rose-50 border border-rose-200 rounded-xl p-4 text-rose-600 text-sm">
              <i className="fa-solid fa-circle-exclamation mr-2" />
              {error}
            </div>
          )}
        </div>
      )}

      {step === 4 && (
        <div className="py-8 space-y-5">
          <div className="text-center space-y-4">
            <div className={clsx(
              'w-20 h-20 rounded-full flex items-center justify-center mx-auto',
              resultFailed ? 'bg-rose-100' : 'bg-emerald-100'
            )}>
              <i className={clsx(
                'fa-solid text-3xl',
                resultFailed ? 'fa-triangle-exclamation text-rose-600' : 'fa-check text-emerald-600'
              )} />
            </div>
            <p className="text-xl font-bold text-slate-800">
              {resultFailed ? 'Mapping Stopped' : 'Mapping Created!'}
            </p>
            {result?.batch_id && <p className="text-sm text-slate-500 font-mono">Batch: {result.batch_id}</p>}
            {result?.overall_status && (
              <p className={clsx(
                'text-xs font-bold uppercase',
                resultFailed ? 'text-rose-600' : 'text-emerald-600'
              )}>
                Status: {result.overall_status}
              </p>
            )}
            {result?.message && <p className="text-sm text-slate-600">{result.message}</p>}
            {(outcome.integrationType || outcome.ikm || outcome.key) && (
              <div className="text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 text-left space-y-1">
                {outcome.integrationType && <p>Integration type: {outcome.integrationType}</p>}
                {outcome.ikm && <p>IKM: {outcome.ikm}</p>}
                {outcome.key && <p>Key outcome: {outcome.key}</p>}
              </div>
            )}
            {friendlyError && (
              <div className="text-sm text-rose-700 bg-rose-50 border border-rose-100 rounded-lg px-3 py-2 text-left">
                <p>{friendlyError}</p>
                {primaryError && (
                  <details className="mt-2 text-xs text-rose-500">
                    <summary className="cursor-pointer font-semibold">Technical details</summary>
                    <p className="font-mono break-words mt-1">{stripHelpLinks(primaryError)}</p>
                  </details>
                )}
              </div>
            )}
          </div>

          {resultSteps.length > 0 && (
            <div className="text-left border border-slate-200 rounded-xl overflow-hidden">
              <div className="px-4 py-3 bg-slate-50 border-b border-slate-200">
                <p className="text-xs font-bold uppercase text-slate-500">Run Progress</p>
              </div>
              <div className="divide-y divide-slate-100 max-h-64 overflow-auto">
                {resultSteps.map((entry, idx) => {
                  const status = String(entry.status || '').toUpperCase();
                  const tone = wizardStepTone(status);
                  return (
                    <div key={`${entry.step || 'step'}-${idx}`} className="px-4 py-3 grid grid-cols-[minmax(0,1fr)_auto] gap-3 text-sm">
                      <div className="min-w-0">
                        <p className="font-semibold text-slate-700 capitalize">{getStepLabel(entry.step)}</p>
                        <p className="text-xs text-slate-500 break-words mt-0.5">{getFriendlyStepDetail(entry, result)}</p>
                        {tone === 'failure' && entry.detail && (
                          <details className="mt-1 text-[11px] text-slate-400">
                            <summary className="cursor-pointer font-semibold">Technical details</summary>
                            <p className="font-mono break-words mt-1">{stripHelpLinks(entry.detail)}</p>
                          </details>
                        )}
                      </div>
                      <span className={clsx(
                        'self-start text-[10px] font-black uppercase px-2 py-1 rounded-full',
                        tone === 'failure' ? 'bg-rose-100 text-rose-700' :
                          tone === 'warning' ? 'bg-amber-100 text-amber-700' :
                            tone === 'success' ? 'bg-emerald-100 text-emerald-700' :
                              'bg-slate-100 text-slate-600'
                      )}>
                        {getStatusLabel(status)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="flex justify-between mt-6 pt-4 border-t border-slate-200">
        <button
          onClick={step === 4 ? handleClose : () => setStep(p => p - 1)}
          disabled={step === 0}
          className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-0 transition-all"
        >
          {step === 4 ? 'Close' : 'Back'}
        </button>
        {step < 3 && (
          <button
            onClick={handleNext}
            disabled={!isStepValid()}
            className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
          >
            Next
          </button>
        )}
        {step === 3 && (
          <button
            onClick={handleGenerate}
            disabled={loading}
            className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white px-6 py-2 rounded-lg text-sm font-bold flex items-center gap-2 transition-colors"
          >
            <i className={`fa-solid ${loading ? 'fa-spinner fa-spin' : 'fa-play'}`} />
            {loading ? 'Creating...' : 'Create'}
          </button>
        )}
        {step === 4 && (
          <button onClick={handleClose} className="bg-slate-800 text-white px-5 py-2 rounded-lg text-sm font-medium">
            OK
          </button>
        )}
      </div>
    </Modal>
  );
}
