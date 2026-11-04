import { useState, useEffect, useCallback } from 'react';
import { Modal } from '../ui/Modal';
import { getSnapshots, compareSnapshots } from '../../services/api';
import clsx from 'clsx';

export function SnapshotCompare({ open, onClose, mappingName: initialName = '' }) {
  const [name, setName]             = useState(initialName);
  const [snapshots, setSnapshots]   = useState([]);
  const [snapsLoading, setSnapsLoad] = useState(false);
  const [snapA, setSnapA]           = useState('');
  const [snapB, setSnapB]           = useState('');
  const [diff, setDiff]             = useState(null);
  const [loading, setLoading]       = useState(false);
  const [error, setError]           = useState(null);

  const loadSnapshots = useCallback(async () => {
    if (!name.trim()) return;
    setSnapsLoad(true);
    try {
      const data = await getSnapshots(name, 20);
      const list = Array.isArray(data) ? data : (data.snapshots || []);
      setSnapshots(list);
      if (list.length >= 2) { setSnapA(list[1].snap_id || list[1].id); setSnapB(list[0].snap_id || list[0].id); }
    } catch (e) { setError(e.message); }
    finally { setSnapsLoad(false); }
  }, [name]);

  useEffect(() => {
    if (!open || !name) return undefined;
    const id = setTimeout(loadSnapshots, 0);
    return () => clearTimeout(id);
  }, [open, name, loadSnapshots]);

  const handleCompare = async () => {
    if (!snapA || !snapB) return;
    setLoading(true); setDiff(null); setError(null);
    try {
      const data = await compareSnapshots(name, { snap_a: snapA, snap_b: snapB });
      setDiff(data);
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  };

  const handleClose = () => { setDiff(null); setError(null); onClose(); };

  const selectCls = "w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400";

  return (
    <Modal open={open} onClose={handleClose} title="🔍 Snapshot Compare" size="xl">
      <div className="space-y-5">
        {/* Mapping name input */}
        <div className="flex gap-3">
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="Mapping name (e.g. MAP_SALES_CUSTOMER_TO_ODS)"
            className="flex-1 border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-400"
          />
          <button
            onClick={loadSnapshots}
            disabled={snapsLoading || !name.trim()}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2"
          >
            <i className={`fa-solid ${snapsLoading ? 'fa-spinner fa-spin' : 'fa-clock-rotate-left'}`}></i>
            Load Snapshots
          </button>
        </div>

        {/* Snapshot selectors */}
        {snapshots.length > 0 && (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Snapshot A (Old)</label>
              <select value={snapA} onChange={e => setSnapA(e.target.value)} className={selectCls}>
                {snapshots.map(s => (
                  <option key={s.snap_id || s.id} value={s.snap_id || s.id}>
                    {s.created_at || s.snap_id || s.id} — {s.description || ''}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Snapshot B (New)</label>
              <select value={snapB} onChange={e => setSnapB(e.target.value)} className={selectCls}>
                {snapshots.map(s => (
                  <option key={s.snap_id || s.id} value={s.snap_id || s.id}>
                    {s.created_at || s.snap_id || s.id} — {s.description || ''}
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}

        {snapshots.length > 0 && (
          <button
            onClick={handleCompare}
            disabled={loading || !snapA || !snapB || snapA === snapB}
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2.5 rounded-xl text-sm font-bold flex items-center justify-center gap-2"
          >
            <i className={`fa-solid ${loading ? 'fa-spinner fa-spin' : 'fa-code-compare'}`}></i>
            {loading ? 'Comparing...' : 'Compare'}
          </button>
        )}

        {error && (
          <div className="bg-rose-50 border border-rose-200 rounded-xl p-4 text-rose-600 text-sm">
            <i className="fa-solid fa-circle-exclamation mr-2"></i>{error}
          </div>
        )}

        {/* Diff result */}
        {diff && (
          <div className="space-y-4">
            {/* Summary */}
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: 'Added', count: diff.added?.length || 0,   color: 'emerald', icon: 'fa-plus'  },
                { label: 'Removed', count: diff.removed?.length || 0, color: 'rose',    icon: 'fa-minus' },
                { label: 'Changed', count: diff.changed?.length || 0, color: 'amber',   icon: 'fa-pen'  },
              ].map(s => (
                <div key={s.label} className={clsx(
                  'rounded-xl p-4 text-center border',
                  s.color === 'emerald' ? 'bg-emerald-50 border-emerald-200' :
                  s.color === 'rose' ? 'bg-rose-50 border-rose-200' : 'bg-amber-50 border-amber-200'
                )}>
                  <div className={clsx(
                    'text-3xl font-black',
                    s.color === 'emerald' ? 'text-emerald-700' : s.color === 'rose' ? 'text-rose-600' : 'text-amber-700'
                  )}>{s.count}</div>
                  <div className={clsx(
                    'text-xs font-semibold mt-1',
                    s.color === 'emerald' ? 'text-emerald-600' : s.color === 'rose' ? 'text-rose-500' : 'text-amber-600'
                  )}>
                    <i className={`fa-solid ${s.icon} mr-1`}></i>{s.label}
                  </div>
                </div>
              ))}
            </div>

            {/* Detail lines */}
            <div className="bg-slate-900 rounded-xl p-4 max-h-72 overflow-y-auto font-mono text-xs space-y-1">
              {(diff.added || []).map((line, i) => (
                <div key={'a'+i} className="text-emerald-400">+ {typeof line === 'object' ? JSON.stringify(line) : line}</div>
              ))}
              {(diff.removed || []).map((line, i) => (
                <div key={'r'+i} className="text-rose-400">- {typeof line === 'object' ? JSON.stringify(line) : line}</div>
              ))}
              {(diff.changed || []).map((line, i) => (
                <div key={'c'+i} className="text-amber-400">~ {typeof line === 'object' ? JSON.stringify(line) : line}</div>
              ))}
              {(!diff.added?.length && !diff.removed?.length && !diff.changed?.length) && (
                <div className="text-slate-400 text-center py-4">No differences — snapshots are identical.</div>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
