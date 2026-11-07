import assert from "node:assert/strict";
import { createServer } from "node:http";
import { Readable } from "node:stream";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";

import { createCatalogRuntime } from "../src/catalog.mjs";
import { enforceOrcaRouterReauth } from "../src/billing-route.mjs";
import {
  ORCAROUTER_DEFAULT_MODEL,
  ORCAROUTER_SEED,
  createOrcaRouterRefresh,
  discoveryFromPayload,
  discoveryUrl,
  modelDefinition,
  seedDefinitions,
} from "../src/orcarouter-catalog.mjs";
import {
  ORCAROUTER_API_BASE_URL,
  ORCAROUTER_AUTH_BASE_URL,
  authorizeUrl,
  b64url,
  connectLoopback,
  createAuthHealth,
  createOrcaConnectLogin,
  createPkce,
  exchangeCode,
  exchangeUrl,
  normalizeBase,
  redactOrcaSecret,
  registerOrcaRouter,
  resolveOrigins,
  statesMatch,
} from "../src/orcarouter.mjs";

const FAKE_KEY = "sk-orca-test-not-a-real-key";
const SECOND_KEY = "sk-orca-second-fixture-key";
const FIXTURE = JSON.parse(readFileSync(
  new URL("./fixtures/orcarouter-models.json", import.meta.url), "utf8",
));
const TEST_ENV = { ORCA_AUTH_BASE_URL: ORCAROUTER_AUTH_BASE_URL, ORCA_API_BASE_URL: ORCAROUTER_API_BASE_URL };

// --- Origins -----------------------------------------------------------------

test("provider uses the documented auth and inference origins", () => {
  assert.deepEqual(resolveOrigins({}), {
    auth: "https://www.orcarouter.ai",
    api: "https://api.orcarouter.ai/v1",
  });
  assert.equal(exchangeUrl("https://www.orcarouter.ai"), "https://www.orcarouter.ai/api/v1/auth/keys");
  assert.ok(!/^https:\/\/api\.orcarouter\.ai\/v1\/auth\/keys$/u.test(exchangeUrl("https://www.orcarouter.ai")));
  assert.equal(authorizeUrl("https://www.orcarouter.ai", { callback_url: "oob" }),
    "https://www.orcarouter.ai/auth?callback_url=oob");
});

test("explicit overrides win and the shared base is only a fallback", () => {
  assert.deepEqual(resolveOrigins({ ORCA_BASE_URL: "https://self.example.com" }), {
    auth: "https://self.example.com", api: "https://self.example.com",
  });
  assert.deepEqual(resolveOrigins({
    ORCA_BASE_URL: "https://shared.example.com",
    ORCA_AUTH_BASE_URL: "https://auth.example.com/",
    ORCA_API_BASE_URL: "https://relay.example.com/v1",
  }), { auth: "https://auth.example.com", api: "https://relay.example.com/v1" });
  // The auth origin is never derived from the API origin or vice versa.
  assert.equal(resolveOrigins({ ORCA_API_BASE_URL: "https://relay.example.com/v1" }).auth,
    ORCAROUTER_AUTH_BASE_URL);
});

test("remote origins require HTTPS; loopback HTTP is allowed for development", () => {
  assert.deepEqual(resolveOrigins({ ORCA_BASE_URL: "http://127.0.0.1:8080" }), {
    auth: "http://127.0.0.1:8080", api: "http://127.0.0.1:8080",
  });
  assert.throws(() => normalizeBase("http://example.com", "x"), /HTTPS/);
  assert.throws(() => normalizeBase("https://user:pass@example.com", "x"), /credentials/);
  assert.throws(() => normalizeBase("ftp://example.com", "x"), /HTTPS/);
  assert.throws(() => resolveOrigins({ ORCA_AUTH_BASE_URL: "http://10.0.0.2" }), /HTTPS/);
});

// --- PKCE primitives ---------------------------------------------------------

