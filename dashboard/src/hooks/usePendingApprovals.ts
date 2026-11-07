import { useCallback, useRef, useState } from 'react';

export function usePendingApprovals<T extends { request_id: string }>(key: string, recommendationKey: keyof T) {
  const [requests, setRequests] = useState<T[]>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || '[]');
      return (Array.isArray(saved) ? saved : [saved]).filter(value =>
        value && typeof value.request_id === 'string' && typeof value[recommendationKey] === 'string');
    } catch { return []; }
  });
  const current = useRef(requests);
  const update = useCallback((request: T, keep: boolean) => {
    const next = current.current.filter(item => item.request_id !== request.request_id);
    if (keep) next.push(request);
    current.current = next;
    // Persist before submitting so a reload can retry the same ID.
    try {
      if (next.length) sessionStorage.setItem(key, JSON.stringify(next));
      else sessionStorage.removeItem(key);
    } catch { /* The in-memory queue still supports retries. */ }
    setRequests(next);
  }, [key]);
  return [requests, update] as const;
}
