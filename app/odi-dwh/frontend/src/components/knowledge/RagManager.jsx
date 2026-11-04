import { useState, useEffect, useCallback } from 'react';
import { getRag, saveRag, deleteRag } from '../../services/api';
import clsx from 'clsx';

const COLLECTIONS = ['business_rules', 'schema_info', 'dwh_modeling', 'examples'];

const COLLECTION_META = {
  business_rules:  { label: 'Business Rules',    icon: 'fa-scale-balanced',  color: 'violet'  },
  schema_info:     { label: 'Schema Info',        icon: 'fa-database',        color: 'blue'    },
  dwh_modeling:    { label: 'DWH Modeling',       icon: 'fa-cubes',           color: 'emerald' },
  examples:        { label: 'Examples',           icon: 'fa-flask',           color: 'amber'   },
};

const COLOR = {
  violet:  'bg-violet-100 text-violet-700 border-violet-200',
  blue:    'bg-blue-100 text-blue-700 border-blue-200',
  emerald: 'bg-emerald-100 text-emerald-700 border-emerald-200',
  amber:   'bg-amber-100 text-amber-700 border-amber-200',
};

export function RagManager() {
  const [docs, setDocs]             = useState({});
  const [loading, setLoading]       = useState(true);
  const [activeCol, setActiveCol]   = useState('business_rules');
  const [search, setSearch]         = useState('');
  const [form, setForm]             = useState(null);       // null | { id?, collection, text, metadata }
  const [saving, setSaving]         = useState(false);
  const [toast, setToast]           = useState(null);

  const showToast = useCallback((message, type = 'success') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  }, []);

  const loadRag = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getRag();
      // data may be { collection: [...] } or flat array
      if (Array.isArray(data)) {
        // group by collection
        const grouped = {};
        data.forEach(d => {
          const col = d.collection || d.metadata?.collection || 'business_rules';
          if (!grouped[col]) grouped[col] = [];
          grouped[col].push(d);
        });
        setDocs(grouped);
      } else {
        setDocs(data || {});
      }
    } catch (e) {
      showToast('Failed to load RAG: ' + e.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    const id = setTimeout(loadRag, 0);
    return () => clearTimeout(id);
  }, [loadRag]);

  const openNew = () => setForm({ collection: activeCol, text: '', metadata: '{}' });
  const openEdit = (doc) => setForm({
    id: doc.id,
    collection: doc.collection || activeCol,
    text: doc.text || doc.content || '',
    metadata: JSON.stringify(doc.metadata || {}, null, 2),
  });

  const handleSave = async () => {
    if (!form.text.trim()) return;
    if (!window.confirm('Save this knowledge document?')) return;
    setSaving(true);
    try {
      let meta = {};
      try { meta = JSON.parse(form.metadata || '{}'); } catch { meta = {}; }
      await saveRag({ approved: true, id: form.id, collection: form.collection, text: form.text, metadata: meta });
      showToast(form.id ? 'Updated' : 'Added');
      setForm(null);
      loadRag();
    } catch (e) {
      showToast('Save error: ' + e.message, 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (doc) => {
    if (!window.confirm(`Delete "${(doc.text || '').slice(0, 60)}..."?`)) return;
    try {
      await deleteRag({ approved: true, id: doc.id, collection: doc.collection || activeCol });
      showToast('Deleted');
      loadRag();
    } catch (e) {
      showToast('Delete error: ' + e.message, 'error');
    }
  };

  const activeDocs = (docs[activeCol] || []).filter(d => {
    const q = search.toLowerCase();
    return !q || (d.text || d.content || '').toLowerCase().includes(q);
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Toast */}
      {toast && (
        <div className={clsx(
          'fixed top-6 right-6 z-50 rounded-xl px-5 py-3 shadow-xl text-white text-sm font-medium',
          toast.type === 'success' ? 'bg-emerald-600' : 'bg-rose-600'
        )}>{toast.message}</div>
      )}

      {/* Header */}
      <div className="shrink-0 px-6 pt-4 pb-3 border-b border-slate-200">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-xl font-bold text-slate-800 flex items-center gap-2">
            <i className="fa-solid fa-brain text-violet-500"></i>
            Knowledge Hub
            <span className="text-sm font-normal text-slate-400 bg-slate-100 rounded-full px-3 py-0.5">
              RAG Memory
            </span>
          </h3>
          <div className="flex gap-2">
            <div className="relative">
              <i className="fa-solid fa-search absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 text-xs"></i>
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search documents..."
                className="pl-8 pr-4 py-2 text-sm border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-violet-400 w-52"
              />
            </div>
            <button
              onClick={openNew}
              className="bg-violet-600 hover:bg-violet-700 text-white px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition-colors"
            >
              <i className="fa-solid fa-plus"></i>
              New Rule
            </button>
          </div>
        </div>

        {/* Collection tabs */}
        <div className="flex gap-2">
          {COLLECTIONS.map(col => {
            const meta = COLLECTION_META[col] || { label: col, icon: 'fa-file', color: 'slate' };
            const count = (docs[col] || []).length;
            const isActive = activeCol === col;
            return (
              <button
                key={col}
                onClick={() => setActiveCol(col)}
                className={clsx(
                  'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all',
                  isActive
                    ? COLOR[meta.color] || 'bg-slate-100 text-slate-700 border-slate-200'
                    : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                )}
              >
                <i className={`fa-solid ${meta.icon}`}></i>
                {meta.label}
                <span className={clsx(
                  'ml-1 rounded-full px-1.5 py-0.5 text-[10px] font-bold',
                  isActive ? 'bg-white/60' : 'bg-slate-200 text-slate-500'
                )}>{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Doc list */}
      <div className="flex-1 overflow-y-auto p-6">
        {loading ? (
          <div className="flex justify-center py-12 text-slate-400">
            <i className="fa-solid fa-spinner fa-spin text-2xl"></i>
          </div>
        ) : activeDocs.length === 0 ? (
          <div className="text-center py-16 text-slate-400">
            <i className="fa-solid fa-inbox text-4xl mb-3 block opacity-30"></i>
            <p>No documents in this collection yet.</p>
            <button onClick={openNew} className="mt-4 text-violet-600 hover:underline text-sm font-medium">
              + Add first rule
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {activeDocs.map((doc, i) => (
              <div key={doc.id || i} className="bg-white border border-slate-200 rounded-xl p-4 hover:border-violet-200 hover:shadow-sm transition-all group">
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-slate-700 leading-relaxed line-clamp-3">{doc.text || doc.content}</p>
                    {doc.metadata && Object.keys(doc.metadata).length > 0 && (
                      <div className="flex gap-1.5 flex-wrap mt-2">
                        {Object.entries(doc.metadata).slice(0, 4).map(([k, v]) => (
                          <span key={k} className="text-[10px] bg-slate-100 text-slate-500 px-2 py-0.5 rounded font-mono">
                            {k}: {String(v).slice(0, 20)}
                          </span>
                        ))}
                      </div>
                    )}
                    <div className="text-[10px] text-slate-400 mt-1.5 font-mono">
                      ID: {(doc.id || 'N/A').slice(0, 16)}...
                    </div>
                  </div>
                  <div className="flex gap-1.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => openEdit(doc)}
                      className="p-2 rounded-lg bg-amber-50 text-amber-600 hover:bg-amber-100 transition-colors"
                      title="Edit"
                    >
                      <i className="fa-solid fa-pen-to-square text-xs"></i>
                    </button>
                    <button
                      onClick={() => handleDelete(doc)}
                      className="p-2 rounded-lg bg-rose-50 text-rose-500 hover:bg-rose-100 transition-colors"
                      title="Delete"
                    >
                      <i className="fa-solid fa-trash-can text-xs"></i>
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Edit/Add Modal */}
      {form && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => setForm(null)}>
          <div className="absolute inset-0 bg-black/40 backdrop-blur-sm"></div>
          <div
            className="relative z-50 bg-white rounded-2xl shadow-2xl w-full max-w-2xl mx-4 p-6 space-y-4"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h4 className="font-bold text-slate-800 text-lg">
                {form.id ? 'Edit Document' : 'Add New Rule'}
              </h4>
              <button onClick={() => setForm(null)} className="text-slate-400 hover:text-slate-700">
                <i className="fa-solid fa-xmark text-lg"></i>
              </button>
            </div>

            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Collection</label>
              <select
                value={form.collection}
                onChange={e => setForm(p => ({ ...p, collection: e.target.value }))}
                className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400"
              >
                {COLLECTIONS.map(c => (
                  <option key={c} value={c}>{COLLECTION_META[c]?.label || c}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Rule / Content</label>
              <textarea
                value={form.text}
                onChange={e => setForm(p => ({ ...p, text: e.target.value }))}
                rows={6}
                placeholder="Write the business rule or information here..."
                className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400 resize-none"
              />
            </div>

            <div>
              <label className="text-xs font-semibold text-slate-500 uppercase mb-1 block">Metadata (JSON)</label>
              <textarea
                value={form.metadata}
                onChange={e => setForm(p => ({ ...p, metadata: e.target.value }))}
                rows={3}
                className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-400 resize-none bg-slate-50"
              />
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setForm(null)} className="px-4 py-2 rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 transition-colors">
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !form.text.trim()}
                className="bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition-colors"
              >
                <i className={`fa-solid ${saving ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`}></i>
                {saving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
