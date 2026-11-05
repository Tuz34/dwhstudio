import { useState, useEffect, useCallback } from 'react';
import { getDqScores, reconcileTable, autofixColumn, autofixAllColumns, syncOdiMapping } from '../../services/api';
import clsx from 'clsx';
import {
  buildAutofixAllRequest,
  buildAutofixColumnRequest,
  buildSyncMappingRequest,
} from './dqActionRequests';
import { dqScoreDisplay } from './dqScoreDisplay';

function severityClass(severity) {
  if (severity === 'ERROR') return 'bg-rose-50/40 text-rose-600 border-rose-100';
  if (severity === 'WARNING') return 'bg-amber-50/40 text-amber-600 border-amber-100';
  return 'bg-blue-50/40 text-blue-600 border-blue-100';
}

function needsOdiSync(finding) {
  return finding?.type === 'UNMAPPED_COLUMN' ||
    finding?.action === 'SYNC_ODI_MAPPING' ||
    ['DATA_NULL_MISMATCH', 'DATA_VALUE_MISMATCH', 'MISSING_TARGET_ROW'].includes(finding?.type);
}

export function DqDashboard() {
  const [scores, setScores] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reconModal, setReconModal] = useState({ open: false, data: null, loading: false, table: null });
  const [fixingColumn, setFixingColumn] = useState(null);
  const [fixingAll, setFixingAll] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const loadScores = useCallback(async () => {
    try {
      setLoading(true);
      const data = await Promise.race([
        getDqScores({ owner: 'DWH' }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('DQ score endpoint timed out.')), 8000)),
      ]);
      if (data.error) throw new Error(data.error);
      setScores(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = setTimeout(loadScores, 0);
    return () => clearTimeout(id);
  }, [loadScores]);

  const handleReconcile = async (tableName) => {
    setReconModal({ open: true, data: null, loading: true, table: tableName });
    try {
      const res = await reconcileTable({ owner: 'DWH', table: tableName });
      if (res.error) throw new Error(res.error);
      setReconModal(p => ({ ...p, data: res, loading: false }));
    } catch (e) {
      setReconModal(p => ({ ...p, data: { error: e.message }, loading: false }));
    }
  };

  const handleAutoFix = async (columnName) => {
    if (!window.confirm(`${columnName} column should be added to the target Oracle table and the mapping updated?`)) return;
    try {
      setFixingColumn(columnName);
      const res = await autofixColumn(buildAutofixColumnRequest(reconModal.table, columnName));
      if (res.error) throw new Error(res.error);
      alert(`Success: ${res.message}`);
      handleReconcile(reconModal.table);
    } catch (e) {
      alert(`Error: ${e.message}`);
    } finally {
      setFixingColumn(null);
    }
  };

  const handleSyncMapping = async () => {
    if (!window.confirm(`${reconModal.table} ODI mapping synchronization should run?`)) return;
    try {
      setSyncing(true);
      const res = await syncOdiMapping(buildSyncMappingRequest(reconModal.table));
      if (res.error) throw new Error(res.error);
      alert(`Success: ${res.message}`);
      handleReconcile(reconModal.table);
    } catch (e) {
      alert(`Error: ${e.message}`);
    } finally {
      setSyncing(false);
    }
  };

  const handleAutoFixAll = async () => {
    const missingCols = reconModal.data?.findings
      ?.filter(f => f.type === 'MAPPED_OR_EXCLUDED' && f.message.includes('Excluded'))
      ?.map(f => f.column) || [];

    if (missingCols.length === 0) return;
    if (!window.confirm(`${missingCols.length} columns should be added to the target Oracle table?`)) return;

    try {
      setFixingAll(true);
      const res = await autofixAllColumns(buildAutofixAllRequest(reconModal.table, missingCols));
      if (res.error) throw new Error(res.error);
      alert(`Bulk add succeeded: ${res.message}`);
      handleReconcile(reconModal.table);
    } catch (e) {
      alert(`Error: ${e.message}`);
    } finally {
      setFixingAll(false);
    }
  };

  const exportToCsv = () => {
    if (!reconModal.data || !reconModal.data.findings) return;
    const { source, target, findings } = reconModal.data;
    let csv = `Reconciliation Report\nSource Oracle,${source}\nTarget Oracle,${target}\n\nTYPE,SEVERITY,COLUMN,MESSAGE\n`;
    findings.forEach(f => {
      csv += `${f.type},${f.severity},${f.column || ''},"${String(f.message || '').replace(/"/g, '""')}"\n`;
    });
    const blob = new Blob([new Uint8Array([0xEF, 0xBB, 0xBF]), csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `Reconciliation_${reconModal.table}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  if (loading) {
    return (
      <div className="text-slate-400 p-8 flex items-center justify-center animate-pulse italic">
        <i className="fa-solid fa-spinner fa-spin mr-2 text-indigo-500" />
        Loading DQ scores...
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-red-500 p-8 bg-rose-50 rounded-xl border border-rose-100 font-bold m-6">
        Error: {error}
      </div>
    );
  }

  return (
    <div className="flex gap-5 h-full animate-fade-in w-full">
      <div className="flex-1 flex flex-col gap-4 min-w-0">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 className="text-lg font-bold text-slate-800 flex items-center gap-2">
              <i className="fa-solid fa-code-compare text-indigo-500" />
              DQ and Reconciliation
            </h3>
            <p className="mt-1 text-xs text-slate-500">
              Score and difference checks for Oracle source and DWH target tables.
            </p>
          </div>
          <button
            onClick={loadScores}
            className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-slate-500 hover:border-indigo-200 hover:text-indigo-600"
          >
            <i className="fa-solid fa-arrows-rotate mr-1.5" />
            Refresh
          </button>
        </div>

        {scores.length === 0 ? (
          <div className="text-slate-400 text-center py-20 bg-white rounded-2xl border border-slate-100 italic">
            <i className="fa-solid fa-clipboard-check mb-3 text-3xl text-slate-200 block" />
            No calculated DQ score yet.
          </div>
        ) : (
          <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">
                  <th className="px-5 py-3 w-64">DWH Table</th>
                  <th className="px-4 py-3 w-32 text-center">Health Score</th>
                  <th className="px-5 py-3 w-40 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {scores.map((d, i) => {
                  const display = dqScoreDisplay(d.score);
                  return (
                    <tr key={i} className="hover:bg-slate-50/60 transition-colors h-14">
                      <td className="px-5 py-3">
                        <span className="text-sm font-semibold text-slate-700 truncate" title={d.table}>
                          {String(d.table || '').replace(/^DWH\./, '')}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-col items-center gap-1">
                          <div className="w-24 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                            <div className={clsx('h-full transition-all duration-700', display.bar)} style={{ width: display.width }} />
                          </div>
                          <span className={clsx('text-[11px] font-black', display.text)}>{display.label}</span>
                        </div>
                      </td>
                      <td className="px-5 py-3 text-right">
                        <button
                          onClick={() => handleReconcile(d.table)}
                          className="px-3 py-1.5 rounded-lg border border-indigo-200 text-indigo-600 text-[10px] font-bold hover:bg-indigo-600 hover:text-white transition-all flex items-center gap-1.5 ml-auto active:scale-95"
                          title="Compare source and target Oracle tables"
                        >
                          <i className="fa-solid fa-code-compare" />
                          Reconcile
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {reconModal.open && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-[100] flex items-center justify-center p-4">
          <div
            aria-labelledby="dq-reconciliation-title"
            aria-modal="true"
            className="bg-white rounded-2xl shadow-2xl w-[900px] max-w-full flex flex-col max-h-[90vh] overflow-hidden animate-slide-up"
            role="dialog"
          >
            <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between bg-slate-50">
              <div>
                <h3 id="dq-reconciliation-title" className="text-lg font-black text-slate-800 flex items-center gap-2">
                  <i className="fa-solid fa-code-compare text-indigo-500" />
                  Reconciliation Check
                </h3>
                <p className="text-xs text-slate-500 font-medium mt-1">
                  Target Oracle table: <span className="font-bold text-slate-700">{reconModal.table}</span>
                </p>
              </div>
              <button
                aria-label="Close reconciliation dialog"
                type="button"
                onClick={() => setReconModal({ open: false, data: null, loading: false, table: null })}
                className="w-8 h-8 rounded-full hover:bg-slate-200 text-slate-400 hover:text-slate-600 transition-colors"
              >
                <i className="fa-solid fa-xmark" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-6 bg-slate-50/50">
              {reconModal.loading ? (
                <div className="flex flex-col items-center justify-center py-20 text-indigo-500">
                  <i className="fa-solid fa-circle-notch fa-spin text-4xl mb-4" />
                  <span className="text-sm font-bold animate-pulse">Comparing Oracle source and target...</span>
                </div>
              ) : reconModal.data?.error ? (
                <div className="bg-rose-50 text-rose-600 p-4 rounded-xl border border-rose-200 font-bold text-sm">
                  <i className="fa-solid fa-triangle-exclamation mr-2" />
                  {reconModal.data.error}
                </div>
              ) : reconModal.data && (
                <div className="space-y-6">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
                      <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Source (Oracle)</span>
                      <div className="text-sm font-bold text-slate-800 mt-1 truncate">{reconModal.data.source}</div>
                      <div className="text-2xl font-black text-indigo-600 mt-2">
                        {reconModal.data.source_count?.toLocaleString() || 0}
                        <span className="ml-1 text-xs font-bold text-slate-400">rows</span>
                      </div>
                    </div>
                    <div className="bg-white border border-slate-200 rounded-xl p-4 shadow-sm">
                      <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Target (Oracle DWH)</span>
                      <div className="text-sm font-bold text-slate-800 mt-1 truncate">{reconModal.data.target}</div>
                      <div className="text-2xl font-black text-emerald-600 mt-2">
                        {reconModal.data.target_count?.toLocaleString() || 0}
                        <span className="ml-1 text-xs font-bold text-slate-400">rows</span>
                      </div>
                    </div>
                  </div>

                  <div>
                    <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
                      <h4 className="text-sm font-bold text-slate-700 uppercase tracking-widest">
                        Check Findings ({reconModal.data.findings?.length || 0})
                      </h4>
                      <div className="flex flex-wrap items-center gap-2">
                        {reconModal.data.findings?.some(f => f.type === 'MAPPED_OR_EXCLUDED' && f.message.includes('Excluded')) && (
                          <button
                            onClick={handleAutoFixAll}
                            disabled={fixingAll || syncing}
                            className={clsx('text-xs font-bold px-3 py-1.5 rounded-lg transition-colors border flex items-center gap-1.5',
                              fixingAll ? 'bg-indigo-50 text-indigo-400 border-indigo-100 cursor-not-allowed' : 'text-white bg-indigo-600 hover:bg-indigo-700 border-indigo-600 shadow-sm')}
                          >
                            {fixingAll ? <><i className="fa-solid fa-spinner fa-spin" /> Processing...</> : <><i className="fa-solid fa-plus-circle" /> Add Missing Columns to DB</>}
                          </button>
                        )}
                        {reconModal.data.findings?.some(needsOdiSync) && (
                          <button
                            onClick={handleSyncMapping}
                            disabled={fixingAll || syncing}
                            className={clsx('text-xs font-bold px-3 py-1.5 rounded-lg transition-colors border flex items-center gap-1.5',
                              syncing ? 'bg-rose-50 text-rose-400 border-rose-100 cursor-not-allowed' : 'text-white bg-rose-600 hover:bg-rose-700 border-rose-600 shadow-sm')}
                          >
                            {syncing ? <><i className="fa-solid fa-spinner fa-spin" /> Syncing...</> : <><i className="fa-solid fa-rotate" /> Sync to ODI</>}
                          </button>
                        )}
                        <button
                          onClick={exportToCsv}
                          className="text-xs font-bold text-emerald-600 bg-emerald-50 hover:bg-emerald-100 px-3 py-1.5 rounded-lg transition-colors border border-emerald-100 flex items-center gap-1.5"
                          title="Download as CSV; opens in Excel"
                        >
                          <i className="fa-solid fa-file-csv" /> Download CSV
                        </button>
                      </div>
                    </div>

                    <div className="border border-slate-200 rounded-xl overflow-hidden bg-white shadow-sm">
                      {reconModal.data.findings?.length === 0 ? (
                        <div className="text-center py-10 bg-white text-emerald-600">
                          <i className="fa-solid fa-check-circle text-3xl mb-2 block" />
                          No differences found.
                        </div>
                      ) : (
                        <div className="overflow-x-auto max-h-[40vh] overflow-y-auto">
                          <table className="w-full text-left border-collapse whitespace-nowrap">
                            <thead className="bg-slate-50 border-b border-slate-200 sticky top-0 z-10">
                              <tr className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">
                                <th className="px-4 py-3 w-10 text-center border-r border-slate-200">Status</th>
                                <th className="px-4 py-3 border-r border-slate-200">Column</th>
                                <th className="px-4 py-3 border-r border-slate-200">Finding Type</th>
                                <th className="px-4 py-3 w-full border-r border-slate-200">Detail</th>
                                <th className="px-4 py-3 text-center">Action</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 text-xs">
                              {reconModal.data.findings?.map((f, i) => (
                                <tr key={i} className={clsx('hover:bg-slate-50/50 transition-colors border-l-4', severityClass(f.severity))}>
                                  <td className="px-4 py-3 text-center border-r border-slate-100">
                                    <i className={clsx('fa-solid text-sm',
                                      f.severity === 'ERROR' ? 'fa-circle-xmark text-rose-500' :
                                      f.severity === 'WARNING' ? 'fa-triangle-exclamation text-amber-500' :
                                      'fa-circle-info text-blue-500'
                                    )} title={f.severity} />
                                  </td>
                                  <td className="px-4 py-3 font-bold text-slate-700 border-r border-slate-100">{f.column || '-'}</td>
                                  <td className="px-4 py-3 border-r border-slate-100">
                                    <span className="text-[9px] font-black px-1.5 py-0.5 rounded uppercase bg-slate-100 text-slate-700">
                                      {f.type}
                                    </span>
                                  </td>
                                  <td className="px-4 py-3 text-slate-600 whitespace-normal border-r border-slate-100">
                                    <div>{f.message}</div>
                                    {Number.isInteger(f.affected_count) && (
                                      <div className="mt-2 text-[10px] font-semibold text-slate-500">
                                        Affected sampled rows: {f.affected_count}
                                      </div>
                                    )}
                                  </td>
                                  <td className="px-4 py-3 text-center whitespace-nowrap">
                                    {needsOdiSync(f) && (
                                      <button
                                        onClick={handleSyncMapping}
                                        disabled={syncing}
                                        className={clsx('px-3 py-1.5 rounded-lg text-[10px] font-bold transition-all flex items-center gap-1.5 mx-auto mb-1',
                                          syncing ? 'bg-rose-100 text-rose-400 cursor-not-allowed' : 'border border-rose-200 text-rose-600 hover:bg-rose-600 hover:text-white active:scale-95')}
                                      >
                                        {syncing ? <><i className="fa-solid fa-spinner fa-spin" /> Syncing...</> : <><i className="fa-solid fa-rotate" /> Sync to ODI</>}
                                      </button>
                                    )}
                                    {f.type === 'MAPPED_OR_EXCLUDED' && f.message.includes('Excluded') && (
                                      <button
                                        onClick={() => handleAutoFix(f.column)}
                                        disabled={fixingColumn === f.column}
                                        className={clsx('px-3 py-1.5 rounded-lg text-[10px] font-bold transition-all flex items-center gap-1.5 mx-auto',
                                          fixingColumn === f.column ? 'bg-indigo-100 text-indigo-400 cursor-not-allowed' : 'border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white active:scale-95')}
                                      >
                                        {fixingColumn === f.column ? <><i className="fa-solid fa-spinner fa-spin" /> Adding...</> : <><i className="fa-solid fa-wand-magic-sparkles" /> Add and Map</>}
                                      </button>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div className="px-6 py-4 bg-white border-t border-slate-100 flex justify-end">
              <button
                onClick={() => setReconModal({ open: false, data: null, loading: false, table: null })}
                className="px-5 py-2 rounded-xl bg-slate-800 hover:bg-slate-900 text-white text-xs font-bold transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
