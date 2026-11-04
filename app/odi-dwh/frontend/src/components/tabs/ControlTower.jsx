import { useCallback, useEffect, useState } from 'react';
import { SystemHealth }  from '../knowledge/SystemHealth';
import { AiSettings }    from '../knowledge/AiSettings';
import clsx from 'clsx';
import { getSecurityAudit } from '../../services/api';

const VIEWS = [
  { id: 'health',    label: 'System Health', icon: 'fa-tower-broadcast', color: 'emerald' },
  { id: 'settings',  label: 'Settings & AI',  icon: 'fa-gears',            color: 'blue'   },
  { id: 'security',  label: 'Security Audit', icon: 'fa-shield-halved',     color: 'orange' },
];

function SecurityAudit() {
  const [data, setData] = useState({ events: [], chain_valid: true });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadAudit = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await getSecurityAudit(100);
      setData(result);
    } catch (err) {
      setError(err.message || 'Security audit could not be loaded.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = setTimeout(loadAudit, 0);
    return () => clearTimeout(id);
  }, [loadAudit]);

  if (loading) {
    return <div className="text-sm text-slate-500">Loading security audit</div>;
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">Security Audit</h2>
          <p className="text-sm text-slate-500">
            Local audit evidence log for approvals and sensitive actions; not a vulnerability scanner.
          </p>
          <p className="mt-1 text-sm text-slate-500">
            Hash chain: {data.chain_valid ? 'valid' : 'invalid'}
          </p>
        </div>
        <button className="rounded-md border border-slate-300 px-3 py-2 text-sm" type="button" onClick={loadAudit}>
          Refresh
        </button>
      </div>
      <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-50 text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">Time</th>
              <th className="px-3 py-2">Action</th>
              <th className="px-3 py-2">Outcome</th>
              <th className="px-3 py-2">Actor</th>
              <th className="px-3 py-2">Hash</th>
            </tr>
          </thead>
          <tbody>
            {(data.events || []).map(event => (
              <tr key={event.event_hash} className="border-t border-slate-100">
                <td className="px-3 py-2 text-slate-600">{event.ts}</td>
                <td className="px-3 py-2 font-medium text-slate-800">{event.action}</td>
                <td className="px-3 py-2 text-slate-600">{event.outcome}</td>
                <td className="px-3 py-2 text-slate-600">{event.actor}</td>
                <td className="px-3 py-2 font-mono text-xs text-slate-500">{event.event_hash?.slice(0, 12)}</td>
              </tr>
            ))}
            {(!data.events || data.events.length === 0) && (
              <tr>
                <td className="px-3 py-6 text-center text-slate-500" colSpan="5">No audit events</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ControlTower() {
  const [view, setView] = useState('health');
  
  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Sub-nav */}
      <div className="shrink-0 px-6 pt-4 pb-0 border-b border-slate-200 flex items-center justify-between">
        <div className="flex gap-2">
          {VIEWS.map(v => {
            const isActive = view === v.id;
            return (
              <button
                key={v.id}
                onClick={() => setView(v.id)}
                className={clsx(
                  'flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-all',
                  isActive
                    ? v.color === 'emerald' ? 'border-b-emerald-500 text-emerald-700 bg-emerald-50 rounded-t-lg'
                      : v.color === 'orange' ? 'border-b-orange-500 text-orange-700 bg-orange-50 rounded-t-lg'
                      : 'border-b-blue-500 text-blue-700 bg-blue-50 rounded-t-lg'
                    : 'border-b-transparent text-slate-500 hover:text-slate-700 hover:bg-slate-50 rounded-t-lg'
                )}
              >
                <i className={`fa-solid ${v.icon} text-xs`}></i>
                {v.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        {view === 'health'    && <SystemHealth />}
        {view === 'settings'  && <AiSettings />}
        {view === 'security'  && <SecurityAudit />}
      </div>
    </div>
  );
}
