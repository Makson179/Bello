// Retry only OpenRouter's documented pre-provider in-flight-budget rejection.
// Never retry an accepted (HTTP 200) stream or replay a model/agent turn.
import { setTimeout as sleep } from "node:timers/promises";

const ENDPOINT = "https://openrouter.ai/api/v1/chat/completions";
const MAX_ERROR_BYTES = 64 * 1024;
const MAX_INSPECTION_MS = 5000;
const installed = new WeakSet();

function throwIfAborted(signal, response) {
  if (!signal?.aborted) return;
  // Once fetch has returned, this wrapper owns the response until it returns
  // it to the SDK. Release that body if cancellation prevents the handoff.
  // Do not await tee cancellation: an inspection clone may still be closing.
  if (response?.body) void response.body.cancel().catch(() => {});
  signal.throwIfAborted();
}

export function retryAfterMs(value, now = Date.now()) {
  if (typeof value !== "string" || value.length > 128) return undefined;
  const text = value.trim();
  if (/^\d+$/.test(text)) {
    const seconds = Number(text);
    return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000
      ? seconds * 1000 : undefined;
  }
  // Accept the standard IMF-fixdate form; do not treat arbitrary parseable text
  // (e.g. "1.5", "-1", or a bare year) as an HTTP date.
  if (!/^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(text)) return undefined;
  const date = Date.parse(text);
  return Number.isFinite(date) && new Date(date).toUTCString() === text ? Math.max(0, date - now) : undefined;
}

function replayableRequest(input, init) {
  // The pinned OpenAI adapter passes a URL and serialized JSON. A consumed
  // Request/stream body is deliberately not replayed, even if its URL matches.
  if (!(typeof input === "string" || input instanceof URL)) return false;
  return String(input) === ENDPOINT && init?.method?.toUpperCase() === "POST"
    && typeof init.body === "string"
    && new Headers(init.headers).get("content-type")?.split(";", 1)[0].trim().toLowerCase() === "application/json";
}

async function errorBody(response, signal) {
  const declaredLength = response.headers.get("content-length");
  if (declaredLength !== null && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > MAX_ERROR_BYTES)) return undefined;
  if (response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") return undefined;
  let reader;
  let timer;
  let stop;
  let onAbort;
  try {
    reader = response.clone().body?.getReader();
    if (!reader) return undefined;
    const stopped = new Promise((resolve) => { stop = () => resolve({ stopped: true }); });
    timer = setTimeout(stop, MAX_INSPECTION_MS);
    onAbort = stop;
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) return undefined;
    const chunks = [];
    let size = 0;
    for (;;) {
      const part = await Promise.race([reader.read(), stopped]);
      if (part.stopped) return undefined;
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_ERROR_BYTES) return undefined;
      chunks.push(part.value);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return undefined; // Preserve the original HTTP response, not a parser error.
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
    // A clone is tee'd with the original response. Awaiting cancel can wait for
    // the original SDK reader; do not block its access to the unchanged error.
    if (reader) void reader.cancel().catch(() => {});
  }
}

export function createOpenRouterBackpressureFetch(baseFetch, {
  maxRetries = 2,
  maxWaitMs = 120_000,
  now = Date.now,
  wait = (ms, signal) => sleep(ms, undefined, { signal }),
} = {}) {
  if (typeof baseFetch !== "function" || !Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 2
      || !Number.isFinite(maxWaitMs) || maxWaitMs < 0 || maxWaitMs > 120_000) {
    throw new TypeError("Invalid OpenRouter backpressure policy");
  }
  return async function fetchWithBackpressure(input, init) {
    if (!replayableRequest(input, init)) return baseFetch(input, init);
    const signal = init.signal;
    const started = now();
    let waited = 0;
    let attempts = 0;
    for (;;) {
      signal?.throwIfAborted();
      const response = await baseFetch(input, init);
      throwIfAborted(signal, response);
      if (response.status !== 402 || response.redirected || (response.url && response.url !== ENDPOINT)
          || attempts >= maxRetries) return response;
      const delay = retryAfterMs(response.headers.get("retry-after"), now());
      if (delay === undefined) return response;
      const body = await errorBody(response, signal);
      throwIfAborted(signal, response);
      if (body?.error?.code !== 402 || body.error.metadata?.limit_source !== "openrouter_in_flight_budget"
          || body.error.metadata?.reason !== "in_flight_budget_exhausted") return response;
      // Never shorten Retry-After to fit the cap. Preserve the last original
      // 402 for normal SDK error reporting when the bounded budget is exhausted.
      if (delay > maxWaitMs - waited || delay > maxWaitMs - (now() - started)) return response;
      attempts++;
      waited += delay;
      if (response.body) void response.body.cancel().catch(() => {});
      await wait(delay, signal);
    }
  };
}

export function installOpenRouterBackpressure(agent) {
  if (installed.has(agent)) return;
  const previous = agent.streamFunction;
  if (typeof previous !== "function") throw new TypeError("Pi stream hook is unavailable");
  agent.streamFunction = function streamWithBackpressure(model, context, options) {
    if (model?.provider !== "openrouter" || model?.api !== "openai-completions") {
      return previous.call(this, model, context, options);
    }
    return previous.call(this, model, context, {
      ...options,
      fetch: createOpenRouterBackpressureFetch(options?.fetch ?? globalThis.fetch),
    });
  };
  installed.add(agent);
}