test("each attempt gets fresh random PKCE values with an unpadded S256 challenge", () => {
  const first = createPkce();
  const second = createPkce();
  assert.notEqual(first.verifier, second.verifier);
  assert.notEqual(first.state, second.state);
  assert.match(first.verifier, /^[A-Za-z0-9_-]{43}$/u);
  assert.equal(b64url(Buffer.from("test")), "dGVzdA");
  assert.equal(first.challenge, createHash("sha256").update(first.verifier).digest("base64url"));
  assert.ok(!first.challenge.includes("="));
  assert.notEqual(first.challenge, first.verifier);
});

test("state comparison rejects mismatches and missing values", () => {
  assert.equal(statesMatch("abc", "abc"), true);
  assert.equal(statesMatch("abc", "abd"), false);
  assert.equal(statesMatch("abc", "abcd"), false);
  assert.equal(statesMatch("abc", undefined), false);
});

// --- Connect flow ------------------------------------------------------------

function jsonResponse(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(text) });
  res.end(text);
}

test("connect performs OAuth 2.0 + PKCE on the auth origin and returns a plain API key", async () => {
  let opened;
  const exchanges = [];
  const login = createOrcaConnectLogin({
    env: TEST_ENV,
    fetchImpl: async (url, init) => {
      exchanges.push({ url, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ key: FAKE_KEY, user_id: "42", scope: "api" }), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
    },
    spawnImpl: () => ({ on() {}, unref() {} }),
    timeoutMs: 5000,
  });
  const notifications = [];
  const credential = await login.login({
    prompt: async (prompt) => {
      assert.equal(prompt.type, "select");
      assert.deepEqual(prompt.options.map((option) => option.label),
        ["Connect with OrcaRouter", "OrcaRouter API key"]);
      return "connect";
    },
    notify: (event) => {
      notifications.push(event);
      if (event.type === "auth_url") {
        opened = event.url;
        const url = new URL(event.url);
        const callback = new URL(url.searchParams.get("callback_url"));
        fetch(`${callback}?code=one-time-code&state=${url.searchParams.get("state")}`).catch(() => {});
      }
    },
  });
  assert.deepEqual(credential, { type: "api_key", key: FAKE_KEY });
  const authorize = new URL(opened);
  assert.equal(authorize.origin, "https://www.orcarouter.ai");
  assert.equal(authorize.pathname, "/auth");
  assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
  assert.equal(authorize.searchParams.get("scope"), "api");
  assert.equal(authorize.searchParams.get("app_name"), "Bello");
  assert.match(authorize.searchParams.get("callback_url"), /^http:\/\/127\.0\.0\.1:\d+\/cb$/u);
  assert.equal(exchanges.length, 1);
  assert.equal(exchanges[0].url, "https://www.orcarouter.ai/api/v1/auth/keys");
  assert.equal(exchanges[0].body.code, "one-time-code");
  assert.equal(exchanges[0].body.code_challenge_method, "S256");
  assert.match(exchanges[0].body.code_verifier, /^[A-Za-z0-9_-]{43}$/u);
  // The verifier appears only in the exchange body, never in the URL or notices.
  assert.ok(!opened.includes(exchanges[0].body.code_verifier));
  assert.ok(!JSON.stringify(notifications).includes(exchanges[0].body.code_verifier));
});

test("existing-key login is offered side by side and never starts a login flow", async () => {
  const exchanges = [];
  const login = createOrcaConnectLogin({
    env: TEST_ENV,
    fetchImpl: async () => { exchanges.push("fetch"); throw new Error("must not run"); },
    timeoutMs: 5000,
  });
  const credential = await login.login({
    prompt: async (prompt) => (prompt.type === "select" ? "api_key" : FAKE_KEY),
    notify: () => assert.fail("no authorization URL for the API-key choice"),
  });
  assert.deepEqual(credential, { type: "api_key", key: FAKE_KEY });
  assert.deepEqual(exchanges, []);
});

