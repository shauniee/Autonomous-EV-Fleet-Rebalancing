export class ApiError extends Error {
  constructor(public status: number, message: string, public reference?: string,
    public approvalDecision?: unknown) { super(message); }
}
export async function api<T>(path: string, options: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const { timeoutMs = 8000, ...requestOptions } = options;
  const response = await fetch(path, {
    ...requestOptions,
    signal: requestOptions.signal ? AbortSignal.any([requestOptions.signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
    headers: { 'Content-Type': 'application/json', ...requestOptions.headers },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const code = body?.error?.code ?? body?.detail;
    const reference = body?.error?.request_id ?? response.headers.get('X-Request-ID');
    throw new ApiError(response.status, typeof code === 'string' ? code : `HTTP ${response.status}`,
      typeof reference === 'string' ? reference : undefined, body?.error?.approval_decision);
  }
  if (body === null) throw new Error('Invalid response from service');
  return body as T;
}
export const message = (error: unknown) => error instanceof Error ? error.message : 'Service request failed';
