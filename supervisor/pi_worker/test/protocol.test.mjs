import assert from "node:assert/strict";
import { PassThrough } from "node:stream";
import test from "node:test";

import { HostCallBroker, JsonlWriter, WorkerServer } from "../src/protocol.mjs";

function captureWriter() {
  const stream = new PassThrough();
  const frames = [];
  stream.setEncoding("utf8");
  let pending = "";
  stream.on("data", (chunk) => {
    pending += chunk;
    const lines = pending.split("\n");
    pending = lines.pop();
    for (const line of lines) if (line) frames.push(JSON.parse(line));
  });
  return { writer: new JsonlWriter(stream), frames };
}

test("host calls carry stable tool ids and accept object results", async () => {
  const { writer, frames } = captureWriter();
  const broker = new HostCallBroker((message) => writer.send(message));
  const resultPromise = broker.call({
    threadId: "thread-1",
    turnId: "turn-1",
    callId: "pi-call-1",
    name: "read_file",
    arguments: { path: "README.md" },
  });
  await writer.tail;
  assert.deepEqual(frames[0].params, {
    threadId: "thread-1",
    turnId: "turn-1",
    callId: "pi-call-1",
    name: "read_file",
    arguments: { path: "README.md" },
  });
  assert.equal(broker.accept({ id: frames[0].id, result: { content: [{ type: "text", text: "ok" }], details: {} } }), true);
  assert.equal((await resultPromise).content[0].text, "ok");
});

test("aborting a host call emits bello/tool/cancel and ignores a late result", async () => {
  const { writer, frames } = captureWriter();
  const broker = new HostCallBroker((message) => writer.send(message));
  const controller = new AbortController();
  const resultPromise = broker.call({
    threadId: "thread-1",
    turnId: "turn-1",
    callId: "pi-call-2",
    name: "exec_command",
    arguments: { command: "long" },
  }, controller.signal);
  await writer.tail;
  const requestId = frames[0].id;
  controller.abort();
  await assert.rejects(resultPromise, { name: "AbortError" });
  await writer.tail;
  assert.deepEqual(frames[1], {
    method: "bello/tool/cancel",
    params: { requestId, threadId: "thread-1", turnId: "turn-1", callId: "pi-call-2" },
  });
  assert.equal(broker.accept({ id: requestId, result: { content: [{ type: "text", text: "late" }] } }), false);
});

test("worker server multiplexes requests and returns object errors", async () => {
  const { writer, frames } = captureWriter();
  const broker = new HostCallBroker((message) => writer.send(message));
  const runtime = {
    async dispatch(method, params) {
      if (method === "fail") throw new Error("boom");
      await new Promise((resolve) => setTimeout(resolve, params.delay));
      return { method };
    },
  };
  const server = new WorkerServer({ runtime, writer, hostCalls: broker });
  server.accept({ id: 1, method: "slow", params: { delay: 20 } });
  server.accept({ id: 2, method: "fast", params: { delay: 0 } });
  server.accept({ id: 3, method: "fail", params: {} });
  await server.waitForIdle();
  assert.deepEqual(frames.map((frame) => frame.id), [3, 2, 1]);
  assert.equal(frames[0].error.code, "internal_error");
  assert.deepEqual(frames[1].result, { method: "fast" });
});
