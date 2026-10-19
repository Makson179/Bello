import assert from "node:assert/strict";
import test from "node:test";
import { AsyncToolCoordinator, lateToolMessage } from "../src/async-tools.mjs";

const result = (text) => ({ content: [{ type: "text", text }], details: {} });
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function mockClock(t) {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 10_000 });
  t.mock.method(performance, "now", () => Date.now());
  return async (milliseconds) => {
    t.mock.timers.tick(milliseconds);
    await flush();
  };
}

function controlledBatch(t, scheduler, ids) {
  scheduler.beginBatch(ids);
  const gates = Object.fromEntries(ids.map((id) => [id, deferred()]));
  let settled = false;
  const outputs = Promise.all(ids.map((id) => scheduler.execute(id, "exec_command", (signal) => {
    signal.addEventListener("abort", () => gates[id].reject(new Error("cancelled")), { once: true });
    return gates[id].promise;
  })));
  void outputs.then(() => { settled = true; }, () => { settled = true; });
  t.after(async () => {
    for (const id of ids) gates[id].resolve(result(id));
    await Promise.allSettled([outputs, ...[...scheduler.jobs.values()].filter((job) => ids.includes(job.callId)).map((job) => job.promise)]);
  });
  return { gates, outputs, get settled() { return settled; } };
}

async function placeholderBatch(t, scheduler, tick, ids) {
  const batch = controlledBatch(t, scheduler, ids);
  batch.gates[ids[0]].resolve(result(ids[0]));
  await flush();
  await tick(1000);
  const outputs = await batch.outputs;
  assert.deepEqual(outputs[0], result(ids[0]));
  for (const output of outputs.slice(1)) assert.equal(output.details.status, "running");
  return batch;
}

test("all fast results bypass the grace and launch concurrently", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 10_000 });
  scheduler.beginBatch(["a", "b"]);
  const started = [];
  const gate = deferred();
  const launch = (id) => scheduler.execute(id, "exec_command", async () => {
    started.push(id);
    await gate.promise;
    return result(id);
  });
  const a = launch("a");
  const b = launch("b");
  await Promise.resolve();
  assert.deepEqual(started, ["a", "b"]);
  gate.resolve();
  assert.deepEqual(await Promise.all([a, b]), [result("a"), result("b")]);
  assert.equal(scheduler.pending, false);
});

test("grace never releases a placeholder-only batch; late output is delivered once", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  scheduler.beginBatch(["a", "b"]);
  const ga = deferred();
  const gb = deferred();
  let returned = false;
  const a = scheduler.execute("a", "exec_command", () => ga.promise);
  const b = scheduler.execute("b", "exec_command", () => gb.promise);
  void Promise.all([a, b]).then(() => { returned = true; });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(returned, false);
  ga.resolve(result("first"));
  const [first, pending] = await Promise.all([a, b]);
  assert.deepEqual(first, result("first"));
  assert.equal(pending.details.status, "running");
  const snapshot = JSON.stringify(pending);
  gb.resolve(result("second"));
  const late = await scheduler.ready({ wait: true });
  assert.equal(late[0].callId, "b");
  assert.equal(JSON.stringify(pending), snapshot);
  assert.deepEqual(await scheduler.ready(), []);
  assert.match(lateToolMessage(late).content[0].text, /b \(exec_command\)/);
});

test("pending failure stays visible and cancellation wakes an idle wait", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  scheduler.beginBatch(["a", "b"]);
  let reject;
  const failure = new Promise((_resolve, fail) => { reject = fail; });
  await Promise.all([
    scheduler.execute("a", "exec_command", async () => result("ok")),
    scheduler.execute("b", "exec_command", () => failure),
  ]);
  reject(new Error("command failed"));
  const late = await scheduler.ready({ wait: true });
  assert.equal(late[0].isError, true);
  assert.equal(late[0].content[0].text, "command failed");

  scheduler.beginBatch(["c"]);
  const running = scheduler.execute("c", "exec_command", (signal) => new Promise((_resolve, fail) => {
    signal.addEventListener("abort", () => fail(new Error("cancelled")), { once: true });
  }));
  await Promise.resolve();
  const rejected = assert.rejects(running, /interrupted/);
  await scheduler.cancel();
  await rejected;
});

