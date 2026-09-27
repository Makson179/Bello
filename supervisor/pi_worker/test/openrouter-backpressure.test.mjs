import assert from "node:assert/strict";
import test from "node:test";
import { createOpenRouterBackpressureFetch, installOpenRouterBackpressure, retryAfterMs } from "../src/openrouter-backpressure.mjs";

const URL = "https://openrouter.ai/api/v1/chat/completions";
const INIT = { method: "POST", headers: { "content-type": "application/json", Authorization: "Bearer offline-test-only" }, body: JSON.stringify({ model: "test", messages: [], max_tokens: 8192 }) };
const error = (metadata = {}, retry = "1", status = 402, code = 402) => new Response(JSON.stringify({ error: { code, message: "private error body", metadata: { limit_source: "openrouter_in_flight_budget", reason: "in_flight_budget_exhausted", ...metadata } } }), {
  status, headers: { "content-type": "application/json", ...(retry === undefined ? {} : { "retry-after": retry }) },
});

test("exact transient 402 retries the same HTTP body/headers, not an agent turn", async () => {
  const calls = [], waits = [];
  const final = new Response("accepted", { status: 200 });
  const fetch = createOpenRouterBackpressureFetch(async (url, init) => {
    calls.push([url, init]);
    return calls.length === 1 ? error() : final;
  }, { wait: async (ms) => waits.push(ms) });
  assert.equal(await fetch(URL, INIT), final);
  assert.deepEqual(waits, [1000]);
  assert.equal(calls.length, 2);
  assert.equal(calls[0][1], calls[1][1]);
  assert.equal(JSON.parse(calls[1][1].body).max_tokens, 8192);
});

for (const [name, metadata, header, status, code] of [
  ["account credit", { limit_source: "openrouter_credits" }, "1", 402, 402],
  ["single request weight", { limit_source: "openrouter_credits", reason: "weight_exceeds_budget" }, "1", 402, 402],
  ["key cap", { limit_source: "openrouter_key_limit" }, "1", 402, 402],
  ["unknown source", { limit_source: "new-source" }, "1", 402, 402],
  ["unknown reason", { reason: "new-reason" }, "1", 402, 402],
  ["missing reason", { reason: undefined }, "1", 402, 402],
  ["wrong code", {}, "1", 402, 429],
  ["429", {}, "1", 429, 429],
  ["503", {}, "1", 503, 503],
  ["accepted 200 body error", {}, "1", 200, 402],
  ["bad header", {}, "garbage", 402, 402],
  ["negative header", {}, "-1", 402, 402],
  ["fraction header", {}, "1.5", 402, 402],
  ["oversized delay", {}, "121", 402, 402],
]) {
  test(`does not retry ${name}; original status, headers and body survive`, async () => {
    let calls = 0;
    const response = error(metadata, header, status, code);
    const expected = await response.clone().text();
    const fetch = createOpenRouterBackpressureFetch(async () => { calls++; return response; }, { wait: async () => assert.fail("must not wait") });
    assert.equal(await fetch(URL, INIT), response);
    assert.equal(calls, 1);
    assert.equal(response.status, status);
    assert.equal(await response.text(), expected);
  });
}

test("missing Retry-After is not a retry case", async () => {
  const response = error(); response.headers.delete("retry-after");
  let calls = 0;
  const fetch = createOpenRouterBackpressureFetch(async () => { calls++; return response; });
  assert.equal(await fetch(URL, INIT), response); assert.equal(calls, 1);
});

test("strict Retry-After parsing supports integer seconds and standard HTTP date", () => {
  const now = Date.parse("Sat, 26 Sep 2026 00:00:00 GMT");
  assert.equal(retryAfterMs("0", now), 0);
  assert.equal(retryAfterMs("3", now), 3000);
  assert.equal(retryAfterMs("Sat, 26 Sep 2026 00:00:03 GMT", now), 3000);
  assert.equal(retryAfterMs("Sat, 26 Sep 2026 00:00:00 GMT", now), 0);
  assert.equal(retryAfterMs("Sat, 31 Feb 2026 00:00:00 GMT", now), undefined);
  for (const value of [undefined, "", "Infinity", "-1", "1.5", "2026", "9".repeat(24)]) {
    if (value === "2026") assert.equal(retryAfterMs(value, now), 2026000);
    else assert.equal(retryAfterMs(value, now), undefined);
  }
});

