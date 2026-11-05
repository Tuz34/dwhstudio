import { useState } from 'react';
import { FaBrain, FaUser, FaCopy, FaCheck, FaPlay, FaCircleInfo,
         FaTriangleExclamation, FaDiagramProject, FaMicrochip, FaRobot, FaCloud } from 'react-icons/fa6';
import { isDmlAction } from './dmlActions';

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  async function handleCopy() {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <button onClick={handleCopy} className="flex items-center gap-1 px-2 py-1 rounded text-xs text-slate-400 hover:text-slate-200 hover:bg-white/10 transition-colors">
      {copied ? <FaCheck className="text-emerald-400" /> : <FaCopy />}
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

function SqlBlock({ query, onRun }) {
  const isError = !!query.error;
  const hasResult = query.result && query.result.columns?.length > 0;
  const runLabel = hasResult && query.preview_limited ? 'Show more' : 'Run';

  return (
    <div className={`rounded-xl border overflow-hidden mt-2 ${isError ? 'border-red-200' : 'border-slate-200/60'}`}>
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-slate-800">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${isError ? 'bg-red-400' : 'bg-emerald-400'}`} />
          <span className="text-xs text-slate-300 font-medium">{query.label || 'SQL'}</span>
          {isDmlAction(query.action) && (
            <span className="badge-red text-[10px] px-1.5 py-0.5">DML</span>
          )}
        </div>
        {!query.auto_executed && (
          <div className="flex items-center gap-1">
            <CopyButton text={query.sql} />
            {onRun && !isDmlAction(query.action) && (
              <button
                onClick={() => onRun(query)}
                className="flex items-center gap-1 px-2 py-1 rounded text-xs text-sky-400 hover:text-sky-300 hover:bg-white/10 transition-colors"
              >
                <FaPlay className="text-[10px]" /> {runLabel}
              </button>
            )}
          </div>
        )}
      </div>

      {/* SQL */}
      {!query.auto_executed && (
        <pre className="code-block rounded-none text-[12px] leading-relaxed overflow-x-auto">
          <code>{query.sql}</code>
        </pre>
      )}

      {/* Error */}
      {isError && (
        <div className="flex items-start gap-2 px-3 py-2 bg-red-50 border-t border-red-200 text-xs text-red-700">
          <FaTriangleExclamation className="mt-0.5 flex-shrink-0" />
          <span>{query.error}</span>
        </div>
      )}

      {/* Result Table */}
      {hasResult && (
        <div className="overflow-x-auto border-t border-slate-200 max-h-56">
          <table className="data-table text-xs">
            <thead>
              <tr>{query.result.columns.map(c => <th key={c}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {query.result.rows.map((row, i) => (
                <tr key={i}>
                  {query.result.columns.map(c => (
                    <td key={c} className="max-w-[200px] truncate" title={String(row[c] ?? '')}>
                      {row[c] ?? '—'}
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

function LlmRuntimeStatusCard({ query }) {
  const rows = query?.result?.rows || [];
  const selected = rows.find(row => ['YES', 'FIRST'].includes(row.SELECTED)) || rows[0] || {};
  const iconFor = {
    local_openai: <FaMicrochip />,
    ollama: <FaRobot />,
    claude: <FaCloud />,
  };
  const toneFor = status => {
    if (['UP', 'CONFIGURED'].includes(status)) return 'border-emerald-200 bg-emerald-50 text-emerald-700';
    if (status === 'DOWN') return 'border-rose-200 bg-rose-50 text-rose-700';
    return 'border-slate-200 bg-slate-50 text-slate-600';
  };

  return (
    <div className="mt-2 max-w-3xl rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-slate-100 bg-slate-50 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-black text-slate-800">
            {iconFor[selected.PROVIDER] || <FaBrain />}
            <span className="truncate">{selected.PROVIDER || 'Provider'}</span>
          </div>
          <div className="mt-0.5 truncate font-mono text-xs text-slate-500">
            {selected.CONFIGURED_MODEL || '-'}
          </div>
        </div>
        <span className={`shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-black ${toneFor(selected.STATUS)}`}>
          {selected.SELECTED || 'ACTIVE'} / {selected.STATUS || '-'}
        </span>
      </div>
      <div className="grid grid-cols-1 gap-2 p-3 md:grid-cols-3">
        {rows.map(row => (
          <div key={row.PROVIDER} className={`rounded-lg border p-3 ${row.SELECTED === 'NO' ? 'border-slate-100 bg-slate-50' : 'border-slate-200 bg-white'}`}>
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2 text-xs font-black text-slate-700">
                <span className="text-slate-400">{iconFor[row.PROVIDER] || <FaBrain />}</span>
                <span className="truncate">{row.PROVIDER}</span>
              </div>
              <span className={`rounded-full border px-2 py-0.5 text-[10px] font-black ${toneFor(row.STATUS)}`}>
                {row.STATUS}
              </span>
            </div>
            <div className="truncate font-mono text-[11px] text-slate-600" title={row.CONFIGURED_MODEL || ''}>
              {row.CONFIGURED_MODEL || '-'}
            </div>
            <div className="mt-1 truncate text-[11px] text-slate-400" title={row.AVAILABLE_MODELS || ''}>
              {row.AVAILABLE_MODELS && row.AVAILABLE_MODELS !== '-' ? row.AVAILABLE_MODELS : 'No model list'}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function MappingRequestCard({ mappingRequest, onWizardOpen }) {
  const op = mappingRequest?.operation || 'CREATE';
  const colors = {
    CREATE: 'border-purple-200 bg-purple-50',
    UPDATE: 'border-amber-200 bg-amber-50',
    DELETE: 'border-red-200 bg-red-50',
  };
  const btnColors = {
    CREATE: 'bg-purple-600 hover:bg-purple-700 text-white',
    UPDATE: 'bg-amber-600 hover:bg-amber-700 text-white',
    DELETE: 'bg-red-600 hover:bg-red-700 text-white',
  };
  const labels = {
    CREATE: 'Create Mapping',
    UPDATE: 'Update Mapping',
    DELETE: 'Delete Mapping',
  };
  const icons = {
    CREATE: <FaDiagramProject />,
    UPDATE: <FaDiagramProject />,
    DELETE: <FaTriangleExclamation />,
  };

  return (
    <div className={`rounded-xl border-2 p-4 mt-2 ${colors[op] || colors.CREATE} animate-fade-in`}>
      <div className="flex items-center gap-2 mb-3">
        <span className="text-lg">{icons[op]}</span>
        <span className="font-semibold text-slate-700 text-sm">
          ODI Mapping {op === 'CREATE' ? 'Create' : op === 'UPDATE' ? 'Update' : 'Delete'} Request
        </span>
      </div>
      {mappingRequest?.mapping_name && (
        <p className="text-xs text-slate-600 mb-1">
          <strong>Mapping:</strong> {mappingRequest.mapping_name}
        </p>
      )}
      {mappingRequest?.src_table && (
        <p className="text-xs text-slate-600 mb-1">
          <strong>Source:</strong> {mappingRequest.src_table}
        </p>
      )}
      {mappingRequest?.tgt_table && (
        <p className="text-xs text-slate-600 mb-3">
          <strong>Target:</strong> {mappingRequest.tgt_table}
        </p>
      )}
      <button
        onClick={() => onWizardOpen?.(mappingRequest)}
        className={`btn text-xs px-4 py-2 rounded-lg font-medium transition-colors ${btnColors[op] || btnColors.CREATE}`}
      >
        {labels[op] || 'Continue'}
      </button>
    </div>
  );
}

export function ChatMessage({ message, onRun, onWizardOpen }) {
  const isUser = message.role === 'user';
  const isLoading = message.loading;

  if (isLoading) {
    return (
      <div className="flex gap-3 px-4 py-2 animate-fade-in">
        <div className="w-7 h-7 rounded-full bg-oracle flex items-center justify-center flex-shrink-0 mt-1">
          <FaBrain className="text-white text-[11px]" />
        </div>
        <div className="chat-bubble-ai flex items-center gap-2">
          <span className="flex gap-1">
            {[0,1,2].map(i => (
              <span
                key={i}
                className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-typing-bounce"
                style={{ animationDelay: `${i * 0.2}s` }}
              />
            ))}
          </span>
          <span className="text-slate-400 text-xs">Thinking...</span>
        </div>
      </div>
    );
  }

  if (isUser) {
    return (
      <div className="flex gap-3 px-4 py-2 justify-end animate-fade-in">
        <div className="chat-bubble-user">
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>
        <div className="w-7 h-7 rounded-full bg-slate-200 flex items-center justify-center flex-shrink-0 mt-1">
          <FaUser className="text-slate-500 text-[11px]" />
        </div>
      </div>
    );
  }

  // AI message
  return (
    <div className="flex gap-3 px-4 py-2 animate-fade-in">
      <div className="w-7 h-7 rounded-full bg-oracle flex items-center justify-center flex-shrink-0 mt-1">
        <FaBrain className="text-white text-[11px]" />
      </div>
      <div className="flex-1 min-w-0">
        {/* Description text */}
        {message.content && (
          <div className="chat-bubble-ai mb-2 inline-block">
            <p className="whitespace-pre-wrap">{message.content}</p>
          </div>
        )}

        {/* SQL Blocks */}
        {(message.queries || []).map((q, i) => (
          q.label === 'LLM Runtime Status'
            ? <LlmRuntimeStatusCard key={i} query={q} />
            : <SqlBlock key={i} query={q} onRun={onRun} />
        ))}

        {/* Mapping request card */}
        {message.mapping_request && (
          <MappingRequestCard
            mappingRequest={message.mapping_request}
            onWizardOpen={onWizardOpen}
          />
        )}

        {Array.isArray(message.mapping_requests) && message.mapping_requests.length > 0 && (
          <div className="mt-2 space-y-2">
            <div className="text-xs font-semibold text-slate-500">
              {message.mapping_requests.length} mapping requests prepared
            </div>
            {message.mapping_requests.map((mappingRequest, index) => (
              <MappingRequestCard
                key={mappingRequest.mapping_name || index}
                mappingRequest={mappingRequest}
                onWizardOpen={onWizardOpen}
              />
            ))}
          </div>
        )}

        {/* System / error message style */}
        {message.role === 'system' && (
          <div className="flex items-center gap-2 text-xs text-slate-500 px-3 py-2 bg-slate-50 rounded-lg border border-slate-200 inline-flex">
            <FaCircleInfo />
            {message.content}
          </div>
        )}
      </div>
    </div>
  );
}