test("a ready result from an earlier batch releases later independent calls", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  const earlier = deferred();
  scheduler.beginBatch(["fast", "earlier"]);
  await Promise.all([
    scheduler.execute("fast", "exec_command", async () => result("fast")),
    scheduler.execute("earlier", "exec_command", () => earlier.promise),
  ]);
  const later = deferred();
  scheduler.beginBatch(["later"]);
  const waiting = scheduler.execute("later", "exec_command", () => later.promise);
  earlier.resolve(result("earlier"));
  assert.equal((await waiting).details.status, "running");
  assert.equal((await scheduler.ready())[0].callId, "earlier");
  later.resolve(result("later"));
  assert.equal((await scheduler.ready({ wait: true }))[0].callId, "later");
});

test("external steering wakes a result wait without inventing a tool result", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  const pending = deferred();
  scheduler.beginBatch(["fast", "slow"]);
  await Promise.all([
    scheduler.execute("fast", "exec_command", async () => result("fast")),
    scheduler.execute("slow", "exec_command", () => pending.promise),
  ]);
  const wait = scheduler.ready({ wait: true });
  scheduler.notifyExternalInput();
  assert.deepEqual(await wait, []);
  assert.equal(scheduler.pending, true);
  pending.resolve(result("slow"));
  assert.equal((await scheduler.ready({ wait: true }))[0].callId, "slow");
});

for (const firstCompletionAt of [100, 5000]) {
  test(`initial collection debounces real completions starting at ${firstCompletionAt}ms`, async (t) => {
    const tick = mockClock(t);
    const scheduler = new AsyncToolCoordinator();
    const batch = controlledBatch(t, scheduler, ["a", "b", "c"]);
    await tick(firstCompletionAt);
    assert.equal(batch.settled, false, "elapsed launch grace alone cannot release placeholders");
    batch.gates.a.resolve(result("a"));
    await flush();
    await tick(700);
    batch.gates.b.resolve(result("b"));
    await flush();
    await tick(300);
    assert.equal(batch.settled, false, "the first completion's stale deadline cannot release the batch");
    scheduler.changed();
    await tick(699);
    assert.equal(batch.settled, false);
    await tick(1);
    const outputs = await batch.outputs;
    assert.deepEqual(outputs.slice(0, 2), [result("a"), result("b")]);
    assert.equal(outputs[2].details.status, "running");
  });
}

test("the last initial sibling releases the batch before the completion deadline", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = controlledBatch(t, scheduler, ["a", "b"]);
  await tick(100);
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(200);
  assert.equal(batch.settled, false);
  batch.gates.b.resolve(result("b"));
  await flush();
  assert.equal(batch.settled, true);
  assert.deepEqual(await batch.outputs, [result("a"), result("b")]);
});

test("late siblings rearm their batch window and preserve exact-once immutable output", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b", "c"]);
  const original = JSON.stringify(await batch.outputs);
  let delivered = false;
  const waiting = scheduler.ready({ wait: true });
  void waiting.then(() => { delivered = true; });
  await tick(60_000);
  assert.equal(delivered, false, "waiting without new results cannot wake the model");
  assert.deepEqual(await scheduler.ready(), []);
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(600);
  batch.gates.b.resolve(result("b"));
  await flush();
  await tick(400);
  assert.equal(delivered, false, "a stale timer must not deliver after a sibling rearms");
  assert.deepEqual(await scheduler.ready(), [], "nonblocking collection respects the same window");
  scheduler.changed();
  await tick(599);
  assert.equal(delivered, false);
  await tick(1);
  assert.deepEqual((await waiting).map((entry) => entry.callId), ["a", "b"]);
  assert.equal(JSON.stringify(await batch.outputs), original);
  await tick(5000);
  assert.deepEqual(await scheduler.ready(), [], "consumed completions cannot rearm or repeat");
  batch.gates.c.resolve(result("c"));
  await flush();
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["c"]);
  assert.equal(scheduler.pending, false);
});

