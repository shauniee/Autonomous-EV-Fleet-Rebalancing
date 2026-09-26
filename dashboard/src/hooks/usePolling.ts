import { useCallback, useEffect, useState } from 'react';
import { api, message } from '../api/client';

export interface Resource<T> { data?: T; error?: string; updatedAt?: string }
export function usePolling<T>(path: string, interval = 2000) {
  const [state, setState] = useState<Resource<T>>({});
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(x => x + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    async function poll() {
      try {
        const data = await api<T>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        failures = 0;
        setState({ data, updatedAt: new Date().toISOString() });
      } catch (error) {
        if (controller.signal.aborted) return;
        failures++;
        setState(previous => ({ ...previous, error: message(error) }));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, Math.min(interval * 2 ** failures, 10000));
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, interval, revision]);
  useEffect(() => {
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [refresh]);
  return { ...state, refresh };
}
