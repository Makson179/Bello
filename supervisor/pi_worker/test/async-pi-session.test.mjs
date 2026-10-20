import assert from "node:assert/strict";
import test from "node:test";
import { SessionManager } from "@earendil-works/pi-coding-agent";

import { AsyncToolCoordinator, lateToolMessage } from "../src/async-tools.mjs";
import { realPiSdk } from "../src/pi-sdk.mjs";
import { temporaryLayout, TEST_TOOL, waitFor } from "./helpers.mjs";

test("real pinned Pi loop waits for late results without polling and preserves sent prefix", async (t) => {
  const layout = temporaryLayout(t);
  const modelRuntime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
  const model = modelRuntime.getModel("openai-codex", "gpt-5.6-sol");
  assert.ok(model);
  // Offline deterministic provider: no auth read, network or paid inference.
  modelRuntime.hasConfiguredAuth = () => true;
  const scheduler = new AsyncToolCoordinator({ graceMs: 5 });
  let finishSlow;
  const slow = new Promise((resolve) => { finishSlow = resolve; });
  let session;
  session = await realPiSdk.createSession({
    cwd: layout.workspace,
    agentDir: layout.agentDir,
    modelRuntime,
    model,
    thinkingLevel: "high",
    sessionManager: SessionManager.inMemory(layout.workspace),
    customTools: [{ ...TEST_TOOL, label: "Command", executionMode: "parallel", execute: (id, args, signal) => scheduler.execute(id, "exec_command", async () => {
      if (args.command === "slow") await slow;
      return { content: [{ type: "text", text: `${args.command} finished` }], details: {} };
    }, signal) }],
    activeToolNames: [TEST_TOOL.name],
    developerInstructions: "Use independent commands in one turn, then wait for their results.",
    requestOptions: { current: {} },
    asyncAfterTurn: async (turn) => {
      const results = await scheduler.ready({ wait: !turn.message.content.some((block) => block.type === "toolCall") });
      if (results.length) await session.sendCustomMessage(lateToolMessage(results), { deliverAs: "steer" });
    },
  });
  t.after(async () => { finishSlow(); await scheduler.cancel(); session.dispose(); });
  session.agent.getApiKey = async () => "offline-test-key";
  session.subscribe((event) => {
    if (event.type === "message_end" && event.message.role === "assistant") {
      scheduler.beginBatch(event.message.content.filter((block) => block.type === "toolCall").map((block) => block.id));
    }
  });
  const requests = [];
  session.agent.streamFunction = (_model, context) => {
    requests.push(JSON.parse(JSON.stringify(context.messages)));
    const content = requests.length === 1
      ? [{ type: "toolCall", id: "fast-call", name: "exec_command", arguments: { command: "fast" } }, { type: "toolCall", id: "slow-call", name: "exec_command", arguments: { command: "slow" } }]
      : [{ type: "text", text: requests.length === 2 ? "Waiting for the slow command." : "Both results checked." }];
    const message = {
      role: "assistant", content, api: model.api, provider: model.provider, model: model.id,
      stopReason: requests.length === 1 ? "toolUse" : "stop", timestamp: Date.now(),
      usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    return { async *[Symbol.asyncIterator]() { yield { type: "done", reason: message.stopReason, message }; }, result: async () => message };
  };
  const running = session.prompt("Run fast and slow commands independently", { expandPromptTemplates: false });
  await waitFor(() => requests.length === 2);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2, "idle wait must not make another model request");
  assert.ok(JSON.stringify(requests[1]).includes("still running"));
  await session.steer("Check an additional condition while the command runs.");
  scheduler.notifyExternalInput();
  await waitFor(() => requests.length === 3);
  assert.ok(JSON.stringify(requests[2]).includes("additional condition"));
  finishSlow();
  await running;
  assert.equal(requests.length, 4);
  assert.deepEqual(requests[3].slice(0, requests[1].length), requests[1]);
  assert.ok(JSON.stringify(requests[3]).includes("slow finished"));
  assert.equal(scheduler.pending, false);
});

