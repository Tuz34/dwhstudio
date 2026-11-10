import { useState, useEffect, useCallback, useRef } from 'react';
import { getOdiMappings, getOdiMappingDetail, runMapping, getMappingState, deleteMapping } from '../../services/api';
import { useDispatch } from '../../store/AppContext';
import clsx from 'clsx';
import { buildRunMappingRequest, openRunDialogState, runModeWarning, selectRunMode } from './mappingRunDialog';
import {
  NOT_REPORTED,
  applyRefreshToPanels,
  mappingRunBadge,
  mappingRunOutcomeLines,
  mappingRunToast,
  projectMappingRunOutcome,
  reconcileLastRuns,
} from './mappingRunOutcome';

const ODI_SCHEMA_TO_DB_SCHEMA = {
  SRC_HR: 'HR',
  SRC_SH: 'SH',
  TGT_DWH: 'DWH',
  LGCL_ORACLE_DWH_UNIT: 'DWH',
};

function displayDbTable(value) {
  if (!value || value === '?') return value || '?';
  return String(value)
    .split(',')
    .map(part => {
      const text = part.trim();
      return text.replace(/^([A-Z0-9_]+)\./i, (match, schema) => `${ODI_SCHEMA_TO_DB_SCHEMA[schema.toUpperCase()] || schema}.`);
    })
    .join(', ');
}

