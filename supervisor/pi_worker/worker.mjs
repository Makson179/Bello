#!/usr/bin/env node

import { createInterface } from "node:readline";

import { realPiSdk } from "./src/pi-sdk.mjs";
import { HostCallBroker, JsonlWriter, WorkerServer } from "./src/protocol.mjs";
import { PiWorkerRuntime } from "./src/runtime.mjs";

const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 19)) {
  process.stderr.write(`Bello Pi worker requires Node.js >=22.19.0; found ${process.versions.node}.\n`);
  process.exit(1);
}

const writer = new JsonlWriter(process.stdout);
const hostCalls = new HostCallBroker((message) => writer.send(message));
const runtime = new PiWorkerRuntime({
  sdk: realPiSdk,
  emit: (message) => writer.send(message),
  hostCalls,
});
const server = new WorkerServer({ runtime, writer, hostCalls });
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });

for await (const line of lines) {
  if (!line.trim()) continue;
  try {
    server.accept(JSON.parse(line));
  } catch (error) {
    await writer.send({
      method: "worker/error",
      params: { message: `invalid JSON: ${error instanceof Error ? error.message : String(error)}` },
    });
  }
}

await server.waitForIdle();
await runtime.close();
hostCalls.close();