test("a late error counts as a completion and the final sibling releases all results early", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b", "c"]);
  let delivered = false;
  const waiting = scheduler.ready({ wait: true });
  void waiting.then(() => { delivered = true; });
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(800);
  batch.gates.b.reject(new Error("command failed"));
  await flush();
  await tick(200);
  assert.equal(delivered, false, "a failed sibling rearms the completion window");
  batch.gates.c.reject(new Error("last command failed"));
  await flush();
  assert.equal(delivered, true, "the last failure completes the batch without waiting");
  const ready = await waiting;
  assert.deepEqual(ready.map((entry) => entry.callId), ["a", "b", "c"]);
  assert.equal(ready[1].isError, true);
  assert.equal(ready[1].content[0].text, "command failed");
  assert.equal(ready[2].isError, true);
  assert.deepEqual(await scheduler.ready(), []);
});

test("a different batch cannot extend or join a collecting batch before its own deadline", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const first = await placeholderBatch(t, scheduler, tick, ["first-fast", "a", "a-pending"]);
  const second = await placeholderBatch(t, scheduler, tick, ["second-fast", "b", "b-pending"]);
  let delivered = false;
  const waiting = scheduler.ready({ wait: true });
  void waiting.then(() => { delivered = true; });
  first.gates.a.resolve(result("a"));
  await flush();
  await tick(500);
  second.gates.b.resolve(result("b"));
  await flush();
  await tick(499);
  assert.equal(delivered, false);
  await tick(1);
  assert.deepEqual((await waiting).map((entry) => entry.callId), ["a"]);
  assert.deepEqual(await scheduler.ready(), []);
  await tick(499);
  assert.deepEqual(await scheduler.ready(), []);
  await tick(1);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["b"]);
});

test("an older mature result releases a newer launch without extending its collection", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const first = await placeholderBatch(t, scheduler, tick, ["fast", "a", "a-pending"]);
  first.gates.a.resolve(result("a"));
  await flush();
  await tick(700);
  const second = controlledBatch(t, scheduler, ["b", "b-pending"]);
  second.gates.b.resolve(result("b"));
  await flush();
  await tick(299);
  assert.equal(second.settled, false);
  await tick(1);
  assert.equal(second.settled, true, "the older batch's deadline controls its delivery");
  assert.equal((await second.outputs)[0].details.status, "awaiting_delivery");
  assert.match((await second.outputs)[0].content[0].text, /has completed and is awaiting delivery/);
  assert.equal((await second.outputs)[1].details.status, "running");
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["a"]);
  await tick(699);
  assert.deepEqual(await scheduler.ready(), []);
  await tick(1);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["b"]);
});

test("a completion after an unconsumed deadline cannot restart the expired window", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b", "pending"]);
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(1500);
  batch.gates.b.resolve(result("b"));
  await flush();
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["a", "b"]);
  await tick(5000);
  assert.deepEqual(await scheduler.ready(), []);
});

test("steering releases initial placeholders immediately without fabricating a result", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = controlledBatch(t, scheduler, ["a", "b"]);
  await tick(100);
  scheduler.notifyExternalInput();
  await flush();
  assert.equal(batch.settled, true);
  for (const output of await batch.outputs) assert.equal(output.details.status, "running");
  assert.deepEqual(await scheduler.ready({ wait: true }), []);
  assert.equal(scheduler.pending, true);
});

test("steering bypasses a late completion window and keeps its ready evidence", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b"]);
  batch.gates.a.resolve(result("a"));
  await flush();
  let delivered = false;
  const waiting = scheduler.ready({ wait: true });
  void waiting.then(() => { delivered = true; });
  await tick(100);
  assert.equal(delivered, false);
  scheduler.notifyExternalInput();
  await flush();
  assert.equal(delivered, true);
  assert.deepEqual((await waiting).map((entry) => entry.callId), ["a"]);
  await tick(5000);
  assert.deepEqual(await scheduler.ready(), []);
  batch.gates.b.resolve(result("b"));
  await flush();
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["b"]);
});

