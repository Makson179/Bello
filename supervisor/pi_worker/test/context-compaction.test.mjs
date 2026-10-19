import assert from "node:assert/strict";
import test from "node:test";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { getEventListeners } from "node:events";
import { SessionManager, estimateTokens } from "@earendil-works/pi-coding-agent";
import { clampMaxTokensToContext } from "@earendil-works/pi-ai/api/simple-options";
import { realPiSdk } from "../src/pi-sdk.mjs";
import { CONTEXT_COMPACTION_BEGIN_TYPE, CONTEXT_COMPACTION_STATE_TYPE, installContextTransactionGuard,
  contextBudget, normalizeContextPolicy, planContextCompaction, contextCompactionExtension, assertContextFits,
  CONTEXT_SUMMARY_TIMEOUT_MS, CONTEXT_SUMMARY_MAX_CHARS } from "../src/context-compaction.mjs";
import { AsyncToolCoordinator, lateToolMessage } from "../src/async-tools.mjs";
import { PiWorkerRuntime } from "../src/runtime.mjs";
import { createFakeSdk, temporaryLayout, TEST_TOOL, waitFor } from "./helpers.mjs";

const clone = (value) => JSON.parse(JSON.stringify(value));
const ZERO_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };
const policy = { enabled: true, thresholdTokens: 1200, retainPercent: 10 };

function response(model, content, stopReason = "stop", input = 0) {
  return { role: "assistant", content, stopReason, api: model.api, provider: model.provider, model: model.id,
    timestamp: Date.now(), usage: { input, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: input + 5, cost: ZERO_COST } };
}

function stream(message) {
  return { async *[Symbol.asyncIterator]() { yield { type: "done", reason: message.stopReason, message }; }, result: async () => message };
}

async function setup(t, overrides = {}) {
  const layout = temporaryLayout(t);
  const modelRuntime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
  const catalogModel = modelRuntime.getModel("openai-codex", "gpt-5.6-sol");
  const model = overrides.model ? { ...catalogModel, ...overrides.model } : catalogModel;
  modelRuntime.hasConfiguredAuth = () => true;
  const summaries = [];
  modelRuntime.completeSimple = async (...args) => {
    summaries.push(args);
    return overrides.summarize ? overrides.summarize(...args) : response(model,
      [{ type: "text", text: `Preserve goal and completed checks ${summaries.length}.` }]);
  };
  const events = [];
  const notificationSnapshots = [];
  const manager = overrides.persist ? SessionManager.create(layout.workspace, layout.stateDir) : SessionManager.inMemory(layout.workspace);
  const session = await realPiSdk.createSession({
    cwd: layout.workspace, agentDir: layout.agentDir, modelRuntime, model,
    thinkingLevel: "high", sessionManager: manager,
    customTools: overrides.customTools ?? [{ ...TEST_TOOL, label: "Command", execute: async (_id, args) => ({
      content: [{ type: "text", text: `private-output-${args.command} ` + "long tool evidence ".repeat(300) }], details: {},
    }) }], activeToolNames: [TEST_TOOL.name], systemPrompt: overrides.systemPrompt ?? "Immutable role instructions.",
    noNeedForContext: overrides.policy ?? policy,
    requestOptions: { current: { nativeEffort: "ultra", serviceTier: "priority" } },
    onContextCompaction: async (event) => {
      events.push(event);
      notificationSnapshots.push(clone(manager.buildSessionProjection().messages));
      await overrides.onContextCompaction?.(event);
    },
    pendingCallIds: overrides.pendingCallIds,
    asyncAfterTurn: overrides.asyncAfterTurn,
  });
  t.after(() => session.dispose());
  session.agent.getApiKey = async () => "offline-test-key";
  const requests = [];
  const toolCount = overrides.toolCount ?? 4;
  session.agent.streamFunction = (_model, context) => {
    requests.push({ context: clone(context), compactions: events.filter((event) => event.status === "completed").length });
    const index = requests.length;
    return stream(response(model, overrides.assistantContent ? overrides.assistantContent(index) : index <= toolCount
      ? [{ type: "toolCall", id: `call-${index}`, name: TEST_TOOL.name, arguments: { command: String(index) } }]
      : [{ type: "text", text: "Finished checking." }], index <= toolCount ? "toolUse" : "stop",
    context.messages.reduce((sum, message) => sum + estimateTokens(message), 0)));
  };
  return { ...layout, model, modelRuntime, session, manager, summaries, events, notificationSnapshots, requests };
}

test("real Pi projection compacts repeatedly with unchanged prefix and private append-only history", async (t) => {
  const { session, manager, summaries, events, notificationSnapshots, requests, model } = await setup(t);
  await session.prompt("Immutable original task.", { expandPromptTemplates: false });
  assert.ok(summaries.length >= 2, "long loop crosses threshold again after first compaction");
  assert.equal(events.every((event) => event.status === "completed"), true);
  assert.ok(notificationSnapshots.every((messages) => JSON.stringify(messages).includes("bello_context_summary")), "success notifications observe the already-committed projection");
  assert.equal(requests.length, 5, "summary does not become an agent turn");
  for (const [summaryModel, summaryContext, options] of summaries) {
    assert.equal(summaryModel, model);
    assert.deepEqual(summaryContext.tools, []);
    assert.equal(summaryContext.systemPrompt, session.systemPrompt);
    assert.equal(options.reasoning, "high");
    assert.deepEqual(options.onPayload({ reasoning: {} }), { reasoning: { effort: "ultra" }, service_tier: "priority" });
  }
  const first = requests[0].context.messages;
  for (const request of requests) assert.deepEqual(request.context.messages.slice(0, first.length), first);
  for (let index = 1; index < requests.length; index++) {
    if (requests[index].compactions === requests[index - 1].compactions) {
      assert.deepEqual(requests[index].context.messages.slice(0, requests[index - 1].context.messages.length), requests[index - 1].context.messages);
    }
  }
  const projected = manager.buildSessionProjection().messages;
  assert.match(JSON.stringify(projected), /bello_context_summary/);
  assert.doesNotMatch(JSON.stringify(projected), /private-output-1/);
  assert.match(JSON.stringify(manager.getEntries()), /private-output-1/);
  assert.ok(summaries.slice(1).some(([, context]) => JSON.stringify(context).includes("Preserve goal and completed checks 1")), "next summary folds the earlier summary into the old region");
  for (const request of requests) {
    const callIds = new Set(request.context.messages.flatMap((message) => message.role === "assistant"
      ? message.content.filter((block) => block.type === "toolCall").map((block) => block.id) : []));
    for (const message of request.context.messages) if (message.role === "toolResult") assert.ok(callIds.has(message.toolCallId));
  }
});

test("ordinary plain-text summaries are accepted and only wrapped by Bello", async (t) => {
  const state = await setup(t, { summarize: async (model) => response(model,
    [{ type: "text", text: "Completed checks are preserved. Continue with the remaining neutral records." }]) });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  assert.ok(state.events.length >= 2);
  assert.ok(state.events.every((event) => event.status === "completed"));
  const projection = JSON.stringify(state.manager.buildSessionProjection().messages);
  assert.match(projection, /bello_context_summary/);
  assert.match(projection, /Completed checks are preserved/);
  assert.ok(state.summaries.every(([, context]) => context.messages[0].content[0].text.includes("No special headings, JSON or XML format is required")));
});

for (const [label, handoff] of Object.entries({
  "useful blocker with later evidence": "I cannot run integration tests because the database is unavailable.\nImplemented the retry fix in worker.js; unit checks pass. Next: restore the database and run integration tests.",
  "useful blocker on one line": "I cannot run integration tests; unit checks pass and the retry fix is saved in worker.js.",
  "standalone concrete blocker": "I cannot run the integration test because the database is unavailable.",
  "standalone access blocker": "I am unable to read src/config.json because the filesystem returned permission denied.",
  "apology with a concrete blocker": "I'm sorry, the integration test could not run because the database is unavailable.",
  "recorded retry constraint": "I will not rerun the failed migration until the database schema is restored.",
  "reported refusal": '> "I cannot summarize this conversation."\nThe prior attempt failed; keep the retry fix and run unit tests.',
  "quoted code data": '```text\nI cannot summarize this conversation.\n```',
  "unstructured non-English facts": "修复已完成。下一步运行测试。",
})) {
  test(`real Pi accepts handoff content: ${label}`, async (t) => {
    const state = await setup(t, { toolCount: 2, summarize: async (model) => response(model,
      [{ type: "text", text: handoff }]) });
    await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
    assert.equal(state.events.length, 1);
    assert.equal(state.events[0].status, "completed");
    assert.equal(state.summaries.length, 1, "valid blocker handoffs need no retry");
    assert.match(JSON.stringify(state.manager.buildSessionProjection().messages), /bello_context_summary/);
  });
}

