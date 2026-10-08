import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";

import { PiWorkerRuntime } from "../src/runtime.mjs";
import { realPiSdk } from "../src/pi-sdk.mjs";
import { createFakeSdk, fakeModel, temporaryLayout } from "./helpers.mjs";

async function setup(t, sdk = createFakeSdk()) {
  const layout = temporaryLayout(t);
  const runtime = new PiWorkerRuntime({ sdk, emit: async () => {}, hostCalls: {} });
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  t.after(() => runtime.close());
  return { ...layout, runtime, sdk };
}

test("catalog reads retain a local snapshot until explicitly refreshed", async (t) => {
  const { runtime, sdk, agentDir } = await setup(t);
  const calls = [];
  const previous = runtime.modelRuntime;
  const candidate = createFakeSdk({ models: [fakeModel({ id: "newly-configured" })] }).modelRuntime;
  sdk.createCatalogRuntime = async (options) => {
    calls.push(options);
    return candidate;
  };
  const first = await runtime.dispatch("model/list", {});
  const repeated = await runtime.dispatch("model/list", {});
  assert.deepEqual(repeated.catalogFreshness, first.catalogFreshness);
  assert.deepEqual(calls, []);
  assert.equal(first.catalogFreshness.source, "local");
  assert.equal(first.catalogFreshness.refreshPolicy, "explicit-local");
  assert.equal(first.catalogFreshness.remoteFreshness, "unknown");
  assert.equal(first.catalogFreshness.networkAllowed, false);
  assert.ok(Number.isFinite(Date.parse(first.catalogFreshness.loadedAt)));
  const refreshed = await runtime.dispatch("model/list", { refresh: true });
  assert.deepEqual(calls, [{ agentDir, allowModelNetwork: false }]);
  assert.equal(runtime.modelRuntime, candidate);
  assert.equal(previous.models[0].id, "gpt-test");
  assert.equal(refreshed.data[0].id, "newly-configured");
});

test("local catalog refresh never accepts network escalation or nonboolean refresh", async (t) => {
  const { runtime } = await setup(t);
  for (const params of [{ refresh: "true" }, { refresh: 1 }, { refresh: true, allowModelNetwork: true }]) {
    await assert.rejects(runtime.dispatch("model/list", params), { code: "invalid_params" });
  }
});

test("refresh reports a safe failure and can recover without claiming a new snapshot", async (t) => {
  const { runtime, sdk } = await setup(t);
  const previous = runtime.modelRuntime;
  const initial = await runtime.dispatch("model/list", {});
  const before = initial.catalogFreshness;
  const candidate = createFakeSdk({ models: [fakeModel({ id: "unpublished" })] }).modelRuntime;
  candidate.getError = () => "secret-auth-body";
  sdk.createCatalogRuntime = async () => candidate;
  await assert.rejects(runtime.dispatch("model/list", { refresh: true }), (error) => {
    assert.equal(error.code, "catalog_refresh_failed");
    assert.equal(error.message.includes("secret-auth-body"), false);
    return true;
  });
  assert.equal(runtime.catalogRefreshing, false);
  assert.equal(runtime.modelRuntime, previous);
  const failed = await runtime.dispatch("model/list", {});
  assert.deepEqual(failed.data, initial.data);
  assert.deepEqual(failed.catalogFreshness, { ...before, lastRefreshSucceeded: false });
  assert.match(failed.catalogError, /refresh failed/);
  for (const method of ["model/validate", "thread/start", "thread/resume", "turn/start"]) {
    await assert.rejects(runtime.dispatch(method, {}), { code: "catalog_refresh_failed" });
  }
  sdk.createCatalogRuntime = async () => createFakeSdk().modelRuntime;
  await runtime.dispatch("model/list", { refresh: true });
  assert.equal((await runtime.dispatch("model/list", {})).catalogFreshness.lastRefreshSucceeded, true);
});

