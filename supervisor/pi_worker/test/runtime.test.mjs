import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { PiWorkerRuntime } from "../src/runtime.mjs";
import { createFakeSdk, fakeModel, temporaryLayout, TEST_TOOL, waitFor } from "./helpers.mjs";

async function setup(t, options = {}) {
  const layout = temporaryLayout(t);
  const events = [];
  const hostRequests = [];
  const sdk = createFakeSdk(options);
  const hostCalls = options.hostCalls ?? {
    async call(params) {
      hostRequests.push(params);
      return { content: [{ type: "text", text: "host ok" }], details: { exitCode: 0 }, isError: false };
    },
  };
  const runtime = new PiWorkerRuntime({
    sdk,
    emit: async (message) => events.push(message),
    hostCalls,
  });
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  return { runtime, sdk, events, hostRequests, ...layout };
}

async function startThread(runtime, workspace, overrides = {}) {
  return runtime.dispatch("thread/start", {
    threadId: overrides.threadId ?? "thread-1",
    cwd: workspace,
    provider: "openai-codex",
    model: "gpt-test",
    effort: overrides.effort ?? "xhigh",
    serviceTier: overrides.serviceTier,
    tools: overrides.tools ?? [TEST_TOOL],
    developerInstructions: "Use only Bello-hosted tools.",
  });
}

