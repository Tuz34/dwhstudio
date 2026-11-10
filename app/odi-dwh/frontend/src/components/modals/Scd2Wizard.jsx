import { useCallback, useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import { Modal } from '../ui/Modal';
import { shouldDismissModal } from '../ui/modalFocus';
import { useDispatch } from '../../store/AppContext';
import { generateScd2, getScd2Postflight, planScd2, runScd2 } from '../../services/api';
import {
  SOURCE_OWNERS,
  TARGET_OWNERS,
  applyPrefill,
  buildGenerateRequest,
  buildPlanRequest,
  buildPostflightParams,
  buildRunRequest,
  canGenerate,
  canRefreshEvidence,
  createWriteGate,
  dialogWriteDecision,
  emptyScd2Form,
  postflightRows,
  projectGenerate,
  projectPlan,
  projectPostflight,
  projectRejected,
  projectRun,
  replaceWriteGate,
  REVIEW_SENTENCE,
  setWriteConfirmation,
  settleWrite,
  stageSucceeded,
  validateScd2Form,
  WRITE_IN_FLIGHT_NOTE,
  writeButtonState,
  writesInFlight,
} from './scd2WizardModel';

const STEPS = ['Setup', 'Plan', 'Generate', 'Run'];

const inputCls = 'w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400';

function visibleError(errors, key, raw) {
  if (!errors[key]) return '';
  if (typeof raw === 'string' && raw.trim() === '') return '';
  return errors[key];
}

function Field({ label, required, error, note, children }) {
  return (
    <label className="block">
      <span className="text-xs font-semibold text-slate-500 uppercase mb-1 block">
        {label}{required ? ' *' : ''}
      </span>
      {children}
      {note ? <span className="mt-1 block text-xs text-amber-700">{note}</span> : null}
      {error ? <span className="mt-1 block text-xs text-rose-600">{error}</span> : null}
    </label>
  );
}

function ErrorBanner({ message }) {
  if (!message) return null;
  return (
    <div className="bg-rose-50 border border-rose-200 rounded-xl p-4 text-rose-600 text-sm">
      <i className="fa-solid fa-circle-exclamation mr-2" />
      {message}
    </div>
  );
}

function ReviewPanel({ view }) {
  const headline = view?.detail || '';
  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
      <p className="font-semibold">{headline ? `Needs review: ${headline}` : 'Needs review'}</p>
      {view?.session_status ? <p>Session: {view.session_status}</p> : null}
      {view?.scenario ? (
        <p className="font-mono">{view.scenario.name} {view.scenario.version} {view.scenario.context}</p>
      ) : null}
      <p className="mt-2">{REVIEW_SENTENCE}</p>
    </div>
  );
}

function OwnerSelect({ value, owners, onChange }) {
  return (
    <select value={value} onChange={event => onChange(event.target.value)} className={inputCls}>
      <option value="">Select owner</option>
      {value && !owners.includes(value) ? <option value={value}>{value}</option> : null}
      {owners.map(owner => <option key={owner} value={owner}>{owner}</option>)}
    </select>
  );
}

