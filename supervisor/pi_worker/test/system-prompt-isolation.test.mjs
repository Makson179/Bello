import assert from "node:assert/strict";
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { join, resolve } from "node:path";
import test from "node:test";

import { SessionManager } from "@earendil-works/pi-coding-agent";

import { realPiSdk } from "../src/pi-sdk.mjs";
import { temporaryLayout, TEST_TOOL } from "./helpers.mjs";

const DEVELOPER_INSTRUCTIONS = "BELLO_EXPLICIT_DEVELOPER_INSTRUCTIONS_SENTINEL";
const EXPLICIT_SYSTEM_PROMPT = "BELLO_EXPLICIT_SYSTEM_PROMPT_SENTINEL";
const AMBIENT_SENTINEL = "UNREQUESTED_SYSTEM_FILE_SENTINEL";

async function createOfflineSession(t, layout, systemPrompt) {
  const modelRuntime = await realPiSdk.createModelRuntime({
    agentDir: layout.agentDir,
    allowModelNetwork: false,
  });
  const model = modelRuntime.getModel("openai-codex", "gpt-5.6-sol");
  assert.ok(model, "the pinned offline catalog must contain the selected model");
  const session = await realPiSdk.createSession({
    cwd: layout.workspace,
    agentDir: layout.agentDir,
    modelRuntime,
    model,
    thinkingLevel: "high",
    sessionManager: SessionManager.inMemory(layout.workspace),
    customTools: [{
      ...TEST_TOOL,
      label: "Bello test command",
      execute: async () => { throw new Error("this test must not execute tools"); },
    }],
    activeToolNames: [TEST_TOOL.name],
    developerInstructions: DEVELOPER_INSTRUCTIONS,
    systemPrompt,
    requestOptions: { current: {} },
  });
  t.after(() => session.dispose());
  return session;
}

function watchAmbientFileReads(t, paths) {
  const watchedPaths = new Set(paths.map((path) => resolve(path)));
  const reads = [];
  const original = fs.readFileSync;
  const spy = t.mock.method(fs, "readFileSync", function (path, ...args) {
    if (typeof path === "string" && watchedPaths.has(resolve(path))) reads.push(path);
    return original.call(this, path, ...args);
  });
  syncBuiltinESMExports();
  t.after(() => {
    spy.mock.restore();
    syncBuiltinESMExports();
  });
  return reads;
}

function assertSessionScope(session, explicitSystemPrompt) {
  assert.ok(session.systemPrompt.includes(DEVELOPER_INSTRUCTIONS));
  assert.ok(!session.systemPrompt.includes(AMBIENT_SENTINEL));
  if (explicitSystemPrompt) {
    assert.ok(session.systemPrompt.includes(EXPLICIT_SYSTEM_PROMPT));
  } else {
    // Disabling ambient files must not silently remove Pi's generic base prompt.
    assert.ok(session.systemPrompt.includes("operating inside pi"));
  }
  assert.deepEqual(session.getActiveToolNames(), [TEST_TOOL.name]);
  assert.equal(session.getToolDefinition(TEST_TOOL.name).description, TEST_TOOL.description);
  assert.deepEqual(session.getToolDefinition(TEST_TOOL.name).parameters, TEST_TOOL.parameters);
}

for (const location of ["project", "agent", "both"]) {
  for (const [label, systemPrompt] of [
    ["omitted", undefined],
    ["empty", ""],
    ["blank", "   "],
    ["explicit", EXPLICIT_SYSTEM_PROMPT],
  ]) {
    test(`real Pi session ignores ${location} SYSTEM files with ${label} system prompt`, async (t) => {
      const layout = temporaryLayout(t);
      fs.mkdirSync(join(layout.workspace, ".pi"));
      const paths = [];
      if (location !== "agent") paths.push(join(layout.workspace, ".pi", "SYSTEM.md"));
      if (location !== "project") paths.push(join(layout.agentDir, "SYSTEM.md"));
      // APPEND_SYSTEM.md must remain disabled too; explicit developer instructions
      // continue to be supplied through Bello, not discovered from the filesystem.
      paths.push(join(layout.workspace, ".pi", "APPEND_SYSTEM.md"));
      paths.push(join(layout.agentDir, "APPEND_SYSTEM.md"));
      for (const path of paths) fs.writeFileSync(path, AMBIENT_SENTINEL);
      const reads = watchAmbientFileReads(t, paths);
      const session = await createOfflineSession(t, layout, systemPrompt);

      assertSessionScope(session, label === "explicit");
      assert.deepEqual(reads, [], "ambient prompt files must not even be read");

      await session.reload();
      assertSessionScope(session, label === "explicit");
      assert.deepEqual(reads, [], "reloading must not discover ambient prompt files");
    });
  }
}