test("model catalog exposes provider identity, effort support, and auth state", async (t) => {
  const { runtime, workspace } = await setup(t);
  const result = await runtime.dispatch("model/list", {});
  assert.deepEqual(result.data[0], {
    id: "gpt-test",
    model: "gpt-test",
    provider: "openai-codex",
    qualifiedId: "openai-codex/gpt-test",
    name: "GPT Test",
    api: "openai-codex-responses",
    reasoning: true,
    inputModalities: ["text", "image"],
    supportedEfforts: ["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    defaultEffort: "medium",
    effortCapabilitySources: {
      off: "pi-catalog",
      minimal: "pi-catalog",
      low: "pi-catalog",
      medium: "pi-catalog",
      high: "pi-catalog",
      xhigh: "pi-catalog",
      max: "pi-catalog",
      ultra: "pi-catalog",
    },
    effortRoutes: {
      off: { piThinkingLevel: "off", providerControl: "reasoning_omitted", providerReasoningEffort: null, mappingStatus: "exact" },
      minimal: { piThinkingLevel: "minimal", providerControl: "reasoning_effort", providerReasoningEffort: "minimal", mappingStatus: "exact" },
      low: { piThinkingLevel: "low", providerControl: "reasoning_effort", providerReasoningEffort: "low", mappingStatus: "exact" },
      medium: { piThinkingLevel: "medium", providerControl: "reasoning_effort", providerReasoningEffort: "medium", mappingStatus: "exact" },
      high: { piThinkingLevel: "high", providerControl: "reasoning_effort", providerReasoningEffort: "high", mappingStatus: "exact" },
      xhigh: { piThinkingLevel: "xhigh", providerControl: "reasoning_effort", providerReasoningEffort: "xhigh", mappingStatus: "exact" },
      max: { piThinkingLevel: "max", providerControl: "reasoning_effort", providerReasoningEffort: "max", mappingStatus: "exact" },
      ultra: { piThinkingLevel: "max", providerControl: "reasoning_effort", providerReasoningEffort: "ultra", mappingStatus: "exact" },
    },
    supportsServiceTier: true,
    supportedServiceTiers: ["auto", "default", "flex", "scale", "priority"],
    configured: true,
  });
  const legacy = await runtime.dispatch("thread/start", {
    threadId: "legacy-gpt",
    cwd: workspace,
    model: "gpt-test",
    effort: "ultra",
    serviceTier: "priority",
    tools: [],
  });
  assert.equal(legacy.thread.qualifiedModel, "openai-codex/gpt-test");
  await assert.rejects(
    runtime.dispatch("thread/start", { threadId: "implicit-other", cwd: workspace, model: "claude-test", tools: [] }),
    /explicit provider\/model/,
  );
});

test("model validation returns the exact provider effort route before model execution", async (t) => {
  const { runtime, sdk } = await setup(t);
  const result = await runtime.dispatch("model/validate", {
    provider: "openai-codex",
    model: "gpt-test",
    effort: "ultra",
    serviceTier: "priority",
  });
  assert.equal(result.valid, true);
  assert.equal(result.model.qualifiedId, "openai-codex/gpt-test");
  assert.deepEqual(result.requested, { effort: "ultra", effortDefaulted: false, serviceTier: "priority" });
  assert.deepEqual(result.execution, {
    piThinkingLevel: "max",
    providerControl: "reasoning_effort",
    providerReasoningEffort: "ultra",
    mappingStatus: "exact",
    effortCapabilitySource: "pi-catalog",
  });
  assert.equal(sdk.created.length, 0);
});

test("omitted effort resolves and reports Pi's effective default instead of explicit off", async (t) => {
  const selected = fakeModel({
    thinkingLevelMap: { off: null, minimal: null, low: null, medium: null, high: "high", max: "max" },
    supportedEfforts: ["high", "max"],
  });
  const { runtime, workspace, sdk } = await setup(t, { models: [selected] });
  const validation = await runtime.dispatch("model/validate", {
    provider: "openai-codex",
    model: "gpt-test",
  });
  assert.deepEqual(validation.requested, {
    effort: "high",
    effortDefaulted: true,
    serviceTier: null,
  });
  assert.equal(validation.execution.piThinkingLevel, "high");

  const started = await runtime.dispatch("thread/start", {
    threadId: "default-effort",
    cwd: workspace,
    provider: "openai-codex",
    model: "gpt-test",
    tools: [],
  });
  assert.equal(started.thread.reasoningEffort, "high");
  assert.equal(sdk.created[0].thinkingLevel, "high");
});

test("an OpenAI provider alias is rejected before authentication", async (t) => {
  const selected = fakeModel({
    thinkingLevelMap: { minimal: "low", low: "low", medium: "medium", high: "high" },
  });
  const { runtime } = await setup(t, { models: [selected] });
  runtime.modelRuntime.hasConfiguredAuth = () => false;
  runtime.modelRuntime.checkAuth = async () => {
    throw new Error("authentication must not be consulted for an inexact alias");
  };
  await assert.rejects(runtime.dispatch("model/validate", {
    provider: "openai-codex",
    model: "gpt-test",
    effort: "minimal",
  }), /minimal is only an inexact alias to provider effort low/);
});

test("text-only models never receive view_image while resume keeps the host tool contract", async (t) => {
  const viewImage = {
    name: "view_image",
    description: "Open an image.",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
      additionalProperties: false,
    },
  };
  const selected = fakeModel({ input: ["text"] });
  const { runtime, workspace, sdk, hostRequests } = await setup(t, { models: [selected] });
  const tools = [TEST_TOOL, viewImage];
  await startThread(runtime, workspace, { tools });
  assert.deepEqual([...sdk.created[0].registry.keys()], ["exec_command", "submit_result"]);
  assert.deepEqual(sdk.created[0].state.tools.map((tool) => tool.name), ["exec_command"]);

  const record = runtime.getRecord("thread-1");
  record.active = {
    turn: { id: "image-turn", status: "inProgress", items: [], startedAt: new Date().toISOString() },
  };
  await assert.rejects(
    runtime.executeHostTool(record, "view_image", "image-call", { path: "image.png" }),
    /does not accept image input/,
  );
  assert.equal(hostRequests.length, 0);
  record.active = undefined;

  await runtime.dispatch("thread/archive", { threadId: "thread-1" });
  const resumed = await runtime.dispatch("thread/resume", { threadId: "thread-1", tools });
  assert.equal(resumed.thread.status, "idle");
  assert.deepEqual([...sdk.created[1].registry.keys()], ["exec_command", "submit_result"]);
});

