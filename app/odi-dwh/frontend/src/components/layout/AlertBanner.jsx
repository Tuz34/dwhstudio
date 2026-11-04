import { FaBell, FaCheckDouble } from 'react-icons/fa';
import { useApp, useDispatch } from '../../store/AppContext';
import { acknowledgeAlerts } from '../../services/api';

export function AlertBanner() {
  const { alerts } = useApp();
  const dispatch = useDispatch();

  if (!alerts || alerts.length === 0) return null;

  const critical = alerts.filter(a => a.severity === 'CRITICAL').length;
  const label = critical > 0
    ? `${critical} critical alerts pending`
    : `${alerts.length} alerts pending`;

  async function handleDismissAll() {
    if (!window.confirm('Acknowledge all visible alerts?')) return;
    try {
      await acknowledgeAlerts({ approved: true, all: true });
      dispatch({ type: 'SET_ALERTS', payload: [] });
    } catch { /* ignore */ }
  }

  return (
    <div className="flex items-center justify-between px-4 py-2 bg-red-600 text-white text-sm animate-slide-down">
      <div className="flex items-center gap-2">
        <FaBell className="animate-pulse" />
        <span className="font-medium">{label}</span>
      </div>
      <button
        onClick={handleDismissAll}
        className="flex items-center gap-1.5 px-3 py-1 rounded-lg bg-white/20 hover:bg-white/30 transition-colors text-xs font-medium"
      >
        <FaCheckDouble />
        Acknowledge All
      </button>
    </div>
  );
}
