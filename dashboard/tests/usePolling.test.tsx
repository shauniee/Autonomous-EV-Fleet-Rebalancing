import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { usePolling } from '../src/hooks/usePolling';

afterEach(() => vi.useRealTimers());
it('retains the last snapshot with an explicit error after a failed refresh', async () => {
  const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ version: 1 })));
  const { result } = renderHook(() => usePolling<{ version: number }>('/test'));
  await waitFor(() => expect(result.current.data?.version).toBe(1));
  fetch.mockRejectedValue(new Error('Disconnected'));
  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.error).toBe('Disconnected'));
  expect(result.current.data?.version).toBe(1);
  expect(result.current.updatedAt).toBeDefined();
});
it('does not overlap polls and ignores a cancelled older response', async () => {
  vi.useFakeTimers();
  const complete: ((response: Response) => void)[] = [];
  const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(resolve => complete.push(resolve)));
  const { result } = renderHook(() => usePolling<{ version: number }>('/test', 1000));
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(fetch).toHaveBeenCalledTimes(1);
  act(() => result.current.refresh());
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async () => complete[1](new Response(JSON.stringify({ version: 2 }))));
  await act(async () => complete[0](new Response(JSON.stringify({ version: 1 }))));
  expect(result.current.data?.version).toBe(2);
});