test("real pinned Pi loop groups clustered late siblings into one model request", async (t) => {
  const layout = temporaryLayout(t);
  const modelRuntime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
  const model = modelRuntime.getModel("openai-codex", "gpt-5.6-sol");
  assert.ok(model);
  modelRuntime.hasConfiguredAuth = () => true;
  const scheduler = new AsyncToolCoordinator();
  const finish = {};
  const gates = Object.fromEntries(["a", "b", "c"].map((id) => [id, new Promise((resolve) => { finish[id] = resolve; })]));
  let afterTurns = 0;
  let session;
  session = await realPiSdk.createSession({
    cwd: layout.workspace, agentDir: layout.agentDir, modelRuntime, model,
    thinkingLevel: "high", sessionManager: SessionManager.inMemory(layout.workspace),
    customTools: [{ ...TEST_TOOL, label: "Command", executionMode: "parallel", execute: (id, args, signal) => scheduler.execute(id, "exec_command", async () => {
      if (gates[args.command]) await gates[args.command];
      if (args.command === "b") throw new Error("b failed offline");
      return { content: [{ type: "text", text: `${args.command} finished` }], details: {} };
    }, signal) }],
    activeToolNames: [TEST_TOOL.name], requestOptions: { current: {} },
    asyncAfterTurn: async (turn) => {
      afterTurns++;
      const results = await scheduler.ready({ wait: !turn.message.content.some((block) => block.type === "toolCall") });
      if (results.length) await session.sendCustomMessage(lateToolMessage(results), { deliverAs: "steer" });
    },
  });
  t.after(async () => {
    for (const resolve of Object.values(finish)) resolve();
    await scheduler.cancel();
    session.dispose();
  });
  session.agent.getApiKey = async () => "offline-test-key";
  session.subscribe((event) => {
    if (event.type === "message_end" && event.message.role === "assistant") {
      scheduler.beginBatch(event.message.content.filter((block) => block.type === "toolCall").map((block) => block.id));
    }
  });
  const requests = [];
  session.agent.streamFunction = (_model, context) => {
    requests.push(JSON.parse(JSON.stringify(context.messages)));
    const message = {
      role: "assistant",
      content: requests.length === 1
        ? ["fast", "a", "b", "c"].map((id) => ({ type: "toolCall", id, name: "exec_command", arguments: { command: id } }))
        : [{ type: "text", text: "Checked the available results." }],
      api: model.api, provider: model.provider, model: model.id,
      stopReason: requests.length === 1 ? "toolUse" : "stop", timestamp: Date.now(),
      usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    return { async *[Symbol.asyncIterator]() { yield { type: "done", reason: message.stopReason, message }; }, result: async () => message };
  };
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 10_000 });
  t.mock.method(performance, "now", () => Date.now());
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  const drainUntil = async (condition) => {
    for (let attempt = 0; attempt < 200 && !condition(); attempt++) {
      t.mock.timers.tick(0);
      await flush();
    }
    assert.ok(condition(), "the offline Pi loop reached its expected state");
  };
  const tick = async (milliseconds) => { t.mock.timers.tick(milliseconds); await flush(); };
  const running = session.prompt("Run four independent commands and inspect their results", { expandPromptTemplates: false });
  await drainUntil(() => scheduler.jobs.get("fast")?.done);
  assert.equal(requests.length, 1);
  await tick(1000);
  await drainUntil(() => afterTurns === 2);
  assert.equal(requests.length, 2);
  const sentPrefix = JSON.parse(JSON.stringify(requests[1]));
  await tick(60_000);
  assert.equal(requests.length, 2, "no result means no periodic model wake");
  finish.a();
  await drainUntil(() => scheduler.jobs.get("a").done);
  await tick(700);
  finish.b();
  await drainUntil(() => scheduler.jobs.get("b").done);
  await tick(300);
  assert.equal(requests.length, 2, "the old completion timer cannot split siblings across requests");
  await tick(699);
  assert.equal(requests.length, 2);
  await tick(1);
  await drainUntil(() => afterTurns === 3);
  assert.equal(requests.length, 3);
  assert.deepEqual(requests[2].slice(0, sentPrefix.length), sentPrefix);
  const grouped = requests[2].slice(sentPrefix.length).filter((message) => JSON.stringify(message).includes("Completed tool call"));
  assert.equal(grouped.length, 1);
  assert.match(JSON.stringify(grouped[0]), /a finished/);
  assert.match(JSON.stringify(grouped[0]), /b failed offline/);
  await tick(60_000);
  assert.equal(requests.length, 3, "consumed results cannot cause another model wake");
  finish.c();
  await running;
  assert.equal(requests.length, 4, "the final sibling releases immediately");
  assert.deepEqual(requests[3].slice(0, requests[2].length), requests[2]);
  assert.equal(scheduler.pending, false);
});

