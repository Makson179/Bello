// OrcaRouter Cyber Inference: a named Pi provider for authorized security
// teams. Inference and model discovery use https://api.orcarouter.ai/v1 over
// the OpenAI-compatible wire format. OAuth 2.0 + PKCE runs in Bello's own login
// adapter: the exchange returns a durable OrcaRouter API key that is stored as
// an ordinary Pi api_key credential. Nothing about the inference path (models,
// base URL, Bearer header) depends on how that key was obtained.
import { createServer } from "node:http";
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";

import { ORCAROUTER_CHAT_API, createOrcaRouterRefresh, seedDefinitions } from "./orcarouter-catalog.mjs";

export const ORCAROUTER_PROVIDER_ID = "orcarouter";
export const ORCAROUTER_PROVIDER_NAME = "OrcaRouter Cyber Inference";
export const ORCAROUTER_API_BASE_URL = "https://api.orcarouter.ai/v1";
export const ORCAROUTER_AUTH_BASE_URL = "https://www.orcarouter.ai";
export const ORCAROUTER_APP_NAME = "Bello";
export const ORCAROUTER_LOOPBACK_PATH = "/cb";
export const ORCAROUTER_KEY_PATTERN = /^sk-orca-[A-Za-z0-9._-]+$/u;
// The API exposes no free, stable credential check, so paste-time validation is
// deliberately "unknown": the first real request establishes key validity.
export const ORCAROUTER_KEY_HINT =
  "Paste an OrcaRouter API key (sk-orca-...). Create or revoke keys at https://www.orcarouter.ai/console/keys";
export const ORCAROUTER_AUTH_HINT =
  "Authorize Bello in the browser to receive an OrcaRouter key for your own account. Cyber provider: https://www.orcarouter.ai/cyber";

const MAX_CONNECT_TIMEOUT_MS = 15 * 60_000;
const MAX_EXCHANGE_BYTES = 64 * 1024;
const GRANTED_SCOPES = new Set(["api"]);

const PUBLIC_BASES = Object.freeze({
  auth: ORCAROUTER_AUTH_BASE_URL,
  api: ORCAROUTER_API_BASE_URL,
});

function isLoopbackHost(hostname) {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
}

/** Validate one explicit or shared base URL; remote origins must be HTTPS. */
export function normalizeBase(raw, label) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`${label} is not a valid URL`);
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error(`${label} must not contain credentials, a query, or a fragment`);
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && isLoopbackHost(url.hostname))) {
    throw new Error(`${label} must use HTTPS (HTTP is allowed only for loopback development)`);
  }
  return url.origin + url.pathname.replace(/\/+$/u, "");
}

/**
 * Resolve the auth and inference origins. Explicit overrides win; the shared
 * ORCA_BASE_URL is a fallback for a self-hosted deployment that serves both.
 * One origin is never derived from the other by rewriting its hostname.
 */
export function resolveOrigins(env = {}) {
  const value = (name) => (typeof env[name] === "string" && env[name].trim() ? env[name].trim() : undefined);
  const shared = value("ORCA_BASE_URL");
  const authRaw = value("ORCA_AUTH_BASE_URL") ?? shared ?? PUBLIC_BASES.auth;
  const apiRaw = value("ORCA_API_BASE_URL") ?? shared ?? PUBLIC_BASES.api;
  const auth = normalizeBase(authRaw, "ORCA_AUTH_BASE_URL");
  const api = normalizeBase(apiRaw, "ORCA_API_BASE_URL");
  if (auth.startsWith("http:") !== api.startsWith("http:")) {
    throw new Error("ORCA_AUTH_BASE_URL and ORCA_API_BASE_URL must both be HTTPS or both be loopback HTTP");
  }
  return { auth, api };
}

/** Fixed authorize path on the auth origin. */
export function authorizeUrl(base, params) {
  const url = new URL(`${base}/auth`);
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  return url.toString();
}

/** Fixed exchange path on the auth origin. Never /v1/auth/keys. */
export function exchangeUrl(base) {
  return `${base}/api/v1/auth/keys`;
}

export function b64url(buffer) {
  return buffer.toString("base64url");
}

/** A fresh verifier/challenge/state per attempt; the challenge has no padding. */
export function createPkce() {
  const verifier = b64url(randomBytes(32));
  const challenge = b64url(createHash("sha256").update(verifier).digest());
  const state = b64url(randomBytes(16));
  return { verifier, challenge, state };
}