test("exactly two retries, final original 402 is returned unchanged", async () => {
  const errors = [error(), error(), error()];
  let calls = 0, waits = 0;
  const fetch = createOpenRouterBackpressureFetch(async () => errors[calls++], { wait: async () => { waits++; } });
  const final = await fetch(URL, INIT);
  assert.equal(calls, 3); assert.equal(waits, 2); assert.equal(final, errors[2]);
  assert.equal((await final.json()).error.code, 402);
});

test("total wait cap never shortens Retry-After or performs an early retry", async () => {
  let calls = 0; const waits = [];
  const last = error({}, "61");
  const fetch = createOpenRouterBackpressureFetch(async () => ++calls === 1 ? error({}, "60") : last, { wait: async (ms) => waits.push(ms) });
  assert.equal(await fetch(URL, INIT), last); assert.equal(calls, 2); assert.deepEqual(waits, [60000]);
});

test("elapsed time cap includes time outside sleep", async () => {
  let clock = 0, calls = 0; const final = error();
  const fetch = createOpenRouterBackpressureFetch(async () => { calls++; clock += 120001; return final; }, { now: () => clock, wait: async () => assert.fail("must not wait") });
  assert.equal(await fetch(URL, INIT), final); assert.equal(calls, 1);
});

test("abort during backoff is prompt and prevents another HTTP request", async () => {
  const controller = new AbortController(); let calls = 0;
  const fetch = createOpenRouterBackpressureFetch(async () => { calls++; return error({}, "60"); });
  const pending = fetch(URL, { ...INIT, signal: controller.signal });
  setTimeout(() => controller.abort(), 10);
  await assert.rejects(pending, { name: "AbortError" }); assert.equal(calls, 1);
});

test("already aborted does not call fetch", async () => {
  const controller = new AbortController(); controller.abort();
  const fetch = createOpenRouterBackpressureFetch(async () => assert.fail("no HTTP call"));
  await assert.rejects(fetch(URL, { ...INIT, signal: controller.signal }), { name: "AbortError" });
});

test("network exception is passed through with no wrapper retry", async () => {
  const original = new Error("offline disconnect"); let calls = 0;
  const fetch = createOpenRouterBackpressureFetch(async () => { calls++; throw original; });
  await assert.rejects(fetch(URL, INIT), (caught) => caught === original); assert.equal(calls, 1);
});

test("nonmatching URL, API, method and nonreplayable bodies are called once", async () => {
  for (const [url, init] of [
    ["https://other.example/api/v1/chat/completions", INIT],
    [URL+"?extra=1", INIT],
    ["https://openrouter.ai/api/v1/responses", INIT],
    [URL, { ...INIT, method: "GET" }],
    [URL, { ...INIT, body: new ReadableStream() }],
    [new Request(URL, INIT), undefined],
  ]) {
    let calls=0; const response=error();
    const fetch=createOpenRouterBackpressureFetch(async () => { calls++; return response; });
    assert.equal(await fetch(url,init),response); assert.equal(calls,1);
  }
});

test("invalid JSON, oversized errors and non-JSON MIME preserve the response", async () => {
  for (const response of [
    new Response("not json", {status:402,headers:{"retry-after":"1","content-type":"application/json"}}),
    new Response("x".repeat(65537), {status:402,headers:{"retry-after":"1","content-type":"application/json"}}),
    new Response("{}", {status:402,headers:{"retry-after":"1","content-type":"text/html"}}),
  ]) {
    let calls=0; const expected=await response.clone().text();
    const fetch=createOpenRouterBackpressureFetch(async () => { calls++; return response; });
    assert.equal(await fetch(URL,INIT),response); assert.equal(calls,1); assert.equal(await response.text(),expected);
  }
});

test("HTTP200 SSE error or partial output is never read or retried by wrapper", async () => {
  const body='data: {"error":{"code":402,"metadata":{"limit_source":"openrouter_in_flight_budget","reason":"in_flight_budget_exhausted"}}}\n\n';
  const response=new Response(body,{headers:{"content-type":"text/event-stream","retry-after":"0"}});
  let calls=0; const fetch=createOpenRouterBackpressureFetch(async () => { calls++;return response; });
  assert.equal(await fetch(URL,INIT),response); assert.equal(response.bodyUsed,false); assert.equal(calls,1); assert.equal(await response.text(),body);
});

