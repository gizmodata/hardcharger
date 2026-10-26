// Normal API requests belong to this page. Abort them explicitly before navigation
// tears down its networking; keepalive session DELETEs use fetch directly instead.
export function createPageRequests(transport: typeof fetch = (input, init) => globalThis.fetch(input, init)) {
  const lifetime = new AbortController();
  const request: typeof fetch = async (input, init) => {
    lifetime.signal.throwIfAborted();
    const caller = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    return transport(input, {
      ...init,
      signal: caller ? AbortSignal.any([lifetime.signal, caller]) : lifetime.signal,
    });
  };
  return { fetch: request, abort: () => lifetime.abort() };
}

export const pageRequests = createPageRequests();