test("every answered summary request is accounted exactly once across checkpoints and failed retries", async (t) => {
  let answered = 0;
  const accounted = [];
  const state = await setup(t, { toolCount: 14,
    // Each checkpoint: an unusable first answer, then a usable retry.
    summarize: async (model) => {
      answered++;
      return response(model, [{ type: "text", text: answered % 2 === 1 ? "" : `Summary body ${answered}.` }], "stop", 1000 + answered);
    },
    onContextCompaction: (event) => { accounted.push(...event.usage.map((usage) => usage.input)); },
  });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  await state.session.flushContextCompactionNotifications();
  assert.ok(state.events.length >= 3);
  assert.ok(state.events.every((event) => event.status === "completed" && event.attempts === 2));
  assert.deepEqual([...accounted].sort((a, b) => a - b), Array.from({ length: answered }, (_, index) => 1001 + index));
  assert.equal(answered, state.events.length * 2);
});

test("a legacy envelope summary is still accepted and wrapped exactly once", async (t) => {
  const state = await setup(t, { toolCount: 2, summarize: async (model) => response(model,
    [{ type: "text", text: "<bello_context_summary>\nLegacy envelope body.\n</bello_context_summary>" }]) });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  assert.equal(state.events.length, 1);
  assert.equal(state.events[0].status, "completed");
  const summary = state.manager.buildSessionProjection().messages
    .flatMap((message) => Array.isArray(message.content) ? message.content : [])
    .map((block) => block.text ?? "").find((text) => text.includes("Legacy envelope body."));
  assert.equal(summary, "<bello_context_summary>\nLegacy envelope body.\n</bello_context_summary>");
});

test("the summary request asks for plain text near 16,000 characters and prescribes no format", async (t) => {
  const state = await setup(t, { toolCount: 2 });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  assert.ok(state.summaries.length >= 1);
  const prompt = state.summaries[0][1].messages[0].content[0].text;
  assert.match(prompt, /plain text/);
  assert.match(prompt, /at most 16000 characters \(about 16,000\)/);
  assert.match(prompt, /longer than 48,000 characters is discarded/);
  assert.match(prompt, /No special headings, JSON or XML format is required/);
  assert.doesNotMatch(prompt, /Return only <bello_context_summary>/);
});

function directSummaryBoundary(completeSimple, parentSignal, { onInterruptedSummary, summaryTimeoutMs, policyOverride = policy,
  systemPrompt = () => "Neutral role.",
  messages = [{ role: "user", content: "Original neutral task." },
    { role: "assistant", content: [{ type: "text", text: "Earlier neutral evidence. ".repeat(400) }] },
    { role: "assistant", content: [{ type: "text", text: "Recent neutral evidence. ".repeat(100) }] }],
  model = { id: "offline", provider: "local", contextWindow: 32768, maxTokens: 4096 } } = {}) {
  const handlers = new Map();
  contextCompactionExtension({ policy: policyOverride, modelRuntime: { completeSimple }, requestOptions: { current: {} },
    transformPayload: () => undefined, onInterruptedSummary, summaryTimeoutMs })({ on: (name, handler) => handlers.set(name, handler),
      getActiveTools: () => [], getAllTools: () => [] });
  const context = { contextMessages: messages, contextEntries: messages.map((message, index) => ({
    sourceEntry: { id: String(index), type: "message" }, messages: [message],
  })) };
  return handlers.get("turn_end")({ outcome: "completed", context }, {
    signal: parentSignal, model, thinkingLevel: "off", sessionManager: { getBranch: () => [] },
    getSystemPrompt: systemPrompt, getContextUsage: () => ({ tokens: 5000 }),
  });
}

test("one 500-second deadline aborts a stuck provider without retry or a late checkpoint", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const parent = new AbortController();
  const interrupted = [];
  let signal, release, calls = 0;
  const running = directSummaryBoundary((_model, _context, options) => {
    calls++; signal = options.signal;
    return new Promise((resolve) => { release = resolve; });
  }, parent.signal, { onInterruptedSummary: (data) => interrupted.push(data) });
  await Promise.resolve();
  assert.equal(CONTEXT_SUMMARY_TIMEOUT_MS, 500000);
  assert.equal(calls, 1);
  t.mock.timers.tick(CONTEXT_SUMMARY_TIMEOUT_MS);
  const result = await running;
  assert.equal(signal.aborted, true);
  assert.equal(parent.signal.aborted, false);
  assert.equal(getEventListeners(parent.signal, "abort").length, 0);
  assert.equal(result.entries.length, 1);
  assert.equal(result.entries[0].data.status, "failed");
  assert.equal(result.entries[0].data.attempts, 1);
  assert.equal(result.entries[0].data.failureReason, "summary_timeout");
  assert.equal(result.entries[0].data.usageIncomplete, true);
  assert.deepEqual(interrupted, [], "a deadline is a durable failed checkpoint, not a cancellation");
  release({ stopReason: "stop", content: [{ type: "text", text: "Late response must not commit." }] });
  await Promise.resolve();
  assert.equal(result.entries.length, 1);
  assert.equal(calls, 1);
});

test("both attempts share one deadline: the retry only gets the time the first attempt left over", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  const seen = [];
  const resolvers = [];
  const running = directSummaryBoundary((_model, _context, options) => {
    seen.push(options.signal);
    return new Promise((resolve) => resolvers.push(resolve));
  }, undefined, { summaryTimeoutMs: 1000 });
  await flush();
  assert.equal(seen.length, 1);
  t.mock.timers.tick(600);
  resolvers[0]({ stopReason: "stop", content: [{ type: "text", text: "" }], usage: { input: 3, output: 1, totalTokens: 4 } });
  await flush();
  assert.equal(seen.length, 2, "an unusable first answer is retried once");
  t.mock.timers.tick(399);
  await flush();
  assert.equal(seen[1].aborted, false);
  t.mock.timers.tick(1);
  const result = await running;
  assert.equal(seen[1].aborted, true, "the second attempt is cut off 1000 ms after the step began, not after its own start");
  assert.equal(seen.length, 2);
  const data = result.entries.at(-1).data;
  assert.equal(data.status, "failed");
  assert.equal(data.attempts, 2);
  assert.equal(data.failureReason, "summary_timeout");
  assert.equal(data.usageIncomplete, true);
  assert.deepEqual(data.usage.map((usage) => usage.input), [3]);
});

test("a failed first attempt may retry once inside the same deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const parent = new AbortController();
  let calls = 0;
  const result = await directSummaryBoundary(async () => {
    calls++;
    return calls === 1
      ? { stopReason: "stop", content: [{ type: "text", text: "" }], usage: { input: 5, output: 1, totalTokens: 6 } }
      : { stopReason: "stop", content: [{ type: "text", text: "Compact neutral summary." }], usage: { input: 7, output: 2, totalTokens: 9 } };
  }, parent.signal);
  assert.equal(calls, 2);
  assert.equal(result.entries.at(-1).data.status, "completed");
  assert.equal(result.entries.at(-1).data.attempts, 2);
  assert.deepEqual(result.entries.at(-1).data.usage.map((usage) => usage.input), [5, 7], "both answered attempts are accounted once");
  assert.equal(result.entries.at(-1).data.usageIncomplete, undefined);
});

test("parent cancellation promptly aborts a summary and leaves no durable telemetry or edit drafts", async () => {
  const parent = new AbortController();
  const interrupted = [];
  let signal, calls = 0, rejectLate;
  const running = directSummaryBoundary((_model, _context, options) => {
    calls++; signal = options.signal;
    return new Promise((_resolve, reject) => { rejectLate = reject; });
  }, parent.signal, { onInterruptedSummary: (data) => interrupted.push(data) });
  await Promise.resolve();
  parent.abort(new DOMException("Stopped", "AbortError"));
  assert.equal(await running, undefined);
  assert.equal(signal.aborted, true);
  assert.equal(calls, 1);
  assert.equal(getEventListeners(parent.signal, "abort").length, 0);
  assert.equal(interrupted.length, 1);
  assert.equal(interrupted[0].status, "interrupted");
  assert.equal(interrupted[0].durable, false);
  assert.equal(interrupted[0].usageIncomplete, true, "the unanswered request has unknown usage");
  assert.deepEqual(interrupted[0].usage, []);
  rejectLate(new Error("Late provider rejection is handled"));
  await Promise.resolve();
});

