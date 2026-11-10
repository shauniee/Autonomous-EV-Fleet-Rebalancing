import { useCallback, useEffect, useState } from 'react';
import { api, message } from '../api/client';

export interface Resource<T> { data?: T; error?: string; updatedAt?: string }
export function usePolling<T>(path: string | null, interval = 2000, read: typeof api<T> = api<T>) {
  const [state, setState] = useState<Resource<T> & { path?: string }>({});
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(x => x + 1), []);
  useEffect(() => {
    if (path === null) return;
    const endpoint = path;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    async function poll() {
      try {
        const data = await read(endpoint, { signal: controller.signal });
        if (controller.signal.aborted) return;
        failures = 0;
        setState({ path: endpoint, data, updatedAt: new Date().toISOString() });
      } catch (error) {
        if (controller.signal.aborted) return;
        failures++;
        setState(previous => ({ ...(previous.path === endpoint ? previous : {}), path: endpoint, error: message(error) }));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, Math.min(interval * 2 ** failures, 10000));
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, interval, revision, read]);
  useEffect(() => {
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [refresh]);
  return { ...(state.path === path ? state : {}), refresh };
}