test("unsupported capability fails before the worker consults provider authentication", async (t) => {
  const { runtime } = await setup(t, {
    models: [fakeModel({ supportedEfforts: ["off", "low"] })],
  });
  runtime.modelRuntime.hasConfiguredAuth = () => false;
  runtime.modelRuntime.checkAuth = async () => {
    throw new Error("authentication must not be consulted for an unsupported capability");
  };
  await assert.rejects(runtime.dispatch("model/validate", {
    provider: "openai-codex",
    model: "gpt-test",
    effort: "ultra",
  }), /reasoning effort ultra is not supported/);
});

test("a turn forwards only supplied host tools and records assistant and host items", async (t) => {
  const behavior = async (session, input, signal) => {
    assert.equal(input, "do the work");
    assert.deepEqual(session.state.tools.map((tool) => tool.name), ["exec_command"]);
    const tool = session.state.tools[0];
    const result = await tool.execute("pi-stable-call", { command: "printf ok" }, signal);
    assert.equal(result.content[0].text, "host ok");
    session.emitAssistant("finished");
  };
  const { runtime, workspace, events, hostRequests } = await setup(t, { behavior });
  await startThread(runtime, workspace);
  const response = await runtime.dispatch("turn/start", {
    threadId: "thread-1",
    turnId: "turn-1",
    input: [{ type: "text", text: "do the work" }],
    effort: "xhigh",
  });
  assert.equal(response.turn.status, "inProgress");
  const completed = await waitFor(() => events.find((event) => event.method === "turn/completed"));
  assert.deepEqual(hostRequests[0], {
    threadId: "thread-1",
    turnId: "turn-1",
    callId: "pi-stable-call",
    name: "exec_command",
    arguments: { command: "printf ok" },
  });
  assert.equal(completed.params.turn.status, "completed");
  assert.deepEqual(completed.params.turn.items.map((item) => item.type), ["hostTool", "agentMessage"]);
  assert.equal(completed.params.turn.items[1].text, "finished");
  assert.deepEqual(events.map((event) => event.method), [
    "turn/started",
    "item/started",
    "item/completed",
    "turn/completed",
  ]);
});

test("assistant usage keeps per-response provider provenance and exact aggregate fields", async (t) => {
  const usages = [
    {
      input: 11,
      output: 5,
      cacheRead: 3,
      cacheWrite: 2,
      reasoning: 4,
      totalTokens: 21,
      cost: { input: 0.11, output: 0.5, cacheRead: 0.03, cacheWrite: 0.02, total: 0.66 },
    },
    {
      input: 17,
      output: 7,
      cacheRead: 13,
      cacheWrite: 0,
      totalTokens: 37,
      cost: { input: 0.17, output: 0.7, cacheRead: 0.13, cacheWrite: 0, total: 1 },
    },
  ];
  const behavior = async (session) => {
    for (const [index, usage] of usages.entries()) {
      session.emitAssistant(`response ${index + 1}`, {
        usage,
        provider: "openai-codex",
        model: "gpt-test",
        api: "openai-codex-responses",
        responseModel: `provider-model-${index + 1}`,
        providerThinkingLevel: "ultra",
      });
    }
  };
  const { runtime, workspace, events } = await setup(t, { behavior });
  await startThread(runtime, workspace);
  await runtime.dispatch("turn/start", {
    threadId: "thread-1",
    turnId: "usage-turn",
    input: "measure",
    effort: "ultra",
  });
  const completed = await waitFor(() => events.find((event) => event.method === "turn/completed"));
  const turnUsage = completed.params.turn.usage;
  assert.deepEqual(
    Object.fromEntries(Object.entries(turnUsage).filter(([key]) => key !== "responses")),
    {
      input: 28,
      output: 12,
      cacheRead: 16,
      cacheWrite: 2,
      reasoning: 4,
      totalTokens: 58,
      cost: { input: 0.28, output: 1.2, cacheRead: 0.16, cacheWrite: 0.02, total: 1.6600000000000001 },
    },
  );
  assert.deepEqual(turnUsage.responses, usages.map((usage, index) => ({
    itemId: `usage-turn-message-${index + 1}`,
    usage,
    provider: "openai-codex",
    model: "gpt-test",
    api: "openai-codex-responses",
    responseModel: `provider-model-${index + 1}`,
    providerThinkingLevel: "ultra",
  })));
  const messages = completed.params.turn.items.filter((item) => item.type === "agentMessage");
  assert.deepEqual(messages.map((item) => item.usage), usages);
  assert.deepEqual(messages[0].usageProvenance, {
    provider: "openai-codex",
    model: "gpt-test",
    api: "openai-codex-responses",
    responseModel: "provider-model-1",
    providerThinkingLevel: "ultra",
  });
  const read = await runtime.dispatch("thread/read", { threadId: "thread-1", includeTurns: true });
  assert.deepEqual(read.thread.turns[0].usage, turnUsage);
});