test("cancellation between answered attempts reports their exact usage, not an unknown one", async () => {
  const parent = new AbortController();
  const interrupted = [];
  let prompts = 0, requests = 0;
  const result = await directSummaryBoundary(async () => {
    requests++;
    return { stopReason: "stop", content: [{ type: "text", text: "" }], usage: { input: 9, output: 2, totalTokens: 11 } };
  }, parent.signal, {
    onInterruptedSummary: (data) => interrupted.push(data),
    // Calls: overhead estimate, first attempt, second attempt (aborted before it is sent).
    systemPrompt: () => { if (++prompts === 3) parent.abort(new DOMException("Stopped", "AbortError")); return "Neutral role."; },
  });
  assert.equal(result, undefined);
  assert.equal(requests, 1, "the cancelled retry is never sent");
  assert.equal(interrupted.length, 1);
  assert.equal(interrupted[0].attempts, 2);
  assert.equal(interrupted[0].usageIncomplete, false);
  assert.deepEqual(interrupted[0].usage.map((usage) => usage.input), [9]);
});

test("a cancellation before any request starts emits nothing", async () => {
  const parent = new AbortController();
  const interrupted = [];
  let calls = 0;
  parent.abort(new DOMException("Stopped", "AbortError"));
  const result = await directSummaryBoundary(async () => { calls++; return undefined; }, parent.signal,
    { onInterruptedSummary: (data) => interrupted.push(data) });
  assert.equal(result, undefined);
  assert.equal(calls, 0);
  assert.deepEqual(interrupted, []);
});

test("a summary of exactly the maximum length is accepted and one character more is rejected, never truncated", async () => {
  const large = [{ role: "user", content: "Original neutral task." },
    { role: "assistant", content: [{ type: "text", text: "Earlier neutral evidence. ".repeat(12000) }] },
    { role: "assistant", content: [{ type: "text", text: "Recent neutral evidence. ".repeat(100) }] }];
  const options = { messages: large, policyOverride: { enabled: true, thresholdTokens: 1200, retainPercent: 10 },
    model: { id: "offline", provider: "local", contextWindow: 1000000, maxTokens: 16384 } };
  const answer = (length) => async () => ({ stopReason: "stop", content: [{ type: "text", text: "s".repeat(length) }] });
  const accepted = await directSummaryBoundary(answer(CONTEXT_SUMMARY_MAX_CHARS), undefined, options);
  assert.equal(accepted.entries.at(-1).data.status, "completed");
  assert.equal(accepted.entries.find((entry) => entry.type === "context_edit").replacement.content[0].text.length,
    CONTEXT_SUMMARY_MAX_CHARS + "<bello_context_summary>\n\n</bello_context_summary>".length);
  const rejected = await directSummaryBoundary(answer(CONTEXT_SUMMARY_MAX_CHARS + 1), undefined, options);
  assert.deepEqual(rejected.entries.map((entry) => entry.type), ["custom"]);
  assert.equal(rejected.entries[0].data.status, "failed");
});

for (const phase of ["begin", "first_edit", "second_edit", "commit"]) {
  for (const fault of ["before", "after", "torn"]) {
    test(`persisted Pi rolls back ${phase} failure ${fault} durable append`, async (t) => {
      const state = await setup(t, { persist: true, toolCount: 2 });
      const persist = state.manager._persist.bind(state.manager);
      let fired = false;
      let edits = 0;
      let baseline;
      state.manager._persist = (entry) => {
        if (entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_BEGIN_TYPE) {
          baseline ??= clone(state.manager.buildSessionProjection().messages);
        }
        if (entry.type === "context_edit") edits++;
        const selected = phase === "begin" ? entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_BEGIN_TYPE
          : phase === "commit" ? entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_STATE_TYPE && entry.data.status === "completed"
            : entry.type === "context_edit" && edits === (phase === "first_edit" ? 1 : 2);
        if (!fired && selected) {
          fired = true;
          if (fault === "after") persist(entry);
          if (fault === "torn") appendFileSync(state.manager.getSessionFile(), JSON.stringify(entry).slice(0, 35));
          throw new Error("synthetic checkpoint storage fault");
        }
        persist(entry);
      };
      await state.session.prompt("Immutable original task.", { expandPromptTemplates: false });
      assert.ok(fired);
      assert.equal(state.events.length, 1);
      assert.equal(state.events[0].status, "failed");
      assert.equal(state.events[0].checkpointRolledBack, true);
      assert.deepEqual(state.requests.at(-1).context.messages.slice(0, baseline.length), baseline);
      const reopened = SessionManager.open(state.manager.getSessionFile());
      installContextTransactionGuard(reopened);
      assert.deepEqual(reopened.buildSessionProjection().messages.slice(0, baseline.length), baseline);
      assert.equal(reopened.getBranch().some((entry) => entry.type === "context_edit"), false);
    });
  }
}

test("failed-summary telemetry storage failure cannot poison the next provider request or resume", async (t) => {
  const state = await setup(t, { persist: true, toolCount: 2,
    summarize: (model) => response(model, [{ type: "text", text: "" }]),
  });
  const persist = state.manager._persist.bind(state.manager);
  let fired = false;
  let baseline;
  state.manager._persist = (entry) => {
    if (!fired && entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_STATE_TYPE) {
      fired = true;
      baseline = clone(state.manager.buildSessionProjection().messages);
      appendFileSync(state.manager.getSessionFile(), JSON.stringify(entry).slice(0, 35));
      throw new Error("synthetic torn telemetry write");
    }
    persist(entry);
  };
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.ok(fired);
  assert.equal(state.events[0].status, "failed");
  assert.deepEqual(state.requests.at(-1).context.messages.slice(0, baseline.length), baseline);
  const reopened = SessionManager.open(state.manager.getSessionFile());
  installContextTransactionGuard(reopened);
  assert.deepEqual(reopened.buildSessionProjection().messages.slice(0, baseline.length), baseline);
});

test("a crashed torn begin record is fenced before the next user message can be appended", async (t) => {
  const state = await setup(t, { persist: true, policy: { enabled: false }, toolCount: 0 });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  const baseline = clone(state.manager.buildSessionProjection().messages);
  appendFileSync(state.manager.getSessionFile(), '{"type":"custom","customType":"bello-context');
  const reopened = SessionManager.open(state.manager.getSessionFile());
  installContextTransactionGuard(reopened);
  reopened.appendMessage({ role: "user", content: "New constraint after storage recovery.", timestamp: Date.now() });
  const final = SessionManager.open(state.manager.getSessionFile());
  installContextTransactionGuard(final);
  assert.deepEqual(final.buildSessionProjection().messages.slice(0, baseline.length), baseline);
  assert.equal(final.buildSessionProjection().messages.at(-1).content, "New constraint after storage recovery.");
});

test("interrupted persisted checkpoint recovers before resume; failed recovery remains closed and retryable", async (t) => {
  const state = await setup(t, { persist: true, toolCount: 0, policy: { enabled: false } });
  await state.session.prompt("Immutable original task.", { expandPromptTemplates: false });
  const baseline = clone(state.manager.buildSessionProjection().messages);
  const first = state.manager.getBranch().find((entry) => entry.type === "message" && entry.message.role === "user");
  const data = { transactionId: "synthetic-crashed-checkpoint", status: "completed", tokensBefore: 2000,
    effectiveThresholdTokens: 1200, attempts: 1, usage: [] };
  state.manager.appendCustomEntry(CONTEXT_COMPACTION_BEGIN_TYPE, data);
  state.manager.appendContextEdit(first.id, { content: "incomplete replacement" });
  // A process exits here: no commit/rollback state was appended.
  const failedResume = SessionManager.open(state.manager.getSessionFile());
  failedResume._persist = () => { throw new Error("storage remains unavailable"); };
  assert.throws(() => installContextTransactionGuard(failedResume), /recovery could not be persisted/);
  const resumed = SessionManager.open(state.manager.getSessionFile());
  installContextTransactionGuard(resumed);
  assert.deepEqual(resumed.buildSessionProjection().messages, baseline);
  assert.equal(resumed.getBranch().at(-1).data.checkpointRolledBack, true);
  const reopened = SessionManager.open(state.manager.getSessionFile());
  installContextTransactionGuard(reopened);
  assert.deepEqual(reopened.buildSessionProjection().messages, baseline);
});