function LastRunPanel({ outcome, onClose, onRefresh }) {
  const lines = mappingRunOutcomeLines(outcome).filter(([label]) => label !== 'Result');
  const badge = mappingRunBadge(outcome);
  const badgeClass = badge.tone === 'success'
    ? 'bg-emerald-100 text-emerald-700'
    : badge.tone === 'warning'
      ? 'bg-amber-100 text-amber-800'
      : 'bg-rose-100 text-rose-700';
  const attention = outcome.outcomeClass === 'unknown'
    ? 'border-amber-200 bg-amber-50'
    : 'border-rose-100 bg-rose-50';
  const attentionText = outcome.outcomeClass === 'unknown' ? 'text-amber-800' : 'text-rose-700';
  return (
    <div className="p-4 rounded-xl bg-slate-50 border border-slate-100">
      <div className="flex items-center justify-between gap-2 mb-3">
        <span className="text-[9px] font-bold text-slate-500 uppercase tracking-wide">Last run</span>
        <span className={clsx('text-[9px] px-2 py-1 rounded-full font-black uppercase', badgeClass)}>
          {badge.label}
        </span>
      </div>
      {badge.historyNotice && (
        <p className="mb-3 text-[10px] font-semibold leading-4 text-amber-800">{badge.historyNotice}</p>
      )}
      {badge.notice && (
        <p className="mb-3 text-[10px] font-semibold leading-4 text-amber-800">{badge.notice}</p>
      )}
      <div className="grid grid-cols-2 gap-2 text-[10px]">
        {lines.map(([label, value]) => {
          const showAttention = label === 'Error' && value !== NOT_REPORTED && outcome.outcomeClass !== 'succeeded';
          return (
            <div
              key={label}
              className={clsx(
                'bg-white rounded-lg border border-slate-100 p-2',
                (label === 'Scenario' || label === 'Requested mapping' || label === 'Error' || label === 'Batch' || label === 'Session' || label === 'Counts') && 'col-span-2',
                showAttention && attention,
              )}
            >
              <div className="text-slate-400 font-bold uppercase mb-1">{label}</div>
              <div className={clsx('font-mono break-all', showAttention ? attentionText : 'text-slate-700')}>
                {value}
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <button
          type="button"
          onClick={onRefresh}
          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-[10px] font-bold text-slate-600 hover:bg-slate-50"
        >
          Refresh
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-[10px] font-bold text-slate-600 hover:bg-slate-50"
        >
          Close
        </button>
      </div>
    </div>
  );
}

function mappingContext(mapping = {}) {
  const project = mapping.PROJECT_CODE || mapping.project_code || mapping.PROJECT_NAME || mapping.project_name || mapping.project || '';
  const folder = mapping.FOLDER_NAME || mapping.folder_name || '';
  return {
    project: project || '-',
    folder: folder || '-',
  };
}

export function MappingList() {
  const dispatch = useDispatch();
  const [mappings, setMappings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [detail, setDetail] = useState(null);
  const [runStatus, setRunStatus] = useState({});
  const [toast, setToast] = useState(null);
  const [mappingState, setMappingState] = useState(null);
  const [stateLoading, setStateLoading] = useState(false);
  const [runDialog, setRunDialog] = useState(null);
  const [lastRuns, setLastRuns] = useState({});
  const autoSelectedRef = useRef(false);
  const runSeq = useRef(0);

  const showToast = useCallback((message, type = 'success') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  }, []);

  const loadDetail = useCallback(async (name) => {
    try {
      const data = await getOdiMappingDetail(name);
      setDetail(data);
      setMappingState(null);
      setStateLoading(true);
      try {
        const s = await getMappingState(name);
        setMappingState(s?.state || null);
      } catch {
        setMappingState(null);
      } finally {
        setStateLoading(false);
      }
    } catch (e) {
      showToast('Could not load details: ' + e.message, 'error');
    }
  }, [showToast]);

  const loadMappings = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getOdiMappings();
      const unique = [];
      const seen = new Set();
      (Array.isArray(data) ? data : []).forEach(m => {
        const key = (m.MAPPING_NAME || '').toUpperCase().trim();
        if (key && !seen.has(key)) {
          unique.push(m);
          seen.add(key);
        }
      });
      setMappings(unique);
      setLastRuns((prev) => reconcileLastRuns(prev, unique.map((row) => row.MAPPING_NAME)));
      if (!detail && !autoSelectedRef.current && unique.length > 0) {
        autoSelectedRef.current = true;
        setTimeout(() => loadDetail(unique[0].MAPPING_NAME), 0);
      }
    } catch (e) {
      showToast('Failed to load: ' + e.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [detail, loadDetail, showToast]);

  useEffect(() => {
    const id = setTimeout(loadMappings, 0);
    return () => clearTimeout(id);
  }, [loadMappings]);

  useEffect(() => {
    if (loading || detail || autoSelectedRef.current || mappings.length === 0) return;
    autoSelectedRef.current = true;
    loadDetail(mappings[0].MAPPING_NAME);
  }, [loading, detail, mappings, loadDetail]);

  const openRunDialog = (name, e) => {
    e.stopPropagation();
    // Non-destructive by default; truncate is only ever an explicit operator choice.
    setRunDialog(openRunDialogState(name));
  };

  const handleRun = async () => {
    if (!runDialog?.mappingName) return;
    const request = buildRunMappingRequest(runDialog);
    const name = request.mapping_name;
    const generation = runSeq.current + 1;
    runSeq.current = generation;
    setRunDialog(null);
    setLastRuns((prev) => {
      if (!prev[name]) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });
    setRunStatus(p => ({ ...p, [name]: 'running' }));
    const publish = (outcome) => {
      setLastRuns((prev) => ({ ...prev, [name]: outcome }));
      const toast = mappingRunToast(outcome, name);
      showToast(toast.message, toast.type);
      setRunStatus(p => ({ ...p, [name]: outcome.outcomeClass === 'succeeded' ? 'done' : 'error' }));
    };
    try {
      const res = await runMapping(request);
      publish(projectMappingRunOutcome({ httpStatus: 200, payload: res, request, generation }));
    } catch (e) {
      publish(projectMappingRunOutcome({
        httpStatus: typeof e?.status === 'number' ? e.status : null,
        payload: e?.body,
        thrown: e,
        request,
        generation,
      }));
    } finally {
      setTimeout(() => setRunStatus(p => { const n = {...p}; delete n[name]; return n; }), 4000);
    }
  };

  const closeLastRun = (name) => {
    setLastRuns((prev) => {
      const next = { ...prev };
      delete next[name];
      return next;
    });
  };

  const refreshLastRun = async (name) => {
    const started = lastRuns[name];
    if (!started) return;
    try {
      const response = await getMappingState(name);
      let decision = null;
      setLastRuns((prev) => {
        decision = applyRefreshToPanels(prev, name, started, response);
        return decision.applied ? decision.panels : prev;
      });
      if (decision && !decision.applied && decision.reason === 'mismatch') {
        showToast('Saved state does not match this run.', 'error');
      }
    } catch (err) {
      showToast(`Could not refresh run state: ${err.message}`, 'error');
    }
  };

  const handleEdit = (mapping, e) => {
    e.stopPropagation();
    dispatch({ type: 'OPEN_MODAL', payload: { type: 'surgeon', props: { mapping } } });
  };

  const handleEditKm = (mapping, e) => {
    e.stopPropagation();
    const currentTruncateTargetTable = detail?.km_options?.truncate_target_table ?? mappingState?.truncate_target_table;
    dispatch({
      type: 'OPEN_MODAL',
      payload: {
        type: 'surgeon',
        props: {
          mapping,
          initialOperation: 'update_km',
          initialTruncateTargetTable: currentTruncateTargetTable,
        },
      },
    });
  };

  const handleDelete = async (name, e) => {
    e.stopPropagation();
    if (!window.confirm(`${name} mapping will be permanently deleted. Continue?`)) return;

    try {
      const res = await deleteMapping({ mapping_name: name, approved: true });
      if (res.error) throw new Error(res.error);
      
      showToast(`${name} deleted successfully.`, 'success');
      setMappings(prev => prev.filter(m => m.MAPPING_NAME !== name));
      if (detail?.MAPPING_NAME === name) {
        autoSelectedRef.current = false;
        setDetail(null);
      }
    } catch (err) {
      showToast(`Delete failed: ${err.message}`, 'error');
    }
  };

  const filtered = mappings.filter(m => {
    const q = search.toLowerCase();
    return !q || (m.MAPPING_NAME || '').toLowerCase().includes(q) 
              || (m.SRC_TABLE || '').toLowerCase().includes(q) 
              || (m.TGT_TABLE || '').toLowerCase().includes(q);
  });
  const filteredFirstName = filtered[0]?.MAPPING_NAME || '';
  const selectedDetailName = detail?.MAPPING_NAME || '';

  useEffect(() => {
    if (loading) return;
    if (!search.trim()) return;
    if (filtered.length !== 1) return;
    if (!filteredFirstName || filteredFirstName === selectedDetailName) return;
    const id = setTimeout(() => loadDetail(filteredFirstName), 0);
    return () => clearTimeout(id);
  }, [filtered.length, filteredFirstName, loadDetail, loading, search, selectedDetailName]);

  return (
    <div className="space-y-4">
      {/* Toast */}
      {toast && (
        <div className={clsx("fixed top-6 right-6 z-50 rounded-xl px-5 py-3 shadow-xl text-white text-sm font-medium animate-fade-in",
          toast.type === 'success' ? 'bg-emerald-600' : toast.type === 'warning' ? 'bg-amber-600' : 'bg-rose-600')}>
          {toast.message}
        </div>
      )}

      {runDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 px-4">
          <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white shadow-2xl">
            <div className="border-b border-slate-100 px-5 py-4">
              <p className="text-sm font-bold text-slate-800">Run Scenario</p>
              <p className="mt-1 text-xs text-slate-500 break-all">{runDialog.mappingName}</p>
            </div>
            <div className="space-y-3 px-5 py-4">
              <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-violet-200 bg-violet-50 p-3">
                <input
                  type="radio"
                  name="target-run-mode"
                  checked={runDialog.truncateTargetTable === true}
                  onChange={() => setRunDialog(prev => selectRunMode(prev, true))}
                  className="mt-1 h-4 w-4 accent-violet-600"
                />
                <span>
                  <span className="block text-sm font-bold text-slate-800">Truncate target before run</span>
                  <span className="block text-xs leading-5 text-slate-500">Reloads the target table and avoids duplicate rows for insert-based IKMs.</span>
                </span>
              </label>
              <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 bg-white p-3">
                <input
                  type="radio"
                  name="target-run-mode"
                  checked={runDialog.truncateTargetTable === false}
                  onChange={() => setRunDialog(prev => selectRunMode(prev, false))}
                  className="mt-1 h-4 w-4 accent-violet-600"
                />
                <span>
                  <span className="block text-sm font-bold text-slate-800">Append without truncate</span>
                  <span className="block text-xs leading-5 text-slate-500">Keeps existing target rows and appends new rows.</span>
                </span>
              </label>
              {runModeWarning(runDialog) && (
                <p role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-semibold text-rose-700">
                  {runModeWarning(runDialog)}
                </p>
              )}
            </div>
            <div className="flex justify-end gap-2 border-t border-slate-100 px-5 py-4">
              <button
                type="button"
                onClick={() => setRunDialog(null)}
                className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-xs font-bold text-slate-600 hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleRun}
                className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-bold text-white shadow-lg shadow-violet-100 hover:bg-violet-700"
              >
                Run Scenario
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        <h3 className="text-xl font-bold text-slate-800 flex items-center gap-2 shrink-0">
          <i className="fa-solid fa-diagram-project text-violet-500"></i>
          ODI Mappings
        </h3>
        <div className="flex gap-2 flex-1 max-w-sm">
          <div className="relative flex-1">
            <i className="fa-solid fa-search absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 text-xs"></i>
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search mappings..."
              className="w-full pl-8 pr-4 py-2 text-sm border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-violet-400"
            />
          </div>
          <button
            type="button"
            onClick={loadMappings}
            title="Refresh mappings"
            aria-label="Refresh mappings"
            className="inline-flex shrink-0 items-center gap-2 rounded-lg border border-violet-200 bg-white px-3 py-2 text-xs font-bold text-violet-600 transition-colors hover:bg-violet-50 hover:text-violet-700"
          >
            <i className="fa-solid fa-arrows-rotate text-[11px]"></i>
            Refresh
          </button>
        </div>
      </div>

      {loading ? (
        <div className="text-slate-400 p-12 flex flex-col items-center justify-center bg-white rounded-2xl border border-slate-100 italic">
          <i className="fa-solid fa-spinner fa-spin text-2xl mb-3 text-violet-400"></i>
          Loading...
        </div>
      ) : (
        <div className="flex flex-col lg:flex-row gap-4 h-[calc(100vh-280px)] min-h-[500px]">
          {/* Left: List (Premium Data Table) */}
          <div className="flex-1 bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden flex flex-col">
            <div className="overflow-y-auto flex-1 custom-scrollbar">
              <table className="w-full text-left border-collapse table-fixed excel-grid">
                <thead className="sticky top-0 bg-slate-50 z-20 shadow-sm">
                  <tr className="text-[10px] font-bold text-slate-500 uppercase tracking-wider border-b border-slate-200">
                    <th className="px-4 py-3 border-r border-slate-100 w-12 text-center text-slate-300">#</th>
                    <th className="px-4 py-3 border-r border-slate-100">
                      <div className="flex items-center gap-2">
                        <i className="fa-solid fa-file-invoice text-slate-400"></i>
                         MAPPING NAME
                      </div>
                    </th>
                    <th className="px-4 py-3 border-r border-slate-100 text-center w-20">Comps</th>
                    <th className="px-4 py-3 text-center w-20">Joins</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {filtered.map((m, idx) => {
                    const name = m.MAPPING_NAME || '';
                    const isSelected = detail?.MAPPING_NAME === name;
                    const status = runStatus[name];
                    return (
                      <tr
                        key={name}
                        onClick={() => loadDetail(name)}
                        className={clsx(
                          "cursor-pointer transition-all group border-l-4",
                          isSelected 
                            ? "bg-violet-50/70 border-l-violet-500" 
                            : "hover:bg-slate-50 border-l-transparent odd:bg-slate-50/20"
                        )}
                      >
                        <td className="px-4 py-3 border-r border-slate-50/50 text-center text-[10px] text-slate-300 font-mono">
                           {idx + 1}
                        </td>
                        <td className="px-4 py-3 border-r border-slate-50/50">
                          <div className="flex flex-col min-w-0">
                            <div className="flex items-center gap-2">
                               <span className={clsx("text-sm font-semibold truncate", isSelected ? "text-violet-700" : "text-slate-700")}>
                                  {name}
                               </span>
                               <div className="flex items-center gap-1 shrink-0">
                                  {m.LAYER && (
                                     <span className={clsx(
                                        "text-[8px] px-1.5 py-0.5 rounded font-black border uppercase shadow-sm mr-1",
                                        m.LAYER === 'ODS' ? "bg-emerald-50 text-emerald-600 border-emerald-100" :
                                        m.LAYER === 'SKEY' ? "bg-amber-50 text-amber-600 border-amber-100" :
                                        m.LAYER === 'EDW' ? "bg-blue-50 text-blue-600 border-blue-100" :
                                        m.LAYER === 'DM' ? "bg-rose-50 text-rose-600 border-rose-100" :
                                        "bg-slate-50 text-slate-500 border-slate-100"
                                     )}>
                                        {m.LAYER}
                                     </span>
                                  )}
                                  {m.HAS_EXP && <span className="text-[8px] bg-amber-100 text-amber-600 px-1 py-0.5 rounded font-black shadow-sm">EXP</span>}
                                  {m.HAS_FILTER && <span className="text-[8px] bg-blue-100 text-blue-600 px-1 py-0.5 rounded font-black shadow-sm">FLT</span>}
                                  {status === 'running' && <i className="fa-solid fa-spinner fa-spin text-violet-400 text-[10px]"></i>}
                               </div>
                            </div>
                            <span className="text-[9px] text-slate-400 font-medium mt-0.5 uppercase tracking-wide">
                               {m.DESC || 'DATA INTEGRATION MAPPING'}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-3 border-r border-slate-50/50 text-center">
                          <span className={clsx(
                             "text-[10px] px-2 py-0.5 rounded-md font-bold",
                             isSelected ? "bg-violet-100 text-violet-600" : "bg-slate-50 text-slate-400 border border-slate-100"
                          )}>
                             {m.COMP_COUNT || 0}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-center">
                          <span className={clsx(
                             "text-[10px] px-2 py-0.5 rounded-md font-bold",
                             (m.JOIN_COUNT > 0) ? "bg-blue-50 text-blue-500 border border-blue-100" : "text-slate-300"
                          )}>
                             {m.JOIN_COUNT || 0}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {filtered.length === 0 && (
                 <div className="p-12 text-center text-slate-400 italic text-sm"> No records found. </div>
              )}
            </div>
          </div>

          {/* Right: Details and actions */}
          <div className="w-full lg:w-[500px] flex flex-col gap-4">
            {detail ? (
              <div className="bg-white rounded-2xl border border-slate-200 shadow-xl p-6 flex flex-col h-full animate-slide-in-right overflow-hidden transition-all">
                <div className="flex items-start justify-between mb-6 shrink-0">
                  <div className="min-w-0">
                    <h4 className="text-lg font-bold text-slate-800 truncate mb-1">{detail.MAPPING_NAME}</h4>
                    <span className="text-[10px] font-bold text-violet-500 uppercase tracking-widest bg-violet-50 px-2 py-1 rounded-md"> MAPPING CONFIGURATION </span>
                  </div>
                  <button onClick={() => setDetail(null)} className="text-slate-300 hover:text-slate-600"> <i className="fa-solid fa-xmark"></i> </button>
                </div>

                <div className="flex-1 overflow-y-auto custom-scrollbar space-y-6 pr-1">
                  {lastRuns[detail.MAPPING_NAME] && (
                    <LastRunPanel
                      outcome={lastRuns[detail.MAPPING_NAME]}
                      onClose={() => closeLastRun(detail.MAPPING_NAME)}
                      onRefresh={() => refreshLastRun(detail.MAPPING_NAME)}
                    />
                  )}
                  {/* Metadata Stacked */}
                  <div className="flex flex-col gap-2">
                    <div className="p-3 rounded-xl bg-indigo-50/40 border border-indigo-100">
                      <div className="flex items-center gap-2 mb-1">
                         <i className="fa-solid fa-folder-tree text-[10px] text-indigo-400"></i>
                         <span className="text-[9px] font-bold text-indigo-600 uppercase">Project / Folder</span>
                      </div>
                      {(() => {
                        const ctx = mappingContext(detail);
                        return (
                          <div className="grid grid-cols-1 gap-1 text-xs">
                            <span className="font-mono font-bold text-indigo-800 bg-white p-1.5 rounded border border-indigo-100 shadow-sm" title="ODI project">
                              {ctx.project}
                            </span>
                            <span className="font-mono font-bold text-indigo-700 bg-white p-1.5 rounded border border-indigo-100 shadow-sm" title="ODI folder">
                              {ctx.folder}
                            </span>
                          </div>
                        );
                      })()}
                    </div>
                    <div className="p-3 rounded-xl bg-slate-50 border border-slate-100">
                      <div className="flex items-center gap-2 mb-1">
                         <i className="fa-solid fa-shapes text-[10px] text-slate-400"></i>
                         <span className="text-[9px] font-bold text-slate-400 uppercase">Oracle Source Tables</span>
                      </div>
                      <div className="flex flex-wrap gap-1">
                         {detail.SRC_TABLE?.split(', ').map((s, si) => (
                            <span key={si} className="text-xs font-mono font-bold text-slate-700 bg-white p-1.5 rounded border border-slate-200/50 shadow-sm" title={`ODI: ${s}`}>{displayDbTable(s)}</span>
                         ))}
                         {(!detail.SRC_TABLE || detail.SRC_TABLE === '?') && <span className="text-xs text-slate-300 italic">?</span>}
                      </div>
                    </div>
                    <div className="p-3 rounded-xl bg-emerald-50/30 border border-emerald-100">
                      <div className="flex items-center gap-2 mb-1">
                         <i className="fa-solid fa-bullseye text-[10px] text-emerald-400"></i>
                         <span className="text-[9px] font-bold text-emerald-600 uppercase">Oracle Target Table</span>
                      </div>
                      <span className="text-xs font-mono font-bold text-emerald-800 bg-white p-1.5 rounded border border-emerald-100 shadow-sm block" title={`ODI: ${detail.TGT_TABLE || '?'}`}>{displayDbTable(detail.TGT_TABLE)}</span>
                    </div>
                    {detail.km_options && (
                      <div className="p-3 rounded-xl bg-cyan-50/40 border border-cyan-100">
                        <div className="flex items-center gap-2 mb-1">
                           <i className="fa-solid fa-gears text-[10px] text-cyan-500"></i>
                           <span className="text-[9px] font-bold text-cyan-700 uppercase">ODI KM Options</span>
                        </div>
                        <div className="grid grid-cols-1 gap-1 text-xs">
                          <span className="font-mono font-bold text-cyan-800 bg-white p-1.5 rounded border border-cyan-100 shadow-sm">
                            TRUNCATE_TARGET_TABLE: {detail.km_options.truncate_target_table === true ? 'true' : detail.km_options.truncate_target_table === false ? 'false' : 'unknown'}
                          </span>
                          {(detail.km_options.ikm_name || detail.km_options.lkm_name) && (
                            <span className="font-mono text-[10px] text-cyan-700 bg-white p-1.5 rounded border border-cyan-100 shadow-sm break-all">
                              {detail.km_options.ikm_name || 'IKM unknown'} / {detail.km_options.lkm_name || 'LKM unknown'}
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>

                  {/* LangGraph Persistent State */}
                  <div className="p-4 rounded-xl bg-slate-50 border border-slate-100">
                    <div className="flex items-center justify-between gap-2 mb-3">
                      <span className="text-[9px] font-bold text-slate-500 uppercase tracking-wide">
                        LangGraph State
                      </span>
                      {stateLoading ? (
                        <span className="text-[10px] text-slate-400 flex items-center gap-1">
                          <i className="fa-solid fa-spinner fa-spin"></i> Checking
                        </span>
                      ) : mappingState ? (() => {
                        const st = String(mappingState.overall_status || '').toUpperCase();
                        const ok = ['SUCCEEDED', 'SUCCESS', 'ODI_SYNCED', 'IMPACT_READY', 'RUN_READY'].includes(st);
                        const bad = ['FAILED', 'DQ_FAILED', 'RUN_FAILED', 'ERROR'].includes(st);
                        return (
                          <span className={clsx(
                            "text-[9px] px-2 py-1 rounded-full font-black uppercase",
                            ok ? "bg-emerald-100 text-emerald-700"
                              : bad ? "bg-rose-100 text-rose-700"
                              : "bg-violet-100 text-violet-700"
                          )}>
                            {st || 'state'}
                          </span>
                        );
                      })() : (
                        <span className="text-[9px] px-2 py-1 rounded-full bg-slate-100 text-slate-400 font-bold">
                          NO STATE
                        </span>
                      )}
                    </div>
                    {mappingState ? (
                      <div className="grid grid-cols-2 gap-2 text-[10px]">
                        <div className="bg-white rounded-lg border border-slate-100 p-2">
                          <div className="text-slate-400 font-bold uppercase mb-1">Last Step</div>
                          <div className="font-mono text-slate-700 break-all">{mappingState.current_step || '-'}</div>
                        </div>
                        <div className="bg-white rounded-lg border border-slate-100 p-2">
                          <div className="text-slate-400 font-bold uppercase mb-1">Batch</div>
                          <div className="font-mono text-slate-700 break-all">{mappingState.batch_id || '-'}</div>
                        </div>
                        <div className="col-span-2 bg-white rounded-lg border border-slate-100 p-2">
                          <div className="text-slate-400 font-bold uppercase mb-1">Completed</div>
                          <div className="flex flex-wrap gap-1">
                            {(mappingState.completed_steps || []).length > 0
                              ? mappingState.completed_steps.map(step => (
                                <span key={step} className="px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-bold">
                                  {step}
                                </span>
                              ))
                              : <span className="text-slate-300 italic">None yet</span>}
                          </div>
                        </div>
                        {mappingState.error && (
                          <div className="col-span-2 bg-rose-50 rounded-lg border border-rose-100 p-2 text-rose-700">
                            {mappingState.error}
                          </div>
                        )}
                      </div>
                    ) : (
                      <p className="text-[10px] text-slate-400 italic">
                        No persistent LangGraph state record was found for this mapping.
                      </p>
                    )}
                  </div>

                  {/* Filters */}
                  {detail.filters && detail.filters.length > 0 && (
                    <div className="p-4 rounded-xl bg-blue-50/50 border border-blue-100">
                      <span className="text-[9px] font-bold text-blue-400 uppercase block mb-2">Active Filters</span>
                      <div className="space-y-2">
                        {detail.filters.map((f, fi) => (
                          <div key={fi} className="text-[10px] font-mono text-blue-700 bg-white p-2 rounded border border-blue-50 shadow-sm leading-relaxed break-all"> {f} </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Actions */}
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={(e) => openRunDialog(detail.MAPPING_NAME, e)}
                      disabled={runStatus[detail.MAPPING_NAME] === 'running'}
                      title="Choose the target run mode and run the ODI scenario"
                      className="flex items-center justify-center gap-2 py-2.5 rounded-xl bg-violet-600 text-white text-xs font-bold hover:bg-violet-700 transition-all active:scale-95 shadow-lg shadow-violet-100"
                    >
                      <i className="fa-solid fa-play text-[10px]"></i> Run Scenario
                    </button>
                    <button
                      onClick={(e) => handleEditKm(detail, e)}
                      title="Open KM options for this mapping"
                      className="flex items-center justify-center gap-2 py-2.5 rounded-xl bg-cyan-600 text-white text-xs font-bold hover:bg-cyan-700 transition-all active:scale-95 shadow-lg shadow-cyan-100"
                    >
                      <i className="fa-solid fa-gears text-[10px]"></i> KM Options
                    </button>
                    <button onClick={(e) => handleEdit(detail, e)} className="flex items-center justify-center gap-2 py-2.5 rounded-xl bg-amber-500 text-white text-xs font-bold hover:bg-amber-600 transition-all active:scale-95 shadow-lg shadow-amber-100">
                      <i className="fa-solid fa-pen-nib text-[10px]"></i> Edit
                    </button>
                    <button onClick={(e) => handleDelete(detail.MAPPING_NAME, e)} className="flex items-center justify-center gap-2 py-2.5 rounded-xl border border-rose-200 bg-rose-50 text-rose-500 text-xs font-bold hover:bg-rose-100 hover:text-rose-600 transition-all active:scale-95 shadow-sm">
                      <i className="fa-solid fa-trash-can text-[10px]"></i> Delete
                    </button>
                  </div>

                  {/* Columns */}
                  <div>
                    <p className="text-xs font-bold text-slate-500 uppercase mb-2 flex items-center justify-between"> COLUMN MAPPINGS <span className="text-[10px] bg-slate-100 px-2 py-0.5 rounded-full">{detail.columns?.length || 0}</span> </p>
                    <div className="border border-slate-100 rounded-xl overflow-hidden excel-grid bg-white shadow-sm">
                      <table className="w-full text-left text-[11px] border-collapse table-fixed">
                        <thead className="bg-slate-50 border-b border-slate-100">
                          <tr>
                            <th className="px-3 py-2 text-slate-400 w-[60%] border-r border-slate-50 text-[10px]">SOURCE</th>
                            <th className="px-3 py-2 text-slate-400 text-[10px]">TARGET</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-50">
                          {detail.columns?.map((c, i) => (
                            <tr key={i} className="group transition-colors">
                              <td className="px-3 py-2 font-mono text-slate-500 break-all border-r border-slate-50/50">
                                <div className="flex items-start gap-1.5">
                                  {c.is_exp && <span className="mt-0.5 text-[8px] bg-amber-50 text-amber-600 px-1 py-0 rounded border border-amber-100 font-black shrink-0">EXP</span>}
                                  <span className={clsx(c.is_exp ? "text-amber-800" : "text-slate-500")}>{c.src}</span>
                                </div>
                              </td>
                              <td className="px-3 py-2 font-mono font-bold text-slate-700">{c.tgt}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="bg-slate-50 rounded-2xl border border-dashed border-slate-200 h-full flex flex-col items-center justify-center p-8 text-center text-slate-400 italic">
                <i className="fa-solid fa-list-check text-4xl mb-4 text-slate-200"></i> Select a mapping from the left to view details.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
