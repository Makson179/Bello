import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { estimateTokens } from "@earendil-works/pi-coding-agent";

import { realPiSdk } from "../src/pi-sdk.mjs";
import { PiWorkerRuntime } from "../src/runtime.mjs";
import { temporaryLayout, TEST_TOOL, waitFor } from "./helpers.mjs";

// Worker-level No Need For Context: the real runtime and the real Pi SDK with an
// offline provider, scripted model answers and scripted summaries.
const MODEL_ID = "offline-context";
const POLICY = { enabled: true, thresholdTokens: 3000, retainPercent: 10 };
const ZERO_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };

function configure(layout) {
  writeFileSync(join(layout.agentDir, "models.json"), JSON.stringify({ providers: { "bello-local": {
    api: "openai-completions", baseUrl: "http://127.0.0.1:1/v1",
    models: [{ id: MODEL_ID, name: MODEL_ID, reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32768, maxTokens: 4096 }],
  } } }));
  writeFileSync(join(layout.agentDir, "auth.json"), JSON.stringify({ "bello-local": { type: "api_key", key: "offline-fixture-key" } }));
}

const reply = (model, content, stopReason, input) => ({ role: "assistant", content, stopReason, api: model.api,
  provider: model.provider, model: model.id, timestamp: Date.now(),
  usage: { input, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: input + 5, cost: ZERO_COST } });
const streamOf = (message) => ({ async *[Symbol.asyncIterator]() { yield { type: "done", reason: message.stopReason, message }; },
  result: async () => message });

// One scripted world shared by every worker generation that opens the thread.
function world(t, layout) {
  configure(layout);
  const state = { steps: 0, requests: 0, assistantInputs: [], summaryInputs: [], summaries: 0, summarize: undefined,
    events: [], hostCalls: 0, toolText: "e".repeat(400) };
  const create = async () => {
    const sdk = { ...realPiSdk, async createSession(options) {
      const session = await realPiSdk.createSession(options);
      options.modelRuntime.completeSimple = async (model, context, requestOptions) => {
        const index = ++state.summaries;
        const answer = state.summarize
          ? await state.summarize(model, context, requestOptions, index)
          : reply(model, [{ type: "text", text: `Compact summary ${index}.` }], "stop", 5000 + index);
        if (answer?.usage) state.summaryInputs.push(answer.usage.input);
        return answer;
      };
      session.agent.getApiKey = async () => "offline-fixture-key";
      session.agent.streamFunction = (model, context) => {
        const index = ++state.requests;
        const input = context.messages.reduce((sum, message) => sum + estimateTokens(message), 0);
        state.assistantInputs.push(input);
        const call = state.steps > 0;
        if (call) state.steps--;
        return streamOf(reply(model, call
          ? [{ type: "toolCall", id: `scripted-call-${index}`, name: TEST_TOOL.name, arguments: { command: String(index) } }]
          : [{ type: "text", text: "{}" }], call ? "toolUse" : "stop", input));
      };
      return session;
    } };
    const runtime = new PiWorkerRuntime({ sdk, emit: async (message) => state.events.push(message), hostCalls: {
      async call() {
        state.hostCalls++;
        return { content: [{ type: "text", text: state.toolText }], details: {}, isError: false };
      } } });
    t.after(() => runtime.close());
    await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
    return runtime;
  };
  return { state, create };
}

const start = (runtime, layout, extra = {}) => runtime.dispatch("thread/start", { threadId: "thread-1", cwd: layout.workspace,
  provider: "bello-local", model: MODEL_ID, tools: [TEST_TOOL], systemPrompt: "Immutable role instructions.",
  noNeedForContext: POLICY, ...extra });

async function runTurn(runtime, state, turnId, steps, extra = {}) {
  const before = state.events.filter((event) => event.method === "turn/completed").length;
  state.steps = steps;
  await runtime.dispatch("turn/start", { threadId: "thread-1", turnId, input: `Neutral task for ${turnId}.`, ...extra });
  await waitFor(() => state.events.filter((event) => event.method === "turn/completed").length === before + 1, 5000);
  return state.events.filter((event) => event.method === "turn/completed").at(-1).params.turn;
}

const compactionEvents = (state) => state.events.filter((event) => event.method === "thread/contextCompaction").map((event) => event.params);
const sum = (values) => values.reduce((total, value) => total + value, 0);

test("a worker turn accounts every summary request once and publishes one event per checkpoint", async (t) => {
  const layout = temporaryLayout(t);
  const { state, create } = world(t, layout);
  const runtime = await create();
  await start(runtime, layout);
  const turn = await runTurn(runtime, state, "accounting", 90);
  assert.equal(turn.status, "completed");
  const events = compactionEvents(state);
  assert.ok(events.filter((event) => event.status === "completed").length >= 2, "the scripted loop crosses the threshold repeatedly");
  assert.equal(events.length, state.summaries, "one answered request per checkpoint event");
  assert.equal(events.every((event) => event.turnId === "accounting"), true);
  assert.equal(turn.usage.input, sum(state.assistantInputs) + sum(state.summaryInputs), "summary usage is added once, never twice");
  assert.equal(turn.usage.responses.length, state.requests + state.summaries);
  assert.equal(turn.usageIncomplete, undefined);
  const read = await runtime.dispatch("thread/read", { threadId: "thread-1" });
  assert.equal(read.thread.turns[0].usage.input, turn.usage.input);
});