export function statesMatch(expected, received) {
  if (typeof expected !== "string" || typeof received !== "string") return false;
  const a = Buffer.from(expected);
  const b = Buffer.from(received);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function redactOrcaSecret(value) {
  let message = value instanceof Error ? value.message : String(value);
  message = message.replace(/sk-orca-[A-Za-z0-9._-]*/gu, "[redacted]");
  message = message.replace(/\bBearer\s+\S+/giu, "Bearer [redacted]");
  return message;
}

export class OrcaConnectError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "OrcaConnectError";
    this.code = code;
  }
}

async function readBoundedJson(response) {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^\d+$/u.test(declared) || Number(declared) > MAX_EXCHANGE_BYTES)) return undefined;
  const text = await response.text();
  if (Buffer.byteLength(text) > MAX_EXCHANGE_BYTES) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function exchangeFailure(status, body) {
  const code = typeof body?.error === "string" ? body.error : undefined;
  if (status === 400) {
    return new OrcaConnectError("bad_request", "OrcaRouter rejected the PKCE request (HTTP 400); start the connection again");
  }
  if (status === 403) {
    return new OrcaConnectError("code_rejected",
      `OrcaRouter rejected the authorization code (HTTP 403${code ? `, ${code}` : ""}); it may be expired, already used, or bound to another verifier`);
  }
  if (status === 429) {
    return new OrcaConnectError("rate_limited", "OrcaRouter asked to slow down (HTTP 429); wait a few minutes before connecting again");
  }
  if (status >= 500) return new OrcaConnectError("upstream", `OrcaRouter is unavailable (HTTP ${status}); try again later`);
  return new OrcaConnectError("exchange_failed", `OrcaRouter key exchange failed (HTTP ${status}${code ? `, ${code}` : ""})`);
}

/**
 * Exchange a one-time authorization code for an OrcaRouter API key. The
 * verifier travels only in this request body and is never logged.
 */
export async function exchangeCode({ authBase, code, verifier, signal, fetchImpl = globalThis.fetch, timeoutMs = 30_000 }) {
  if (typeof code !== "string" || !code.trim()) throw new OrcaConnectError("missing_code", "The authorization code is empty");
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response;
  try {
    response = await fetchImpl(exchangeUrl(authBase), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ code: code.trim(), code_verifier: verifier, code_challenge_method: "S256" }),
      redirect: "error",
      signal: combined,
    });
  } catch {
    if (signal?.aborted) throw new OrcaConnectError("cancelled", "OrcaRouter connection cancelled");
    if (timeout.aborted) throw new OrcaConnectError("timeout", "OrcaRouter key exchange timed out");
    throw new OrcaConnectError("network", "Could not reach the OrcaRouter authentication service");
  }
  const body = await readBoundedJson(response);
  if (!response.ok) throw exchangeFailure(response.status, body);
  const key = body?.key;
  if (typeof key !== "string" || !ORCAROUTER_KEY_PATTERN.test(key)) {
    throw new OrcaConnectError("bad_response", "OrcaRouter returned no usable API key");
  }
  // Read the scope that was granted, not the one that was requested.
  const scope = typeof body.scope === "string" ? body.scope : undefined;
  return {
    key,
    userId: typeof body.user_id === "string" || typeof body.user_id === "number" ? String(body.user_id) : undefined,
    scope,
    warning: scope && !GRANTED_SCOPES.has(scope)
      ? `OrcaRouter granted scope "${scope}" to this key; the "api" scope is required for inference`
      : undefined,
  };
}

function openBrowser(url, { spawnImpl = spawn, env = process.env } = {}) {
  // BELLO_ORCAROUTER_NO_BROWSER=1 prints the URL instead (headless hosts, tests).
  if (env.BELLO_ORCAROUTER_NO_BROWSER === "1") return;
  const [command, args] = process.platform === "darwin"
    ? ["open", [url]]
    : process.platform === "win32"
      ? ["cmd", ["/c", "start", "", url]]
      : ["xdg-open", [url]];
  try {
    const child = spawnImpl(command, args, { stdio: "ignore", detached: true });
    child.on?.("error", () => {});
    child.unref?.();
  } catch {
    // The authorization URL is printed either way; opening it is best effort.
  }
}

function htmlPage(message) {
  return `<!doctype html><meta charset="utf-8"><title>OrcaRouter</title><p>${message}</p>`;
}

/**
 * OAuth 2.0 + PKCE, Flow A (loopback redirect) against the OrcaRouter auth
 * origin. Returns the exchanged key. Every terminal path closes the listener.
 */