test("structured turns can optionally use submit_result with the exact schema", async (t) => {
  const schema = {
    type: "object",
    properties: { verdict: { $ref: "#/$defs/Verdict" } },
    required: ["verdict"],
    additionalProperties: false,
    $defs: { Verdict: { type: "string", enum: ["accept", "return"] } },
  };
  const behavior = async (session) => {
    assert.deepEqual(session.state.tools.map((tool) => tool.name), ["exec_command", "submit_result"]);
    const submit = session.state.tools[1];
    assert.deepEqual(submit.parameters, schema);
    assert.match(submit.description, /Optional/);
    assert.match(submit.description, /final assistant message/);
    await assert.rejects(submit.execute("invalid-structured-call", { verdict: "unsupported" }), /schema validation/);
    const result = await submit.execute("structured-call", { verdict: "accept" });
    assert.equal(result.terminate, true);
    assert.equal(result.content[0].text, "Structured result received; Bello will validate the decision.");
    session.emitAssistant(undefined, { stopReason: "toolUse" });
  };
  const { runtime, workspace, events } = await setup(t, { behavior });
  await startThread(runtime, workspace);
  await runtime.dispatch("turn/start", {
    threadId: "thread-1",
    turnId: "structured-turn",
    input: "review",
    effort: "high",
    outputSchema: schema,
  });
  const completed = await waitFor(() => events.find((event) => event.method === "turn/completed"));
  assert.equal(completed.params.turn.status, "completed");
  assert.deepEqual(completed.params.turn.structuredResult, { verdict: "accept" });
  assert.equal(completed.params.turn.items.at(-1).text, '{"verdict":"accept"}');
});

test("structured turns deliver plain final text unchanged for Bello validation and repair", async (t) => {
  for (const text of [
    '{"verdict":"accept"}',
    '```json\n{"verdict":"accept"}\n```',
    '{"verdict":',
    '{"forward_to_coder":true,"reason":"keep finding","report_to_coder":null}',
  ]) {
    await t.test(text, async (t) => {
      const { runtime, workspace, events } = await setup(t, {
        behavior: async (session) => session.emitAssistant(text),
      });
      await startThread(runtime, workspace);
      await runtime.dispatch("turn/start", {
        threadId: "thread-1",
        turnId: "plain-json",
        input: "review",
        outputSchema: { type: "object", properties: { verdict: { type: "string" } }, required: ["verdict"] },
      });
      const completed = await waitFor(() => events.find((event) => event.method === "turn/completed"));
      // Delivery success is not an accepted decision. Python still validates
      // malformed JSON and cross-field invariants, and performs its own repair.
      assert.equal(completed.params.turn.status, "completed");
      assert.equal(completed.params.turn.error, undefined);
      assert.equal(completed.params.turn.structuredResult, undefined);
      assert.deepEqual(completed.params.turn.items.filter((item) => item.type === "agentMessage").map((item) => item.text), [text]);
      const read = await runtime.dispatch("thread/read", { threadId: "thread-1", includeTurns: true });
      assert.deepEqual(read.thread.turns[0], completed.params.turn);
    });
  }
});