test("denial and a foreign state end the flow with actionable errors", async () => {
  const run = async (callback) => {
    const login = createOrcaConnectLogin({
      env: TEST_ENV,
      fetchImpl: async () => Response.json({ key: FAKE_KEY, scope: "api" }),
      spawnImpl: () => ({ on() {}, unref() {} }),
      timeoutMs: 5000,
    });
    await assert.rejects(login.login({
      prompt: async () => "connect",
      notify: (event) => {
        if (event.type !== "auth_url") return;
        const url = new URL(event.url);
        const callbackUrl = url.searchParams.get("callback_url");
        const state = url.searchParams.get("state");
        fetch(`${callbackUrl}${callback(state)}`).catch(() => {});
      },
    }), callback.expected);
  };
  await run(Object.assign((state) => `?error=access_denied&state=${state}`, { expected: /declined/u }));
  await run(Object.assign((state) => `?code=stolen&state=${state}-x`, { expected: /state did not match/u }));
  await run(Object.assign(() => "?code=stolen&state=", { expected: /state did not match/u }));
});

test("timeout and cancellation close the loopback listener", async () => {
  const before = process.getActiveResourcesInfo().filter((name) => name === "TCPServerWrap").length;
  const login = createOrcaConnectLogin({
    env: TEST_ENV, fetchImpl: async () => assert.fail("no exchange after a timeout"),
    spawnImpl: () => ({ on() {}, unref() {} }), timeoutMs: 1000,
  });
  await assert.rejects(login.login({ prompt: async () => "connect", notify: () => {} }), /timed out/u);
  const controller = new AbortController();
  const cancelled = connectLoopback({
    authBase: ORCAROUTER_AUTH_BASE_URL, open: false, signal: controller.signal, timeoutMs: 10_000,
    fetchImpl: async () => assert.fail("no exchange after cancellation"),
  });
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(cancelled, /cancelled/u);
  const after = process.getActiveResourcesInfo().filter((name) => name === "TCPServerWrap").length;
  assert.equal(after, before);
});

test("exchange errors are classified without leaking the verifier or key", async () => {
  const verifier = "v".repeat(43);
  const cases = [
    [400, { error: "invalid_request" }, "bad_request"],
    [403, { error: "invalid_grant" }, "code_rejected"],
    [429, { error: "slow_down" }, "rate_limited"],
    [500, {}, "upstream"],
    [200, { key: "not-an-orca-key" }, "bad_response"],
  ];
  for (const [status, body, code] of cases) {
    const error = await exchangeCode({
      authBase: "https://www.orcarouter.ai", code: "c", verifier,
      fetchImpl: async () => jsonResponseLike(status, body),
    }).catch((caught) => caught);
    assert.equal(error.code, code, `status ${status}`);
    assert.ok(!String(error.message).includes(verifier));
    assert.ok(!String(error.message).includes("not-an-orca-key"));
  }
  const network = await exchangeCode({
    authBase: "https://www.orcarouter.ai", code: "c", verifier,
    fetchImpl: async () => { throw new Error(`Cannot reach ${FAKE_KEY}`); },
  }).catch((caught) => caught);
  assert.equal(network.code, "network");
  assert.ok(!network.message.includes(FAKE_KEY));
});