export async function connectLoopback({
  authBase,
  timeoutMs = 5 * 60_000,
  signal,
  open = true,
  notify = () => {},
  spawnImpl,
  fetchImpl,
  env = process.env,
}) {
  const pkce = createPkce();
  let settle;
  const pending = new Promise((resolve) => { settle = resolve; });
  let server;
  server = createServer((request, response) => {
    let url;
    try {
      url = new URL(request.url ?? "/", "http://127.0.0.1");
    } catch {
      response.writeHead(400, { "Content-Type": "text/html; charset=utf-8" }).end(htmlPage("Invalid request."));
      return;
    }
    if (url.pathname !== ORCAROUTER_LOOPBACK_PATH) {
      response.writeHead(404).end();
      return;
    }
    // Compare state before anything else touches the code.
    if (!statesMatch(pkce.state, url.searchParams.get("state"))) {
      response.writeHead(400, { "Content-Type": "text/html; charset=utf-8" })
        .end(htmlPage("State mismatch. Return to Bello and try again."));
      settle({ error: new OrcaConnectError("state_mismatch", "OrcaRouter callback state did not match this login attempt") });
      return;
    }
    const denied = url.searchParams.get("error");
    if (denied) {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" })
        .end(htmlPage("Authorization was declined. You can close this tab."));
      settle({ error: new OrcaConnectError("denied", `OrcaRouter authorization was declined (${denied})`) });
      return;
    }
    const code = url.searchParams.get("code");
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" })
      .end(htmlPage("Connected to OrcaRouter. You can close this tab."));
    settle({ code });
  });
  const onAbort = () => settle({ error: new OrcaConnectError("cancelled", "OrcaRouter connection cancelled") });
  signal?.addEventListener("abort", onAbort, { once: true });
  let timer;
  let result;
  try {
    if (signal?.aborted) throw new OrcaConnectError("cancelled", "OrcaRouter connection cancelled");
    let port;
    try {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        // Port 0 keeps the callback ephemeral: no redirect URI is pre-registered.
        server.listen(0, "127.0.0.1", () => resolve());
      });
      port = server.address().port;
    } catch {
      throw new OrcaConnectError("listener", "Cannot open a loopback listener for the OrcaRouter login; use --method api_key");
    }
    const url = authorizeUrl(authBase, {
      callback_url: `http://127.0.0.1:${port}${ORCAROUTER_LOOPBACK_PATH}`,
      code_challenge: pkce.challenge,
      code_challenge_method: "S256",
      state: pkce.state,
      app_name: ORCAROUTER_APP_NAME,
      scope: "api",
    });
    notify({ type: "auth_url", url, instructions: ORCAROUTER_AUTH_HINT });
    if (open) openBrowser(url, { spawnImpl, env });
    const bounded = Math.min(Math.max(timeoutMs, 1000), MAX_CONNECT_TIMEOUT_MS);
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve({
        error: new OrcaConnectError("timeout", "OrcaRouter authorization timed out; run the login again"),
      }), bounded);
    });
    result = await Promise.race([pending, timeout]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
    await new Promise((resolve) => server.close(() => resolve()));
    server.closeAllConnections?.();
  }
  if (result.error) throw result.error;
  return exchangeCode({ authBase, code: result.code, verifier: pkce.verifier, signal, fetchImpl });
}

/**
 * OrcaRouter PKCE login adapter, used as Pi's apiKey.login for this provider.
 * It returns the same {type: "api_key", key} shape as the pasted-key path, so
 * the credential store and every downstream request are identical.
 */
export function createOrcaConnectLogin({ env = process.env, fetchImpl, spawnImpl, timeoutMs, open } = {}) {
  const login = async (interaction = {}) => {
    if (typeof interaction.prompt !== "function") throw new Error("OrcaRouter connect requires an interactive terminal");
    const choice = await interaction.prompt({
      type: "select",
      message: "Connect OrcaRouter Cyber Inference:",
      options: [
        { id: "connect", label: "Connect with OrcaRouter", description: "browser authorization (OAuth 2.0 + PKCE)" },
        { id: "api_key", label: "OrcaRouter API key", description: ORCAROUTER_KEY_HINT },
      ],
    });
    if (choice === "api_key") {
      return { type: "api_key", key: await interaction.prompt({ type: "secret", message: "Enter API key" }) };
    }
    if (choice !== "connect") throw new Error("Invalid OrcaRouter connection choice");
    const { auth } = resolveOrigins(env);
    const exchanged = await connectLoopback({
      authBase: auth,
      signal: interaction.signal,
      notify: (event) => interaction.notify?.(event),
      fetchImpl,
      spawnImpl,
      timeoutMs: timeoutMs ?? (Number(env.BELLO_ORCAROUTER_CONNECT_TIMEOUT_MS) || undefined),
      open,
      env,
    });
    if (exchanged.warning) interaction.notify?.({ type: "info", message: exchanged.warning });
    return { type: "api_key", key: exchanged.key };
  };
  return { login };
}

