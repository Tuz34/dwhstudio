import { FaTriangleExclamation, FaCheck, FaXmark } from 'react-icons/fa6';
import { useApp, useDispatch } from '../../store/AppContext';
import { executeQuery } from '../../services/api';

export function DmlBanner({ onResult }) {
  const { pendingDml } = useApp();
  const dispatch = useDispatch();

  if (!pendingDml) return null;

  async function handleApprove() {
    try {
      const result = await executeQuery({ sql: pendingDml.sql, label: pendingDml.label, approved: true });
      dispatch({ type: 'CLEAR_PENDING_DML' });
      onResult?.({ success: true, result });
    } catch (e) {
      dispatch({ type: 'CLEAR_PENDING_DML' });
      onResult?.({ success: false, error: e.message });
    }
  }

  function handleCancel() {
    dispatch({ type: 'CLEAR_PENDING_DML' });
  }

  return (
    <div className="flex items-center justify-between px-4 py-3 bg-amber-50 border-t border-amber-200 animate-slide-down">
      <div className="flex items-center gap-3">
        <FaTriangleExclamation className="text-amber-500 flex-shrink-0" />
        <div>
          <p className="text-xs font-semibold text-amber-800">DML Approval Required</p>
          <p className="text-xs text-amber-600 truncate max-w-[400px]">{pendingDml.label || pendingDml.sql}</p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={handleApprove}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-700 transition-colors"
        >
          <FaCheck /> Approve
        </button>
        <button
          onClick={handleCancel}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-200 text-slate-700 text-xs font-medium hover:bg-slate-300 transition-colors"
        >
          <FaXmark /> Cancel
        </button>
      </div>
    </div>
  );
}
