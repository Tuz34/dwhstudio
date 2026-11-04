import { useState, useEffect } from 'react';
import { apiPost, getStatus, summarizeLocal } from '../../services/api';
import clsx from 'clsx';
import { LOCAL_OPENAI_BASE_URL } from '../../config/runtime';

const PROVIDERS = [
  {
    id: 'hybrid',
    label: 'Hybrid',
    icon: 'fa-layer-group',
    note: 'Local first, cloud fallback',
    color: 'indigo',
  },
  {
    id: 'ollama',
    label: 'Ollama',
    icon: 'fa-robot',
    note: 'Local model only',
    color: 'emerald',
  },
  {
    id: 'claude',
    label: 'Claude API',
    icon: 'fa-wand-magic-sparkles',
    note: 'Cloud reviewer',
    color: 'amber',
  },
  {
    id: 'local_openai',
    label: 'Local OpenAI',
    icon: 'fa-microchip',
    note: 'LM Studio or compatible',
    color: 'sky',
  },
];

const ACTIVE_STYLE = {
  indigo: 'border-indigo-500 bg-indigo-50 text-indigo-800',
  emerald: 'border-emerald-500 bg-emerald-50 text-emerald-800',
  amber: 'border-amber-500 bg-amber-50 text-amber-800',
  sky: 'border-sky-500 bg-sky-50 text-sky-800',
};