test("per-agent hook preserves other providers/options and is idempotent", async () => {
  const marker={}, calls=[];
  const stop = async () => true;
  const agent={shouldStopAfterTurn:stop,streamFunction(model,context,options){calls.push({model,context,options,thisArg:this});return marker;}};
  installOpenRouterBackpressure(agent); const once=agent.streamFunction; installOpenRouterBackpressure(agent); assert.equal(agent.streamFunction,once);
  assert.equal(agent.shouldStopAfterTurn,stop);
  assert.equal(await agent.shouldStopAfterTurn(),true);
  const options={maxTokens:8192,reasoning:"high",onPayload:()=>{},sessionId:"fixed-session"};
  assert.equal(agent.streamFunction({provider:"anthropic",api:"anthropic-messages"},{},options),marker);
  assert.equal(calls[0].options,options);
  agent.streamFunction({provider:"openrouter",api:"openai-completions"},{},options);
  assert.equal(calls[1].thisArg,agent); assert.equal(calls[1].options.maxTokens,8192); assert.equal(calls[1].options.reasoning,"high");
  assert.equal(calls[1].options.onPayload,options.onPayload); assert.equal(calls[1].options.sessionId,"fixed-session");
  assert.equal(typeof calls[1].options.fetch,"function");
});

test("invalid unbounded policy is rejected", () => {
  for (const policy of [{maxRetries:3},{maxRetries:-1},{maxWaitMs:Infinity},{maxWaitMs:120001}]) assert.throws(()=>createOpenRouterBackpressureFetch(()=>{},policy),TypeError);
});

test("abort while inspecting a stalled error body cancels inspection", async () => {
  const controller=new AbortController();let calls=0, cancelled=0;
  const response=new Response(new ReadableStream({start(stream){stream.enqueue(new TextEncoder().encode('{"error":'));},cancel(){cancelled++;}}),
    {status:402,headers:{"content-type":"application/json","retry-after":"0"}});
  const fetch=createOpenRouterBackpressureFetch(async()=>{calls++;return response;});
  const pending=fetch(URL,{...INIT,signal:controller.signal});setTimeout(()=>controller.abort(),10);
  await assert.rejects(pending,{name:"AbortError"});assert.equal(calls,1);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(cancelled,1,"wrapper cancels both branches without caller cleanup");
});

test("abort immediately after fetch cancels the unreturned response and preserves abort reason",async()=>{
  const controller=new AbortController();const reason=new Error("offline cancellation");let calls=0,cancelled=0;
  const response=new Response(new ReadableStream({cancel(){cancelled++;}}));
  const fetch=createOpenRouterBackpressureFetch(async()=>{calls++;controller.abort(reason);return response;});
  await assert.rejects(fetch(URL,{...INIT,signal:controller.signal}),error=>error===reason);
  assert.equal(calls,1);assert.equal(cancelled,1);
});

test("redirected response cannot authorize a retry",async()=>{
  const response=error();Object.defineProperty(response,"redirected",{value:true});let calls=0;
  const fetch=createOpenRouterBackpressureFetch(async()=>{calls++;return response;});
  assert.equal(await fetch(URL,INIT),response);assert.equal(calls,1);
});

test("retry budget is per logical request with independent cancellation",async()=>{
  const calls=new Map();const waits=[];
  const fetch=createOpenRouterBackpressureFetch(async(_url,init)=>{
    const n=(calls.get(init.body)??0)+1;calls.set(init.body,n);return n<3?error({},"0"):new Response("accepted");
  },{wait:async(ms)=>waits.push(ms)});
  const a={...INIT,body:'{"model":"a"}'};const b={...INIT,body:'{"model":"b"}'};
  const results=await Promise.all([fetch(URL,a),fetch(URL,b)]);
  assert.deepEqual([...calls.values()],[3,3]);assert.equal(waits.length,4);assert.deepEqual(results.map(r=>r.status),[200,200]);
});
