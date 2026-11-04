import { useState, useEffect, useCallback, useMemo } from 'react';
import { checkAlerts, getMappingRuns } from '../../services/api';
import clsx from 'clsx';

const RUN_FILTERS = [
  { value: '', label: 'All' },
  { value: 'SUCCEEDED', label: 'Succeeded' },
  { value: 'RUN_FAILED', label: 'Failed' },
  { value: 'RUNNING', label: 'Running' },
];

function formatDateTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  const total = Number(seconds);
  if (!Number.isFinite(total)) return '-';
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}m ${rest}s`;
}

function statusClasses(status) {
  const normalized = String(status || '').toUpperCase();
  if (['SUCCEEDED', 'DONE', 'D'].includes(normalized)) return 'bg-emerald-100 text-emerald-700 border-emerald-200';
  if (['RUN_FAILED', 'FAILED', 'ERROR', 'E'].includes(normalized)) return 'bg-rose-100 text-rose-700 border-rose-200';
  if (['RUNNING', 'R'].includes(normalized)) return 'bg-amber-100 text-amber-700 border-amber-200';
  return 'bg-slate-100 text-slate-700 border-slate-200';
}

export function Sessions() {
  const [runs, setRuns] = useState([]);
  const [runFilter, setRunFilter] = useState('');
  const [runSearch, setRunSearch] = useState('');
  const [lastRefreshedAt, setLastRefreshedAt] = useState(null);
  const [freshnessCheck, setFreshnessCheck] = useState(null);
  const [freshnessChecking, setFreshnessChecking] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const loadSessions = useCallback(async () => {
    try {
      setLoading(true);
      const runData = await getMappingRuns({ limit: 25, status: runFilter || undefined });
      setRuns(Array.isArray(runData?.runs) ? runData.runs : []);
      setLastRefreshedAt(new Date().toISOString());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [runFilter]);

  const runFreshnessCheck = useCallback(async () => {
    if (!window.confirm('This check can create persistent alert records. Continue?')) return;
    try {
      setFreshnessChecking(true);
      const result = await checkAlerts({ approved: true, freshness_days: 2 });
      setFreshnessCheck(result);
      await loadSessions();
    } catch (e) {
      setFreshnessCheck({ error: e.message || String(e), created_alerts: [], count: 0 });
    } finally {
      setFreshnessChecking(false);
    }
  }, [loadSessions]);

  const filteredRuns = useMemo(() => {
    const needle = runSearch.trim().toUpperCase();
    if (!needle) return runs;
    return runs.filter(run => {
      const haystack = [
        run.mapping_name,
        run.scenario_name,
        run.batch_id,
        run.current_step,
      ].filter(Boolean).join(' ').toUpperCase();
      return haystack.includes(needle);
    });
  }, [runs, runSearch]);

  useEffect(() => {
    const id = setTimeout(loadSessions, 0);
    return () => clearTimeout(id);
  }, [loadSessions]);

  if (loading) {
    return (
      <div className="text-slate-400 p-8 flex items-center justify-center">
        <i className="fa-solid fa-spinner fa-spin mr-2"></i>
        Loading run status...
      </div>
    );
  }

  if (error) return <div className="text-red-500 p-8">Failed to load: {error}</div>;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h3 className="text-xl font-bold text-slate-800 flex items-center gap-2">
          <i className="fa-solid fa-list-ol text-blue-500"></i>
          Run Status
        </h3>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-slate-400">
            Last refreshed {lastRefreshedAt ? formatDateTime(lastRefreshedAt) : '-'}
          </span>
          <input
            type="search"
            value={runSearch}
            onChange={(event) => setRunSearch(event.target.value)}
            placeholder="Search mapping or scenario"
            className="h-9 min-w-[220px] rounded-lg border border-slate-200 bg-white px-3 text-xs font-semibold text-slate-600 outline-none focus:ring-2 focus:ring-blue-300"
            aria-label="Search mapping or scenario"
          />
          <select
            value={runFilter}
            onChange={(event) => setRunFilter(event.target.value)}
            className="h-9 rounded-lg border border-slate-200 bg-white px-3 text-xs font-bold text-slate-600 outline-none focus:ring-2 focus:ring-blue-300"
            aria-label="Filter run history by status"
          >
            {RUN_FILTERS.map(filter => (
              <option key={filter.value || 'all'} value={filter.value}>{filter.label}</option>
            ))}
          </select>
          <button
            onClick={loadSessions}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-blue-200 bg-white px-3 text-xs font-bold text-blue-600 transition-colors hover:bg-blue-50"
            title="Refresh run status"
            aria-label="Refresh run status"
          >
            <i className="fa-solid fa-arrows-rotate text-[11px]"></i>
            Refresh
          </button>
          <button
            onClick={runFreshnessCheck}
            disabled={freshnessChecking}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-amber-200 bg-white px-3 text-xs font-bold text-amber-700 transition-colors hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-60"
            title="Check freshness alerts"
            aria-label="Check freshness alerts"
          >
            <i className={clsx('fa-solid text-[11px]', freshnessChecking ? 'fa-spinner fa-spin' : 'fa-bell')}></i>
            Check freshness alerts
          </button>
        </div>
      </div>

      {freshnessCheck && (
        <section className={clsx(
          'rounded-xl border p-4 shadow-sm',
          freshnessCheck.error ? 'border-rose-200 bg-rose-50' : 'border-amber-200 bg-amber-50'
        )}>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h4 className={clsx(
                'flex items-center gap-2 text-sm font-bold',
                freshnessCheck.error ? 'text-rose-800' : 'text-amber-800'
              )}>
                <i className={clsx('fa-solid', freshnessCheck.error ? 'fa-triangle-exclamation' : 'fa-bell')}></i>
                Freshness Alert Check
              </h4>
              <p className={clsx('text-xs', freshnessCheck.error ? 'text-rose-600' : 'text-amber-700')}>
                {freshnessCheck.error
                  ? freshnessCheck.error
                  : `${freshnessCheck.count || 0} freshness or schema alert${(freshnessCheck.count || 0) === 1 ? '' : 's'} created.`}
              </p>
            </div>
            <span className="inline-flex w-fit rounded-full border bg-white px-2.5 py-1 text-[11px] font-black text-amber-700">
              {(freshnessCheck.created_alerts || []).length} created_alerts
            </span>
          </div>
          {(freshnessCheck.created_alerts || []).length > 0 && (
            <div className="mt-3 grid gap-2">
              {freshnessCheck.created_alerts.map(alert => (
                <div key={`${alert.alert_id || alert.source}-${alert.type}`} className="rounded-lg border border-amber-100 bg-white px-3 py-2">
                  <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                    <span className="font-mono text-xs font-bold text-amber-800">{alert.source || '-'}</span>
                    <span className="text-[11px] font-black uppercase text-amber-500">{alert.type || '-'}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      <section className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-5 py-3">
          <div>
            <h4 className="text-sm font-bold text-slate-800">Operations Timeline</h4>
            <p className="text-xs text-slate-500">Recent application-tracked mapping runs</p>
          </div>
          <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-slate-500">
            {filteredRuns.length} rows
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[920px] text-left text-sm text-slate-600">
            <thead className="text-xs text-slate-500 uppercase bg-white border-b border-slate-200">
              <tr>
                <th className="px-5 py-3 font-medium">Updated</th>
                <th className="px-5 py-3 font-medium">Mapping</th>
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium">Step</th>
                <th className="px-5 py-3 font-medium">Mode</th>
                <th className="px-5 py-3 font-medium">Duration</th>
                <th className="px-5 py-3 font-medium">Batch</th>
                <th className="px-5 py-3 font-medium">Error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredRuns.length === 0 ? (
                <tr>
                  <td colSpan="8" className="px-5 py-8 text-center text-slate-500">
                    {runs.length === 0 ? 'No run history found.' : 'No run history matches the current filters.'}
                  </td>
                </tr>
              ) : filteredRuns.map(run => {
                const status = String(run.overall_status || '').toUpperCase();
                return (
                  <tr key={run.batch_id || `${run.mapping_name}-${run.updated_at}`} className="align-top transition-colors hover:bg-slate-50">
                    <td className="px-5 py-3 whitespace-nowrap text-xs text-slate-500">{formatDateTime(run.updated_at)}</td>
                    <td className="px-5 py-3">
                      <div className="max-w-[260px]">
                        <div className="truncate font-bold text-slate-800" title={run.mapping_name || '-'}>
                          {run.mapping_name || '-'}
                        </div>
                        <div className="truncate text-[11px] text-slate-400" title={run.scenario_name || '-'}>
                          {run.scenario_name || '-'} {run.scenario_version ? `v${run.scenario_version}` : ''}
                        </div>
                      </div>
                    </td>
                    <td className="px-5 py-3">
                      <span className={clsx('inline-flex rounded-full border px-2.5 py-1 text-[11px] font-black uppercase', statusClasses(status))}>
                        {status || '-'}
                      </span>
                    </td>
                    <td className="px-5 py-3 font-mono text-xs text-slate-600">{run.current_step || '-'}</td>
                    <td className="px-5 py-3">
                      <span className={clsx(
                        'inline-flex rounded-md px-2 py-1 text-[11px] font-bold',
                        run.truncate_target_table === true
                          ? 'bg-cyan-50 text-cyan-700 border border-cyan-100'
                          : 'bg-slate-50 text-slate-600 border border-slate-100'
                      )}>
                        {run.truncate_target_table === true ? 'Truncate' : 'Append'}
                      </span>
                    </td>
                    <td className="px-5 py-3 font-mono text-xs">{formatDuration(run.duration_seconds)}</td>
                    <td className="px-5 py-3 font-mono text-[11px] text-slate-500">{run.batch_id || '-'}</td>
                    <td className="px-5 py-3">
                      {run.error ? (
                        <details className="max-w-[260px]">
                          <summary className="cursor-pointer text-xs font-bold text-rose-600">View error</summary>
                          <p className="mt-2 whitespace-pre-wrap break-words rounded-lg border border-rose-100 bg-rose-50 p-2 text-[11px] leading-5 text-rose-700">
                            {run.error}
                          </p>
                        </details>
                      ) : (
                        <span className="text-xs text-slate-300">-</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

    </div>
  );
}