function jsonResponseLike(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

test("a narrower granted scope is reported, not assumed", async () => {
  const notices = [];
  const login = createOrcaConnectLogin({
    env: TEST_ENV,
    fetchImpl: async () => jsonResponseLike(200, { key: FAKE_KEY, scope: "connector" }),
    spawnImpl: () => ({ on() {}, unref() {} }),
    timeoutMs: 5000,
  });
  const credential = await login.login({
    prompt: async () => "connect",
    notify: (event) => {
      notices.push(event);
      if (event.type !== "auth_url") return;
      const url = new URL(event.url);
      fetch(`${url.searchParams.get("callback_url")}?code=c&state=${url.searchParams.get("state")}`).catch(() => {});
    },
  });
  assert.equal(credential.key, FAKE_KEY);
  assert.ok(notices.some((event) => event.type === "info" && /connector/u.test(event.message)));
});

// --- Credential lifecycle ----------------------------------------------------

test("a relay 401 marks only the exact credential generation for reauthentication", () => {
  const health = createAuthHealth({ now: () => Date.parse("2026-01-01T00:00:00Z") });
  const old = health.observe({ type: "api_key", key: FAKE_KEY });
  // Re-authorization replaces the stored key before the old request fails.
  const fresh = health.observe({ type: "api_key", key: SECOND_KEY });
  assert.equal(health.recordRejected(old, 401), undefined);
  assert.equal(health.status({ type: "api_key", key: SECOND_KEY }), undefined);
  const state = health.recordRejected(fresh, 401);
  assert.equal(state.needsReauth, true);
  assert.match(state.action, /runtime login orcarouter/u);
  assert.equal(health.status({ type: "api_key", key: SECOND_KEY })?.needsReauth, true);
  // Re-login starts a new generation; no refresh grant exists to call.
  health.observe({ type: "api_key", key: SECOND_KEY });
  assert.equal(health.status({ type: "api_key", key: SECOND_KEY }), undefined);
  // A rejection for a non-auth status never marks the credential.
  assert.equal(health.recordRejected(health.observe({ type: "api_key", key: SECOND_KEY }), 403), undefined);
});

test("redaction removes OrcaRouter-shaped secrets from messages", () => {
  const message = redactOrcaSecret(new Error(`failed with ${FAKE_KEY} and Bearer abc`));
  assert.ok(!message.includes(FAKE_KEY));
  assert.ok(!message.includes("Bearer abc"));
});

// --- Catalog -----------------------------------------------------------------

test("discovery keeps text chat models and fails closed on every other route", () => {
  const ids = discoveryFromPayload(FIXTURE).map((entry) => entry.id);
  assert.deepEqual(ids, ["openai/gpt-5.5", "orcacyber-ultra-1.0", "orcacyber-zero-1.5", "vendor/vision-chat"]);
  const vision = discoveryFromPayload(FIXTURE).find((entry) => entry.id === "vendor/vision-chat");
  assert.deepEqual(vision.input, ["text", "image"]);
  // Unknown ids, undeclared capabilities, and path escapes are dropped.
  assert.ok(!ids.includes("vendor/undeclared"));
  assert.ok(!ids.includes("../escape"));
});

test("discovery URL targets the configured API origin and the chat capability", () => {
  assert.equal(discoveryUrl(), "https://api.orcarouter.ai/v1/models?capability=chat");
  assert.equal(discoveryUrl("https://relay.example.com/v1"),
    "https://relay.example.com/v1/models?capability=chat");
});

test("the verified seed keeps the documented defaults and never mixes into a live catalog", async () => {
  // The default is a model any key grant can reach; the cyber models are the
  // recommended routes for scoped security inference.
  assert.equal(ORCAROUTER_DEFAULT_MODEL, "deepseek/deepseek-v4.1-flash");
  assert.deepEqual(ORCAROUTER_SEED.map((entry) => entry.id),
    ["deepseek/deepseek-v4.1-flash", "orca/orcacyber-ultra-1.0", "orca/orcacyber-zero-1.5", "orcarouter/fusion"]);
  // The model list is the hook's result; Pi's composer publishes it, and a
  // publish from the hook itself would be overwritten by the composer's own
  // later, empty update.
  const publications = [];
  const context = { allowNetwork: false, credential: { type: "api_key", key: FAKE_KEY }, publish: async (value) => { publications.push(value); } };
  const offline = await createOrcaRouterRefresh({ env: TEST_ENV })(context);
  assert.deepEqual(offline.map((model) => model.id), ORCAROUTER_SEED.map((entry) => entry.id));
  assert.equal(publications.length, 0);

  const live = [];
  const fetched = await createOrcaRouterRefresh({
    env: TEST_ENV,
    fetchImpl: async (url, init) => {
      live.push({ url, auth: init.headers.Authorization });
      return new Response(JSON.stringify(FIXTURE), { status: 200, headers: { "Content-Type": "application/json" } });
    },
  })({ ...context, allowNetwork: true });
  assert.deepEqual(fetched.map((model) => model.id), ["openai/gpt-5.5", "orcacyber-ultra-1.0", "orcacyber-zero-1.5", "vendor/vision-chat"]);
  assert.ok(!fetched.some((model) => model.id === "deepseek/deepseek-v4.1-flash"));
  // Pi's cost accounting reads every rate field on each model.
  assert.deepEqual(offline[0].cost, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, tiers: [] });
  assert.equal(live[0].auth, `Bearer ${FAKE_KEY}`);

  const degraded = await createOrcaRouterRefresh({
    env: TEST_ENV,
    fetchImpl: async () => new Response("down", { status: 503 }),
  })({ ...context, allowNetwork: true });
  assert.deepEqual(degraded.map((model) => model.id), ORCAROUTER_SEED.map((entry) => entry.id));
  assert.equal(seedDefinitions().find((model) => model.id === "orca/orcacyber-ultra-1.0").reasoning, true);
  assert.deepEqual(modelDefinition({ id: "vendor/x", name: "X", input: ["text"] }).input, ["text"]);
});