for (const phase of ["initial", "late"]) {
  test(`cancellation interrupts an active ${phase} completion window`, async (t) => {
    const tick = mockClock(t);
    const scheduler = new AsyncToolCoordinator();
    const batch = phase === "initial"
      ? controlledBatch(t, scheduler, ["a", "b"])
      : await placeholderBatch(t, scheduler, tick, ["fast", "a", "b"]);
    batch.gates.a.resolve(result("a"));
    await flush();
    const interrupted = assert.rejects(phase === "initial" ? batch.outputs : scheduler.ready({ wait: true }), /interrupted/);
    await tick(100);
    await scheduler.cancel();
    await interrupted;
    assert.equal([...scheduler.jobs.values()].every((job) => job.done), true);
    assert.equal(scheduler.waiters.size, 0);
    assert.equal(scheduler.pending, false, "cancellation leaves no result pending");
    await assert.rejects(scheduler.ready({ wait: true }), /interrupted/);
    await tick(60_000);
    assert.equal(scheduler.waiters.size, 0, "no timer or waiter survives cancellation");
  });
}

test("a call that arrives after cancellation is refused without starting its tool", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  scheduler.beginBatch(["late-call"]);
  await scheduler.cancel();
  let started = 0;
  const tool = async () => { started++; return result("started"); };
  await assert.rejects(scheduler.execute("late-call", "exec_command", tool), /interrupted/);
  await assert.rejects(scheduler.execute("outside-batch", "exec_command", tool), /interrupted/);
  await flush();
  assert.equal(started, 0, "nobody could await or abort a tool started after cancellation");
  assert.equal(scheduler.jobs.size, 0);
  assert.equal(scheduler.pending, false);
  await assert.rejects(scheduler.ready(), /interrupted/);
  await scheduler.cancel();
});

test("cancellation settles running tools, drops done-but-undelivered results and rejects every waiter", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const early = await placeholderBatch(t, scheduler, tick, ["fast", "done-late", "running-late"]);
  early.gates["done-late"].resolve(result("done-late"));
  await flush();
  const current = controlledBatch(t, scheduler, ["now-a", "now-b"]);
  current.gates["now-a"].resolve(result("now-a"));
  await flush();
  const collector = scheduler.ready({ wait: true });
  const rejections = [assert.rejects(collector, /interrupted/), assert.rejects(current.outputs, /interrupted/)];
  assert.equal(scheduler.pending, true);
  await scheduler.cancel();
  await Promise.all(rejections);
  assert.equal(scheduler.pending, false);
  assert.equal(scheduler.waiters.size, 0);
  assert.equal([...scheduler.jobs.values()].every((job) => job.done), true, "every tool was aborted and settled");
  assert.equal(scheduler.jobs.get("running-late").controller.signal.aborted, true);
  await assert.rejects(scheduler.ready(), /interrupted/);
});

test("a call whose own signal is already aborted never starts its tool and leaves nothing pending", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  scheduler.beginBatch(["a"]);
  const caller = new AbortController();
  caller.abort();
  let started = false;
  await assert.rejects(scheduler.execute("a", "exec_command", async () => { started = true; return result("a"); }, caller.signal), /interrupted/);
  await scheduler.jobs.get("a").promise;
  assert.equal(started, false);
  assert.equal(scheduler.pending, false);
  assert.equal(scheduler.waiters.size, 0);
});

test("aborting a call's own signal rejects it, stops its tool and never leaves the turn pending", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  scheduler.beginBatch(["a"]);
  const caller = new AbortController();
  let toolSignal;
  const running = scheduler.execute("a", "exec_command", (signal) => new Promise((_resolve, reject) => {
    toolSignal = signal;
    signal.addEventListener("abort", () => reject(new Error("tool stopped")), { once: true });
  }), caller.signal);
  const rejected = assert.rejects(running, /interrupted/);
  await tick(10);
  caller.abort();
  await rejected;
  assert.equal(toolSignal.aborted, true);
  assert.equal(scheduler.pending, false, "the abandoned call has nothing left to deliver");
  await scheduler.jobs.get("a").promise;
  assert.equal(scheduler.pending, false);
  assert.deepEqual(await scheduler.ready({ wait: true }), []);
  assert.equal(scheduler.waiters.size, 0);
});

test("a call that Pi finished without executing never keeps its batch open", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const gates = { a: deferred(), b: deferred() };
  scheduler.beginBatch(["a", "invalid", "b"]);
  scheduler.skip("invalid");
  let settled = false;
  const outputs = Promise.all(["a", "b"].map((id) => scheduler.execute(id, "exec_command", () => gates[id].promise)));
  void outputs.then(() => { settled = true; }, () => { settled = true; });
  gates.a.resolve(result("a"));
  await flush();
  assert.equal(settled, false, "the executed sibling is still running");
  gates.b.resolve(result("b"));
  await flush();
  assert.equal(settled, true, "all executed calls finished, so the batch returns without waiting for the window");
  assert.deepEqual(await outputs, [result("a"), result("b")]);
  await tick(60_000);
  assert.deepEqual(await scheduler.ready(), []);
  assert.equal(scheduler.pending, false);
});

