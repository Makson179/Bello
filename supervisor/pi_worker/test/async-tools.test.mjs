import assert from "node:assert/strict";
import test from "node:test";
import { AsyncToolCoordinator, lateToolMessage } from "../src/async-tools.mjs";

const result = (text) => ({ content: [{ type: "text", text }], details: {} });
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
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