// Pi finishes an invalid call (here: missing required arguments) with an immediate
// error result and never invokes execute() for it. The runtime reports that through
// tool_execution_end; the batch must then count as finished without a window.
for (const wired of [true, false]) {
  test(`real pinned Pi loop ${wired ? "releases" : "keeps waiting for"} a batch whose other call was rejected before execution`, async (t) => {
    const layout = temporaryLayout(t);
    const modelRuntime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
    const model = modelRuntime.getModel("openai-codex", "gpt-5.6-sol");
    assert.ok(model);
    modelRuntime.hasConfiguredAuth = () => true;
    const scheduler = new AsyncToolCoordinator();
    let session;
    session = await realPiSdk.createSession({
      cwd: layout.workspace, agentDir: layout.agentDir, modelRuntime, model,
      thinkingLevel: "high", sessionManager: SessionManager.inMemory(layout.workspace),
      customTools: [{ ...TEST_TOOL, label: "Command", executionMode: "parallel", execute: (id, args, signal) => scheduler.execute(id, "exec_command", async () => ({
        content: [{ type: "text", text: `${args.command} finished` }], details: {},
      }), signal) }],
      activeToolNames: [TEST_TOOL.name], requestOptions: { current: {} },
      asyncAfterTurn: async (turn) => {
        const results = await scheduler.ready({ wait: !turn.message.content.some((block) => block.type === "toolCall") });
        if (results.length) await session.sendCustomMessage(lateToolMessage(results), { deliverAs: "steer" });
      },
    });
    t.after(async () => { await scheduler.cancel(); session.dispose(); });
    session.agent.getApiKey = async () => "offline-test-key";
    session.subscribe((event) => {
      if (event.type === "message_end" && event.message.role === "assistant") {
        scheduler.beginBatch(event.message.content.filter((block) => block.type === "toolCall").map((block) => block.id));
      }
      if (wired && event.type === "tool_execution_end") scheduler.skip(event.toolCallId);
    });
    const requests = [];
    session.agent.streamFunction = (_model, context) => {
      requests.push(JSON.parse(JSON.stringify(context.messages)));
      const message = {
        role: "assistant",
        content: requests.length === 1
          ? [{ type: "toolCall", id: "valid-call", name: "exec_command", arguments: { command: "valid" } },
            { type: "toolCall", id: "invalid-call", name: "exec_command", arguments: {} }]
          : [{ type: "text", text: "Checked the valid command." }],
        api: model.api, provider: model.provider, model: model.id,
        stopReason: requests.length === 1 ? "toolUse" : "stop", timestamp: Date.now(),
        usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      };
      return { async *[Symbol.asyncIterator]() { yield { type: "done", reason: message.stopReason, message }; }, result: async () => message };
    };
    t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 10_000 });
    t.mock.method(performance, "now", () => Date.now());
    const flush = () => new Promise((resolve) => setImmediate(resolve));
    const drainUntil = async (condition, attempts = 100) => {
      for (let attempt = 0; attempt < attempts && !condition(); attempt++) { t.mock.timers.tick(0); await flush(); }
      return condition();
    };
    const running = session.prompt("Run one valid and one invalid command", { expandPromptTemplates: false });
    assert.equal(await drainUntil(() => scheduler.jobs.get("valid-call")?.done), true);
    if (wired) {
      assert.equal(await drainUntil(() => requests.length === 2), true, "no collection window is paid for a call that can never finish");
    } else {
      assert.equal(await drainUntil(() => requests.length === 2, 30), false, "without the notice the batch is held open");
      t.mock.timers.tick(1000);
      assert.equal(await drainUntil(() => requests.length === 2), true);
    }
    await running;
    const toolResults = requests[1].filter((message) => message.role === "toolResult");
    assert.deepEqual(toolResults.map((message) => [message.toolCallId, message.isError]), [["valid-call", false], ["invalid-call", true]]);
    assert.match(JSON.stringify(toolResults[0]), /valid finished/, "the real result is delivered directly, not as a placeholder");
    assert.equal(requests.length, 2);
    assert.equal(scheduler.pending, false);
  });
}

for (const stopReason of ["error", "aborted", "length"]) {
  test(`Pi 1.0 finishTurn does not await late tools after a terminal ${stopReason}`, async (t) => {
    const layout = temporaryLayout(t);
    const modelRuntime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
    const model = modelRuntime.getModel("openai-codex", "gpt-5.6-sol");
    assert.ok(model);
    modelRuntime.hasConfiguredAuth = () => true;
    let afterTurns = 0;
    const session = await realPiSdk.createSession({
      cwd: layout.workspace, agentDir: layout.agentDir, modelRuntime, model,
      thinkingLevel: "high", sessionManager: SessionManager.inMemory(layout.workspace),
      customTools: [], activeToolNames: [], requestOptions: { current: {} },
      asyncAfterTurn: async () => { afterTurns++; throw new Error("terminal turn cannot wait for tools"); },
    });
    t.after(() => session.dispose());
    session.agent.getApiKey = async () => "offline-test-key";
    let requests = 0;
    session.agent.streamFunction = () => {
      requests++;
      const message = {
        role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
        stopReason, errorMessage: "synthetic terminal response", timestamp: Date.now(),
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      };
      return { async *[Symbol.asyncIterator]() { yield { type: "error", reason: stopReason, error: message }; },
        result: async () => message };
    };
    await session.prompt("Offline terminal response", { expandPromptTemplates: false });
    assert.equal(requests, 1, "a terminal result must not be replayed");
    assert.equal(afterTurns, 0);
    // Pi may drop a recoverable output-limit attempt from its own projection, but
    // never turns it into another request or a wait for tool output.
    if (stopReason !== "length") assert.equal(session.agent.state.messages.at(-1).stopReason, stopReason);
    assert.equal(session.agent.state.isStreaming, false);
  });
}