test("plain JSON never hides a provider error or interruption on a structured turn", async (t) => {
  for (const [stopReason, status, expectedError] of [
    ["error", "failed", "test provider failure"],
    ["aborted", "interrupted", "Turn was interrupted."],
  ]) {
    await t.test(stopReason, async (t) => {
      const { runtime, workspace, events } = await setup(t, {
        behavior: async (session) => session.emitAssistant('{"verdict":"accept"}', {
          stopReason,
          errorMessage: "test provider failure",
        }),
      });
      await startThread(runtime, workspace);
      await runtime.dispatch("turn/start", {
        threadId: "thread-1",
        turnId: "unsuccessful-json",
        input: "review",
        outputSchema: { type: "object", properties: { verdict: { type: "string" } }, required: ["verdict"] },
      });
      const completed = await waitFor(() => events.find((event) => event.method === "turn/completed"));
      assert.equal(completed.params.turn.status, status);
      assert.equal(completed.params.turn.error.message, expectedError);
      assert.equal(completed.params.turn.structuredResult, undefined);
    });
  }
});

test("unsupported effort and service tier fail before any model request", async (t) => {
  const model = fakeModel({
    id: "limited",
    api: "anthropic-messages",
    provider: "anthropic",
    supportedEfforts: ["off", "low", "high"],
  });
  const { runtime, workspace, sdk } = await setup(t, { models: [model] });
  await assert.rejects(runtime.dispatch("thread/start", {
    threadId: "bad-effort",
    cwd: workspace,
    provider: "anthropic",
    model: "limited",
    effort: "ultra",
    tools: [],
  }), /not supported/);
  await assert.rejects(runtime.dispatch("thread/start", {
    threadId: "bad-tier",
    cwd: workspace,
    provider: "anthropic",
    model: "limited",
    effort: "high",
    serviceTier: "priority",
    tools: [],
  }), /serviceTier priority is not supported/);
  await assert.rejects(runtime.dispatch("thread/start", {
    threadId: "invalid-tier",
    cwd: workspace,
    provider: "anthropic",
    model: "limited",
    effort: "high",
    serviceTier: "made-up-tier",
    tools: [],
  }), /serviceTier made-up-tier is not supported/);
  assert.equal(sdk.created.length, 0);
});

test("restart marks an unfinished turn interrupted without replaying it", async (t) => {
  const layout = temporaryLayout(t);
  const firstSdk = createFakeSdk();
  const first = new PiWorkerRuntime({
    sdk: firstSdk,
    emit: async () => {},
    hostCalls: { async call() { throw new Error("must not run"); } },
    schedule: () => {},
  });
  await first.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  await startThread(first, layout.workspace);
  await first.dispatch("turn/start", { threadId: "thread-1", turnId: "crashed-turn", input: "do not replay" });
  const before = JSON.parse(readFileSync(`${layout.stateDir}/threads/thread-1.json`, "utf8"));
  assert.equal(before.turns[0].status, "inProgress");

  const secondSdk = createFakeSdk();
  const second = new PiWorkerRuntime({
    sdk: secondSdk,
    emit: async () => {},
    hostCalls: { async call() { throw new Error("must not run"); } },
  });
  await second.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  const read = await second.dispatch("thread/read", { threadId: "thread-1", includeTurns: true });
  assert.equal(read.thread.turns[0].status, "interrupted");
  assert.match(read.thread.turns[0].error.message, /not replayed/);
  assert.equal(secondSdk.created.length, 0);
});

test("two sessions can have turns in flight concurrently", async (t) => {
  const started = [];
  const releases = new Map();
  const behavior = async (session) => {
    const threadId = session.options.sessionManager.getSessionId();
    started.push(threadId);
    await new Promise((resolve) => releases.set(threadId, resolve));
    session.emitAssistant(`done ${threadId}`);
  };
  const { runtime, workspace, events } = await setup(t, { behavior });
  await startThread(runtime, workspace, { threadId: "thread-a" });
  await startThread(runtime, workspace, { threadId: "thread-b" });
  await runtime.dispatch("turn/start", { threadId: "thread-a", turnId: "turn-a", input: "a" });
  await runtime.dispatch("turn/start", { threadId: "thread-b", turnId: "turn-b", input: "b" });
  await waitFor(() => started.length === 2);
  assert.deepEqual(new Set(started), new Set(["thread-a", "thread-b"]));
  releases.get("thread-a")();
  releases.get("thread-b")();
  await waitFor(() => events.filter((event) => event.method === "turn/completed").length === 2);
});
