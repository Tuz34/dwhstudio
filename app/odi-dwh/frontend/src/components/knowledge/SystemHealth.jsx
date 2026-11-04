import { useApp } from '../../store/AppContext';
import { useStatus } from '../../hooks/useStatus';
import { getFreshness } from '../../services/api';
import { useState, useEffect } from 'react';
import clsx from 'clsx';

const CHECK_META = {
  ai_orchestrator: { label: 'AI Orchestrator', icon: 'fa-diagram-project' },
  oracle: { label: 'Oracle 19c', icon: 'fa-database' },
  source: { label: 'Oracle Source', icon: 'fa-server' },
  rag: { label: 'Knowledge Base', icon: 'fa-brain' },
  dba: { label: 'DBA Monitor', icon: 'fa-gauge-high' },
  mcp: { label: 'MCP Servers', icon: 'fa-plug' },
  monitoring: { label: 'Monitoring', icon: 'fa-chart-line' },
  airflow: { label: 'Airflow', icon: 'fa-wind' },
  langgraph: { label: 'LangGraph', icon: 'fa-sitemap' },
  llm: { label: 'Ollama LLM', icon: 'fa-robot' },
  claude: { label: 'Claude API', icon: 'fa-wand-magic-sparkles' },
  docker: { label: 'Docker', icon: 'fa-docker' },
  local_openai: { label: 'Local OpenAI', icon: 'fa-microchip' },
};

const OPTIONAL_CHECKS = new Set(['claude', 'docker', 'llm', 'local_openai']);
const HIDDEN_CHECKS = new Set(['git', 'gitea', 'mssql']);

const DOWN_HINTS = {
  claude: 'Cloud API is disabled or not responding.',
  docker: 'Docker is optional for this view.',
  llm: 'Ollama is not responding.',
  local_openai: 'LM Studio or another local OpenAI-compatible server is down.',
};

function StatusRow({ id, ok, optional = false }) {
  const meta = CHECK_META[id] || { label: id, icon: 'fa-circle' };
  const label = ok ? 'UP' : optional ? 'OPTIONAL DOWN' : 'DOWN';

  return (
    <div className="flex min-h-10 items-center gap-3 border-b border-slate-100 px-3 py-2 last:border-b-0">
      <span
        className={clsx(
          'h-2.5 w-2.5 rounded-full',
          ok ? 'bg-emerald-500' : optional ? 'bg-slate-300' : 'bg-rose-500'
        )}
      />
      <i className={clsx('fa-solid w-4 text-center text-xs', meta.icon, ok ? 'text-slate-500' : 'text-slate-400')} />
      <span className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-700">{meta.label}</span>
      {!ok && DOWN_HINTS[id] && (
        <span className="hidden min-w-0 max-w-[48%] truncate text-xs text-slate-400 lg:block">
          {DOWN_HINTS[id]}
        </span>
      )}
      <span
        className={clsx(
          'shrink-0 rounded-full px-2 py-0.5 text-[10px] font-black',
          ok
            ? 'bg-emerald-50 text-emerald-700'
            : optional
              ? 'bg-slate-100 text-slate-500'
              : 'bg-rose-50 text-rose-700'
        )}
      >
        {label}
      </span>
    </div>
  );
}

function StatusSection({ title, rows, optional = false }) {
  if (rows.length === 0) return null;

  return (
    <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-3 py-2">
        <h4 className="text-xs font-black tracking-wide text-slate-500">{title}</h4>
        <span className="text-[10px] font-semibold text-slate-400">{rows.length} services</span>
      </div>
      <div>
        {rows.map(([id, ok]) => (
          <StatusRow key={id} id={id} ok={ok} optional={optional} />
        ))}
      </div>
    </section>
  );
}

function FreshnessTable({ data }) {
  if (!data || data.length === 0) return null;

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-black text-slate-500">
        <i className="fa-solid fa-droplet text-blue-400" />
        Data Freshness
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs text-slate-600">
          <thead className="border-b border-slate-100 bg-white text-slate-500">
            <tr>
              <th className="px-3 py-2 font-medium">Table</th>
              <th className="px-3 py-2 font-medium">Last Update</th>
              <th className="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {data.map((row, i) => (
              <tr key={i} className="hover:bg-slate-50">
                <td className="px-3 py-2 font-mono font-medium">{row.TABLE_NAME || row.table || '-'}</td>
                <td className="px-3 py-2 text-slate-500">{row.LAST_EXTRACT_DATE || row.last_update || '-'}</td>
                <td className="px-3 py-2">
                  <span
                    className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px] font-semibold',
                      row.status === 'fresh' || !row.status
                        ? 'bg-emerald-100 text-emerald-700'
                        : 'bg-amber-100 text-amber-700'
                    )}
                  >
                    {row.status || 'OK'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SystemHealth() {
  const { systemStatus, statusLoading } = useApp();
  const { check } = useStatus();
  const [freshness, setFreshness] = useState([]);

  useEffect(() => {
    getFreshness()
      .then((d) => {
        if (Array.isArray(d)) setFreshness(d);
        else if (d && Array.isArray(d.data)) setFreshness(d.data);
      })
      .catch(() => {});
  }, []);

  const checks = systemStatus?.checks || {};
  const visibleRows = Object.entries(checks).filter(([id]) => !HIDDEN_CHECKS.has(id));
  const coreRows = visibleRows.filter(([id]) => !OPTIONAL_CHECKS.has(id));
  const optionalRows = visibleRows.filter(([id]) => OPTIONAL_CHECKS.has(id));
  const coreUp = coreRows.filter(([, ok]) => Boolean(ok)).length;
  const coreTotal = coreRows.length;
  const coreOk = coreTotal > 0 && coreUp === coreTotal;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <div className="flex items-center gap-3">
          <span
            className={clsx(
              'h-3 w-3 rounded-full',
              statusLoading ? 'animate-pulse bg-blue-500' : coreOk ? 'bg-emerald-500' : 'bg-rose-500'
            )}
          />
          <div>
            <div className="text-sm font-black text-slate-800">
              {statusLoading ? 'Checking services' : `${coreUp}/${coreTotal || 0} core services up`}
            </div>
            <div className="text-xs text-slate-400">
              Optional integrations are listed separately and do not block the core status.
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={check}
          disabled={statusLoading}
          className="inline-flex h-8 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-xs font-bold text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50 disabled:opacity-50"
        >
          <i className={clsx('fa-solid fa-arrows-rotate text-[10px]', statusLoading && 'fa-spin')} />
          Refresh
        </button>
      </div>

      {statusLoading && coreTotal === 0 ? (
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-400">
          <i className="fa-solid fa-spinner fa-spin mr-2" />
          Reading service status...
        </div>
      ) : coreTotal === 0 ? (
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-400">
          Status information could not be loaded. Press Refresh.
        </div>
      ) : (
        <StatusSection title="Core Services" rows={coreRows} />
      )}

      <StatusSection title="Optional Integrations" rows={optionalRows} optional />

      {freshness.length > 0 && <FreshnessTable data={freshness} />}
    </div>
  );
}
