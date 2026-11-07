// Bello-owned discovery for the named OrcaRouter provider. Bello registers an
// empty OrcaRouter provider, so the provider's refreshModels hook supplies the
// catalog: the live GET {api}/models for the tenant key when the host allows
// network, otherwise the verified seed. Live results are authoritative; the
// seed is used only when discovery is unavailable, and it never replaces a
// non-empty live catalog with hand-written examples.
import { ORCAROUTER_API_BASE_URL, resolveOrigins } from "./orcarouter.mjs";

export const ORCAROUTER_CHAT_CAPABILITY = "chat";
const DISCOVERY_PATH = "/models";
const MAX_ID_LENGTH = 1024;
const MAX_DISCOVERY_MS = 15_000;
const MAX_DISCOVERY_BYTES = 2 * 1024 * 1024;
const MAX_DISCOVERY_ITEMS = 2_000;
const MAX_NAME = 120;
const TEXT_ENDPOINTS = new Set(["openai", "anthropic", "gemini", "openai-response"]);
// Router aliases (`orcarouter/*`) and some vendor records omit architecture.
// Input modalities are an optional hint, not a security filter; the required
// signal is that the model is served on a text chat endpoint above.
const REQUIRED_MODALITIES = new Set(["text"]);
// Chat models this client can speak. Non-text routes (image generation,
// video, rerank) are excluded by the endpoint filter below.
export const ORCAROUTER_CHAT_API = "openai-completions";

/**
 * Verified outage fallback. Only ids served by OrcaRouter are listed, metadata
 * is limited to what was verified, and `orcarouter/auto` is the default.
 */
export const ORCAROUTER_SEED = Object.freeze([
  Object.freeze({ id: "deepseek/deepseek-v4.1-flash", name: "DeepSeek V4.1 Flash", reasoning: false, input: ["text"] }),
  Object.freeze({ id: "orca/orcacyber-ultra-1.0", name: "OrcaCyber Ultra", reasoning: true, input: ["text"] }),
  Object.freeze({ id: "orca/orcacyber-zero-1.5", name: "OrcaCyber Zero", reasoning: true, input: ["text"] }),
  Object.freeze({ id: "orcarouter/fusion", name: "OrcaRouter Fusion", reasoning: false, input: ["text"] }),
]);

const SEED_METADATA = {
  contextWindow: 128_000,
  maxTokens: 16_384,
  // Verified reasoning ladders are kept for the cyber models; every other seed
  // model keeps Pi's default mapping.
  thinkingLevelMap: Object.freeze({ low: "low", medium: "medium", high: "high" }),
};

export const ORCAROUTER_DEFAULT_MODEL = ORCAROUTER_SEED[0].id;

function positiveInt(value) {
  return Number.isSafeInteger(value) && value > 0 && value <= 10_000_000 ? value : undefined;
}

/**
 * One /v1/models record -> catalog entry, or undefined when the record is not a
 * text chat model this client can speak. Unknown records fail closed.
 */
export function discoveryEntry(item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) return undefined;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  if (!id || id.length > MAX_ID_LENGTH || !/^[A-Za-z0-9][A-Za-z0-9._/-]*$/u.test(id)) return undefined;
  const endpoints = Array.isArray(item.supported_endpoint_types)
    ? item.supported_endpoint_types.filter((value) => typeof value === "string") : [];
  if (!endpoints.some((endpoint) => TEXT_ENDPOINTS.has(endpoint))) return undefined;
  // A declared list that excludes text is not a text chat model; an absent
  // list carries no information and keeps the text default.
  if (Array.isArray(item.architecture?.input_modalities)
    && !item.architecture.input_modalities.some((value) => typeof value === "string" && REQUIRED_MODALITIES.has(value))) {
    return undefined;
  }
  const modalities = Array.isArray(item.architecture?.input_modalities)
    ? item.architecture.input_modalities.filter((value) => typeof value === "string") : [];
  return {
    id,
    name: typeof item.name === "string" && item.name.trim() ? item.name.trim().slice(0, MAX_NAME) : id,
    input: modalities.length ? modalities.filter((mode) => mode === "text" || mode === "image") : ["text"],
    contextWindow: positiveInt(item.context_length ?? item.context_window),
    maxTokens: positiveInt(item.top_provider?.max_completion_tokens ?? item.max_completion_tokens),
  };
}

