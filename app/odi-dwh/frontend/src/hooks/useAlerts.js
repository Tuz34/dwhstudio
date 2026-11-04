import { useEffect, useCallback } from 'react';
import { useDispatch } from '../store/AppContext';
import { getAlerts } from '../services/api';

export function useAlerts() {
  const dispatch = useDispatch();

  const check = useCallback(async () => {
    try {
      const data = await getAlerts();
      const alerts = Array.isArray(data) ? data : [];
      dispatch({ type: 'SET_ALERTS', payload: alerts });
    } catch {
      // stay silent
    }
  }, [dispatch]);

  useEffect(() => {
    check();
    const interval = setInterval(check, 60_000);
    return () => clearInterval(interval);
  }, [check]);

  return { check };
}