export function AiSettings() {
  const [provider, setProvider] = useState('hybrid');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState(null);
  const [openPanel, setOpenPanel] = useState(null);
  const [anthropicKey, setAnthropicKey] = useState('');
  const [apiSaving, setApiSaving] = useState(false);
  const [localConfig, setLocalConfig] = useState({
    base_url: LOCAL_OPENAI_BASE_URL,
    model: 'qwen/qwen2.5-coder-14b',
  });
  const [analyst, setAnalyst] = useState({ title: '', content: '', result: '', loading: false });

  useEffect(() => {
    getStatus().then(d => {
      if (d.llm_provider) setProvider(d.llm_provider);
    }).catch(() => {});
  }, []);

  const switchProvider = async (newProvider) => {
    setLoading(true);
    setMsg(null);
    try {
      const res = await apiPost('/api/config/llm', { provider: newProvider });
      if (res.ok) {
        setProvider(newProvider);
        setMsg({ type: 'success', text: `LLM provider updated to ${newProvider.toUpperCase()}.` });
      }
    } catch (e) {
      setMsg({ type: 'error', text: e.message });
    } finally {
      setLoading(false);
    }
  };

  const saveLocalConfig = async () => {
    setMsg(null);
    try {
      await apiPost('/api/config/llm', { provider: 'local_openai', ...localConfig });
      setMsg({ type: 'success', text: 'Local endpoint settings saved.' });
    } catch (e) {
      setMsg({ type: 'error', text: e.message });
    }
  };

  const saveAnthropicKey = async (activateClaude = false) => {
    const key = anthropicKey.trim();
    if (!key) {
      setMsg({ type: 'error', text: 'Paste an Anthropic API key first.' });
      return;
    }
    setApiSaving(true);
    setMsg(null);
    try {
      const nextProvider = activateClaude ? 'claude' : provider;
      const res = await apiPost('/api/config/llm', {
        provider: nextProvider,
        anthropic_api_key: key,
      });
      setAnthropicKey('');
      setProvider(res.provider || nextProvider);
      setMsg({
        type: 'success',
        text: activateClaude ? 'Claude API key saved and Claude is active.' : 'Claude API key saved.',
      });
    } catch (e) {
      setMsg({ type: 'error', text: e.message });
    } finally {
      setApiSaving(false);
    }
  };

  const runLocalAnalyst = async () => {
    setAnalyst(p => ({ ...p, loading: true, result: '' }));
    setMsg(null);
    try {
      const res = await summarizeLocal({
        title: analyst.title || 'Local Analyst',
        content: analyst.content,
        provider: 'local_openai',
      });
      setAnalyst(p => ({ ...p, result: res.summary || '', loading: false }));
      setMsg({ type: 'success', text: 'Local analysis saved to LOCAL_NOTES.md.' });
    } catch (e) {
      setAnalyst(p => ({ ...p, loading: false }));
      setMsg({ type: 'error', text: e.message });
    }
  };

  return (
    <div className="max-w-4xl space-y-4">
      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h4 className="text-sm font-black text-slate-800">AI Provider</h4>
            <p className="text-xs text-slate-400">Choose the runtime used by AI-assisted actions.</p>
          </div>
          {loading && <span className="text-xs font-semibold text-slate-400">Saving...</span>}
        </div>

        <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
          {PROVIDERS.map(item => {
            const active = provider === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => !loading && switchProvider(item.id)}
                className={clsx(
                  'flex items-center gap-3 rounded-lg border px-3 py-3 text-left transition-colors',
                  active
                    ? ACTIVE_STYLE[item.color]
                    : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50'
                )}
              >
                <i className={clsx('fa-solid text-sm', item.icon)} />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-black">{item.label}</span>
                  <span className="block truncate text-[11px] text-current opacity-70">{item.note}</span>
                </span>
                {active && <i className="fa-solid fa-circle-check ml-auto text-xs" />}
              </button>
            );
          })}
        </div>

        {msg && (
          <div
            className={clsx(
              'mt-3 flex gap-2 rounded-lg border px-3 py-2 text-sm',
              msg.type === 'success'
                ? 'border-emerald-100 bg-emerald-50 text-emerald-700'
                : 'border-rose-100 bg-rose-50 text-rose-700'
            )}
          >
            <i className={clsx('fa-solid mt-0.5', msg.type === 'success' ? 'fa-circle-check' : 'fa-circle-xmark')} />
            {msg.text}
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
        <button
          type="button"
          onClick={() => setOpenPanel(openPanel === 'api' ? null : 'api')}
          className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-slate-50"
        >
          <span className="flex items-center gap-3">
            <i className="fa-solid fa-key text-slate-400" />
            <span>
              <span className="block text-sm font-black text-slate-800">API Key Settings</span>
              <span className="block text-xs text-slate-400">Hidden by default. Manage credentials outside the main view.</span>
            </span>
          </span>
          <i className={clsx('fa-solid text-xs text-slate-400', openPanel === 'api' ? 'fa-chevron-up' : 'fa-chevron-down')} />
        </button>
        {openPanel === 'api' && (
          <div className="border-t border-slate-100 bg-slate-50 px-4 py-4">
            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="mb-2 text-xs font-black text-slate-500">Anthropic API Key</div>
              <div className="flex flex-col gap-2 md:flex-row">
                <input
                  type="password"
                  value={anthropicKey}
                  onChange={e => setAnthropicKey(e.target.value)}
                  placeholder="Paste Anthropic API key"
                  className="min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-3 py-2 font-mono text-xs outline-none"
                  autoComplete="off"
                />
                <button
                  type="button"
                  onClick={() => saveAnthropicKey(false)}
                  disabled={apiSaving || !anthropicKey.trim()}
                  className="rounded-md bg-slate-800 px-3 py-2 text-xs font-bold text-white hover:bg-slate-700"
                >
                  {apiSaving ? 'Saving...' : 'Save Key'}
                </button>
                <button
                  type="button"
                  onClick={() => saveAnthropicKey(true)}
                  disabled={apiSaving || !anthropicKey.trim()}
                  className="rounded-md bg-amber-600 px-3 py-2 text-xs font-bold text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  Save & Use Claude
                </button>
              </div>
            </div>
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
        <button
          type="button"
          onClick={() => setOpenPanel(openPanel === 'local' ? null : 'local')}
          className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-slate-50"
        >
          <span className="flex items-center gap-3">
            <i className="fa-solid fa-microchip text-sky-500" />
            <span>
              <span className="block text-sm font-black text-slate-800">Local Analyst</span>
              <span className="block text-xs text-slate-400">Open only when you need a local summary or endpoint change.</span>
            </span>
          </span>
          <i className={clsx('fa-solid text-xs text-slate-400', openPanel === 'local' ? 'fa-chevron-up' : 'fa-chevron-down')} />
        </button>
        {openPanel === 'local' && (
          <div className="space-y-3 border-t border-slate-100 bg-slate-50 px-4 py-4">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-[1fr_220px_auto]">
              <input
                value={localConfig.base_url}
                onChange={e => setLocalConfig(p => ({ ...p, base_url: e.target.value }))}
                className="rounded-md border border-slate-200 bg-white px-3 py-2 font-mono text-xs"
                placeholder={LOCAL_OPENAI_BASE_URL}
              />
              <input
                value={localConfig.model}
                onChange={e => setLocalConfig(p => ({ ...p, model: e.target.value }))}
                className="rounded-md border border-slate-200 bg-white px-3 py-2 font-mono text-xs"
                placeholder="local-model"
              />
              <button
                type="button"
                onClick={saveLocalConfig}
                className="rounded-md bg-sky-600 px-3 py-2 text-xs font-bold text-white hover:bg-sky-700"
              >
                Save
              </button>
            </div>
            <input
              value={analyst.title}
              onChange={e => setAnalyst(p => ({ ...p, title: e.target.value }))}
              className="w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm"
              placeholder="Note title"
            />
            <textarea
              value={analyst.content}
              onChange={e => setAnalyst(p => ({ ...p, content: e.target.value }))}
              rows={6}
              className="w-full resize-none rounded-md border border-slate-200 bg-white px-3 py-2 font-mono text-sm"
              placeholder="Paste a redacted log, error output, file summary, or local model review here..."
            />
            <div className="flex justify-end">
              <button
                type="button"
                onClick={runLocalAnalyst}
                disabled={analyst.loading || !analyst.content.trim()}
                className="rounded-md bg-sky-600 px-4 py-2 text-xs font-bold text-white hover:bg-sky-700 disabled:opacity-50"
              >
                {analyst.loading ? 'Summarizing...' : 'Run Local Analysis'}
              </button>
            </div>
            {analyst.result && (
              <pre className="whitespace-pre-wrap rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-700">
                {analyst.result}
              </pre>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