test("refresh cannot overlap active turns, session creation, or another refresh", async (t) => {
  const { runtime, sdk } = await setup(t);
  const previous = runtime.modelRuntime;
  const record = runtime.makeRecord({});
  runtime.threads.set("test", record);
  for (const field of ["active", "loadPromise"]) {
    record[field] = {};
    await assert.rejects(runtime.dispatch("model/list", { refresh: true }), { code: "catalog_busy" });
    record[field] = undefined;
  }
  let release;
  sdk.createCatalogRuntime = () => new Promise((resolve) => { release = resolve; });
  const pending = runtime.dispatch("model/list", { refresh: true });
  try {
    assert.equal(runtime.modelRuntime, previous);
    for (const method of ["model/list", "account/read", "thread/start", "thread/resume", "turn/start", "model/validate"]) {
      await assert.rejects(runtime.dispatch(method, {}), { code: "catalog_busy" });
    }
  } finally {
    release(createFakeSdk().modelRuntime);
    await pending;
  }
});

test("startup inspects an aborted refresh result even when getError is empty", async (t) => {
  const layout = temporaryLayout(t);
  const candidate = createFakeSdk().modelRuntime;
  let createOptions;
  let refreshOptions;
  candidate.refresh = async (options) => {
    refreshOptions = options;
    return { aborted: true, errors: new Map() };
  };
  t.mock.method(ModelRuntime, "create", async (options) => {
    createOptions = options;
    return candidate;
  });
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await assert.rejects(runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir }), { code: "catalog_load_failed" });
  assert.equal(createOptions.refreshOnCreate, false);
  assert.equal(createOptions.allowModelNetwork, false);
  assert.deepEqual(refreshOptions, { allowNetwork: false });
  assert.equal(runtime.initialized, false);
  assert.equal(runtime.modelRuntime, undefined);
});

test("real local refresh reloads model files without network, auth renewal, or credential commands", async (t) => {
  const layout = temporaryLayout(t);
  const authPath = join(layout.agentDir, "auth.json");
  const auth = JSON.stringify({ "openai-codex": { type: "oauth", access: "expired-test", refresh: "synthetic-test", expires: 0 } });
  writeFileSync(authPath, auth);
  const marker = join(layout.agentDir, "credential-command-ran");
  const modelsPath = join(layout.agentDir, "models.json");
  const modelConfig = (id) => ({ providers: { "bello-local": {
    api: "openai-completions", baseUrl: "http://127.0.0.1:1/v1",
    apiKey: `!${JSON.stringify(process.execPath)} -e 'require("fs").writeFileSync(${JSON.stringify(marker)}, "unsafe")'`,
    models: [{ id, name: id, reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 4096, maxTokens: 1024 }],
  } } });
  writeFileSync(modelsPath, JSON.stringify(modelConfig("before")));
  let networkCalls = 0;
  t.mock.method(globalThis, "fetch", async () => { networkCalls++; throw new Error("network forbidden"); });
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  assert.ok((await runtime.dispatch("model/list", {})).data.some((model) => model.qualifiedId === "bello-local/before"));
  writeFileSync(modelsPath, JSON.stringify(modelConfig("after")));
  assert.ok(!(await runtime.dispatch("model/list", {})).data.some((model) => model.id === "after"));
  const refreshed = await runtime.dispatch("model/list", { refresh: true });
  assert.ok(refreshed.data.some((model) => model.qualifiedId === "bello-local/after"));
  assert.equal(networkCalls, 0);
  assert.equal(existsSync(marker), false);
  assert.equal(readFileSync(authPath, "utf8"), auth);
  assert.equal(existsSync(join(layout.agentDir, "models-store.json")), false);
});

test("refresh cannot race thread startup before a session record exists", async (t) => {
  const { runtime, workspace } = await setup(t);
  const original = runtime.ensureConfigured.bind(runtime);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  runtime.ensureConfigured = async (model) => { await gate; return original(model); };
  const starting = runtime.dispatch("thread/start", {
    threadId: "starting", cwd: workspace, provider: "openai-codex", model: "gpt-test", effort: "high", tools: [],
  });
  try {
    assert.equal(runtime.threads.size, 0);
    await assert.rejects(runtime.dispatch("model/list", { refresh: true }), { code: "catalog_busy" });
  } finally {
    release();
    await starting;
  }
});