test("interrupting a turn during a summary ends it interrupted with honest usage and a usable thread", async (t) => {
  const layout = temporaryLayout(t);
  const { state, create } = world(t, layout);
  let secondStarted = false;
  state.summarize = async (model, _context, options, index) => {
    if (index === 1) return reply(model, [{ type: "text", text: "" }], "stop", 7);
    secondStarted = true;
    return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true }));
  };
  const runtime = await create();
  await start(runtime, layout);
  state.steps = 90;
  await runtime.dispatch("turn/start", { threadId: "thread-1", turnId: "interrupted", input: "Neutral task." });
  await waitFor(() => secondStarted, 5000);
  const requestsAtInterrupt = state.requests;
  await runtime.dispatch("turn/interrupt", { threadId: "thread-1", turnId: "interrupted" });
  await waitFor(() => state.events.some((event) => event.method === "turn/completed"), 5000);
  const terminals = state.events.filter((event) => event.method === "turn/completed");
  assert.equal(terminals.length, 1);
  const turn = terminals[0].params.turn;
  assert.equal(turn.status, "interrupted");
  assert.equal(turn.usageIncomplete, true, "the unanswered summary request has unknown usage");
  assert.equal(turn.usage.input, sum(state.assistantInputs) + 7, "the answered attempt is counted once");
  const events = compactionEvents(state);
  assert.deepEqual(events.map((event) => [event.status, event.durable, event.usageIncomplete]), [["interrupted", false, true]]);
  assert.equal(state.requests, requestsAtInterrupt, "no model request after the interrupt");
  assert.equal(runtime.getRecord("thread-1").active, undefined);
  const journal = readFileSync(runtime.getRecord("thread-1").meta.sessionFile, "utf8");
  assert.equal(journal.includes('"context_edit"'), false, "cancellation left the history untouched");
  assert.equal(journal.includes("bello-context-compaction"), false);

  state.summarize = undefined;
  const followUp = await runTurn(runtime, state, "after-interrupt", 0);
  assert.equal(followUp.status, "completed");
  assert.equal(followUp.usageIncomplete, undefined);
});

test("a restarted worker resumes a compacted thread without re-compacting and keeps compacting later", async (t) => {
  const layout = temporaryLayout(t);
  const { state, create } = world(t, layout);
  const first = await create();
  await start(first, layout);
  const initial = await runTurn(first, state, "before-restart", 60);
  assert.equal(initial.status, "completed");
  const checkpointsBefore = compactionEvents(state).filter((event) => event.status === "completed").length;
  assert.ok(checkpointsBefore >= 1);
  await first.close();

  const second = await create();
  await second.dispatch("thread/resume", { threadId: "thread-1" });
  const summariesBefore = state.summaries;
  const short = await runTurn(second, state, "short-after-restart", 3);
  assert.equal(short.status, "completed");
  assert.equal(state.summaries, summariesBefore, "a restart below the threshold never compacts again");

  const long = await runTurn(second, state, "long-after-restart", 120);
  assert.equal(long.status, "completed");
  const after = compactionEvents(state).filter((event) => event.status === "completed").length;
  assert.ok(after >= checkpointsBefore + 2, "repeated checkpoints continue in the restarted worker");
  const read = await second.dispatch("thread/read", { threadId: "thread-1" });
  assert.deepEqual(read.thread.turns.map((turn) => turn.status), ["completed", "completed", "completed"]);
});

test("checkpoints keep working across turns that switch the structured-result tool on and off", async (t) => {
  const layout = temporaryLayout(t);
  const { state, create } = world(t, layout);
  const runtime = await create();
  await start(runtime, layout);
  const schema = { type: "object", properties: {}, additionalProperties: true };
  // Each switch appends an immutable tool-loadout entry in the middle of the history.
  assert.equal((await runTurn(runtime, state, "plain-one", 20)).status, "completed");
  assert.equal((await runTurn(runtime, state, "structured", 20, { outputSchema: schema })).status, "completed");
  assert.equal((await runTurn(runtime, state, "plain-two", 20)).status, "completed");
  const journal = readFileSync(runtime.getRecord("thread-1").meta.sessionFile, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const loadouts = journal.filter((entry) => entry.type === "message" && entry.message?.role === "system" && JSON.stringify(entry.message).includes("submit_result"));
  assert.ok(loadouts.length >= 2, "adding and removing submit_result created immutable loadout entries");
  assert.equal((await runTurn(runtime, state, "plain-three", 120)).status, "completed");
  const completed = compactionEvents(state).filter((event) => event.status === "completed");
  assert.ok(completed.length >= 3, "checkpoints continue past the loadout entries");
  const after = readFileSync(runtime.getRecord("thread-1").meta.sessionFile, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const loadoutIds = new Set(loadouts.map((entry) => entry.id));
  assert.equal(after.some((entry) => entry.type === "context_edit" && loadoutIds.has(entry.targetId)), false);
});