const toolCallBlock = { type: "toolCall", id: "summary-call", name: TEST_TOOL.name, arguments: { command: "1" } };
const unusableSummaries = {
  empty: (model) => response(model, [{ type: "text", text: "" }], "stop", 11),
  whitespace: (model) => response(model, [{ type: "text", text: " \n\t " }], "stop", 11),
  "empty envelope": (model) => response(model, [{ type: "text", text: "<bello_context_summary>\n</bello_context_summary>" }], "stop", 11),
  refused: (model) => response(model, [{ type: "text", text: "<bello_context_summary>I cannot summarize this.</bello_context_summary>" }], "stop", 11),
  "plain-text refusal": (model) => response(model, [{ type: "text", text: "I'm sorry, but I can't help with that request." }], "stop", 11),
  "explicit summary refusal": (model) => response(model, [{ type: "text", text: "I am unable to provide a summary of this conversation." }], "stop", 11),
  "bare apology": (model) => response(model, [{ type: "text", text: "I'm sorry." }], "stop", 11),
  "bare deictic refusal": (model) => response(model, [{ type: "text", text: "I won't do that." }], "stop", 11),
  "heading-padded refusal": (model) => response(model, [{ type: "text", text: "# Summary\nI cannot summarize this conversation." }], "stop", 11),
  "list-padded refusals": (model) => response(model, [{ type: "text", text: "## Handoff\n- I cannot summarize this.\n- I'm sorry, but I am unable to comply." }], "stop", 11),
  "markup-only": (model) => response(model, [{ type: "text", text: "###\n---\n```\n```" }], "stop", 11),
  "empty template": (model) => response(model, [{ type: "text", text: "# Summary\n## Progress\nNone.\n## Next steps\nN/A" }], "stop", 11),
  "refusal block": (model) => response(model, [{ type: "refusal", refusal: "Declined." }], "stop", 11),
  truncated: (model) => response(model, [{ type: "text", text: "<bello_context_summary>cut short" }], "length", 11),
  "token-limit stop": (model) => response(model, [{ type: "text", text: "A complete-looking summary." }], "max_tokens", 11),
  "tool use": (model) => response(model, [toolCallBlock], "toolUse", 11),
  "tool call beside text": (model) => response(model, [{ type: "text", text: "A summary." }, toolCallBlock], "stop", 11),
  "over the maximum": (model) => response(model, [{ type: "text", text: "x".repeat(CONTEXT_SUMMARY_MAX_CHARS + 1) }], "stop", 11),
  nonshrinking: (model) => response(model, [{ type: "text", text: "Too long. ".repeat(3000) }], "stop", 11),
  "legacy envelope that does not shrink": (model) => response(model,
    [{ type: "text", text: `<bello_context_summary>${"Too long. ".repeat(3000)}</bello_context_summary>` }], "stop", 11),
  throws: () => { throw new Error("offline provider error"); },
};
for (const [bad, summarize] of Object.entries(unusableSummaries)) {
  test(`real Pi keeps old context after two ${bad} summary attempts`, async (t) => {
    const state = await setup(t, { toolCount: 2, summarize });
    await state.session.prompt("Original task.", { expandPromptTemplates: false });
    await state.session.flushContextCompactionNotifications();
    assert.equal(state.summaries.length, 2);
    assert.equal(state.events.length, 1);
    assert.equal(state.events[0].status, "failed");
    assert.equal(state.events[0].attempts, 2);
    assert.equal(state.events[0].usage.length, bad === "throws" ? 0 : 2, "each answered attempt is accounted exactly once");
    assert.equal(state.manager.getEntries().some((entry) => entry.type === "context_edit"), false);
    assert.equal(state.manager.getEntries().some((entry) => entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_BEGIN_TYPE), false);
    assert.match(JSON.stringify(state.manager.buildSessionProjection().messages), /private-output-1/);
    assert.deepEqual(state.requests.at(-1).context.messages.slice(0, state.requests[1].context.messages.length), state.requests[1].context.messages);
  });
}

test("disabled policy makes no summary request and leaves the normal SDK policy alone", async (t) => {
  const state = await setup(t, { policy: { ...policy, enabled: false } });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.equal(state.session.autoCompactionEnabled, true);
  assert.equal(state.summaries.length, 0);
  assert.equal(state.events.length, 0);
  assert.match(JSON.stringify(state.manager.buildSessionProjection().messages), /private-output-1/);
});

test("oversized newest tool group stays whole beyond the soft tail target", async (t) => {
  const toolCall = { type: "toolCall", id: "call-1", name: TEST_TOOL.name, arguments: { command: "1" } };
  const content = [{ type: "thinking", thinking: "Verbatim retained reasoning.", thinkingSignature: "offline-signature" }, toolCall];
  const resultContent = [{ type: "text", text: "private-output-1 " + "tool evidence ".repeat(700) },
    { type: "image", data: "eA==", mimeType: "image/png" }];
  const state = await setup(t, { toolCount: 1,
    assistantContent: (index) => index === 1 ? content : [{ type: "text", text: "Finished." }],
    customTools: [{ ...TEST_TOOL, label: "Command", execute: async () => ({ content: resultContent, details: {} }) }],
  });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.equal(state.summaries.length, 0, "nothing can be removed without touching the latest tool group");
  const projected = state.manager.buildSessionProjection().messages;
  assert.match(JSON.stringify(projected), /private-output-1/);
  assert.ok(projected.reduce((sum, message) => sum + estimateTokens(message), 0) > policy.thresholdTokens);
  const captured = state.requests[1].context.messages;
  assert.deepEqual(captured.find((message) => message.role === "assistant").content, content);
  assert.deepEqual(captured.find((message) => message.role === "toolResult").content, resultContent);
  assert.equal(captured.find((message) => message.role === "toolResult").toolCallId, toolCall.id);
});

test("newest user correction remains verbatim after a long subsequent tool loop", async (t) => {
  let state;
  const correction = "Newest user constraint: preserve the exact identifier RELEASE_83 and do not publish.";
  state = await setup(t, { toolCount: 5, customTools: [{ ...TEST_TOOL, label: "Command", execute: async (_id, args) => {
    if (args.command === "2") await state.session.steer(correction);
    return { content: [{ type: "text", text: `private-output-${args.command} ` + "long evidence ".repeat(500) }], details: {} };
  } }] });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.ok(state.summaries.length >= 3);
  const introduced = state.requests.findIndex((request) => JSON.stringify(request.context.messages).includes(correction));
  assert.ok(introduced >= 0);
  const original = state.requests[introduced].context.messages.find((message) => message.role === "user" && JSON.stringify(message).includes(correction));
  for (const request of state.requests.slice(introduced)) {
    assert.deepEqual(request.context.messages.find((message) => message.role === "user" && JSON.stringify(message).includes(correction)), original);
  }
  assert.ok(state.manager.buildSessionProjection().messages.some((message) => message.role === "user" && JSON.stringify(message).includes(correction)));
});

test("pending asynchronous calls and queued late results pin their original call group", async (t) => {
  const state = await setup(t, { toolCount: 3, pendingCallIds: () => ["call-1"] });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.equal(state.summaries.length, 0);
  const projection = state.manager.buildSessionProjection();
  const late = { sourceEntry: { id: "late", type: "custom_message" }, messages: [{ role: "custom", ...lateToolMessage([
    { callId: "call-1", name: TEST_TOOL.name, content: [{ type: "text", text: "late result" }] },
  ]) }] };
  assert.equal(planContextCompaction({ contextEntries: [...projection.entries, late] }, policy), undefined);
  assert.equal(planContextCompaction({ contextEntries: projection.entries, pendingMessages: late.messages }, policy), undefined);
});

test("a running asynchronous tool completes during summary and is delivered after the commit", async (t) => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  let finishSlow;
  const slow = new Promise((resolve) => { finishSlow = resolve; });
  let releaseSummary;
  const summaryGate = new Promise((resolve) => { releaseSummary = resolve; });
  let summaryStarted = false;
  let state;
  state = await setup(t, {
    pendingCallIds: () => [...scheduler.jobs.values()].filter((job) => !job.delivered).map((job) => job.callId),
    customTools: [{ ...TEST_TOOL, label: "Command", executionMode: "parallel", execute: (id, args, signal) =>
      scheduler.execute(id, TEST_TOOL.name, async () => {
        if (args.command === "slow") await slow;
        return { content: [{ type: "text", text: `${args.command} finished ` + "tool evidence ".repeat(500) }], details: {} };
      }, signal) }],
    summarize: async (model) => {
      summaryStarted = true;
      await summaryGate;
      return response(model, [{ type: "text", text: "<bello_context_summary>Earlier command checked.</bello_context_summary>" }]);
    },
    asyncAfterTurn: async (turn) => {
      const results = await scheduler.ready({ wait: !turn.message.content.some((block) => block.type === "toolCall") });
      if (results.length) await state.session.sendCustomMessage(lateToolMessage(results), { deliverAs: "steer" });
    },
  });
  t.after(async () => { finishSlow(); releaseSummary(); await scheduler.cancel(); });
  state.session.subscribe((event) => {
    if (event.type === "message_end" && event.message.role === "assistant") scheduler.beginBatch(
      event.message.content.filter((block) => block.type === "toolCall").map((block) => block.id));
  });
  const requests = [];
  state.session.agent.streamFunction = (_model, context) => {
    requests.push(clone(context.messages));
    const commands = requests.length === 1 ? ["first"] : requests.length === 2 ? ["fast", "slow"] : [];
    return stream(response(state.model, commands.length ? commands.map((command) => ({
      type: "toolCall", id: command, name: TEST_TOOL.name, arguments: { command },
    })) : [{ type: "text", text: "Checked all output." }], commands.length ? "toolUse" : "stop",
    context.messages.reduce((sum, message) => sum + estimateTokens(message), 0)));
  };
  const running = state.session.prompt("Original task.", { expandPromptTemplates: false });
  await waitFor(() => summaryStarted);
  assert.equal(requests.length, 2, "regular model loop pauses at the summary boundary");
  finishSlow();
  await waitFor(() => scheduler.jobs.get("slow")?.done);
  assert.equal(scheduler.jobs.get("slow").controller.signal.aborted, false);
  assert.equal(requests.length, 2, "tool completion does not race a regular request past compaction");
  releaseSummary();
  await running;
  assert.match(JSON.stringify(requests.at(-1)), /slow finished/);
  assert.match(JSON.stringify(requests.at(-1)), /"id":"slow"/);
  assert.match(JSON.stringify(requests.at(-1)), /bello_context_summary/);
  assert.equal(scheduler.pending, false);
});

