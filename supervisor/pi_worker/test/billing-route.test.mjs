import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { availableAuthTypes, chooseAuthType } from "../src/auth-cli.mjs";
import { enforceExecutionBillingRoute } from "../src/billing-route.mjs";
import { realPiSdk } from "../src/pi-sdk.mjs";
import { PiWorkerRuntime } from "../src/runtime.mjs";
import { temporaryLayout } from "./helpers.mjs";

function fixture(t) {
  const layout = temporaryLayout(t);
  const authPath = join(layout.agentDir, "auth.json");
  const previous = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "synthetic-ambient-api-key";
  t.after(() => {
    if (previous === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previous;
  });
  let networkCalls = 0;
  t.mock.method(globalThis, "fetch", async () => { networkCalls++; throw new Error("network forbidden"); });
  const writeAuth = (credential) => writeFileSync(authPath, JSON.stringify({ openai: credential }));
  const oauth = (expires = Date.now() + 3600000) => ({
    type: "oauth", access: "synthetic-oauth", refresh: "synthetic-refresh", expires,
  });
  t.after(() => assert.equal(networkCalls, 0));
  return { ...layout, authPath, writeAuth, oauth };
}

for (const expired of [false, true]) {
  test(`OpenAI OAuth is unavailable for API selections without ${expired ? "refresh" : "ambient fallback"}`, async (t) => {
    const layout = fixture(t);
    layout.writeAuth(layout.oauth(expired ? 0 : undefined));
    const before = readFileSync(layout.authPath, "utf8");
    const worker = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
    t.after(() => worker.close());
    await worker.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
    const catalog = await worker.dispatch("model/list", { includeUnconfigured: true });
    assert.equal(catalog.data.some((model) => model.provider === "openai"), false);
    const provider = catalog.providers.find((item) => item.id === "openai");
    assert.equal(provider.configured, false);
    assert.match(provider.configurationError, /API-key/);
    assert.deepEqual(provider.loginMethods, ["api_key"]);
    const model = catalog.catalog.find((item) => item.provider === "openai");
    assert.ok(model);
    assert.equal(model.configured, false);
    for (const method of ["model/validate", "thread/start"]) {
      await assert.rejects(worker.dispatch(method, {
        provider: "openai", model: model.id, threadId: "blocked", cwd: layout.workspace, tools: [],
      }), { code: "billing_route_conflict" });
    }
    assert.equal(worker.threads.size, 0);
    assert.equal(readFileSync(layout.authPath, "utf8"), before);
  });
}

test("SDK credential boundary rejects OAuth added after initial availability and before any refresh", async (t) => {
  const layout = fixture(t);
  layout.writeAuth({ type: "api_key", key: "synthetic-explicit-api-key" });
  const runtime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
  assert.equal(runtime.isUsingOAuth("openai"), false);
  assert.notEqual((await runtime.getAuth("openai")).source, "OAuth");
  layout.writeAuth(layout.oauth(0));
  const before = readFileSync(layout.authPath, "utf8");
  // The cached metadata deliberately remains stale. Only the guarded read can
  // prevent this request from renewing or consuming the subscription.
  assert.equal(runtime.isUsingOAuth("openai"), false);
  await assert.rejects(runtime.getAuth("openai"), { code: "billing_route_conflict" });
  const model = runtime.getModels("openai")[0];
  await assert.rejects(runtime.prepareRequest(model, {}), { code: "billing_route_conflict" });
  assert.equal(readFileSync(layout.authPath, "utf8"), before);
  layout.writeAuth({ type: "api_key", key: "synthetic-restored-api-key" });
  assert.notEqual((await runtime.getAuth("openai")).source, "OAuth");
});

test("session start rejects API-to-OAuth drift after catalog discovery", async (t) => {
  const layout = fixture(t);
  layout.writeAuth({ type: "api_key", key: "synthetic-explicit-api-key" });
  const worker = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => worker.close());
  await worker.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  const model = (await worker.dispatch("model/list", {})).data.find((item) => item.provider === "openai");
  assert.ok(model);
  layout.writeAuth(layout.oauth(0));
  await assert.rejects(worker.dispatch("thread/start", {
    provider: "openai", model: model.id, threadId: "changed", cwd: layout.workspace, tools: [],
  }), (error) => ["billing_route_conflict", "catalog_drift"].includes(error.code));
  assert.equal(worker.threads.get("changed").session, undefined);
});

test("an existing real SDK session cannot consume OAuth added after startup", async (t) => {
  const layout = fixture(t);
  layout.writeAuth({ type: "api_key", key: "synthetic-explicit-api-key" });
  const worker = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => worker.close());
  await worker.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  const model = (await worker.dispatch("model/list", {})).data.find((item) => item.provider === "openai");
  await worker.dispatch("thread/start", {
    provider: "openai", model: model.id, threadId: "idle", cwd: layout.workspace, tools: [],
  });
  const session = worker.threads.get("idle").session;
  assert.ok(session);
  layout.writeAuth(layout.oauth(0));
  const before = readFileSync(layout.authPath, "utf8");
  // This exercises the real session/agent provider-request path, not only a
  // direct call to the guard. SDKs may report rejection as an error message.
  await session.prompt("Synthetic offline billing-route regression").catch(() => {});
  const errors = session.agent.state.messages.filter((item) => item.role === "assistant" && item.stopReason === "error");
  assert.ok(errors.length > 0);
  assert.match(errors.at(-1).errorMessage, /API-key/);
  assert.equal(readFileSync(layout.authPath, "utf8"), before);
});

test("the store guard leaves other provider OAuth and API credentials unchanged", async () => {
  const value = { type: "oauth", access: "synthetic-other-provider" };
  let updates = 0;
  const runtime = {
    credentials: {
      async read() { return value; },
      async modify(_provider, fn) { updates++; return fn(value); },
    },
    async getAuth(provider) { return this.credentials.read(provider); },
  };
  enforceExecutionBillingRoute(runtime);
  assert.equal(await runtime.getAuth("anthropic"), value);
  assert.equal(await runtime.getAuth("openai-codex"), value);
  let callbackCalled = false;
  await assert.rejects(runtime.credentials.modify("openai", () => { callbackCalled = true; }), { code: "billing_route_conflict" });
  assert.equal(callbackCalled, false);
  assert.equal(updates, 1);
});

test("OpenAI login exposes only API while unrelated OAuth login stays available", async () => {
  const auth = { apiKey: { login() {} }, oauth: { login() {}, name: "Subscription" } };
  const openai = { id: "openai", name: "OpenAI", auth };
  assert.deepEqual(availableAuthTypes(openai), ["api_key"]);
  assert.equal(await chooseAuthType(openai, undefined, {}), "api_key");
  await assert.rejects(chooseAuthType(openai, "oauth", {}), /native openai-codex/);
  assert.deepEqual(availableAuthTypes({ id: "anthropic", auth }), ["oauth", "api_key"]);
});
