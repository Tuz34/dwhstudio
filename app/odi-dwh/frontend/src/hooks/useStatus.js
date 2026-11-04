import { useEffect, useCallback } from 'react';
import { useDispatch } from '../store/AppContext';
import { getStatus } from '../services/api';

export function useStatus() {
  const dispatch = useDispatch();

  const check = useCallback(async () => {
    dispatch({ type: 'SET_STATUS_LOADING' });
    try {
      const data = await getStatus();
      dispatch({ type: 'SET_STATUS', payload: data });
    } catch {
      dispatch({ type: 'SET_STATUS', payload: { ok: false, checks: {}, count: '0/0' } });
    }
  }, [dispatch]);

  useEffect(() => {
    check();
  }, [check]);

  return { check };
}
