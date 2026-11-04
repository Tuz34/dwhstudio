import { useState, useEffect, useCallback } from 'react';
import { FaTableList, FaXmark, FaTable, FaSpinner, FaMagnifyingGlass } from 'react-icons/fa6';
import { getTables, getPreview, getSourcePreview } from '../../services/api';

function normalizeTR(str) {
  return (str || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\u0131/g, 'i')
    .toLowerCase();
}

function isSourcePlatform(platform) {
  return ['SOURCE', 'MSSQL'].includes(String(platform || '').toUpperCase());
}

function platformBadge(platform) {
  return isSourcePlatform(platform)
    ? { label: 'Source', className: 'bg-blue-100 text-blue-600' }
    : { label: 'Oracle', className: 'bg-orange-100 text-orange-600' };
}

function normalizeColumnName(value) {
  return String(value || '').trim().toUpperCase();
}

function keyBadgeForColumn(data, column) {
  const name = normalizeColumnName(column);
  const primaryKeys = new Set(
    (data?.primary_keys || data?.pk_columns || data?.primaryKeys || [])
      .map(normalizeColumnName)
  );
  const foreignKeys = new Set(
    (data?.foreign_keys || data?.fk_columns || data?.foreignKeys || [])
      .flatMap(item => {
        if (typeof item === 'string') return [item];
        return [item?.column, item?.column_name, item?.COLUMN_NAME].filter(Boolean);
      })
      .map(normalizeColumnName)
  );

  if (primaryKeys.has(name)) return { label: 'PK', className: 'bg-emerald-100 text-emerald-700 border-emerald-200' };
  if (foreignKeys.has(name)) return { label: 'FK', className: 'bg-blue-100 text-blue-700 border-blue-200' };
  return null;
}