test("skipping a call while its siblings already wait releases them at once", async (t) => {
  mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  scheduler.beginBatch(["a", "invalid"]);
  const gate = deferred();
  let settled = false;
  const output = scheduler.execute("a", "exec_command", () => gate.promise);
  void output.then(() => { settled = true; });
  gate.resolve(result("a"));
  await flush();
  assert.equal(settled, false, "one of two calls finished: the window is open");
  scheduler.skip("invalid");
  await flush();
  assert.equal(settled, true);
  assert.deepEqual(await output, result("a"));
});

test("skip ignores calls that run, calls outside the batch and repeated notices", async () => {
  const scheduler = new AsyncToolCoordinator({ graceMs: 0 });
  scheduler.beginBatch(["a", "b"]);
  const gate = deferred();
  const running = scheduler.execute("a", "exec_command", () => gate.promise);
  scheduler.skip("a");
  scheduler.skip("unknown");
  scheduler.skip("b");
  scheduler.skip("b");
  assert.deepEqual([...scheduler.batch.ids], ["a"]);
  gate.resolve(result("a"));
  assert.deepEqual(await running, result("a"));
  new AsyncToolCoordinator().skip("nothing-started");
});

test("a result returned by execute() is never offered again by ready()", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = controlledBatch(t, scheduler, ["a", "b", "c"]);
  batch.gates.a.resolve(result("a"));
  batch.gates.b.resolve(result("b"));
  await flush();
  batch.gates.c.resolve(result("c"));
  await flush();
  assert.deepEqual(await batch.outputs, [result("a"), result("b"), result("c")]);
  assert.deepEqual(await scheduler.ready(), []);
  assert.deepEqual(await scheduler.ready({ wait: true }), []);
  await tick(60_000);
  assert.deepEqual(await scheduler.ready(), []);
  assert.equal(scheduler.pending, false);
});

test("two concurrent collectors never receive the same late result", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b"]);
  const first = scheduler.ready({ wait: true });
  const second = scheduler.ready({ wait: true });
  batch.gates.a.resolve(result("a"));
  await flush();
  batch.gates.b.resolve(result("b"));
  await flush();
  const delivered = [...await first, ...await second].map((entry) => entry.callId);
  assert.deepEqual(delivered.sort(), ["a", "b"]);
  assert.deepEqual(await scheduler.ready(), []);
});

test("consecutive late completions chain one window; the next completion after delivery opens its own", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b", "c", "d"]);
  const waiting = scheduler.ready({ wait: true });
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(800);
  batch.gates.b.resolve(result("b"));
  await flush();
  await tick(800);
  batch.gates.c.resolve(result("c"));
  await flush();
  await tick(999);
  assert.equal(scheduler.jobs.get("a").delivered, false, "each completion moved the shared deadline");
  await tick(1);
  assert.deepEqual((await waiting).map((entry) => entry.callId), ["a", "b", "c"], "one request carries the whole cluster");
  batch.gates.d.resolve(result("d"));
  await flush();
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["d"], "the final sibling needs no further window");
  assert.equal(scheduler.pending, false);
});

test("a late completion after delivery waits for its own window when siblings still run", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a", "b", "c"]);
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(1000);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["a"]);
  batch.gates.b.resolve(result("b"));
  await flush();
  await tick(999);
  assert.deepEqual(await scheduler.ready(), [], "the earlier delivery does not shorten the next window");
  await tick(1);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["b"]);
});

