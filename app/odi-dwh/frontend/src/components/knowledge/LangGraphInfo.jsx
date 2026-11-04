import { useState, useEffect } from 'react';
import { getLangGraphInfo } from '../../services/api';

export function LangGraphInfo() {
  const [info, setInfo] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    getLangGraphInfo()
      .then(d => setInfo(d))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return (
    <div className="flex justify-center py-16 text-slate-400">
      <i className="fa-solid fa-spinner fa-spin text-2xl"></i>
    </div>
  );

  if (error) return (
    <div className="bg-rose-50 border border-rose-200 rounded-xl p-6 text-rose-600 text-sm">
      <i className="fa-solid fa-circle-exclamation mr-2"></i>
      Failed to load LangGraph info: {error}
    </div>
  );

  if (!info) return null;

  return (
    <div className="space-y-6">
      <h3 className="text-xl font-bold text-slate-800 flex items-center gap-2">
        <i className="fa-solid fa-sitemap text-blue-500"></i>
        LangGraph Pipeline
      </h3>

      {/* Nodes */}
      {info.nodes && (
        <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-3 bg-blue-50 border-b border-blue-100 font-semibold text-blue-700 text-sm flex items-center gap-2">
            <i className="fa-solid fa-circle-nodes"></i>
            Nodes ({info.nodes.length})
          </div>
          <div className="divide-y divide-slate-100">
            {info.nodes.map((node, i) => (
              <div key={i} className="px-5 py-3 flex items-center gap-3 text-sm hover:bg-slate-50">
                <div className="w-8 h-8 bg-blue-100 text-blue-700 rounded-full flex items-center justify-center font-bold text-xs shrink-0">
                  {i + 1}
                </div>
                <span className="font-mono font-medium text-slate-700">{node.name || node}</span>
                {node.type && (
                  <span className="ml-auto text-xs bg-slate-100 text-slate-500 px-2 py-0.5 rounded">{node.type}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Edges */}
      {info.edges && info.edges.length > 0 && (
        <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-3 bg-indigo-50 border-b border-indigo-100 font-semibold text-indigo-700 text-sm flex items-center gap-2">
            <i className="fa-solid fa-arrow-right-arrow-left"></i>
            Edges ({info.edges.length})
          </div>
          <div className="divide-y divide-slate-100">
            {info.edges.map((edge, i) => (
              <div key={i} className="px-5 py-3 flex items-center gap-3 text-sm font-mono hover:bg-slate-50">
                <span className="text-slate-600">{edge.source || edge[0]}</span>
                <i className="fa-solid fa-arrow-right text-indigo-400 text-xs"></i>
                <span className="text-slate-600">{edge.target || edge[1]}</span>
                {edge.condition && (
                  <span className="ml-auto text-xs text-slate-400 italic">{edge.condition}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Other info */}
      {info.description && (
        <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 text-sm text-slate-600">
          <i className="fa-solid fa-circle-info mr-2 text-slate-400"></i>
          {info.description}
        </div>
      )}
    </div>
  );
}