export function discoveryFromPayload(payload) {
  const items = Array.isArray(payload?.data) ? payload.data.slice(0, MAX_DISCOVERY_ITEMS) : [];
  const seen = new Set();
  const entries = [];
  for (const item of items) {
    const entry = discoveryEntry(item);
    if (!entry || seen.has(entry.id)) continue;
    seen.add(entry.id);
    entries.push(entry);
  }
  return entries;
}

export function discoveryUrl(apiBase = ORCAROUTER_API_BASE_URL) {
  const url = new URL(`${apiBase}${DISCOVERY_PATH}`);
  url.searchParams.set("capability", ORCAROUTER_CHAT_CAPABILITY);
  return url.toString();
}

/** Pi provider model definition for one catalog entry. */
export function modelDefinition(entry) {
  const seed = ORCAROUTER_SEED.find((item) => item.id === entry.id);
  const reasoning = seed?.reasoning ?? false;
  return {
    id: entry.id,
    name: entry.name,
    api: ORCAROUTER_CHAT_API,
    reasoning,
    input: entry.input ?? ["text"],
    contextWindow: entry.contextWindow ?? SEED_METADATA.contextWindow,
    maxTokens: entry.maxTokens ?? SEED_METADATA.maxTokens,
    // The router does not publish per-model rates, so usage is not billed
    // locally. Pi's cost accounting reads every rate field.
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, tiers: [] },
    ...(reasoning ? { thinkingLevelMap: { ...SEED_METADATA.thinkingLevelMap } } : {}),
  };
}

export function seedDefinitions() {
  return ORCAROUTER_SEED.map((seed) => modelDefinition({ ...seed, input: [...seed.input] }));
}

async function discoverLive(apiBase, key, { fetchImpl, timeoutMs }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(discoveryUrl(apiBase), {
      method: "GET",
      headers: { Authorization: `Bearer ${key}`, Accept: "application/json" },
      // Fixed API origin, no redirects, bounded body.
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok || response.redirected) return undefined;
    const declared = response.headers.get("content-length");
    if (declared !== null && (!/^\d+$/u.test(declared) || Number(declared) > MAX_DISCOVERY_BYTES)) return undefined;
    const text = await response.text();
    if (Buffer.byteLength(text) > MAX_DISCOVERY_BYTES) return undefined;
    const entries = discoveryFromPayload(JSON.parse(text));
    return entries.length ? entries.map(modelDefinition) : undefined;
  } catch {
    return undefined;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Build the provider-level refreshModels hook. Pi resolves the stored tenant
 * credential and passes it here; network access is the host's decision.
 */
export function createOrcaRouterRefresh({
  env = process.env,
  fetchImpl = globalThis.fetch,
  timeoutMs = MAX_DISCOVERY_MS,
} = {}) {
  return async function refreshModels(context = {}) {
    let definitions;
    if (context.allowNetwork === true && context.credential?.type === "api_key" && context.credential.key) {
      const { api } = resolveOrigins(env);
      definitions = await discoverLive(api, context.credential.key, { fetchImpl, timeoutMs });
    }
    // A successful live catalog is authoritative; the seed is only a fallback.
    // The model list is returned to Pi as the hook's result: Pi's composer
    // applies it as this provider's model layer. The hook does not publish from
    // here, because Pi's composer publishes a second, empty update after every
    // extension refresh and that later update would discard the list.
    return definitions ?? seedDefinitions();
  };
}
