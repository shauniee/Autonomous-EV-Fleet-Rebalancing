export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(path: string, options: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const { timeoutMs = 8000, ...requestOptions } = options;
  const response = await fetch(path, {
    ...requestOptions,
    signal: requestOptions.signal ? AbortSignal.any([requestOptions.signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
    headers: { 'Content-Type': 'application/json', ...requestOptions.headers },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, body?.error?.code || body?.detail || `HTTP ${response.status}`);
  if (body === null) throw new Error('Invalid response from service');
  return body as T;
}
export const message = (error: unknown) => error instanceof Error ? error.message : 'Service request failed';
