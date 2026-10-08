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

for (const stopReason of ["error", "aborted"]) {
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
    assert.equal(session.agent.state.messages.at(-1).stopReason, stopReason);
    assert.equal(session.agent.state.isStreaming, false);
  });
}