test("a younger batch that keeps completing cannot postpone an older batch's mature result", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const older = await placeholderBatch(t, scheduler, tick, ["old-fast", "old-a", "old-pending"]);
  older.gates["old-a"].resolve(result("old-a"));
  await flush();
  const younger = controlledBatch(t, scheduler, ["y1", "y2", "y3", "y4"]);
  await tick(400);
  younger.gates.y1.resolve(result("y1"));
  await flush();
  await tick(400);
  younger.gates.y2.resolve(result("y2"));
  await flush();
  assert.equal(younger.settled, false);
  await tick(199);
  assert.equal(younger.settled, false);
  await tick(1);
  assert.equal(younger.settled, true, "the older mature result releases the younger launch");
  assert.deepEqual((await younger.outputs).map((entry) => entry.details.status), ["awaiting_delivery", "awaiting_delivery", "running", "running"]);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["old-a"], "its own deadline decides, not the younger completions");
  await tick(799);
  assert.deepEqual(await scheduler.ready(), []);
  await tick(1);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["y1", "y2"]);
});

test("time alone never wakes a wait while nothing has completed", async (t) => {
  const tick = mockClock(t);
  const initial = new AsyncToolCoordinator();
  const batch = controlledBatch(t, initial, ["a", "b"]);
  await tick(3_600_000);
  assert.equal(batch.settled, false);
  const late = new AsyncToolCoordinator();
  const waiting = await placeholderBatch(t, late, tick, ["fast", "a"]);
  let woke = false;
  void late.ready({ wait: true }).then(() => { woke = true; });
  await tick(3_600_000);
  assert.equal(woke, false);
  waiting.gates.a.resolve(result("a"));
  await flush();
  assert.equal(woke, true);
});

test("steering wakes exactly one wait and is then consumed", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = await placeholderBatch(t, scheduler, tick, ["fast", "a"]);
  scheduler.notifyExternalInput();
  assert.deepEqual(await scheduler.ready({ wait: true }), [], "the notice releases the wait without inventing a result");
  assert.equal(scheduler.externalInputPending, false);
  let woke = false;
  const waiting = scheduler.ready({ wait: true });
  void waiting.then(() => { woke = true; });
  await tick(60_000);
  assert.equal(woke, false, "a consumed steering notice does not wake the next wait");
  batch.gates.a.resolve(result("a"));
  await flush();
  assert.deepEqual((await waiting).map((entry) => entry.callId), ["a"]);
});

test("steering releases every sibling of a waiting batch but delivers only finished results", async (t) => {
  const tick = mockClock(t);
  const scheduler = new AsyncToolCoordinator();
  const batch = controlledBatch(t, scheduler, ["a", "b", "c", "d"]);
  batch.gates.a.resolve(result("a"));
  await flush();
  await tick(300);
  scheduler.notifyExternalInput();
  await flush();
  assert.equal(batch.settled, true);
  const outputs = await batch.outputs;
  assert.deepEqual(outputs[0], result("a"));
  assert.deepEqual(outputs.slice(1).map((entry) => entry.details.status), ["running", "running", "running"]);
  batch.gates.b.resolve(result("b"));
  await flush();
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["b"],
    "the unconsumed steering notice makes the finished result available without a window");
  batch.gates.c.resolve(result("c"));
  await flush();
  await tick(999);
  assert.deepEqual(await scheduler.ready(), [], "the notice was consumed: a later result waits for the normal rules");
  await tick(1);
  assert.deepEqual((await scheduler.ready()).map((entry) => entry.callId), ["c"]);
});