function TablePreview({ table, platform, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const loadPreview = async () => {
      setLoading(true);
      setError(null);
      setData(null);
      try {
        const fetchFn = isSourcePlatform(platform) ? getSourcePreview : getPreview;
        const d = await fetchFn(table);
        if (cancelled) return;
        if (!d || d.error) setError(d?.error || 'Preview unavailable');
        else if (!Array.isArray(d.columns)) setError('Unexpected response format');
        else setData(d);
      } catch (e) {
        if (!cancelled) setError(e.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    loadPreview();
    return () => { cancelled = true; };
  }, [table, platform]);

  return (
    <div className="mt-1 mx-2 mb-2 rounded-xl border border-slate-200 bg-white overflow-hidden animate-fade-in">
      <div className="flex items-center justify-between px-3 py-2 bg-slate-50 border-b border-slate-200">
        <span className="text-xs font-semibold text-slate-600 flex items-center gap-1.5">
          <FaTable className="text-slate-400" /> Data / Columns <span className="text-slate-400 font-normal">(first 3 rows)</span>
        </span>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-600 p-0.5">
          <FaXmark />
        </button>
      </div>

      {loading && (
        <div className="flex items-center gap-2 p-3 text-xs text-slate-400">
          <FaSpinner className="animate-spin" /> Loading...
        </div>
      )}
      {error && <div className="p-3 text-xs text-red-500">❌ {error}</div>}
      {data && (
        <div className="overflow-x-auto">
          <table className="data-table text-xs">
            <thead>
              <tr>
                {(data.columns || []).map(c => {
                  const badge = keyBadgeForColumn(data, c);
                  return (
                    <th key={c}>
                      <span className="inline-flex items-center gap-1.5">
                        <span>{c}</span>
                        {badge && (
                          <span className={`rounded border px-1 py-0.5 text-[9px] font-black ${badge.className}`}>
                            {badge.label}
                          </span>
                        )}
                      </span>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {(data.rows || []).map((row, i) => (
                <tr key={i}>
                  {(data.columns || []).map(c => (
                    <td key={c} className="max-w-[120px] truncate text-xs" title={String(row[c] ?? '')}>
                      {row[c] ?? ''}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function TableSidebar({ open, onClose }) {
  const [tables, setTables] = useState([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [schemaFilter, setSchemaFilter] = useState('');
  const [preview, setPreview] = useState(null); // { table, platform }

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getTables();
      setTables(Array.isArray(data) ? data : []);
    } catch {
      setTables([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open || tables.length > 0) return undefined;
    const id = setTimeout(load, 0);
    return () => clearTimeout(id);
  }, [open, load, tables.length]);

  const tableSchema = (t) => {
    if (typeof t === 'object') return t.schema || (t.original || t.name || '').split('.')[0];
    return (t || '').split('.')[0];
  };
  const schemas = [...new Set(tables.map(tableSchema).filter(Boolean))].sort();

  const filtered = tables.filter(t => {
    const name = normalizeTR(typeof t === 'string' ? t : t.name);
    const comment = normalizeTR(typeof t === 'object' ? t.comment || '' : '');
    const q = normalizeTR(search);
    const schemaOk = !schemaFilter || tableSchema(t) === schemaFilter;
    return schemaOk && (!q || name.includes(q) || comment.includes(q));
  });

  function handleTableClick(t) {
    const name = typeof t === 'string' ? t : (t.original || t.name);
    const platform = typeof t === 'object' ? (t.platform || 'ORACLE') : 'ORACLE';
    if (preview?.table === name) {
      setPreview(null);
    } else {
      setPreview({ table: name, platform });
    }
  }

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-x-0 top-14 bottom-[88px] z-30" onClick={onClose} />

      {/* Sidebar panel */}
      <aside className="fixed right-0 top-14 bottom-[88px] w-80 z-40 flex flex-col bg-white/90 backdrop-blur-[16px] border-l border-white/60 shadow-glass-lg animate-slide-down">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200/60">
          <h2 className="text-sm font-semibold text-slate-700 flex items-center gap-2">
            <FaTableList className="text-oracle" />
            Accessible Tables
          </h2>
          <button onClick={onClose} className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors">
            <FaXmark />
          </button>
        </div>

        {/* Search */}
        <div className="px-3 py-2 border-b border-slate-100">
          <div className="relative">
            <FaMagnifyingGlass className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 text-xs" />
            <input
              className="input-field pl-8 py-2 text-xs"
              placeholder="Search table..."
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
          </div>
        </div>

        {/* Schema filter */}
        {schemas.length > 1 && (
          <div className="px-3 py-2 border-b border-slate-100">
            <select
              className="input-field py-2 text-xs w-full"
              value={schemaFilter}
              onChange={e => setSchemaFilter(e.target.value)}
            >
              <option value="">All schemas ({tables.length})</option>
              {schemas.map(s => (
                <option key={s} value={s}>
                  {s} ({tables.filter(t => tableSchema(t) === s).length})
                </option>
              ))}
            </select>
          </div>
        )}

        {/* List */}
        <div className="flex-1 overflow-y-auto py-2">
          {loading && (
            <div className="flex items-center gap-2 px-4 py-4 text-sm text-slate-400">
              <FaSpinner className="animate-spin" /> Loading...
            </div>
          )}
          {!loading && filtered.length === 0 && (
            <p className="px-4 py-4 text-sm text-slate-400">No tables found.</p>
          )}
          {!loading && filtered.map((t, i) => {
            const apiName = typeof t === 'string' ? t : (t.original || t.name);
            const platform = typeof t === 'object' ? (t.platform || 'ORACLE') : 'ORACLE';
            const badge = platformBadge(platform);
            const comment = typeof t === 'object' ? t.comment || '' : '';
            const shortName = apiName.split('.').pop();
            const isSelected = preview?.table === apiName;

            return (
              <div key={i}>
                <div
                  className={`mx-2 rounded-lg px-3 py-2 cursor-pointer transition-colors ${
                    isSelected ? 'bg-oracle/10 border border-oracle/20' : 'hover:bg-slate-50'
                  }`}
                  onClick={() => handleTableClick(t)}
                >
                  <div className="flex items-start gap-2">
                    <FaTable className={`text-xs flex-shrink-0 mt-0.5 ${isSelected ? 'text-oracle' : 'text-slate-400'}`} />
                    <span className="text-sm font-medium text-slate-700 break-words min-w-0 flex-1" title={shortName}>{shortName}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0 whitespace-nowrap ${badge.className}`}>
                      {badge.label}
                    </span>
                  </div>
                  {comment && (
                    <p className="text-[11px] text-slate-400 truncate mt-0.5 pl-5">{comment}</p>
                  )}
                </div>
                {isSelected && (
                  <TablePreview
                    table={apiName}
                    platform={platform}
                    onClose={() => setPreview(null)}
                  />
                )}
              </div>
            );
          })}
        </div>
      </aside>
    </>
  );
}