function EvidencePanel({ evidence }) {
  if (!evidence) return null;
  const rows = postflightRows(evidence);
  const parity = Array.isArray(evidence.parity) ? evidence.parity : [];
  const tone = evidence.warning
    ? 'border-amber-200 bg-amber-50'
    : evidence.success
      ? 'border-slate-200 bg-slate-50'
      : 'border-rose-200 bg-rose-50';
  return (
    <div className={clsx('rounded-xl border p-4 text-sm', tone)}>
      <p className="text-xs font-bold uppercase text-slate-500">Postflight</p>
      <p className="mt-1 font-medium text-slate-700">Status: {evidence.status || 'unknown'}</p>
      {evidence.statusScope ? <p className="mt-1 text-slate-600">{evidence.statusScope}</p> : null}
      {evidence.warning ? <p className="mt-2 font-medium text-amber-800">{evidence.message}</p> : null}
      {rows.length > 0 && (
        <dl className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {rows.map(row => (
            <div key={row.key} className="flex justify-between gap-3">
              <dt className="text-slate-500">{row.label} <span className="font-mono text-xs">({row.key})</span></dt>
              <dd className="font-mono text-slate-800">{String(row.value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {parity.length > 0 && (
        <dl className="mt-3 space-y-1">
          {parity.map(flag => (
            <div key={flag.key} className="flex justify-between gap-3">
              <dt className="text-slate-500">{flag.label}</dt>
              <dd className="font-mono text-slate-800">{flag.text}</dd>
            </div>
          ))}
        </dl>
      )}
      {!evidence.success && !evidence.warning && evidence.message ? <p className="mt-2 text-rose-700">{evidence.message}</p> : null}
    </div>
  );
}

export function Scd2Wizard({ open, onClose, initialData = {} }) {
  const dispatch = useDispatch();
  const mounted = useRef(true);
  const requestSeq = useRef(0);
  const [step, setStep] = useState(0);
  const [form, setForm] = useState(() => applyPrefill(emptyScd2Form(), initialData));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [planView, setPlanView] = useState(null);
  const [generateView, setGenerateView] = useState(null);
  const [runView, setRunView] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [generateGate, setGenerateGate] = useState(() => createWriteGate({ lockAfterSuccess: true }));
  const [runGate, setRunGate] = useState(() => createWriteGate({ lockAfterSuccess: false }));
  const generateGateRef = useRef(generateGate);
  const runGateRef = useRef(runGate);
  const [closeWarning, setCloseWarning] = useState('');
  const requestCloseRef = useRef(() => {});

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const validation = validateScd2Form(form);
  const set = (key, value) => setForm(prev => {
    const prefillNotes = { ...(prev.prefillNotes || {}) };
    delete prefillNotes[key];
    return { ...prev, [key]: value, prefillNotes };
  });

  function commitGenerateGate(next) {
    generateGateRef.current = next;
    setGenerateGate(next);
  }

  function commitRunGate(next) {
    runGateRef.current = next;
    setRunGate(next);
  }

  function resetWrites(force = false) {
    const nextGenerate = createWriteGate({ lockAfterSuccess: true });
    const nextRun = createWriteGate({ lockAfterSuccess: false });
    commitGenerateGate(force ? nextGenerate : replaceWriteGate(generateGateRef.current, nextGenerate));
    commitRunGate(force ? nextRun : replaceWriteGate(runGateRef.current, nextRun));
  }

  function settleFromView(gate, view) {
    return settleWrite(gate, { ok: view?.success === true, review: view?.needsReview === true });
  }

  function writeBusy() {
    return writesInFlight([generateGateRef.current, runGateRef.current]);
  }

  function beginRequest() {
    requestSeq.current += 1;
    return requestSeq.current;
  }

  function stillCurrent(id) {
    return mounted.current && id === requestSeq.current;
  }

  function toast(message, type) {
    dispatch({ type: 'SHOW_TOAST', payload: { message, type } });
  }

  useEffect(() => {
    requestCloseRef.current = (source) => {
      if (!shouldDismissModal(writeBusy())) return;
      const decision = dialogWriteDecision(generateGateRef.current, runGateRef.current);
      const closing = decision.close({ source });
      if (!closing.close) {
        if (closing.needsConfirm) setCloseWarning(closing.message);
        return;
      }
      setCloseWarning('');
      beginRequest();
      setStep(0);
      setError('');
      setPlanView(null);
      setGenerateView(null);
      setRunView(null);
      setEvidence(null);
      resetWrites(true);
      setForm(emptyScd2Form());
      onClose();
    };
  });

  // Stable so showing the warning does not change Modal's onClose. Modal focuses
  // its first control whenever onClose changes; the confirm button must not take
  // that focus, or a held Enter or Space would confirm the close.
  const handleDismiss = useCallback(() => {
    requestCloseRef.current('dismiss');
  }, []);

  const handleConfirmClose = useCallback(() => {
    requestCloseRef.current('confirm-button');
  }, []);

  async function loadPlan() {
    if (writeBusy()) return;
    const id = beginRequest();
    setLoading(true);
    setError('');
    setPlanView(null);
    try {
      const payload = await planScd2(buildPlanRequest(form));
      if (!stillCurrent(id)) return;
      const view = projectPlan(payload);
      setPlanView(view);
      if (view.success) {
        const decision = dialogWriteDecision(generateGateRef.current, runGateRef.current);
        const next = decision.afterPlan(
          createWriteGate({ lockAfterSuccess: true }),
          createWriteGate({ lockAfterSuccess: false }),
        );
        if (next.reset) {
          commitGenerateGate(next.generateGate);
          commitRunGate(next.runGate);
          setGenerateView(null);
          setRunView(null);
          setEvidence(null);
        }
      } else {
        setError(view.message);
      }
    } catch (err) {
      if (!stillCurrent(id)) return;
      const view = projectRejected(err);
      setPlanView(view);
      setError(view.message);
    } finally {
      if (stillCurrent(id)) setLoading(false);
    }
  }

  async function handleGenerate() {
    const decision = dialogWriteDecision(generateGateRef.current, runGateRef.current);
    const accepted = decision.accept(generateGateRef.current, { ready: canGenerate(planView) });
    if (!accepted.accepted) return;
    commitGenerateGate(accepted.gate);
    const id = beginRequest();
    setLoading(true);
    setError('');
    setGenerateView(null);
    try {
      const payload = await generateScd2(buildGenerateRequest(form));
      if (!stillCurrent(id)) return;
      const view = projectGenerate(payload);
      setGenerateView(view);
      commitGenerateGate(settleFromView(generateGateRef.current, view));
      if (view.needsReview) {
        return;
      }
      if (!view.success) {
        setError(view.message);
        toast('Type 2 generation failed.', 'error');
      } else {
        toast('Type 2 mapping generated.', 'success');
      }
    } catch (err) {
      if (!stillCurrent(id)) return;
      const view = projectRejected(err, { write: true, route: 'generate' });
      setGenerateView(view);
      commitGenerateGate(settleFromView(generateGateRef.current, view));
      if (!view.needsReview) {
        setError(view.message);
        toast('Type 2 generation failed.', 'error');
      }
    } finally {
      if (stillCurrent(id)) setLoading(false);
    }
  }

  async function handleRun() {
    const decision = dialogWriteDecision(generateGateRef.current, runGateRef.current);
    const accepted = decision.accept(runGateRef.current, { ready: stageSucceeded(generateView) });
    if (!accepted.accepted) return;
    commitRunGate(accepted.gate);
    const id = beginRequest();
    setLoading(true);
    setError('');
    setRunView(null);
    setEvidence(null);
    try {
      const payload = await runScd2(buildRunRequest(form, { scenario_version: generateView.scenario_version }));
      if (!stillCurrent(id)) return;
      const view = projectRun(payload);
      setRunView(view);
      setEvidence(view.evidence);
      commitRunGate(settleFromView(runGateRef.current, view));
      if (view.needsReview) {
        return;
      }
      if (!view.success) {
        setError(view.message);
        toast('Type 2 scenario run failed.', 'error');
      } else {
        toast('Type 2 scenario run completed.', 'success');
      }
    } catch (err) {
      if (!stillCurrent(id)) return;
      const view = projectRejected(err, { write: true, route: 'run' });
      setRunView(view);
      setEvidence(view.evidence);
      commitRunGate(settleFromView(runGateRef.current, view));
      if (!view.needsReview) {
        setError(view.message);
        toast('Type 2 scenario run failed.', 'error');
      }
    } finally {
      if (stillCurrent(id)) setLoading(false);
    }
  }

  async function refreshEvidence() {
    if (loading || writeBusy()) return;
    const id = beginRequest();
    setLoading(true);
    setError('');
    try {
      const payload = await getScd2Postflight(buildPostflightParams(form));
      if (!stillCurrent(id)) return;
      const view = projectPostflight(payload);
      setEvidence(view);
      if (view && !view.success && !view.warning) setError(view.message);
    } catch (err) {
      if (!stillCurrent(id)) return;
      const view = projectRejected(err);
      setEvidence(view);
      setError(view.message);
    } finally {
      if (stillCurrent(id)) setLoading(false);
    }
  }

  function confirmGenerate(checked) {
    commitGenerateGate(setWriteConfirmation(generateGateRef.current, checked));
  }

  function confirmRun(checked) {
    commitRunGate(setWriteConfirmation(runGateRef.current, checked));
  }

  const busy = writesInFlight([generateGate, runGate]);
  const decision = dialogWriteDecision(generateGate, runGate);
  const evidenceAllowed = canRefreshEvidence({ loading, gates: [generateGate, runGate] }) && validation.ok;
  const generateButton = writeButtonState(generateGate, {
    ready: canGenerate(planView),
    dialogLocked: decision.locked,
    idleLabel: 'Generate',
    pendingLabel: 'Generating...',
    doneLabel: 'Generated',
  });
  const runButton = writeButtonState(runGate, {
    ready: stageSucceeded(generateView),
    dialogLocked: decision.locked,
    idleLabel: 'Run',
    pendingLabel: 'Running...',
    doneLabel: 'Ran',
    repeatLabel: 'Run again',
  });
  const reviewBanners = decision.banners({ step, generateView, runView });

  const shownEvidence = evidence || runView?.evidence || null;

  return (
    <Modal open={open} onClose={handleDismiss} title="Type 2 (History)" size="lg" closeDisabled={busy}>
      <div className="flex items-center gap-0 mb-6">
        {STEPS.map((label, index) => (
          <div key={label} className="flex items-center flex-1">
            <div className={clsx(
              'w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold border-2 shrink-0 transition-all',
              index < step ? 'bg-indigo-600 border-indigo-600 text-white' :
                index === step ? 'bg-white border-indigo-600 text-indigo-600' :
                  'bg-white border-slate-200 text-slate-400',
            )}>
              {index < step ? <i className="fa-solid fa-check text-[10px]" /> : index + 1}
            </div>
            <div className="text-xs ml-1.5 font-medium hidden sm:block" style={{ color: index <= step ? '#4f46e5' : '#94a3b8' }}>
              {label}
            </div>
            {index < STEPS.length - 1 && (
              <div className={clsx('h-px flex-1 mx-2', index < step ? 'bg-indigo-400' : 'bg-slate-200')} />
            )}
          </div>
        ))}
      </div>

      {busy ? <p className="mb-4 text-sm text-amber-800">{WRITE_IN_FLIGHT_NOTE}</p> : null}
      {reviewBanners.map((view, index) => (
        <div key={`${view.status || 'review'}-${index}`} className="mb-4">
          <ReviewPanel view={view} />
        </div>
      ))}
      {closeWarning ? (
        <div className="mb-4 rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
          <p>{closeWarning}</p>
          <button
            type="button"
            onClick={handleConfirmClose}
            className="mt-3 px-4 py-2 rounded-lg border border-amber-300 text-sm font-medium text-amber-950 hover:bg-amber-100"
          >
            Close dialog
          </button>
        </div>
      ) : null}

      {step === 0 && (
        <div className="space-y-4">
          <p className="text-sm text-slate-500">
            Describe the Type 2 mapping. Column names are entered here and are not inferred from the table name.
          </p>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Source owner" required note={form.prefillNotes?.source_owner} error={visibleError(validation.errors, 'source_owner', form.source_owner)}>
              <OwnerSelect value={form.source_owner} owners={SOURCE_OWNERS} onChange={value => set('source_owner', value)} />
            </Field>
            <Field label="Source table" required note={form.prefillNotes?.source_table} error={visibleError(validation.errors, 'source_table', form.source_table)}>
              <input value={form.source_table} onChange={event => set('source_table', event.target.value)} className={inputCls} autoFocus />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Target owner" required note={form.prefillNotes?.target_owner} error={visibleError(validation.errors, 'target_owner', form.target_owner)}>
              <OwnerSelect value={form.target_owner} owners={TARGET_OWNERS} onChange={value => set('target_owner', value)} />
            </Field>
            <Field label="Target table" required note={form.prefillNotes?.target_table} error={visibleError(validation.errors, 'target_table', form.target_table)}>
              <input value={form.target_table} onChange={event => set('target_table', event.target.value)} className={inputCls} />
            </Field>
          </div>
          <Field label="Mapping name" required note={form.prefillNotes?.mapping_name} error={visibleError(validation.errors, 'mapping_name', form.mapping_name)}>
            <input value={form.mapping_name} onChange={event => set('mapping_name', event.target.value)} className={inputCls} />
          </Field>
          <Field label="Natural key columns" required note={form.prefillNotes?.natural_key_text} error={visibleError(validation.errors, 'natural_key_text', form.natural_key_text)}>
            <input value={form.natural_key_text} onChange={event => set('natural_key_text', event.target.value)} placeholder="CUST_ID" className={inputCls} />
          </Field>
          <div className="space-y-2">
            <span className="text-xs font-semibold text-slate-500 uppercase block">Tracked columns *</span>
            <div className="flex flex-wrap gap-4 text-sm text-slate-700">
              <label className="flex items-center gap-2">
                <input type="radio" name="tracked_mode" checked={form.tracked_mode === 'list'} onChange={() => set('tracked_mode', 'list')} />
                Listed columns
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" name="tracked_mode" checked={form.tracked_mode === 'all'} onChange={() => set('tracked_mode', 'all')} />
                All other source columns
              </label>
            </div>
            {form.tracked_mode === 'list' && (
              <Field label="Tracked column list" required error={visibleError(validation.errors, 'tracked_text', form.tracked_text)}>
                <input value={form.tracked_text} onChange={event => set('tracked_text', event.target.value)} placeholder="CUST_NAME, CUST_CITY" className={inputCls} />
              </Field>
            )}
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Overwrite columns" error={visibleError(validation.errors, 'overwrite_text', form.overwrite_text)}>
              <input value={form.overwrite_text} onChange={event => set('overwrite_text', event.target.value)} className={inputCls} />
            </Field>
            <Field label="Sequence name" error={visibleError(validation.errors, 'sequence_name', form.sequence_name)}>
              <input value={form.sequence_name} onChange={event => set('sequence_name', event.target.value)} placeholder="Leave blank for the server default" className={inputCls} />
            </Field>
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={form.grant_select_to_app_user === true}
              onChange={event => set('grant_select_to_app_user', event.target.checked)}
            />
            Grant SELECT to APP_USER
          </label>
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <p className="text-sm text-slate-500">Plan preview. No approval and no writes.</p>
          {loading && <p className="text-sm text-slate-500"><i className="fa-solid fa-spinner fa-spin mr-2" />Loading plan</p>}
          {planView?.success && (
            <div className="space-y-4">
              <div className="bg-indigo-50 rounded-xl border border-indigo-200 p-5 space-y-2 text-sm">
                <h4 className="font-bold text-indigo-800">Planned Type 2 target</h4>
                <p><span className="text-indigo-600 font-medium">Mapping</span> <span className="font-mono">{planView.mapping_name}</span></p>
                <p><span className="text-indigo-600 font-medium">Scenario</span> <span className="font-mono">{planView.scenario_name} {planView.scenario_version}</span></p>
                <p><span className="text-indigo-600 font-medium">Natural key</span> <span className="font-mono">{planView.natural_key.join(', ')}</span></p>
                <p><span className="text-indigo-600 font-medium">Tracked</span> <span className="font-mono">{planView.tracked_attributes.join(', ') || 'all'}</span></p>
                {planView.metadata_source ? <p className="text-xs text-slate-500">Metadata source: {planView.metadata_source}</p> : null}
                <p className="text-xs text-slate-500">Writes attempted: {planView.write_operations_attempted.length}</p>
              </div>
              <div className="overflow-auto rounded-xl border border-slate-200">
                <table className="w-full text-left text-sm">
                  <thead className="bg-slate-50 text-xs uppercase text-slate-500">
                    <tr>
                      <th className="px-3 py-2">Column</th>
                      <th className="px-3 py-2">Type</th>
                      <th className="px-3 py-2">Role</th>
                      <th className="px-3 py-2">Nullable</th>
                    </tr>
                  </thead>
                  <tbody>
                    {planView.columns.map(column => (
                      <tr key={column.name} className="border-t border-slate-100">
                        <td className="px-3 py-2 font-mono">{column.name}</td>
                        <td className="px-3 py-2 font-mono">{column.data_type}</td>
                        <td className="px-3 py-2">{column.scd_role}</td>
                        <td className="px-3 py-2">{column.nullable ? 'yes' : 'no'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="rounded-xl border border-slate-200 p-4 text-sm">
                <p className="text-xs font-bold uppercase text-slate-500">Validity and current flag</p>
                <ul className="mt-2 space-y-1">
                  {planView.validityColumns.map(column => (
                    <li key={column.name} className="font-mono text-slate-700">{column.name} · {column.scd_role}</li>
                  ))}
                </ul>
              </div>
              {planView.ddl && (
                <div className="space-y-2">
                  <p className="text-xs font-bold uppercase text-slate-500">DDL preview</p>
                  <pre className="max-h-48 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100">{planView.ddl.create_sequence}{'\n\n'}{planView.ddl.create_table}</pre>
                  {planView.ddl.grants.length > 0 && (
                    <pre className="max-h-32 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100">{planView.ddl.grants.join('\n')}</pre>
                  )}
                </div>
              )}
              {planView.warnings.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
                  {planView.warnings.map(warning => <p key={warning}>{warning}</p>)}
                </div>
              )}
            </div>
          )}
          {planView && !planView.success && (
            <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
              <p className="font-semibold">Plan blocked</p>
              <ul className="mt-2 list-disc pl-5">
                {planView.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}
              </ul>
            </div>
          )}
          {!planView && <ErrorBanner message={error} />}
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <div className="bg-indigo-50 rounded-xl border border-indigo-200 p-5 space-y-2 text-sm">
            <h4 className="font-bold text-indigo-800">Generate Type 2 mapping</h4>
            <p className="text-slate-600">Generate creates the target, the ODI mapping, and the scenario.</p>
            <p className="font-mono text-slate-700">{form.mapping_name}</p>
          </div>
          {!decision.locked && !generateButton.finished && generateGate.phase !== 'needs_review' && (
            <label className="flex items-start gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={generateGate.confirmed === true}
                disabled={generateGate.phase === 'in_flight'}
                onChange={event => confirmGenerate(event.target.checked)}
              />
              <span>I approve Type 2 generation.</span>
            </label>
          )}
          {generateView?.success && (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">
              <p className="font-semibold">Status: {generateView.status}</p>
              <p>Mapping: <span className="font-mono">{generateView.mapping_name}</span></p>
              <p>Scenario: <span className="font-mono">{generateView.scenario_name}</span></p>
              <p>Version: <span className="font-mono">{generateView.scenario_version}</span></p>
            </div>
          )}
          {generateView?.needsReview ? <EvidencePanel evidence={evidence} /> : null}
          {generateView && !generateView.success && !generateView.needsReview ? <ErrorBanner message={generateView.message} /> : null}
          {!generateView && <ErrorBanner message={error} />}
        </div>
      )}

      {step === 3 && (
        <div className="space-y-4">
          <div className="bg-indigo-50 rounded-xl border border-indigo-200 p-5 space-y-2 text-sm">
            <h4 className="font-bold text-indigo-800">Run Type 2 scenario</h4>
            <p className="text-slate-600">Run starts the generated scenario, then reads count-only evidence.</p>
            <p className="font-mono text-slate-700">
              {generateView?.scenario_name} {generateView?.scenario_version}
            </p>
          </div>
          {runButton.notice ? <p className="text-sm font-medium text-amber-800">{runButton.notice}</p> : null}
          {!decision.locked && runGate.phase !== 'needs_review' && (
            <label className="flex items-start gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={runGate.confirmed === true}
                disabled={runGate.phase === 'in_flight'}
                onChange={event => confirmRun(event.target.checked)}
              />
              <span>I approve the Type 2 scenario run.</span>
            </label>
          )}
          {runView && !runView.needsReview && (
            <div className={clsx(
              'rounded-xl border p-4 text-sm',
              runView.success ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-rose-200 bg-rose-50 text-rose-800',
            )}>
              <p className="font-semibold">Status: {runView.status || 'failed'}</p>
              {runView.session_status ? <p>Session: {runView.session_status}</p> : null}
              {runView.scenario ? (
                <p className="font-mono">
                  {runView.scenario.name} {runView.scenario.version} {runView.scenario.context}
                </p>
              ) : null}
              {!runView.success && runView.message ? <p className="mt-2">{runView.message}</p> : null}
            </div>
          )}
          <EvidencePanel evidence={shownEvidence} />
          {!runView && <ErrorBanner message={error} />}
          {runView && error && !runView.message?.includes(error) ? <ErrorBanner message={error} /> : null}
        </div>
      )}

      <div className="flex justify-between mt-6 pt-4 border-t border-slate-200">
        <button
          type="button"
          onClick={() => {
            if (writeBusy() || step === 0) return;
            setError('');
            setStep(current => current - 1);
          }}
          disabled={step === 0 || busy}
          className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-0 transition-all"
        >
          Back
        </button>
        <div className="flex gap-2">
          {step === 0 && (
            <>
              {decision.locked && (
                <button
                  type="button"
                  onClick={() => {
                    if (!evidenceAllowed) return;
                    refreshEvidence();
                  }}
                  disabled={!evidenceAllowed}
                  className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                >
                  Refresh evidence
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  if (writeBusy() || !validation.ok || loading) return;
                  setStep(1);
                  loadPlan();
                }}
                disabled={!validation.ok || loading || busy}
                className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                Next
              </button>
            </>
          )}
          {step === 1 && (
            <>
              {decision.locked && (
                <button
                  type="button"
                  onClick={() => {
                    if (!evidenceAllowed) return;
                    refreshEvidence();
                  }}
                  disabled={!evidenceAllowed}
                  className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                >
                  Refresh evidence
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  if (writeBusy()) return;
                  loadPlan();
                }}
                disabled={loading || !validation.ok || busy}
                className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50"
              >
                Refresh plan
              </button>
              <button
                type="button"
                onClick={() => {
                  if (writeBusy() || !canGenerate(planView)) return;
                  setError('');
                  setStep(2);
                }}
                disabled={!canGenerate(planView) || loading || busy}
                className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                Next
              </button>
            </>
          )}
          {step === 2 && (
            <>
              {(decision.locked || generateGate.phase === 'needs_review') && (
                <button
                  type="button"
                  onClick={() => {
                    if (!evidenceAllowed) return;
                    refreshEvidence();
                  }}
                  disabled={!evidenceAllowed}
                  className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                >
                  Refresh evidence
                </button>
              )}
              <button
                type="button"
                onClick={handleGenerate}
                disabled={!generateButton.enabled}
                className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white px-6 py-2 rounded-lg text-sm font-bold transition-colors"
              >
                <i className={clsx('fa-solid mr-2', generateButton.inFlight ? 'fa-spinner fa-spin' : 'fa-play')} />
                {generateButton.label}
              </button>
              <button
                type="button"
                onClick={() => {
                  if (writeBusy() || !stageSucceeded(generateView)) return;
                  setError('');
                  setStep(3);
                }}
                disabled={!stageSucceeded(generateView) || busy}
                className="bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                Next
              </button>
            </>
          )}
          {step === 3 && (
            <>
              <button
                type="button"
                onClick={() => {
                  if (!evidenceAllowed) return;
                  refreshEvidence();
                }}
                disabled={!evidenceAllowed}
                className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50"
              >
                Refresh evidence
              </button>
              <button
                type="button"
                onClick={handleRun}
                disabled={!runButton.enabled}
                className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white px-6 py-2 rounded-lg text-sm font-bold transition-colors"
              >
                <i className={clsx('fa-solid mr-2', runButton.inFlight ? 'fa-spinner fa-spin' : 'fa-play')} />
                {runButton.label}
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}
