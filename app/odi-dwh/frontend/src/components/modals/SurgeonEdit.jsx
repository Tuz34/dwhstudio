import { useEffect, useState } from 'react';
import { Modal } from '../ui/Modal';
import { getOdiMappingDetail, updateMapping } from '../../services/api';
import { useDispatch } from '../../store/AppContext';
import clsx from 'clsx';

function initialTruncateValue(initialTruncateTargetTable) {
  if (initialTruncateTargetTable === true) return 'true';
  if (initialTruncateTargetTable === false) return 'false';
  return '';
}

function applyKmReadback(detail, setForm, setKmReadback) {
  if (!detail?.km_options?.read_only) return;
  const kmOptions = detail.km_options;
  setKmReadback(kmOptions);
  const truncateValue = initialTruncateValue(detail?.km_options?.truncate_target_table);
  setForm(p => ({
    ...p,
    truncate_target_table: truncateValue || p.truncate_target_table,
    ikm_name: kmOptions.ikm_name || p.ikm_name,
    lkm_name: kmOptions.lkm_name || p.lkm_name,
  }));
}

function expressionValue(row) {
  return row.expression ?? row.src ?? '';
}

export function SurgeonEdit({ open, onClose, mapping = {}, initialOperation, initialTruncateTargetTable }) {
  const dispatch = useDispatch();
  const [op, setOp] = useState(initialOperation || 'update_expression'); // update_expression, update_filter, update_km, rename
  const [form, setForm] = useState({
    mapping_name: mapping.MAPPING_NAME || '',
    expressions:  '',
    filter_action: 'add',
    filter_condition: '',
    ikm_name: '',
    lkm_name: '',
    truncate_target_table: initialTruncateValue(initialTruncateTargetTable),
    new_mapping_name: '',
    rename_source_table_name: '',
    rename_target_table_name: '',
    rename_column_side: 'target',
    rename_column_old_name: '',
    rename_column_new_name: '',
    tgt_table: mapping.TGT_TABLE || '',
    tgt_owner: mapping.TGT_OWNER || 'DWH',
    description: '',
  });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expressionRows, setExpressionRows] = useState([]);
  const [sourceColumns, setSourceColumns] = useState([]);
  const [kmReadback, setKmReadback] = useState(null);

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }));
  const setExpression = (index, value) => {
    setExpressionRows(rows => rows.map((row, idx) => (
      idx === index ? { ...row, expression: value } : row
    )));
  };
  const insertFilterColumnReference = (reference) => {
    setForm(p => {
      const current = p.filter_condition || '';
      const separator = current && !/\s$/.test(current) ? ' ' : '';
      return { ...p, filter_condition: `${current}${separator}${reference}` };
    });
  };

  const changedExpressionRows = expressionRows.filter(row => (
    expressionValue(row).trim() !== (row.original_expression ?? '').trim()
  ));
  const hasPendingChanges = (() => {
    if (op === 'update_expression') return changedExpressionRows.length > 0;
    if (op === 'update_filter') return form.filter_action === 'remove' || Boolean(form.filter_condition.trim());
    if (op === 'update_km') return Boolean(form.ikm_name.trim() || form.lkm_name.trim() || form.truncate_target_table !== '');
    if (op === 'rename') {
      return Boolean(
        form.new_mapping_name.trim()
        || form.rename_source_table_name.trim()
        || form.rename_target_table_name.trim()
        || form.rename_column_old_name.trim()
        || form.rename_column_new_name.trim()
      );
    }
    return true;
  })();

  useEffect(() => {
    if (!open) return;
    const mappingName = mapping.MAPPING_NAME || '';
    if (!mappingName) return;

    let cancelled = false;
    getOdiMappingDetail(mappingName)
      .then(detail => {
        if (cancelled || detail?.error) return;
        const rows = (detail.expression_rows || (detail.columns || []).map(col => ({
          target_column: col.tgt,
          expression: col.src,
          data_type: col.type,
          is_expression: col.is_exp,
        }))).map(row => ({
          ...row,
          original_expression: expressionValue(row),
        }));
        setExpressionRows(rows);
        setSourceColumns(detail.source_columns || []);
        setForm(p => ({
          ...p,
          filter_condition: (detail.filters || [])[0] || p.filter_condition,
        }));
        applyKmReadback(detail, setForm, setKmReadback);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [open, mapping.MAPPING_NAME, mapping.TGT_OWNER, mapping.TGT_TABLE]);

  const handleUpdate = async () => {
    setLoading(true); setError(null);
    try {
      const changes = { [op]: true }; // Trigger operation type on backend
      
      if (op === 'update_expression') {
        if (!window.confirm('Expression update modifies ODI repository metadata. Continue?')) {
          setLoading(false);
          return;
        }
        const exprMap = {};
        changedExpressionRows.forEach(row => {
          const target = row.target_column || row.tgt;
          if (target) exprMap[target.trim()] = expressionValue(row).trim();
        });
        changes.expressions = exprMap;
      } else if (op === 'update_filter') {
        changes.filter_action = form.filter_action;
        changes.filter_condition = form.filter_condition;
      } else if (op === 'update_km') {
        if (!window.confirm('KM update modifies ODI repository metadata. Continue?')) {
          setLoading(false);
          return;
        }
        changes.ikm_name = form.ikm_name;
        changes.lkm_name = form.lkm_name;
        if (form.truncate_target_table !== '') {
          changes.truncate_target_table = form.truncate_target_table === 'true';
        }
      } else if (op === 'rename') {
        if (!window.confirm('Rename updates ODI repository metadata. Continue?')) {
          setLoading(false);
          return;
        }
        if (form.new_mapping_name) changes.new_mapping_name = form.new_mapping_name;
        if (form.rename_source_table_name) changes.rename_source_table_name = form.rename_source_table_name;
        if (form.rename_target_table_name) changes.rename_target_table_name = form.rename_target_table_name;
        if (form.rename_column_old_name || form.rename_column_new_name) {
          changes.rename_column_side = form.rename_column_side;
          changes.rename_column_old_name = form.rename_column_old_name;
          changes.rename_column_new_name = form.rename_column_new_name;
        }
      }

      if (form.tgt_table) changes.tgt_table = form.tgt_table;
      changes.tgt_owner = form.tgt_owner;

      const data = await updateMapping({ mapping_name: form.mapping_name, changes, approved: true });
      if (data.error) throw new Error(data.error);
      
      const successMessage = op === 'update_expression'
        ? 'Mapping metadata updated. Regenerate the ODI scenario before expecting runtime behavior to change.'
        : data.message;
      dispatch({ type: 'SHOW_TOAST', payload: { message: '✅ ' + successMessage, type: 'success' } });
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="🔪 Surgeon Edit (Advanced)" size="lg">
      <div className="space-y-6">
        {/* Op Selector */}
        <div className="grid grid-cols-4 gap-2">
          {[
            { id: 'update_expression', label: 'Expression', icon: 'fa-code' },
            { id: 'update_filter',     label: 'Filter',     icon: 'fa-filter' },
            { id: 'update_km',         label: 'KM',         icon: 'fa-gears' },
            { id: 'rename',            label: 'Rename',     icon: 'fa-pen' },
          ].map(o => (
            <button
              key={o.id}
              onClick={() => setOp(o.id)}
              className={clsx(
                "flex flex-col items-center gap-1.5 p-3 rounded-xl border transition-all",
                op === o.id ? "bg-amber-50 border-amber-400 text-amber-700 shadow-sm"
                            : "bg-white border-slate-200 text-slate-500 hover:border-amber-200"
              )}
            >
              <i className={`fa-solid ${o.icon}`}></i>
              <span className="text-[10px] font-bold uppercase">{o.label}</span>
            </button>
          ))}
        </div>

        {/* Common Fields */}
        <div className="p-4 bg-slate-50 rounded-2xl border border-slate-100 flex items-center justify-between gap-4">
           <div>
             <span className="text-[10px] font-bold text-slate-400 uppercase block mb-1">Target Schema</span>
             <input value={form.tgt_owner} onChange={e => set('tgt_owner', e.target.value)} className="bg-transparent text-sm font-bold text-slate-700 border-none p-0 focus:ring-0 w-24" />
           </div>
           <div className="flex-1 border-l border-slate-200 pl-4 text-right">
             <span className="text-[10px] font-bold text-slate-400 uppercase block mb-1">MAPPING: {form.mapping_name}</span>
             <span className="text-sm font-mono text-slate-600">{form.tgt_table || 'N/A'}</span>
           </div>
        </div>

        {/* Dynamic Fields */}
        <div className="min-h-[160px]">
          {op === 'update_expression' && (
            <div className="space-y-3">
              <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                <div className="font-bold text-amber-900">Current Column Link State</div>
                <p className="mt-1">Only existing target-column expressions can be edited here.</p>
                <p className="mt-1">This changes ODI repository metadata only. It does not regenerate or run a scenario.</p>
                <p className="mt-1 font-semibold">Regenerate the ODI scenario before expecting runtime behavior to change.</p>
                <p className="mt-1">Adding target attributes, connecting datastores, and rebuilding joins are not supported in this editor.</p>
              </div>
              <div className="overflow-hidden rounded-xl border border-slate-200">
                <div className="grid grid-cols-[180px_1fr_120px] bg-slate-50 text-[10px] font-bold uppercase text-slate-500">
                  <div className="px-3 py-2">Target Column</div>
                  <div className="px-3 py-2 border-l border-slate-200">Expression</div>
                  <div className="px-3 py-2 border-l border-slate-200">Data Type</div>
                </div>
                <div className="max-h-72 overflow-auto divide-y divide-slate-100">
                  {expressionRows.length === 0 && (
                    <div className="px-3 py-4 text-xs text-slate-400">No expressions found.</div>
                  )}
                  {expressionRows.map((row, idx) => (
                    <div key={`${row.target_column || row.tgt}-${idx}`} className="grid grid-cols-[180px_1fr_120px] items-center">
                      <div className="px-3 py-2 font-mono text-xs font-semibold text-slate-700 truncate">{row.target_column || row.tgt}</div>
                      <input
                        value={row.expression || row.src || ''}
                        onChange={e => setExpression(idx, e.target.value)}
                        onInput={e => setExpression(idx, e.currentTarget.value)}
                        onBlur={e => setExpression(idx, e.currentTarget.value)}
                        className="min-w-0 border-l border-slate-100 px-3 py-2 font-mono text-xs text-slate-700 outline-none focus:bg-amber-50"
                      />
                      <div className="border-l border-slate-100 px-3 py-2 font-mono text-[11px] text-slate-500 truncate">{row.data_type || row.type || 'N/A'}</div>
                    </div>
                  ))}
                </div>
              </div>
              <div className="flex items-center justify-between text-[11px] text-slate-500">
                <span>Changed expressions: {changedExpressionRows.length}</span>
                {changedExpressionRows.length === 0 && <span>No expression changes yet.</span>}
              </div>
            </div>
          )}

          {op === 'update_filter' && (
            <div className="space-y-4">
               <div className="overflow-hidden rounded-xl border border-slate-200">
                 <div className="grid grid-cols-[160px_1fr_110px] bg-slate-50 text-[10px] font-bold uppercase text-slate-500">
                   <div className="px-3 py-2">Component</div>
                   <div className="px-3 py-2 border-l border-slate-200">Source Column</div>
                   <div className="px-3 py-2 border-l border-slate-200">Data Type</div>
                 </div>
                 <div className="max-h-36 overflow-auto divide-y divide-slate-100">
                   {sourceColumns.length === 0 && <div className="px-3 py-3 text-xs text-slate-400">No source columns found.</div>}
                   {sourceColumns.map((col, idx) => (
                     <div key={`${col.component}-${col.column}-${idx}`} className="grid grid-cols-[160px_1fr_110px] text-xs">
                       <div className="px-3 py-2 font-mono text-slate-500 truncate">{col.component}</div>
                       <button
                         type="button"
                         onClick={() => insertFilterColumnReference(`${col.component}.${col.column}`)}
                         className="border-l border-slate-100 px-3 py-2 text-left font-mono font-semibold text-slate-700 hover:bg-amber-50 truncate"
                       >
                         {col.column}
                       </button>
                       <div className="border-l border-slate-100 px-3 py-2 font-mono text-[11px] text-slate-500 truncate">{col.data_type || 'N/A'}</div>
                     </div>
                   ))}
                 </div>
               </div>
               <div className="flex gap-4">
                 {['add', 'modify', 'remove'].map(a => (
                   <label key={a} className="flex items-center gap-2 cursor-pointer">
                     <input type="radio" checked={form.filter_action === a} onChange={() => set('filter_action', a)} className="text-amber-500 focus:ring-amber-500" />
                     <span className="text-xs capitalize text-slate-600">{a}</span>
                   </label>
                 ))}
               </div>
               {form.filter_action !== 'remove' && (
                 <textarea
                   value={form.filter_condition}
                   onChange={e => set('filter_condition', e.target.value)}
                   placeholder="SRC.SALES_AMOUNT > 0"
                   className="w-full h-24 p-4 rounded-xl border border-slate-200 font-mono text-xs focus:ring-2 focus:ring-amber-400 focus:outline-none"
                 ></textarea>
               )}
            </div>
          )}

          {op === 'update_km' && (
            <div className="grid grid-cols-1 gap-4">
               <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">IKM Name</label>
                  <input value={form.ikm_name} onChange={e => set('ikm_name', e.target.value)} placeholder="IKM Oracle Merge" className="w-full p-2.5 rounded-xl border border-slate-200 text-sm" />
               </div>
               <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">LKM Name</label>
                  <input value={form.lkm_name} onChange={e => set('lkm_name', e.target.value)} placeholder="LKM SQL to Oracle" className="w-full p-2.5 rounded-xl border border-slate-200 text-sm" />
               </div>
               <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">TRUNCATE_TARGET_TABLE</label>
                  <select
                    value={form.truncate_target_table}
                    onChange={e => set('truncate_target_table', e.target.value)}
                    className="w-full p-2.5 rounded-xl border border-slate-200 text-sm"
                  >
                    <option value="">No change</option>
                    <option value="true">Truncate target before run</option>
                    <option value="false">Append without truncate</option>
                  </select>
                  {kmReadback?.read_only && (
                    <p className="mt-1 text-[11px] text-cyan-700">
                      Current ODI repository value: {kmReadback.truncate_target_table === true ? 'true' : kmReadback.truncate_target_table === false ? 'false' : 'unknown'}
                    </p>
                  )}
                  <p className="mt-1 text-[11px] text-slate-500">This saves the ODI KM option only. It does not run the scenario.</p>
               </div>
            </div>
          )}

          {op === 'rename' && (
            <div className="space-y-4">
              <div>
                <label className="text-xs font-bold text-slate-500 mb-1 block">New Mapping Name</label>
                <input value={form.new_mapping_name} onChange={e => set('new_mapping_name', e.target.value)} placeholder="MAP_ODS_XXX" className="w-full p-2.5 rounded-xl border border-slate-200 font-mono text-sm" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">New Source Component</label>
                  <input value={form.rename_source_table_name} onChange={e => set('rename_source_table_name', e.target.value)} placeholder="SRC_EMPLOYEES" className="w-full p-2.5 rounded-xl border border-slate-200 font-mono text-sm" />
                </div>
                <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">New Target Component</label>
                  <input value={form.rename_target_table_name} onChange={e => set('rename_target_table_name', e.target.value)} placeholder="ODS_HR_EMPLOYEES" className="w-full p-2.5 rounded-xl border border-slate-200 font-mono text-sm" />
                </div>
              </div>
              <div className="grid grid-cols-[120px_1fr_1fr] gap-3">
                <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">Column Side</label>
                  <select value={form.rename_column_side} onChange={e => set('rename_column_side', e.target.value)} className="w-full p-2.5 rounded-xl border border-slate-200 text-sm">
                    <option value="target">Target</option>
                    <option value="source">Source</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">Old Column</label>
                  <input value={form.rename_column_old_name} onChange={e => set('rename_column_old_name', e.target.value)} placeholder="EMPLOYEE_ID" className="w-full p-2.5 rounded-xl border border-slate-200 font-mono text-sm" />
                </div>
                <div>
                  <label className="text-xs font-bold text-slate-500 mb-1 block">New Column</label>
                  <input value={form.rename_column_new_name} onChange={e => set('rename_column_new_name', e.target.value)} placeholder="EMP_ID" className="w-full p-2.5 rounded-xl border border-slate-200 font-mono text-sm" />
                </div>
              </div>
            </div>
          )}
        </div>

        {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-100 text-rose-600 text-xs">{error}</div>}

        <div className="flex justify-end gap-2 pt-4 border-t border-slate-100">
           <button onClick={onClose} className="px-5 py-2.5 rounded-xl border border-slate-200 text-sm font-bold text-slate-500 hover:bg-slate-50 transition-all">Cancel</button>
           <button
             onClick={handleUpdate}
             disabled={loading || !hasPendingChanges}
             className="px-8 py-2.5 rounded-xl bg-amber-500 text-white text-sm font-bold hover:bg-amber-600 shadow-lg shadow-amber-200 transition-all flex items-center gap-2"
           >
             {loading && <i className="fa-solid fa-spinner fa-spin"></i>}
             Apply Changes
           </button>
        </div>
      </div>
    </Modal>
  );
}
