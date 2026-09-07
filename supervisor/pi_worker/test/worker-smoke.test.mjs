import assert from "node:assert/strict";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { temporaryLayout, waitFor } from "./helpers.mjs";

const worker = fileURLToPath(new URL("../worker.mjs", import.meta.url));

test("the real pinned SDK worker initializes and reads its offline model/account metadata", async (t) => {
  const layout = temporaryLayout(t);
  const child = spawn(process.execPath, [worker], { stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdin.write(`${JSON.stringify({ id: 1, method: "initialize", params: {
      stateDir: layout.stateDir,
      agentDir: layout.agentDir,
      allowModelNetwork: false,
    } })}\n`);
  await waitFor(() => stdout.includes("\n"));
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
  const [code] = await once(child, "close");
  assert.equal(code, 0, stderr);
  const frames = stdout.trim().split("\n").map((line) => JSON.parse(line));
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
