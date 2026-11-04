import { useRef, useEffect, useState } from 'react';
import { FaPaperPlane, FaTableList, FaFileExport, FaTrashCan } from 'react-icons/fa6';

export function InputArea({ onSubmit, onToggleSidebar, onExport, onClearHistory, loading }) {
  const [value, setValue] = useState('');
  const textareaRef = useRef(null);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
  }, [value]);

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  function handleSubmit() {
    const trimmed = value.trim();
    if (!trimmed || loading) return;
    onSubmit(trimmed);
    setValue('');
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }

  return (
    <div className="relative z-50 min-w-0 border-t border-white/60 bg-white/50 backdrop-blur-[12px] px-4 py-3">
      <div className="flex min-w-0 items-end gap-3">
        {/* Textarea */}
        <div className="relative min-w-0 flex-1">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={e => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            disabled={loading}
            className="input-field resize-none pr-4 leading-relaxed min-h-[42px] max-h-[200px] overflow-y-auto disabled:opacity-60"
          />
        </div>

        <div className="mb-0.5 flex shrink-0 items-center gap-2">
          {/* Send button */}
          <button
            onClick={handleSubmit}
            disabled={!value.trim() || loading}
            className="flex h-11 w-11 items-center justify-center rounded-xl bg-oracle text-white shadow-sm
                       hover:bg-oracle-dark active:scale-95 transition-all duration-200
                       disabled:opacity-40 disabled:cursor-not-allowed"
            title="Send"
            aria-label="Send"
          >
            <FaPaperPlane className="text-base" />
          </button>

          {/* Table sidebar toggle */}
          <button
            onClick={onToggleSidebar}
            className="flex h-11 w-11 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-500 shadow-sm
                       hover:border-oracle/30 hover:bg-oracle/5 hover:text-oracle active:scale-95 transition-all duration-200"
            title="Open table list"
            aria-label="Open table list"
          >
            <FaTableList className="text-base" />
          </button>

          {/* Export button */}
          <button
            onClick={onExport}
            className="flex h-11 w-11 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-500 shadow-sm
                       hover:border-emerald-200 hover:bg-emerald-50 hover:text-emerald-600 active:scale-95 transition-all duration-200"
            title="Export last data table to Excel"
            aria-label="Export last data table to Excel"
          >
            <FaFileExport className="text-base" />
          </button>

          {/* Clear chat history */}
          <button
            onClick={onClearHistory}
            className="flex h-11 w-11 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-500 shadow-sm
                       hover:border-red-200 hover:bg-red-50 hover:text-red-600 active:scale-95 transition-all duration-200"
            title="Clear chat history"
            aria-label="Clear chat history"
          >
            <FaTrashCan className="text-base" />
          </button>
        </div>
      </div>
      <p className="text-[10px] text-slate-400 mt-1.5 ml-12">
        Enter → send · Shift+Enter → new line
      </p>
    </div>
  );
}