// A seeded, deterministic interleaving of batches, completions, failures, skipped
// calls and steering driven through the same loop the Pi session uses.
test("randomized interleavings deliver every result exactly once and honour every window", async (t) => {
  t.mock.method(performance, "now", () => Date.now());
  const graceMs = 1000;
  for (let seed = 1; seed <= 60; seed++) {
    t.mock.timers.reset();
    t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 10_000 });
    let state = seed >>> 0;
    const random = () => { state = (state * 1664525 + 1013904223) >>> 0; return state / 2 ** 32; };
    const pick = (count) => Math.floor(random() * count);
    const scheduler = new AsyncToolCoordinator({ graceMs });
    const plan = Array.from({ length: 1 + pick(3) }, (_, b) => Array.from({ length: 1 + pick(4) }, (_, i) => ({
      id: `b${b}c${i}`, finishIn: random() < 0.15 ? Infinity : pick(3500), fail: random() < 0.2, skipped: random() < 0.15,
    })));
    const steerIn = random() < 0.3 ? pick(6000) : Infinity;
    const completedAt = new Map(), batchOf = new Map(), deliveries = new Map(), finishers = new Map();
    const releasedAt = new Map(), startedAt = new Map();
    let steeredAt;
    const deliver = (id, how) => deliveries.set(id, [...(deliveries.get(id) ?? []), how]);
    const checkMature = (entry) => {
      const batch = batchOf.get(entry.callId);
      const live = plan[batch].filter((call) => !call.skipped);
      const siblingsDone = live.every((call) => completedAt.has(call.id));
      const last = Math.max(...live.filter((call) => completedAt.has(call.id)).map((call) => completedAt.get(call.id)));
      assert.ok(siblingsDone || steeredAt !== undefined || Date.now() - last >= graceMs,
        `seed ${seed}: ${entry.callId} delivered at ${Date.now()} but its batch last completed at ${last}`);
    };
    let steeredWait = false;
    const model = (async () => {
      for (const [b, calls] of plan.entries()) {
        scheduler.beginBatch(calls.map((call) => call.id));
        startedAt.set(b, Date.now());
        for (const call of calls) { batchOf.set(call.id, b); if (call.skipped) scheduler.skip(call.id); }
        const outputs = await Promise.all(calls.filter((call) => !call.skipped).map((call) => scheduler.execute(call.id, "tool", (signal) =>
          new Promise((resolve, reject) => {
            finishers.set(call.id, () => {
              completedAt.set(call.id, Date.now());
              if (call.fail) reject(new Error(`failed ${call.id}`)); else resolve(result(call.id));
            });
            signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
            if (call.finishIn === 0) finishers.get(call.id)();
          })).then((value) => ({ value }), (error) => ({ error }))));
        releasedAt.set(b, Date.now());
        for (const output of outputs) {
          if (output.error) deliver(output.error.message.replace("failed ", ""), "direct");
          else if (!output.value.details?.asyncToolCallId) deliver(output.value.content[0].text, "direct");
        }
        for (const entry of await scheduler.ready()) { checkMature(entry); deliver(entry.callId, "ready"); }
      }
      while (scheduler.pending) {
        const delivered = await scheduler.ready({ wait: true });
        for (const entry of delivered) { checkMature(entry); deliver(entry.callId, "ready"); }
        if (!delivered.length && scheduler.pending) {
          assert.ok(steeredWait, `seed ${seed}: a wait resolved without a result or steering`);
          steeredWait = false;
        }
      }
    })();
    let finished = false;
    model.then(() => { finished = true; }, () => {});
    const schedule = plan.flat().filter((call) => !call.skipped && call.finishIn !== Infinity && call.finishIn !== 0);
    // 30 s of mock time is longer than any scripted completion plus its windows.
    for (let step = 0; !finished && step < 600; step++) {
      await flush();
      const now = Date.now() - 10_000;
      if (steerIn !== Infinity && steeredAt === undefined && now >= steerIn) {
        steeredAt = Date.now(); steeredWait = true; scheduler.notifyExternalInput();
        continue;
      }
      for (const call of schedule) {
        const start = startedAt.get(batchOf.get(call.id));
        if (start !== undefined && finishers.has(call.id) && !completedAt.has(call.id) && Date.now() >= start + call.finishIn) {
          finishers.get(call.id)();
          await flush();
        }
      }
      t.mock.timers.tick(50);
    }
    if (!finished) {
      await scheduler.cancel();
      await model.catch(() => {});
      assert.equal(scheduler.pending, false, `seed ${seed}`);
      assert.equal(scheduler.waiters.size, 0, `seed ${seed}`);
    }
    for (const [id, hows] of deliveries) assert.equal(hows.length, 1, `seed ${seed}: ${id} delivered ${hows.join("+")}`);
    for (const [b, calls] of plan.entries()) {
      const live = calls.filter((call) => !call.skipped);
      if (!finished || steeredAt !== undefined || live.some((call) => call.finishIn === Infinity) || !live.length) continue;
      for (const call of live) assert.ok(deliveries.has(call.id), `seed ${seed}: ${call.id} never reached the model`);
      assert.ok(releasedAt.get(b) <= Math.max(...live.map((call) => completedAt.get(call.id))),
        `seed ${seed}: batch ${b} waited for a window although every call had finished`);
    }
  }
});
