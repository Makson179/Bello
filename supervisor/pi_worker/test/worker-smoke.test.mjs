import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { temporaryLayout } from "./helpers.mjs";
import { spawnTestProcess } from "./worker-process.mjs";

const worker = fileURLToPath(new URL("../worker.mjs", import.meta.url));

test("the real pinned SDK worker initializes and reads its offline model/account metadata", { timeout: 75_000 }, async (t) => {
  const running = spawnTestProcess(t, process.execPath, [worker]);
  const layout = temporaryLayout(t);
  const { child } = running;
  child.stdin.write(`${JSON.stringify({ id: 1, method: "initialize", params: {
      stateDir: layout.stateDir,
      agentDir: layout.agentDir,
      allowModelNetwork: false,
    } })}\n`);
  await running.waitForOutput((stdout) => stdout.includes("\n"));
  const first = JSON.parse(running.stdout.slice(0, running.stdout.indexOf("\n")));
  assert.equal(first.id, 1, running.stderr);
  assert.equal(first.error, undefined, JSON.stringify(first));
  child.stdin.end([
    JSON.stringify({ id: 2, method: "model/list", params: {} }),
    JSON.stringify({ id: 3, method: "account/read", params: {} }),
    JSON.stringify({ id: 4, method: "model/list", params: { includeUnconfigured: true } }),
    JSON.stringify({ id: 5, method: "model/validate", params: {
      provider: "openai-codex",
      model: "gpt-5.6-sol",
      effort: "ultra",
    } }),
    "",
  ].join("\n"));
  const { code, signal } = await running.waitForClose();
  assert.equal(signal, null, running.stderr);
  assert.equal(code, 0, running.stderr);
  const frames = running.stdout.trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(frames.map((frame) => frame.id).sort(), [1, 2, 3, 4, 5]);
  const initialized = frames.find((frame) => frame.id === 1);
  assert.equal(initialized.result.serverInfo.piSdkVersion, "0.85.1");
  const listed = frames.find((frame) => frame.id === 2).result;
  assert.deepEqual(listed.data, []);
  assert.equal(Object.hasOwn(listed, "catalog"), false);
  assert.equal(listed.providers.find((provider) => provider.id === "openai-codex").configured, false);
  const account = frames.find((frame) => frame.id === 3).result.account;
  assert.equal(account.type, "pi");
  assert.equal(account.configured, false);
  assert.deepEqual(account.configuredProviders, []);
  const catalog = frames.find((frame) => frame.id === 4).result.catalog;
  const sol = catalog.find((model) => model.qualifiedId === "openai-codex/gpt-5.6-sol");
  assert.equal(sol.configured, false);
  assert.equal(sol.supportedEfforts.includes("ultra"), true);
  assert.equal(sol.supportedEfforts.includes("minimal"), false);
  assert.equal(sol.defaultEffort, "medium");
  assert.equal(sol.effortRoutes.ultra.providerReasoningEffort, "ultra");
  assert.equal(sol.effortRoutes.ultra.mappingStatus, "exact");
  assert.deepEqual(sol.inputModalities, ["text", "image"]);
  const validation = frames.find((frame) => frame.id === 5);
  assert.equal(validation.error.code, "provider_not_configured");
  assert.equal(Object.hasOwn(validation, "result"), false);
});

test("worker startup failure closes the child and does not strand the node test runner", { timeout: 40_000 }, async (t) => {
  const layout = temporaryLayout(t);
  const fixture = join(layout.root, "startup-failure.test.mjs");
  const helper = new URL("./worker-process.mjs", import.meta.url).href;
  writeFileSync(fixture, [
    'import test from "node:test";',
    `import { spawnTestProcess } from ${JSON.stringify(helper)};`,
    'test("intentional startup failure", async (t) => {',
    '  const running = spawnTestProcess(t, process.execPath, ["-e",',
    '    "process.stdout.write(\\\"READY\\\\n\\\"); process.stdin.resume();"]);',
    '  await running.waitForOutput((output) => output.includes("READY"));',
    '  await running.waitForOutput(() => false, 20);',
    '});',
    "",
  ].join("\n"), "utf8");
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  const running = spawnTestProcess(t, process.execPath, ["--test", fixture], { env });
  const { code, signal } = await running.waitForClose();
  assert.equal(signal, null, running.stderr);
  assert.equal(code, 1, running.stdout + running.stderr);
  assert.match(running.stdout, /intentional startup failure/);
  assert.match(running.stdout, /timed out after 20 ms waiting for worker output/);
  assert.match(running.stdout, /READY/);
});

test("worker spawn errors fail promptly and remain cleanable", async (t) => {
  const running = spawnTestProcess(t, `${process.execPath}.does-not-exist`, []);
  await assert.rejects(running.waitForOutput(() => false), /process error=.*ENOENT/);
  await running.cleanup();
  assert.notEqual((await running.waitForClose()).code, 0);
});

test("early worker exits include stderr and close waits are race-free", async (t) => {
  const running = spawnTestProcess(t, process.execPath, ["-e", 'process.stderr.write("early shutdown"); process.exit(17);']);
  await assert.rejects(running.waitForOutput(() => false), /early shutdown/);
  assert.equal((await running.waitForClose()).code, 17);
  assert.equal((await running.waitForClose()).code, 17);
  await running.cleanup();
});