test("aborting while a summary ignores its signal never commits drafts or sends another provider request", async (t) => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let signal, attempts = 0;
  const state = await setup(t, { toolCount: 2, summarize: async (model, _context, options) => {
    if (++attempts === 1) return response(model, [{ type: "text", text: "" }], "stop", 100);
    signal = options.signal;
    await gate;
    return response(model, [{ type: "text", text: "Do not commit after cancellation." }]);
  } });
  t.after(release);
  const running = state.session.prompt("Original task.", { expandPromptTemplates: false });
  await waitFor(() => signal !== undefined);
  const aborted = state.session.abort();
  await waitFor(() => signal.aborted);
  release();
  await Promise.all([running, aborted]);
  await state.session.flushContextCompactionNotifications();
  assert.equal(state.requests.length, 2);
  assert.equal(attempts, 2, "no third provider request after cancellation");
  assert.equal(state.events.length, 1, "known usage of the finished attempt is still reported once");
  assert.equal(state.events[0].status, "interrupted");
  assert.equal(state.events[0].durable, false);
  assert.equal(state.events[0].usageIncomplete, true);
  assert.equal(state.events[0].usage.length, 1);
  assert.equal(state.events[0].usage[0].input, 100, "completed failed-attempt usage is retained once");
  assert.equal(state.manager.getEntries().some((entry) => entry.type === "context_edit"
    || entry.type === "custom" && [CONTEXT_COMPACTION_BEGIN_TYPE, CONTEXT_COMPACTION_STATE_TYPE].includes(entry.customType)), false);
  assert.ok(JSON.stringify(state.manager.buildSessionProjection().messages).includes("private-output-1"));
});

test("SDK exposes an awaited accounting drain and reports a stalled callback within five seconds", async (t) => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  t.after(release);
  const state = await setup(t, { toolCount: 2, onContextCompaction: () => gate });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  assert.equal(state.events.length, 1);
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const draining = state.session.flushContextCompactionNotifications();
  t.mock.timers.tick(5000);
  await assert.rejects(draining, /within 5 seconds/);
  release();
  await state.session.flushContextCompactionNotifications();
});

test("a failed notification is reported to the next drain once and does not poison later drains", async (t) => {
  let fail = true;
  const state = await setup(t, { toolCount: 2, onContextCompaction: () => { if (fail) throw new Error("synthetic accounting sink failure"); } });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  await assert.rejects(state.session.flushContextCompactionNotifications(), /synthetic accounting sink failure/);
  fail = false;
  await state.session.flushContextCompactionNotifications();
  await state.session.flushContextCompactionNotifications();
});

test("notifications are delivered in order, each exactly once, and the drain waits for all of them", async (t) => {
  const seen = [];
  const state = await setup(t, { toolCount: 5, onContextCompaction: async (event) => {
    await new Promise((resolve) => setImmediate(resolve));
    seen.push(event.tokensBefore);
  } });
  await state.session.prompt("Original neutral task.", { expandPromptTemplates: false });
  await state.session.flushContextCompactionNotifications();
  assert.ok(state.events.length >= 2);
  assert.deepEqual(seen, state.events.map((event) => event.tokensBefore));
  assert.equal(new Set(state.events).size, state.events.length);
});

test("enabled policy below threshold does not summarize", async (t) => {
  const state = await setup(t, { toolCount: 0 });
  await state.session.prompt("Small task.", { expandPromptTemplates: false });
  assert.equal(state.summaries.length, 0);
  assert.equal(state.events.length, 0);
});

test("older native image evidence reaches the summarizer as images rather than base64 prose", async (t) => {
  const image = { type: "image", data: "synthetic-base64-image-evidence", mimeType: "image/png" };
  const state = await setup(t, { toolCount: 3, customTools: [{ ...TEST_TOOL, label: "Command",
    execute: async (_id, args) => ({ content: [
      { type: "text", text: `Evidence ${args.command}: ` + "large evidence ".repeat(500) },
      ...(args.command === "1" ? [image] : []),
    ], details: {} }),
  }] });
  await state.session.prompt("Original task with image evidence.", { expandPromptTemplates: false });
  const content = state.summaries.find(([, context]) => context.messages[0].content.some((block) => block.type === "image"))?.[1].messages[0].content;
  assert.ok(content, "the older image must actually be included in a summary request");
  assert.deepEqual(content.find((block) => block.type === "image"), image);
  const serializedText = content.filter((block) => block.type === "text").map((block) => block.text).join("\n");
  assert.match(serializedText, /historical-image-1/);
  assert.ok(!serializedText.includes(image.data), "base64 bytes are never inserted into historical prose");
  assert.ok(state.events.every((event) => event.status === "completed"));
});

test("an incompatible configured threshold fails explicitly instead of being lowered", async (t) => {
  await assert.rejects(setup(t, {
    model: { contextWindow: 16000, maxTokens: 2000 }, policy: { enabled: true },
  }), /Configured context threshold 249000 exceeds the model input capacity 9904/);
  assert.throws(() => contextBudget({ ...policy, thresholdTokens: 9905 }, { contextWindow: 16000, maxTokens: 2000 }),
    /Configured context threshold 9905 exceeds/);
});

test("the configured threshold is unchanged and sets the soft recent-tail target", async (t) => {
  const state = await setup(t, {
    model: { contextWindow: 16000, maxTokens: 2000 }, policy: { enabled: true, thresholdTokens: 9904, retainPercent: 10 }, toolCount: 4,
    customTools: [{ ...TEST_TOOL, label: "Command", execute: async (_id, args) => ({
      content: [{ type: "text", text: `private-output-${args.command} ` + "tool evidence ".repeat(2000) }], details: {},
    }) }],
  });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.ok(state.summaries.length >= 2);
  assert.ok(state.events.every((event) => event.status === "completed"));
  assert.ok(state.events.every((event) => event.requestedThresholdTokens === 9904 && event.effectiveThresholdTokens === 9904));
  assert.ok(state.events.every((event) => event.retainedTokens < 9904 && event.retainedTokens > 990.4), "one whole oversized group exceeds the soft target");
});

test("a 100000-token threshold and a 15000-token tail are exactly representable by the percentage setting", () => {
  const selected = normalizeContextPolicy({ enabled: true, thresholdTokens: 100000, retainPercent: 15 });
  const budget = contextBudget(selected, { contextWindow: 1000000, maxTokens: 16384 });
  assert.equal(budget.effectiveThresholdTokens, 100000);
  assert.equal(selected.thresholdTokens * selected.retainPercent / 100, 15000);
});

test("the input budget leaves exactly the output reserve plus the 4096-token safety margin", () => {
  const selected = { enabled: true, thresholdTokens: 1000, retainPercent: 10 };
  for (const [contextWindow, maxTokens] of [[32768, 4096], [16000, 2000], [1000000, 16384], [200000, 64000]]) {
    const budget = contextBudget(selected, { contextWindow, maxTokens });
    assert.equal(budget.inputLimitTokens, contextWindow - maxTokens - 4096);
    assert.equal(budget.inputLimitTokens + budget.outputReserveTokens + 4096, contextWindow);
  }
  // A model without an advertised output cap reserves Pi's 16384-token default.
  assert.equal(contextBudget(selected, { contextWindow: 65536 }).inputLimitTokens, 65536 - 16384 - 4096);
  assert.throws(() => contextBudget(selected, { contextWindow: 20480, maxTokens: 16384 }), /known model context window/);
});

