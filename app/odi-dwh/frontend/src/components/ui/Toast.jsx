import { useEffect } from 'react';
import { FaCheckCircle, FaTimesCircle } from 'react-icons/fa';
import { useApp, useDispatch } from '../../store/AppContext';

export function Toast() {
  const { toast } = useApp();
  const dispatch = useDispatch();

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => dispatch({ type: 'CLEAR_TOAST' }), 3500);
    return () => clearTimeout(t);
  }, [toast, dispatch]);

  if (!toast) return null;

  return (
    <div className={toast.type === 'success' ? 'toast-success' : 'toast-error'}>
      {toast.type === 'success' ? <FaCheckCircle /> : <FaTimesCircle />}
      {toast.message}
    </div>
  );
}