test("catalog discovery does not create missing agent or credential directories", async (t) => {
  const layout = temporaryLayout(t);
  const agentDir = join(layout.root, "missing-agent");
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir });
  await runtime.dispatch("model/list", { refresh: true, includeUnconfigured: true });
  assert.equal(existsSync(agentDir), false);
});

test("credential commands in auth.json remain inert during discovery and refresh", async (t) => {
  const layout = temporaryLayout(t);
  const marker = join(layout.root, "auth-command-ran");
  const authPath = join(layout.agentDir, "auth.json");
  const auth = JSON.stringify({ openai: { type: "api_key",
    key: `!${JSON.stringify(process.execPath)} -e 'require("fs").writeFileSync(${JSON.stringify(marker)}, "unsafe")'` } });
  writeFileSync(authPath, auth);
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  const first = await runtime.dispatch("model/list", {});
  assert.ok(first.providers.some((provider) => provider.id === "openai" && provider.configured));
  await runtime.dispatch("model/list", { refresh: true });
  assert.equal(existsSync(marker), false);
  assert.equal(readFileSync(authPath, "utf8"), auth);
});

test("discovery honors credential environment templates and reloads changed auth only explicitly", async (t) => {
  const layout = temporaryLayout(t);
  const authPath = join(layout.agentDir, "auth.json");
  const keyName = "BELLO_TEST_PI_MISSING_CREDENTIAL_20261006";
  const previous = process.env[keyName];
  delete process.env[keyName];
  t.after(() => { if (previous !== undefined) process.env[keyName] = previous; });
  writeFileSync(authPath, JSON.stringify({ "bello-local": { type: "api_key", key: `$${keyName}` } }));
  writeFileSync(join(layout.agentDir, "models.json"), JSON.stringify({ providers: { "bello-local": {
    api: "openai-completions", baseUrl: "http://127.0.0.1:1/v1",
    models: [{ id: "test", name: "test", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 4096, maxTokens: 1024 }],
  } } }));
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  assert.equal((await runtime.dispatch("model/list", {})).data.some((model) => model.provider === "bello-local"), false);
  writeFileSync(authPath, JSON.stringify({ "bello-local": { type: "api_key", key: `prefix-$${keyName}-$$-$!`,
    env: { [keyName]: "synthetic" } } }));
  assert.equal((await runtime.dispatch("model/list", {})).data.some((model) => model.provider === "bello-local"), false);
  const refreshed = await runtime.dispatch("model/list", { refresh: true });
  assert.ok(refreshed.data.some((model) => model.qualifiedId === "bello-local/test"));
});

test("started sessions receive the normal execution runtime, never read-only discovery stores", async (t) => {
  const sdk = createFakeSdk();
  const executionRuntime = createFakeSdk().modelRuntime;
  const calls = [];
  sdk.createCatalogRuntime = async () => sdk.modelRuntime;
  sdk.createModelRuntime = async (options) => { calls.push(options); return executionRuntime; };
  const { runtime, workspace, agentDir } = await setup(t, sdk);
  assert.deepEqual(calls, []);
  await runtime.dispatch("thread/start", {
    threadId: "execution", cwd: workspace, provider: "openai-codex", model: "gpt-test", effort: "high", tools: [],
  });
  assert.deepEqual(calls, [{ agentDir, allowModelNetwork: false }]);
  assert.equal(sdk.created[0].options.modelRuntime, executionRuntime);
  assert.equal(sdk.created[0].options.model, executionRuntime.getModel("openai-codex", "gpt-test"));
  assert.notEqual(sdk.created[0].options.model, sdk.modelRuntime.getModel("openai-codex", "gpt-test"));
});