test("a 32K-window model keeps a 20K trigger and soft 5K tail through many tool steps", async (t) => {
  const selected = normalizeContextPolicy({ enabled: true, thresholdTokens: 20000, retainPercent: 25 });
  assert.equal(selected.thresholdTokens * selected.retainPercent / 100, 5000);
  const budget = contextBudget(selected, { contextWindow: 32768, maxTokens: 4096 });
  assert.equal(budget.effectiveThresholdTokens, 20000);
  assert.equal(budget.inputLimitTokens, 24576);
  assert.throws(() => contextBudget(selected, { contextWindow: 32768, maxTokens: 16384 }), /exceeds/);
  const state = await setup(t, {
    model: { contextWindow: 32768, maxTokens: 4096 }, policy: selected, toolCount: 24,
    customTools: [{ ...TEST_TOOL, label: "Neutral reading", execute: async (_id, args) => ({
      content: [{ type: "text", text: `private-output-${args.command} ` + "neutral evidence ".repeat(700) }], details: {},
    }) }],
  });
  await state.session.prompt("Read the neutral records and preserve the original goal.", { expandPromptTemplates: false });
  assert.equal(state.requests.length, 25);
  assert.ok(state.events.length >= 3, "at least three checkpoints and continued tool execution");
  assert.ok(state.events.every((event) => event.status === "completed"));
  assert.ok(state.events.every((event) => event.effectiveThresholdTokens === 20000));
  assert.ok(state.events.every((event) => event.retainedTokens >= 5000 && event.retainedTokens < 10000),
    "retain complete tool groups around, not exactly equal to, the 5K target");
  assert.ok(state.summaries.every(([, , options]) => options.maxTokens === 4096));
  assert.ok(state.requests.at(-1).compactions >= 3, "actual later provider request sees committed checkpoints");
  assert.ok(state.requests.every(({ context }) => context.messages.reduce((sum, message) => sum + estimateTokens(message), 0) < budget.inputLimitTokens));
});

test("a 32K input guard includes Pi's safety reserve and never admits a starved output budget", () => {
  const model = { contextWindow: 32768, maxTokens: 4096 };
  const budget = contextBudget({ enabled: true, thresholdTokens: 20000, retainPercent: 25 }, model);
  const safe = [{ role: "user", content: "x".repeat(98000) }];
  assert.doesNotThrow(() => assertContextFits(safe, budget));
  assert.equal(clampMaxTokensToContext(model, { messages: safe, systemPrompt: "", tools: [] }, 4096), 4096);
  const starved = [{ role: "user", content: "x".repeat(114680) }];
  assert.ok(clampMaxTokensToContext(model, { messages: starved, systemPrompt: "", tools: [] }, 4096) <= 2);
  assert.throws(() => assertContextFits(starved, budget), /exceeds the model input budget/);
});

test("middle cwd and tool-loadout entries are read-only islands and excluded from removed-token accounting", () => {
  const messages = [
    { role: "user", content: "Original task." },
    { role: "system", content: "", sections: { cwd: "/neutral/resumed" }, timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "Earlier work ".repeat(300) },
      { type: "toolCall", id: "older-call", name: "neutral_check", arguments: {} }] },
    { role: "toolResult", toolCallId: "older-call", toolName: "neutral_check",
      content: [{ type: "text", text: "Verified older evidence ".repeat(300) }], isError: false, timestamp: 2 },
    { role: "system", content: "", toolsAdded: [{ name: "neutral_check", description: "Read neutral data",
      parameters: { type: "object", properties: {} } }], timestamp: 3 },
    { role: "assistant", content: [{ type: "text", text: "Recent evidence ".repeat(100) }] },
  ];
  const entries = messages.map((message, index) => ({ sourceEntry: { id: `entry-${index}`, type: "message" }, messages: [message] }));
  const plan = planContextCompaction({ contextEntries: entries }, policy);
  assert.equal(plan.anchorId, "entry-2", "a system island before the first editable entry is not an invalid anchor");
  assert.deepEqual(plan.middle.map((entry) => entry.sourceEntry.id), ["entry-2", "entry-3"]);
  assert.equal(plan.middleTokens, estimateTokens(messages[2]) + estimateTokens(messages[3]));
  assert.deepEqual(plan.messages.filter((message) => message.role === "system"), [messages[1], messages[4]],
    "historical cwd and tool-loadout semantics remain available to the summarizer as read-only data");
  assert.equal(plan.retainedTokens, estimateTokens(messages[5]));
});

test("a system island between editable entries is never edited and keeps its position", async () => {
  const messages = [
    { role: "user", content: "Original neutral task." },
    { role: "assistant", content: [{ type: "text", text: "First earlier work. ".repeat(300) }] },
    { role: "system", content: "", sections: { cwd: "/neutral/other" }, timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "Second earlier work. ".repeat(300) }] },
    { role: "system", content: "", toolsRemoved: ["neutral_check"], timestamp: 2 },
    { role: "assistant", content: [{ type: "text", text: "Third earlier work. ".repeat(300) }] },
    { role: "assistant", content: [{ type: "text", text: "Recent neutral evidence. ".repeat(100) }] },
  ];
  const prompts = [];
  const result = await directSummaryBoundary(async (_model, context) => {
    prompts.push(context.messages[0].content[0].text);
    return { stopReason: "stop", content: [{ type: "text", text: "Compact neutral summary." }] };
  }, undefined, { messages });
  const drafts = result.entries.filter((entry) => entry.type === "context_edit");
  assert.deepEqual(drafts.map((entry) => entry.targetId), ["1", "3", "5"], "only editable middle entries are edited");
  assert.equal(drafts[0].replacement.content[0].text.includes("Compact neutral summary."), true);
  assert.deepEqual(drafts.slice(1).map((entry) => entry.replacement), [null, null]);
  assert.match(prompts[0], /\/neutral\/other/, "the summarizer still reads the historical cwd island");
  assert.match(prompts[0], /neutral_check/);
});

const stateEntries = (manager) => manager.getBranch().filter((entry) => entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_STATE_TYPE);
const sessionTokens = (messages) => messages.reduce((sum, message) => sum + estimateTokens(message), 0);
// Drive a session for `steps` tool calls (or until `until()` holds) and then one text answer.
function scriptedSteps(session, model, label, steps, until) {
  let index = 0;
  session.agent.streamFunction = (_model, context) => {
    index++;
    const call = index <= steps && !until?.();
    return stream(response(model, call
      ? [{ type: "toolCall", id: `${label}-call-${index}`, name: TEST_TOOL.name, arguments: { command: `${label}-${index}` } }]
      : [{ type: "text", text: `${label} segment done.` }], call ? "toolUse" : "stop", sessionTokens(context.messages)));
  };
}

// A persisted session plus a `resume()` that reopens its journal exactly as a worker restart does.
async function resumeFixture(t, { policyOverride, summarize, toolOutput }) {
  const tool = { ...TEST_TOOL, label: "Neutral reading", execute: async (_id, args) => ({
    content: [{ type: "text", text: toolOutput(args.command) }], details: {},
  }) };
  const initial = await setup(t, { persist: true, model: { contextWindow: 32768, maxTokens: 4096 },
    policy: policyOverride, customTools: [tool], summarize, toolCount: 0 });
  const resume = async ({ label = "resumed", cwd = initial.workspace, steps, activeToolNames = [TEST_TOOL.name], extraTools = [] }) => {
    const manager = SessionManager.open(initial.manager.getSessionFile(), initial.stateDir, cwd);
    const events = [], requests = [];
    const session = await realPiSdk.createSession({ cwd, agentDir: initial.agentDir,
      modelRuntime: initial.modelRuntime, model: initial.model, thinkingLevel: "high", sessionManager: manager,
      customTools: [tool, ...extraTools], activeToolNames, systemPrompt: "Immutable role instructions.",
      noNeedForContext: policyOverride, requestOptions: { current: {} }, onContextCompaction: (data) => events.push(data) });
    t.after(() => session.dispose());
    session.agent.getApiKey = async () => "offline-test-key";
    session.agent.streamFunction = (_model, context) => {
      requests.push({ context: clone(context), checkpoints: events.filter((event) => event.status === "completed").length });
      const index = requests.length;
      return stream(response(initial.model, index <= steps
        ? [{ type: "toolCall", id: `${label}-call-${index}`, name: TEST_TOOL.name, arguments: { command: `${label}-${index}` } }]
        : [{ type: "text", text: `${label} neutral work complete.` }], index <= steps ? "toolUse" : "stop",
      context.messages.reduce((sum, message) => sum + estimateTokens(message), 0)));
    };
    return { manager, session, events, requests };
  };
  return { initial, tool, resume };
}

