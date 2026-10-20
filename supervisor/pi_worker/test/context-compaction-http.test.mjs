import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { isDeepStrictEqual } from "node:util";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";
import { realPiSdk } from "../src/pi-sdk.mjs";
import { AsyncToolCoordinator, lateToolMessage } from "../src/async-tools.mjs";
import { temporaryLayout } from "./helpers.mjs";

const MODEL = { id: "offline-context", name: "Offline context", provider: "openrouter", api: "openai-completions",
  reasoning: true, input: ["text"], cost: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32768, maxTokens: 1024 };
const call = (id, index = 0) => ({ index, id, type: "function", function: { name: "offline_check", arguments: JSON.stringify({ id }) } });
const evidence = (id) => `PRIVATE_HTTP_EVIDENCE_${id} ` + "long checked evidence ".repeat(350);

function sse(content, calls, input, stop = "stop") {
  const base = { id: "offline-response", object: "chat.completion.chunk", model: MODEL.id, created: 1 };
  const chunk = { ...base, choices: [{ index: 0, delta: { role: "assistant", ...(calls ? { tool_calls: calls } : { content }) }, finish_reason: null }] };
  const finish = { ...base, choices: [{ index: 0, delta: {}, finish_reason: calls ? "tool_calls" : stop }],
    usage: { prompt_tokens: input, completion_tokens: 7, total_tokens: input + 7 } };
  return `data: ${JSON.stringify(chunk)}\n\ndata: ${JSON.stringify(finish)}\n\ndata: [DONE]\n\n`;
}