for (const [label, files] of [
  ["provider cache refresh error", { "models-store.json": { openai: {} } }],
  ["model configuration error", { "models.json": "private-invalid-config" }],
]) {
  test(`startup fails safely on an initial ${label}`, async (t) => {
    const layout = temporaryLayout(t);
    for (const [name, contents] of Object.entries(files)) {
      writeFileSync(join(layout.agentDir, name), typeof contents === "string" ? contents : JSON.stringify(contents));
    }
    let networkCalls = 0;
    t.mock.method(globalThis, "fetch", async () => { networkCalls++; throw new Error("network forbidden"); });
    const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
    t.after(() => runtime.close());
    await assert.rejects(runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir }), (error) => {
      assert.equal(error.code, "catalog_load_failed");
      assert.equal(error.message.includes("private-invalid-config"), false);
      return true;
    });
    assert.equal(runtime.initialized, false);
    assert.equal(runtime.modelRuntime, undefined);
    assert.equal(runtime.catalogLoadedAt, undefined);
    assert.equal(networkCalls, 0);
    assert.equal(existsSync(join(layout.agentDir, "auth.json")), false);
    assert.equal(existsSync(join(layout.agentDir, "models-store.json.lock")), false);
  });
}

test("failed real refresh preserves provider overrides and auth until a valid replacement is published", async (t) => {
  const layout = temporaryLayout(t);
  const modelsPath = join(layout.agentDir, "models.json");
  const authPath = join(layout.agentDir, "auth.json");
  const baseUrl = "http://127.0.0.1:1/isolated";
  const config = JSON.stringify({ providers: { openai: { baseUrl } } });
  writeFileSync(modelsPath, config);
  writeFileSync(authPath, JSON.stringify({ openai: { type: "api_key", key: "synthetic" } }));
  let networkCalls = 0;
  t.mock.method(globalThis, "fetch", async () => { networkCalls++; throw new Error("network forbidden"); });
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  const previous = runtime.modelRuntime;
  const before = await runtime.dispatch("model/list", {});
  assert.ok(previous.getModels("openai").length);
  writeFileSync(authPath, "{}");
  writeFileSync(modelsPath, "private-invalid-config");
  await assert.rejects(runtime.dispatch("model/list", { refresh: true }), { code: "catalog_refresh_failed" });
  assert.equal(runtime.modelRuntime, previous);
  assert.ok(previous.getModels("openai").every((model) => model.baseUrl === baseUrl));
  const after = await runtime.dispatch("model/list", {});
  assert.deepEqual(after.data, before.data);
  assert.deepEqual(after.providers, before.providers);
  assert.equal(after.catalogFreshness.loadedAt, before.catalogFreshness.loadedAt);
  assert.equal(after.catalogFreshness.lastRefreshSucceeded, false);
  assert.equal(JSON.stringify(after).includes("private-invalid-config"), false);
  await assert.rejects(runtime.dispatch("model/validate", {}), { code: "catalog_refresh_failed" });
  writeFileSync(modelsPath, config);
  const recovered = await runtime.dispatch("model/list", { refresh: true });
  assert.notEqual(runtime.modelRuntime, previous);
  assert.equal(recovered.catalogFreshness.lastRefreshSucceeded, true);
  assert.equal(networkCalls, 0);
});

for (const [label, changed] of [
  ["endpoint", { baseUrl: "http://127.0.0.1:2/new" }],
  ["provider", { provider: "different-provider" }],
  ["capabilities", { input: ["text"], supportedEfforts: ["off"] }],
]) {
  test(`session creation rejects execution ${label} drift before creating a session`, async (t) => {
    const sdk = createFakeSdk();
    const execution = createFakeSdk({ models: [fakeModel(changed)] }).modelRuntime;
    sdk.createCatalogRuntime = async () => sdk.modelRuntime;
    sdk.createModelRuntime = async () => execution;
    const { runtime, workspace } = await setup(t, sdk);
    await assert.rejects(runtime.dispatch("thread/start", {
      threadId: "drift", cwd: workspace, provider: "openai-codex", model: "gpt-test", effort: "high", tools: [],
    }), { code: "catalog_drift" });
    assert.equal(sdk.created.length, 0);
  });
}