test("disk resume preserves a new immutable system entry and continues repeated NNC checkpoints", async (t) => {
  const selected = { enabled: true, thresholdTokens: 20000, retainPercent: 25 };
  const { initial, resume } = await resumeFixture(t, { policyOverride: selected,
    toolOutput: (id) => `private-output-${id} ` + "neutral evidence ".repeat(700) });
  scriptedSteps(initial.session, initial.model, "first", 10);
  await initial.session.prompt("Original task must remain unchanged across resume.", { expandPromptTemplates: false });
  await initial.session.flushContextCompactionNotifications();
  assert.ok(initial.events.some((event) => event.status === "completed"));
  const sessionFile = initial.manager.getSessionFile();
  const journalBefore = readFileSync(sessionFile);
  const originalPrefix = clone(initial.manager.buildSessionProjection().messages.slice(0, 2));
  initial.session.dispose();
  const resumedCwd = join(initial.root, "resumed-workspace");
  mkdirSync(resumedCwd);
  const { manager, session: resumed, events, requests } = await resume({ cwd: resumedCwd, steps: 24 });
  const correction = "Continue the same task; preserve this latest correction verbatim.";
  await resumed.prompt(correction, { expandPromptTemplates: false });
  await resumed.flushContextCompactionNotifications();
  assert.equal(requests.length, 25);
  assert.ok(events.filter((event) => event.status === "completed").length >= 3);
  assert.ok(requests.at(-1).checkpoints >= 3, "later real SDK requests use post-resume checkpoints");
  const raw = manager.getEntries();
  const systemEntries = raw.filter((entry) => entry.type === "message" && entry.message.role === "system");
  assert.ok(systemEntries.length >= 2, "changing resume cwd creates the real SDK immutable middle system entry");
  assert.ok(systemEntries.some((entry) => JSON.stringify(entry.message).includes(resumedCwd)));
  const immutableIds = new Set(systemEntries.map((entry) => entry.id));
  assert.equal(raw.some((entry) => entry.type === "context_edit" && immutableIds.has(entry.targetId)), false);
  const projected = manager.buildSessionProjection().messages;
  assert.deepEqual(projected.filter((message) => message.role === "system"), systemEntries.map((entry) => entry.message),
    "all system/loadout entries keep their exact content and relative order");
  assert.deepEqual(projected.slice(0, originalPrefix.length), originalPrefix);
  assert.ok(projected.some((message) => message.role === "user" && JSON.stringify(message).includes(correction)));
  assert.deepEqual(readFileSync(sessionFile).subarray(0, journalBefore.length), journalBefore, "old journal bytes remain append-only");
  for (const { context } of requests) {
    const ids = new Set(context.messages.flatMap((message) => message.role === "assistant"
      ? message.content.filter((block) => block.type === "toolCall").map((block) => block.id) : []));
    for (const message of context.messages) if (message.role === "toolResult") assert.ok(ids.has(message.toolCallId));
  }
});

test("the checkpoint retry threshold persists across a disk resume", async (t) => {
  const selected = { enabled: true, thresholdTokens: 5000, retainPercent: 10 };
  const { initial, resume } = await resumeFixture(t, { policyOverride: selected, toolOutput: () => "e".repeat(200),
    summarize: () => { throw new Error("offline summary provider failure"); } });
  scriptedSteps(initial.session, initial.model, "first", 500, () => stateEntries(initial.manager).length > 0);
  await initial.session.prompt("Original task.", { expandPromptTemplates: false });
  await initial.session.flushContextCompactionNotifications();
  const last = initial.events.at(-1);
  assert.equal(initial.events.length, 1);
  assert.equal(last.status, "failed");
  assert.ok(last.nextCheckTokens >= last.tokensBefore + selected.thresholdTokens / 10, "a failed attempt waits for 10% more context");
  initial.session.dispose();

  const resumed = await resume({ steps: 80 });
  await resumed.session.prompt("Continue the original task.", { expandPromptTemplates: false });
  await resumed.session.flushContextCompactionNotifications();
  assert.ok(resumed.events.length >= 2, "retries continue after the restart");
  let threshold = last.nextCheckTokens;
  for (const event of resumed.events) {
    assert.ok(event.tokensBefore >= threshold, `retry at ${event.tokensBefore} honours the persisted ${threshold}`);
    threshold = event.nextCheckTokens;
  }
});

test("a resumed context that was already compacted does not compact again below the threshold", async (t) => {
  const selected = { enabled: true, thresholdTokens: 5000, retainPercent: 10 };
  const { initial, resume } = await resumeFixture(t, { policyOverride: selected, toolOutput: () => "e".repeat(200) });
  scriptedSteps(initial.session, initial.model, "first", 500, () => stateEntries(initial.manager).length > 0);
  await initial.session.prompt("Original task.", { expandPromptTemplates: false });
  await initial.session.flushContextCompactionNotifications();
  assert.deepEqual(initial.events.map((event) => event.status), ["completed"]);
  const summariesBefore = initial.summaries.length;
  const checkpoint = clone(initial.manager.buildSessionProjection().messages);
  initial.session.dispose();

  const resumed = await resume({ steps: 4 });
  await resumed.session.prompt("Continue the original task.", { expandPromptTemplates: false });
  await resumed.session.flushContextCompactionNotifications();
  assert.equal(initial.summaries.length, summariesBefore, "no summary request after a restart below the threshold");
  assert.deepEqual(resumed.events, []);
  assert.equal(stateEntries(resumed.manager).length, 1);
  assert.deepEqual(clone(resumed.manager.buildSessionProjection().messages).slice(0, checkpoint.length), checkpoint,
    "the committed checkpoint projection is reproduced unchanged after the restart");
  assert.match(JSON.stringify(checkpoint), /bello_context_summary/);
});

test("closing and reopening the persisted session any number of times does not change when it compacts", async (t) => {
  const selected = { enabled: true, thresholdTokens: 1500, retainPercent: 20 };
  const run = async (reopenAt) => {
    const { initial, resume } = await resumeFixture(t, { policyOverride: selected, toolOutput: (id) => `out-${id} ` + "e".repeat(400) });
    let session = initial.session, manager = initial.manager, completed = 0, previousEnd = 0;
    for (const [segment, end] of [...reopenAt, 120].entries()) {
      const label = `segment${segment}`;
      scriptedSteps(session, initial.model, label, end - previousEnd);
      await session.prompt(segment === 0 ? "Original task." : "Continue the original task.", { expandPromptTemplates: false });
      await session.flushContextCompactionNotifications();
      previousEnd = end;
      if (end !== 120) {
        session.dispose();
        ({ session, manager } = await resume({ label: `reopened${segment}`, steps: 0 }));
      }
    }
    completed = stateEntries(manager).filter((entry) => entry.data.status === "completed").length;
    return { completed, tokens: sessionTokens(manager.buildSessionProjection().messages), entries: stateEntries(manager).length };
  };
  const uninterrupted = await run([]);
  assert.ok(uninterrupted.completed >= 8, "the scenario crosses the threshold repeatedly");
  for (const reopenAt of [[60], [30, 60, 90], [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110]]) {
    const resumed = await run(reopenAt);
    assert.equal(resumed.completed, uninterrupted.completed, `reopen at ${reopenAt}`);
    assert.equal(resumed.entries, uninterrupted.entries, `reopen at ${reopenAt}`);
    assert.ok(Math.abs(resumed.tokens - uninterrupted.tokens) < 100, `reopen at ${reopenAt}`);
  }
});

