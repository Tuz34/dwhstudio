import { FaSyncAlt } from 'react-icons/fa';
import { useApp, useDispatch } from '../../store/AppContext';
import { useStatus } from '../../hooks/useStatus';

export function StatusIndicator() {
  const { systemStatus, statusLoading } = useApp();
  const dispatch = useDispatch();
  const { check } = useStatus();
  const checks = systemStatus?.checks || {};
  const optionalChecks = new Set(['claude', 'docker', 'llm', 'local_openai']);
  const hiddenChecks = new Set(['git', 'gitea', 'mssql']);
  const coreValues = Object.entries(checks)
    .filter(([id]) => !hiddenChecks.has(id) && !optionalChecks.has(id))
    .map(([, ok]) => Boolean(ok));
  const coreTotal = coreValues.length;
  const coreUp = coreValues.filter(Boolean).length;
  const coreOk = coreTotal > 0 && coreUp === coreTotal;

  const dotClass = statusLoading
    ? 'status-dot-connecting'
    : coreOk
      ? 'status-dot-connected'
      : 'status-dot-error';

  const label = statusLoading
    ? 'Checking...'
    : coreOk
      ? 'Core system ready'
      : 'Core issue detected';

  const count = coreTotal > 0 ? `${coreUp}/${coreTotal}` : (systemStatus?.count || '0/0');
  const openControlTower = (event) => {
    event.preventDefault();
    event.stopPropagation();
    dispatch({ type: 'SET_TAB', payload: 'control' });
  };

  return (
    <div className="relative z-10 flex items-center overflow-hidden rounded-xl glass text-sm">
      <button
        type="button"
        onClick={openControlTower}
        className="flex cursor-pointer items-center gap-2 px-2 py-2 glass-hover lg:pl-3 lg:pr-2"
        title="Open Control Tower"
        aria-label="Open Control Tower"
      >
        <span className={dotClass} />
        <span className="text-slate-500 font-medium text-xs">{count}</span>
        <span className="text-slate-600 text-xs hidden lg:block">{label}</span>
      </button>
      <button
        type="button"
        onClick={check}
        className="flex h-9 w-8 items-center justify-center border-l border-white/70 text-slate-400 hover:bg-white/70 hover:text-slate-600"
        title="Refresh connection"
        aria-label="Refresh connection"
      >
        <FaSyncAlt className={`text-xs ${statusLoading ? 'animate-spin' : ''}`} />
      </button>
    </div>
  );
}