test("an idle session cannot use refreshed capabilities with its previous execution model", async (t) => {
  const { runtime, workspace, sdk } = await setup(t);
  await runtime.dispatch("thread/start", {
    threadId: "idle", cwd: workspace, provider: "openai-codex", model: "gpt-test", effort: "high", tools: [],
  });
  sdk.createCatalogRuntime = async () => createFakeSdk().modelRuntime;
  await runtime.dispatch("model/list", { refresh: true });
  await runtime.dispatch("thread/resume", { threadId: "idle" });
  assert.equal(sdk.created.length, 1, "an unchanged refresh preserves the existing session");
  sdk.createCatalogRuntime = async () => createFakeSdk({ models: [fakeModel({ input: ["text"] })] }).modelRuntime;
  await runtime.dispatch("model/list", { refresh: true });
  await assert.rejects(runtime.dispatch("turn/start", {
    threadId: "idle", turnId: "next", input: "must not execute",
  }), { code: "catalog_drift" });
  assert.equal(runtime.threads.get("idle").active, undefined);
});

test("changed endpoint and key cannot be combined across discovery and execution", async (t) => {
  const layout = temporaryLayout(t);
  const modelsPath = join(layout.agentDir, "models.json");
  const config = (baseUrl, apiKey) => JSON.stringify({ providers: { "bello-local": {
    api: "openai-completions", baseUrl, apiKey,
    models: [{ id: "test", name: "test", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 4096, maxTokens: 1024 }],
  } } });
  const oldUrl = "http://127.0.0.1:1/old";
  const newUrl = "http://127.0.0.1:2/new";
  writeFileSync(modelsPath, config(oldUrl, "synthetic-old-key"));
  const sessions = createFakeSdk();
  const sdk = { ...realPiSdk, createSession: sessions.createSession };
  let networkCalls = 0;
  t.mock.method(globalThis, "fetch", async () => { networkCalls++; throw new Error("network forbidden"); });
  const runtime = new PiWorkerRuntime({ sdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  writeFileSync(modelsPath, config(newUrl, "synthetic-new-key"));
  const params = { cwd: layout.workspace, provider: "bello-local", model: "test", effort: "off", tools: [] };
  await assert.rejects(runtime.dispatch("thread/start", { ...params, threadId: "stale" }), { code: "catalog_drift" });
  assert.equal(sessions.created.length, 0);
  await runtime.dispatch("model/list", { refresh: true });
  await runtime.dispatch("thread/start", { ...params, threadId: "current" });
  const { model, modelRuntime } = sessions.created[0].options;
  assert.equal(model.baseUrl, newUrl);
  assert.deepEqual(model, modelRuntime.getModel("bello-local", "test"));
  assert.equal((await modelRuntime.getAuth(model)).auth.apiKey, "synthetic-new-key");
  assert.equal(networkCalls, 0);
});

test("persisted dynamic provider models remain discoverable and refresh without cache writes", async (t) => {
  const layout = temporaryLayout(t);
  const storePath = join(layout.agentDir, "models-store.json");
  writeFileSync(join(layout.agentDir, "auth.json"), JSON.stringify({ openai: { type: "api_key", key: "synthetic" } }));
  const store = (id) => JSON.stringify({ openai: {
    checkedAt: Date.now(), lastModified: Date.now() + 3650 * 86400_000,
    models: [{ id, name: id, provider: "openai", api: "openai-responses", baseUrl: "http://127.0.0.1:1/v1",
      reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 4096, maxTokens: 1024 }],
  } });
  const before = store("cached-first");
  writeFileSync(storePath, before);
  t.mock.method(globalThis, "fetch", async () => { throw new Error("network forbidden"); });
  const runtime = new PiWorkerRuntime({ sdk: realPiSdk, emit: async () => {}, hostCalls: {} });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  assert.ok((await runtime.dispatch("model/list", {})).data.some((model) => model.qualifiedId === "openai/cached-first"));
  assert.equal(readFileSync(storePath, "utf8"), before);
  const after = store("cached-second");
  writeFileSync(storePath, after);
  const refreshed = await runtime.dispatch("model/list", { refresh: true });
  assert.ok(refreshed.data.some((model) => model.qualifiedId === "openai/cached-second"));
  assert.equal(refreshed.data.some((model) => model.id === "cached-first"), false);
  assert.equal(readFileSync(storePath, "utf8"), after);
  assert.equal(existsSync(`${storePath}.lock`), false);
});