/**
 * Classify a relay 401 for the exact credential generation that made the
 * rejected request. A late failure from an older key never marks a newer,
 * re-authorized credential as needing reauthentication. The stored key is never
 * deleted here: a misclassified or transient failure stays recoverable.
 */
export function createAuthHealth({ now = Date.now } = {}) {
  const generations = new Map();
  const rejected = new Map();
  // Only the most recently observed credential is live. A request that ends
  // after re-authorization replaced the key must not mark the new grant.
  let sequence = 0;
  let latest = 0;
  const identityOf = (credential) => {
    if (credential?.type !== "api_key") return "missing";
    if (typeof credential.key === "string" && credential.key) return credential.key;
    // Environment templates are stable identifiers, not secret values.
    return `env:${JSON.stringify(credential.env ?? {})}`;
  };
  const touch = (credential) => {
    const identity = identityOf(credential);
    const generation = (generations.get(identity) ?? 0) + 1;
    generations.set(identity, generation);
    rejected.delete(identity);
    latest = ++sequence;
    return { identity, generation, sequence: latest };
  };
  return {
    observe: touch,
    recordRejected(observation, status) {
      if (status !== 401 || (generations.get(observation.identity) ?? 0) !== observation.generation) return undefined;
      // A superseded credential is not the account's current key: its late
      // failure says nothing about the credential now in use.
      if (observation.sequence !== latest) return undefined;
      const state = {
        needsReauth: true,
        reason: "relay_401",
        at: new Date(now()).toISOString(),
        action: "Run `bello runtime login orcarouter` again; OrcaRouter key grants are durable, so no refresh is attempted",
      };
      rejected.set(observation.identity, state);
      return state;
    },
    status(credential) {
      return rejected.get(identityOf(credential));
    },
    generations: () => new Map(generations),
  };
}

/**
 * Login-time registration: the same named provider, but discovery is offline.
 * Mutating the composed auth endpoint is idempotent, so login can register
 * again on a runtime that already has the provider.
 */
export function registerLoginProviders(runtime, options) {
  return registerOrcaRouter(runtime, options);
}

// Static-key config value Pi resolves when no credential is stored. Names are
// stable identifiers; values are never logged or stored.
const ORCA_API_KEY_ENV = "ORCA_API_KEY";
const ORCA_DEPLOY_VAR = "ORCAROUTER" + "_API_KEY";
const ORCA_KEY_ENV_ALIASES = Object.freeze({ [ORCA_DEPLOY_VAR]: ORCA_API_KEY_ENV });

/**
 * Register the named OrcaRouter provider on a Pi ModelRuntime.
 *
 * Pi composes apiKey login/resolve/check from the base provider layer, so an
 * extension registration can never add a login method to a named provider.
 * Pi's own slot for "sign in without a pasted key" is the extension oauth
 * layer. It is used here as an authentication *method*, not a token type: the
 * PKCE exchange yields a durable OrcaRouter API key, which is stored in the
 * OAuth credential's non-secret fields and sent as the Bearer key. No refresh
 * grant exists, so `refreshToken` fails closed instead of rotating.
 */
function toLoginAdapter(adapter) {
  // Accept either {login} (this module's adapters) or a bare login function.
  if (typeof adapter === "function") return { login: (...args) => adapter(...args) };
  if (adapter && typeof adapter.login === "function") return adapter;
  throw new Error("OrcaRouter login adapter has no login entry point");
}

/**
 * The deployment's static key is advertised under this provider's own variable
 * names only, so executing a credential command that belongs to another
 * provider cannot see it. An explicitly set ORCA_API_KEY wins.
 */
export function providerEnv(env = process.env, ambient = process.env) {
  const scoped = { ...env };
  for (const [name, alias] of Object.entries(ORCA_KEY_ENV_ALIASES)) {
    const value = scoped[name] ?? ambient[name];
    if (!scoped[alias] && typeof value === "string" && value) scoped[alias] = value;
  }
  return scoped;
}