// --- Runtime wiring ----------------------------------------------------------

async function runtimeWith(agentDir, { fetchImpl, login } = {}) {
  const runtime = await ModelRuntime.create({
    authPath: join(agentDir, "auth.json"),
    modelsPath: join(agentDir, "models.json"),
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  await registerOrcaRouter(runtime, { env: TEST_ENV, fetchImpl, ...(login ? { login } : {}) });
  return runtime;
}

test("OrcaRouter is a named provider with Connect (PKCE) and API-key login methods", async () => {
  const root = mkdtempSync("/tmp/bello-orca-catalog-");
  const exchange = async () => jsonResponseLike(200, { key: FAKE_KEY, user_id: "7", scope: "api" });
  const runtime = await runtimeWith(root, { fetchImpl: exchange });
  // Re-read lazily composed providers so each call sees the current registration.
  const provider = () => runtime.getProvider("orcarouter");
  assert.equal(provider().name, "OrcaRouter Cyber Inference");
  assert.equal(provider().baseUrl, "https://api.orcarouter.ai/v1");
  // Pasted API key: the standard Pi api-key path.
  const manual = await provider().auth.apiKey.login({
    prompt: async (prompt) => (prompt.type === "select" ? "api_key" : FAKE_KEY),
  });
  assert.deepEqual(manual, { type: "api_key", key: FAKE_KEY });
  // Connect with OrcaRouter: OAuth 2.0 + PKCE loopback, stored in Pi's oauth
  // slot and sent as the same Bearer key.
  const viaPkce = await provider().auth.oauth.login({
    signal: new AbortController().signal,
    notify: (event) => {
      if (event.type !== "auth_url") return;
      const url = new URL(event.url);
      fetch(`${url.searchParams.get("callback_url")}?code=c&state=${url.searchParams.get("state")}`).catch(() => {});
    },
    onDeviceCode: () => {},
    onPrompt: async () => "",
    prompt: async (prompt) => prompt.options.find((option) => /connect with orcarouter/iu.test(option.label))?.id ?? "",
  });
  assert.equal(viaPkce.access, FAKE_KEY);
  assert.equal(viaPkce.key, FAKE_KEY);
  assert.ok(viaPkce.expires > Date.now());
  // A PKCE credential and a pasted key reach the wire identically: Pi derives
  // the Bearer key through toAuth.
  assert.equal((await provider().auth.oauth.toAuth(viaPkce)).apiKey, manual.key);
  // No refresh grant exists; re-authentication is explicit.
  await assert.rejects(provider().auth.oauth.refresh(viaPkce, new AbortController().signal), /runtime login orcarouter/u);
});

test("catalog discovery stays offline and parses local OrcaRouter credentials only", async () => {
  const agentDir = mkdtempSync("/tmp/bello-orca-local-");
  writeFileSync(join(agentDir, "auth.json"), JSON.stringify({
    orcarouter: { type: "api_key", key: "$BELLO_TEST_UNSET_ORCA_KEY" },
  }));
  // Ensure the template really is unset for this assertion.
  delete process.env.BELLO_TEST_UNSET_ORCA_KEY;
  const runtime = await createCatalogRuntime({ agentDir });
  const provider = runtime.getProvider("orcarouter");
  assert.ok(provider, "local discovery knows the named provider");
  // An unset template must not resolve to the string "undefined".
  const credential = await runtime.credentials.read("orcarouter");
  assert.equal(credential.key, undefined);
});

test("interactive login offers Connect first and stores the same credential shape", async () => {
  const { authMain } = await import("../src/auth-cli.mjs");
  const root = mkdtempSync("/tmp/bello-orca-auth-");
  const exchange = async () => jsonResponseLike(200, { key: FAKE_KEY, user_id: "7", scope: "api" });
  const output = { text: "", write(chunk) { this.text += chunk; return true; } };
  // 1 = Connect with OrcaRouter; the loopback callback is driven from the
  // printed authorize URL, exactly as a browser would.
  let answered = false;
  const outputWithFollow = {
    get text() { return output.text; },
    write(chunk) {
      output.write(chunk);
      if (!answered && chunk.includes("https://www.orcarouter.ai/auth?")) {
        answered = true;
        for (const line of chunk.split("\n").filter((entry) => entry.startsWith("https://"))) {
          const url = new URL(line.trim());
          fetch(`${url.searchParams.get("callback_url")}?code=c&state=${url.searchParams.get("state")}`).catch(() => {});
        }
      }
      return true;
    },
  };
  const code = await authMain({
    argv: ["orcarouter"],
    env: { ...process.env, HOME: root, BELLO_ORCAROUTER_NO_BROWSER: "1" },
    input: Readable.from(["1\n"]),
    output: outputWithFollow,
    runtimeFactory: (options) => ModelRuntime.create({ ...options, authPath: join(root, "auth.json"), modelsPath: join(root, "models.json") }),
    loginOptions: { fetchImpl: exchange },
  });
  assert.equal(code, 0, output.text);
  assert.match(output.text, /Connect with OrcaRouter/u);
  assert.match(output.text, /API key/u);
  assert.match(output.text, /[Aa]uthorize Bello in the browser/u);
  // The verifier never appears in terminal output, notices, or the URL.
  assert.doesNotMatch(output.text, /code_verifier/u);
});

test("a 401 is reported for the exact OrcaRouter credential and never falls back", async () => {
  const root = mkdtempSync("/tmp/bello-orca-reauth-");
  const runtime = await runtimeWith(root, { fetchImpl: async () => jsonResponseLike(200, {}) });
  const { health } = enforceOrcaRouterReauth(runtime);
  const provider = runtime.getProvider("orcarouter");
  const resolveWith = (credential) => provider.auth.apiKey.resolve({ ctx: { env: async () => undefined }, credential, signal: new AbortController().signal });
  // First real request establishes the credential; a 401 marks exactly it.
  await resolveWith({ type: "api_key", key: FAKE_KEY });
  assert.equal(health.recordRejected(health.observe({ type: "api_key", key: FAKE_KEY }), 401)?.needsReauth, true);
  await assert.rejects(resolveWith({ type: "api_key", key: FAKE_KEY }), { code: "orcarouter_needs_reauth" });
  // Re-login stores a new key: the new credential is usable, the old one is not.
  const retry = runtime.getProvider("orcarouter");
  const fresh = await retry.auth.apiKey.resolve({ ctx: { env: async () => undefined }, credential: { type: "api_key", key: SECOND_KEY }, signal: new AbortController().signal });
  assert.equal(fresh.auth.apiKey, SECOND_KEY);
  // The credential is never deleted by the guard.
  const catalogue = runtime.getProvider("orcarouter");
  assert.equal(typeof catalogue.auth.apiKey.resolve, "function");
});
