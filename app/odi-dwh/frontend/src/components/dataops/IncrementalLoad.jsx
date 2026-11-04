import { useCallback, useEffect, useMemo, useState } from 'react';
import { executeIncrementalAppend, getIncrementalPreflight } from '../../services/api';
import clsx from 'clsx';

const SPEC_NAME = 'SH_SALES_ODS';

function firstResult(report) {
  return report?.results?.[0] || null;
}

function statusClass(status) {
  if (status === 'executed' || status === 'passed' || status === 'new_window_ready') {
    return 'bg-emerald-50 text-emerald-700 border-emerald-100';
  }
  if (status === 'duplicate_or_empty_window_blocked' || status === 'blocked_by_rerun_guard') {
    return 'bg-amber-50 text-amber-700 border-amber-100';
  }
  if (status?.includes?.('failed') || status?.includes?.('blocked')) {
    return 'bg-rose-50 text-rose-700 border-rose-100';
  }
  return 'bg-slate-50 text-slate-600 border-slate-100';
}

function Metric({ label, value }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 bg-white px-4 py-3">
      <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">{label}</div>
      <div className="mt-1 truncate text-sm font-bold text-slate-800" title={String(value ?? '-')}>
        {value ?? '-'}
      </div>
    </div>
  );
}

export function IncrementalLoad() {
  const [preflight, setPreflight] = useState(null);
  const [execution, setExecution] = useState(null);
  const [loading, setLoading] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [error, setError] = useState(null);
  const [approved, setApproved] = useState(false);

  const result = firstResult(preflight);
  const rerunState = result?.rerun_guard?.state;
  const gates = execution?.post_load_gates;
  const estimatedRows = result?.dry_run_delta?.estimated_new_rows;
  const canRequestExecution = approved && rerunState === 'new_window_ready' && !executing;

  const loadPreflight = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getIncrementalPreflight(SPEC_NAME);
      setPreflight(data);
      setExecution(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = setTimeout(loadPreflight, 0);
    return () => clearTimeout(id);
  }, [loadPreflight]);

  const statusItems = useMemo(() => ([
    { label: 'Spec', value: result?.spec || SPEC_NAME },
    { label: 'Strategy', value: result?.strategy },
    { label: 'Rerun Guard', value: rerunState },
    { label: 'Estimated Rows', value: estimatedRows },
  ]), [estimatedRows, rerunState, result]);

  const runAppend = async () => {
    if (!canRequestExecution) return;
    if (!window.confirm('This runs a live DB append and persisted watermark update. Continue?')) return;
    setExecuting(true);
    setError(null);
    try {
      const data = await executeIncrementalAppend({
        spec_name: SPEC_NAME,
        approved: true,
      });
      setExecution(data.execution);
      setPreflight(data.preflight);
    } catch (err) {
      setError(err.message);
    } finally {
      setExecuting(false);
    }
  };

  return (
    <div className="flex h-full flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h3 className="flex items-center gap-2 text-lg font-bold text-slate-800">
            <i className="fa-solid fa-water text-orange-500" />
            Incremental Load
          </h3>
          <p className="mt-1 max-w-3xl text-xs text-slate-500">
            Controlled SH incremental append gate. This does not run ODI or truncate targets.
          </p>
        </div>
        <button
          onClick={loadPreflight}
          disabled={loading || executing}
          className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-slate-500 hover:border-orange-200 hover:text-orange-600 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <i className={clsx('fa-solid mr-1.5', loading ? 'fa-spinner fa-spin' : 'fa-arrows-rotate')} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-100 bg-rose-50 px-4 py-3 text-sm font-bold text-rose-600">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        {statusItems.map(item => <Metric key={item.label} label={item.label} value={item.value} />)}
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Current Gate</div>
            <div className={clsx('mt-2 inline-flex rounded-full border px-3 py-1 text-xs font-bold', statusClass(rerunState))}>
              {rerunState || 'not loaded'}
            </div>
          </div>
          {execution && (
            <div className="text-right">
              <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Last Request</div>
              <div className={clsx('mt-2 inline-flex rounded-full border px-3 py-1 text-xs font-bold', statusClass(execution.status))}>
                {execution.status}
              </div>
            </div>
          )}
        </div>

        <div className="mt-5 grid gap-3 text-sm text-slate-700">
          <label className="flex items-start gap-3 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
            <input
              type="checkbox"
              checked={approved}
              onChange={event => setApproved(event.target.checked)}
              className="mt-1"
            />
            <span>
              <span className="block font-bold">I approve a live append and persisted watermark update.</span>
              <span className="text-xs text-slate-500">Backend approval and rerun guard are still enforced server-side.</span>
            </span>
          </label>

          <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="font-bold">Post-load gates (computed by the server)</div>
            <div className="text-xs text-slate-500">
              Golden record and DQ gates run on the server inside the same transaction as the append and watermark update. They cannot be asserted from this page.
            </div>
            <div className="mt-2 flex flex-wrap gap-2 text-xs font-bold">
              <span className={clsx('rounded-full border px-3 py-1', statusClass(gates ? (gates.post_load_golden_record_passed ? 'passed' : 'failed') : null))}>
                Golden record: {gates ? (gates.post_load_golden_record_passed ? 'passed' : 'failed') : 'not run'}
              </span>
              <span className={clsx('rounded-full border px-3 py-1', statusClass(gates ? (gates.post_load_dq_passed ? 'passed' : 'failed') : null))}>
                DQ: {gates ? (gates.post_load_dq_passed ? 'passed' : 'failed') : 'not run'}
              </span>
            </div>
          </div>
        </div>

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-4">
          <div className="text-xs text-slate-500">
            Evidence is count/status-only. Raw rows, PK values, secrets, and connection strings are not returned.
          </div>
          <button
            onClick={runAppend}
            disabled={!canRequestExecution}
            className={clsx(
              'rounded-lg px-4 py-2 text-xs font-black transition-all',
              canRequestExecution
                ? 'bg-orange-600 text-white hover:bg-orange-700 active:scale-95'
                : 'cursor-not-allowed bg-slate-100 text-slate-400'
            )}
          >
            <i className={clsx('fa-solid mr-1.5', executing ? 'fa-spinner fa-spin' : 'fa-play')} />
            Run Approved Append
          </button>
        </div>
      </div>
    </div>
  );
}