for (const mode of ["on", "off", "empty", "truncated", "refused", "pending"]) {
  test(`real Pi localhost HTTP context ${mode}: serialized provider requests and tool execution`, async (t) => {
    const layout = temporaryLayout(t);
    const runtime = await realPiSdk.createModelRuntime({ agentDir: layout.agentDir, allowModelNetwork: false });
    runtime.hasConfiguredAuth = () => true;
    const regular = [], summaries = [], events = [], executed = [];
    const scheduler = new AsyncToolCoordinator({ graceMs: 5 });
    let session, slowDone = false, summaryWhilePending = false, ordinaryPaused = false;
    let finishSlow;
    const slowFinished = new Promise((resolve) => { finishSlow = resolve; });
    const failed = ["empty", "truncated", "refused"].includes(mode);
    const rounds = failed || mode === "pending" ? 2 : 4;
    const server = createServer(async (request, response) => {
      try {
        assert.equal(request.url, "/v1/chat/completions");
        assert.equal(request.headers.authorization, "Bearer offline-context-fixture");
        let data = "";
        for await (const chunk of request) data += chunk;
        const body = JSON.parse(data);
        const input = Math.ceil(data.length / 4);
        let content, calls, stop = "stop";
        if (!body.tools?.length) {
          summaries.push({ body, input });
          if (mode === "pending" && !slowDone) {
            summaryWhilePending = true;
            const before = regular.length;
            await slowFinished;
            ordinaryPaused = regular.length === before;
          }
          // Ordinary plain text: Bello adds the durable marker itself.
          content = mode === "empty" ? "" : mode === "refused" ? "I cannot summarize this."
            : `HTTP_SUMMARY_${summaries.length} Earlier evidence checked; preserve original constraints.`;
          if (mode === "truncated") stop = "length";
        } else {
          regular.push({ body, compactions: events.filter((event) => event.status === "completed").length });
          if (regular.length <= rounds) {
            calls = [call(`round-${regular.length}`)];
            if (mode === "pending" && regular.length === 2) calls.push(call("slow", 1));
          } else content = "Fixture complete.";
        }
        response.writeHead(200, { "content-type": "text/event-stream" });
        response.end(sse(content, calls, input, stop));
      } catch {
        response.writeHead(500); response.end("Offline fixture failed");
      }
    });
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
    const baseUrl = `http://127.0.0.1:${server.address().port}/v1`;
    const nativeFetch = globalThis.fetch;
    globalThis.fetch = (url, options) => {
      assert.equal(String(url), `${baseUrl}/chat/completions`, "no external provider transport is permitted");
      return nativeFetch(url, options);
    };
    t.after(() => { globalThis.fetch = nativeFetch; });
    runtime.streamSimple = (model, context, options) => streamSimple(model, context, { ...options, apiKey: "offline-context-fixture" });
    runtime.completeSimple = async (model, context, options) => runtime.streamSimple(model, context, options).result();
    const model = { ...MODEL, baseUrl };
    const manager = SessionManager.inMemory(layout.workspace);
    const execute = async (id, signal) => {
      executed.push(id);
      if (id === "slow") {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        assert.equal(signal.aborted, false, "summary must not cancel the long-running tool");
        slowDone = true; finishSlow();
      }
      return { content: [{ type: "text", text: id === "slow" ? "SLOW_HTTP_RESULT_COMPLETE" : evidence(id) }], details: {} };
    };
    session = await realPiSdk.createSession({ cwd: layout.workspace, agentDir: layout.agentDir, modelRuntime: runtime, model,
      thinkingLevel: "high", sessionManager: manager, systemPrompt: "Immutable HTTP fixture role instructions.",
      requestOptions: { current: { nativeEffort: "high", serviceTier: "priority" } },
      noNeedForContext: { enabled: mode !== "off", thresholdTokens: 1200, retainPercent: 10 },
      onContextCompaction: (event) => events.push(event),
      pendingCallIds: () => [...scheduler.jobs.values()].filter((job) => !job.delivered).map((job) => job.callId),
      customTools: [{ name: "offline_check", label: "Offline check", description: "Local deterministic evidence", executionMode: "parallel",
        parameters: { type: "object", properties: { id: { type: "string" } }, required: ["id"] },
        execute: (callId, args, signal) => mode === "pending"
          ? scheduler.execute(callId, "offline_check", () => execute(args.id, signal), signal) : execute(args.id, signal),
      }], activeToolNames: ["offline_check"],
      asyncAfterTurn: mode === "pending" ? async (turn) => {
        const ready = await scheduler.ready({ wait: !turn.message.content.some((block) => block.type === "toolCall") });
        if (ready.length) await session.sendCustomMessage(lateToolMessage(ready), { deliverAs: "steer" });
      } : undefined,
    });
    t.after(async () => { finishSlow(); await scheduler.cancel(); session.dispose(); });
    session.agent.getApiKey = async () => "offline-context-fixture";
    session.subscribe((event) => {
      if (mode === "pending" && event.type === "message_end" && event.message.role === "assistant") {
        scheduler.beginBatch(event.message.content.filter((block) => block.type === "toolCall").map((block) => block.id));
      }
    });
    await session.prompt("Immutable original HTTP task.", { expandPromptTemplates: false });
    assert.equal(executed.length, rounds + (mode === "pending" ? 1 : 0));
    assert.equal(new Set(executed).size, executed.length);
    assert.ok(regular.length >= rounds + 1);
    for (const { body } of regular) {
      assert.ok(isDeepStrictEqual(body.messages.slice(0, regular[0].body.messages.length), regular[0].body.messages), "wire prefix unchanged");
      const ids = new Set(body.messages.flatMap((message) => (message.tool_calls ?? []).map((item) => item.id)));
      for (const message of body.messages) if (message.role === "tool") assert.ok(ids.has(message.tool_call_id));
    }
    for (let index = 1; index < regular.length; index++) if (regular[index].compactions === regular[index - 1].compactions) {
      assert.ok(isDeepStrictEqual(regular[index].body.messages.slice(0, regular[index - 1].body.messages.length), regular[index - 1].body.messages), "wire append-only between checkpoints");
    }
    for (const { body } of summaries) {
      assert.equal(body.model, MODEL.id); assert.equal(body.tools?.length ?? 0, 0);
      assert.equal(body.reasoning.effort, "high"); assert.equal(body.service_tier, "priority");
    }
    assert.equal(events.flatMap((event) => event.usage).reduce((sum, usage) => sum + usage.input, 0), summaries.reduce((sum, item) => sum + item.input, 0));
    if (mode === "off") {
      assert.equal(summaries.length, 0); assert.equal(events.length, 0);
      assert.ok(JSON.stringify(regular.at(-1)).includes("PRIVATE_HTTP_EVIDENCE_round-1"));
    } else if (failed) {
      assert.equal(summaries.length, 2); assert.equal(events[0].status, "failed");
      assert.ok(JSON.stringify(regular.at(-1)).includes("PRIVATE_HTTP_EVIDENCE_round-1"));
    } else {
      assert.ok(summaries.length >= (mode === "pending" ? 1 : 2));
      assert.ok(mode === "pending" ? events.some((event) => event.status === "completed")
        : events.every((event) => event.status === "completed"));
      assert.ok(JSON.stringify(regular.at(-1)).includes("HTTP_SUMMARY_"));
      assert.ok(!JSON.stringify(regular.at(-1)).includes("PRIVATE_HTTP_EVIDENCE_round-1"));
      if (mode === "pending") {
        assert.ok(summaryWhilePending && ordinaryPaused && slowDone);
        assert.ok(regular.some(({ body }) => JSON.stringify(body).includes("SLOW_HTTP_RESULT_COMPLETE")));
      }
    }
  });
}