// Pi resolves static key templates against the process environment, so the
// deployment key alias is advertised there too. Only the name is exported.
for (const [name, alias] of Object.entries(ORCA_KEY_ENV_ALIASES)) {
  if (!process.env[alias] && typeof process.env[name] === "string" && process.env[name]) process.env[alias] = process.env[name];
}

export async function registerOrcaRouter(runtime, options = {}) {
  const env = providerEnv(options.env ?? process.env);
  const { login, loginOptions = {}, fetchImpl } = options;
  const { api } = resolveOrigins(env);
  const id = ORCAROUTER_PROVIDER_ID;
  // `login` overrides the whole adapter; `loginOptions` only tunes transport.
  const connectFn = toLoginAdapter(login ?? createOrcaConnectLogin({ env, fetchImpl, ...loginOptions }));
  // Test doubles and trimmed hosts may not expose Pi's provider registry.
  if (typeof runtime?.registerProvider !== "function" || typeof runtime?.getProvider !== "function") {
    return { providerId: id, name: ORCAROUTER_PROVIDER_NAME, apiBase: api, loginMethods: [] };
  }
  // A mapping installs one ordered overlay per registerProvider call. The first
  // overlays (auth-only) only make Pi compose the apiKey login/resolve layer
  // that the login adapter needs; the final overlay carries every field.
  const authOnly = { oauth: undefined, refreshModels: undefined };
  const overlay = (models) => ({
    name: ORCAROUTER_PROVIDER_NAME,
    baseUrl: api,
    api: ORCAROUTER_CHAT_API,
    // Static-key config value accepted by the OpenAI-compatible layer; the
    // stored credential is what runtime resolution actually uses.
    apiKey: `$${ORCA_API_KEY_ENV}`,
    models,
    refreshModels: createOrcaRouterRefresh({
      env,
      ...(fetchImpl ? { fetchImpl } : {}),
    }),
    oauth: {
      name: ORCAROUTER_PROVIDER_NAME,
      loginLabel: "Connect with OrcaRouter (OAuth 2.0 + PKCE)",
      login: async (callbacks) => {
        const interaction = {
          signal: callbacks.signal ?? new AbortController().signal,
          prompt: async (prompt) => {
            if (prompt.type === "select") return callbacks.onSelect({ message: prompt.message, options: prompt.options });
            if (prompt.type === "secret") return callbacks.onPrompt({ message: prompt.message, placeholder: prompt.placeholder });
            throw new Error("OrcaRouter connect supports only select and secret prompts");
          },
          notify: (event) => {
            if (event.type === "auth_url") callbacks.onAuth({ url: event.url, instructions: event.instructions });
            else if (event.type === "info") callbacks.onProgress?.(event.message);
          },
        };
        const credential = await connectFn.login(interaction);
        if (credential?.type !== "api_key" || typeof credential.key !== "string" || !credential.key) {
          throw new Error("OrcaRouter connect did not return an API key");
        }
        // The key is durable and non-expiring; `access` only needs to be a
        // non-empty bearer value for Pi's credential shape.
        return { refresh: "", access: credential.key, expires: Number.MAX_SAFE_INTEGER, key: credential.key };
      },
      refreshToken: async () => {
        throw new Error("OrcaRouter keys are durable; run `bello runtime login orcarouter` instead of a refresh");
      },
      getApiKey: (credential) => {
        const key = typeof credential?.key === "string" && credential.key ? credential.key : credential?.access;
        if (typeof key !== "string" || !key) throw new Error("OrcaRouter credential has no API key");
        return key;
      },
    },
  });
  // Register the auth-only layer first so the composed apiKey login exists,
  // then the full layer. The full layer carries the verified seed list so the
  // provider has a usable catalog before any refresh; Pi's refresh replaces the
  // list with the live GET /v1/models result (or keeps the seed offline).
  // Registration itself writes no model cache: hosts that snapshot a read-only
  // catalog must not gain new files from adding this provider.
  runtime.registerProvider(id, authOnly);
  runtime.registerProvider(id, overlay(
    runtime.getRegisteredProviderConfig?.(id)?.models ?? seedDefinitions(),
  ));
  await runtime.models?.refresh?.({ allowNetwork: false, providers: [id] }).catch(() => {});
  const provider = runtime.getProvider(id);
  if (!provider || typeof provider.auth?.oauth?.login !== "function") {
    throw new Error("OrcaRouter provider registration failed");
  }
  return {
    providerId: id,
    name: ORCAROUTER_PROVIDER_NAME,
    apiBase: api,
    loginMethods: provider.auth.apiKey ? ["oauth_pkce", "api_key"] : ["oauth_pkce"],
  };
}