test("a checkpoint interrupted by a crash is rolled back on resume and later checkpoints still work", async (t) => {
  const selected = { enabled: true, thresholdTokens: 3000, retainPercent: 10 };
  const { initial, resume } = await resumeFixture(t, { policyOverride: selected, toolOutput: () => "e".repeat(200) });
  scriptedSteps(initial.session, initial.model, "first", 10);
  await initial.session.prompt("Original task.", { expandPromptTemplates: false });
  const baseline = clone(initial.manager.buildSessionProjection().messages);
  const target = initial.manager.getBranch().find((entry) => entry.type === "message" && entry.message.role === "assistant");
  // The process dies after a begin marker and one edit, before the commit.
  initial.manager.appendCustomEntry(CONTEXT_COMPACTION_BEGIN_TYPE, { transactionId: "synthetic-crash", status: "completed",
    tokensBefore: 3200, effectiveThresholdTokens: 3000, attempts: 1, usage: [] });
  initial.manager.appendContextEdit(target.id, { content: [{ type: "text", text: "incomplete replacement" }] });
  initial.session.dispose();

  const resumed = await resume({ steps: 120 });
  assert.deepEqual(clone(resumed.manager.buildSessionProjection().messages), baseline, "the half-written checkpoint is invisible");
  await resumed.session.prompt("Continue the original task.", { expandPromptTemplates: false });
  await resumed.session.flushContextCompactionNotifications();
  const recovered = resumed.manager.getBranch().filter((entry) => entry.type === "custom" && entry.customType === CONTEXT_COMPACTION_STATE_TYPE
    && entry.data.transactionId === "synthetic-crash");
  assert.equal(recovered.length, 1, "recovery is recorded once on the live branch");
  assert.equal(recovered[0].data.checkpointRolledBack, true);
  assert.equal(JSON.stringify(resumed.manager.buildSessionProjection().messages).includes("incomplete replacement"), false);
  assert.ok(resumed.events.filter((event) => event.status === "completed").length >= 2, "normal checkpoints continue after recovery");
  assert.ok(resumed.events[0].tokensBefore >= recovered[0].data.nextCheckTokens, "the recovery marker also paces the first retry");
  // A second restart finds nothing left to recover.
  resumed.session.dispose();
  const reopened = SessionManager.open(resumed.manager.getSessionFile());
  const entriesBefore = reopened.getEntries().length;
  installContextTransactionGuard(reopened);
  assert.equal(reopened.getEntries().length, entriesBefore);
});

test("a changed tool loadout between turns does not stop later checkpoints", async (t) => {
  const selected = { enabled: true, thresholdTokens: 6000, retainPercent: 20 };
  const extra = { ...TEST_TOOL, name: "extra_check", label: "Extra", execute: async () => ({ content: [{ type: "text", text: "extra" }], details: {} }) };
  const { initial, resume } = await resumeFixture(t, { policyOverride: selected, toolOutput: (id) => `private-output-${id} ` + "neutral evidence ".repeat(120) });
  await initial.session.prompt("Original task.", { expandPromptTemplates: false });
  initial.session.dispose();
  const resumed = await resume({ steps: 30, activeToolNames: [TEST_TOOL.name, "extra_check"], extraTools: [extra] });
  await resumed.session.prompt("Continue the original task.", { expandPromptTemplates: false });
  await resumed.session.flushContextCompactionNotifications();
  const loadoutEntries = resumed.manager.getEntries().filter((entry) => entry.type === "message" && entry.message.role === "system"
    && JSON.stringify(entry.message).includes("extra_check"));
  assert.ok(loadoutEntries.length >= 1, "adding a tool after resume appends an immutable loadout entry");
  assert.ok(resumed.events.filter((event) => event.status === "completed").length >= 2, "checkpoints continue across the loadout island");
  const ids = new Set(loadoutEntries.map((entry) => entry.id));
  assert.equal(resumed.manager.getEntries().some((entry) => entry.type === "context_edit" && ids.has(entry.targetId)), false);
});

test("an impossible protected prefix stops before a provider request without cutting history", async (t) => {
  const state = await setup(t, { model: { contextWindow: 16000, maxTokens: 2000 }, policy, toolCount: 0 });
  const prompt = "Mandatory original requirement. ".repeat(3000);
  await state.session.prompt(prompt, { expandPromptTemplates: false });
  assert.equal(state.requests.length, 0);
  assert.equal(state.summaries.length, 0);
  assert.match(state.session.agent.state.messages.at(-1).errorMessage, /exceeds the model input budget/);
  assert.ok(state.manager.buildSessionProjection().messages.some((message) => message.role === "user" && JSON.stringify(message).includes(prompt)));
});

for (const source of ["system", "tools"]) {
  test(`large immutable ${source} overhead is included in the hard provider input budget`, async (t) => {
    const huge = "Immutable constraint or schema. ".repeat(3000);
    const state = await setup(t, { model: { contextWindow: 16000, maxTokens: 2000 },
      policy, toolCount: 0,
      ...(source === "system" ? { systemPrompt: huge } : { customTools: [{ ...TEST_TOOL,
        label: "Command", description: huge,
        execute: async () => ({ content: [{ type: "text", text: "unused" }], details: {} }),
      }] }),
    });
    await state.session.prompt("Short original task.", { expandPromptTemplates: false });
    assert.equal(state.requests.length, 0);
    assert.equal(state.summaries.length, 0);
    assert.match(state.session.agent.state.messages.at(-1).errorMessage, /exceeds the model input budget/);
    assert.ok(state.manager.buildSessionProjection().messages.some((message) => message.role === "user"));
    if (source === "system") assert.ok(state.session.systemPrompt.includes(huge));
    else assert.equal(state.session.agent.state.tools[0].description, huge);
  });
}

test("an indivisible result over the hard window is preserved and no overflowing request is sent", async (t) => {
  const resultText = "One indivisible result. ".repeat(4000);
  const state = await setup(t, {
    model: { contextWindow: 16000, maxTokens: 2000 }, policy, toolCount: 1,
    customTools: [{ ...TEST_TOOL, label: "Command", execute: async () => ({ content: [{ type: "text", text: resultText }], details: {} }) }],
  });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.equal(state.requests.length, 1);
  assert.equal(state.summaries.length, 0);
  assert.match(state.session.agent.state.messages.at(-1).errorMessage, /exceeds the model input budget/);
  const projection = state.manager.buildSessionProjection().messages;
  assert.deepEqual(projection.find((message) => message.role === "toolResult").content, [{ type: "text", text: resultText }]);
  assert.equal(projection.find((message) => message.role === "toolResult").toolCallId, "call-1");
  assert.ok(projection.find((message) => message.role === "assistant").content.some((block) => block.type === "toolCall" && block.id === "call-1"));
});

test("an impossible protected tail does not commit a partial compaction of the older middle", async (t) => {
  const resultText = "One indivisible result. ".repeat(4000);
  const state = await setup(t, {
    model: { contextWindow: 16000, maxTokens: 2000 }, toolCount: 2,
    customTools: [{ ...TEST_TOOL, label: "Command", execute: async (_id, args) => ({
      content: [{ type: "text", text: args.command === "1" ? "Older evidence. ".repeat(400) : resultText }], details: {},
    }) }],
  });
  await state.session.prompt("Original task.", { expandPromptTemplates: false });
  assert.equal(state.requests.length, 2);
  assert.equal(state.summaries.length, 0);
  assert.equal(state.manager.getEntries().some((entry) => entry.type === "context_edit"), false);
  assert.match(state.session.agent.state.messages.at(-1).errorMessage, /exceeds the model input budget/);
  const projection = state.manager.buildSessionProjection().messages;
  assert.ok(projection.some((message) => message.role === "toolResult" && message.toolCallId === "call-1"));
  assert.deepEqual(projection.find((message) => message.role === "toolResult" && message.toolCallId === "call-2").content,
    [{ type: "text", text: resultText }]);
});

test("worker persists context policy, forwards it and rejects resume drift before execution", async (t) => {
  const layout = temporaryLayout(t);
  const sdk = createFakeSdk();
  const runtime = new PiWorkerRuntime({ sdk, emit: async () => {}, hostCalls: { call: async () => ({}) } });
  t.after(() => runtime.close());
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
  const params = { threadId: "policy-thread", cwd: layout.workspace, provider: "openai-codex", model: "gpt-test", tools: [], noNeedForContext: policy };
  await runtime.dispatch("thread/start", params);
  assert.deepEqual(sdk.created[0].options.noNeedForContext, policy);
  assert.deepEqual(runtime.getRecord("policy-thread").meta.noNeedForContext, policy);
  const record = runtime.getRecord("policy-thread");
  record.active = { turn: { id: "summary-accounting" }, usageResponses: [] };
  await sdk.created[0].options.onContextCompaction({ status: "failed", model: "gpt-test", provider: "openai-codex",
    usage: [{ input: 10, output: 3, totalTokens: 13 }, { input: 11, output: 4, totalTokens: 15 }] });
  assert.equal(record.active.turn.usage.totalTokens, 28, "failed summary attempts are billed too");
  record.active = undefined;
  await assert.rejects(runtime.dispatch("thread/resume", { threadId: "policy-thread", noNeedForContext: { ...policy, retainPercent: 20 } }), /cannot change noNeedForContext/);
  await assert.rejects(runtime.dispatch("thread/start", { ...params, threadId: "bad", noNeedForContext: { ...policy, retainPercent: 51 } }), /retainPercent/);
  assert.equal(runtime.threads.has("bad"), false);
  assert.deepEqual(normalizeContextPolicy(undefined), { enabled: false, thresholdTokens: 249000, retainPercent: 10 });
});
