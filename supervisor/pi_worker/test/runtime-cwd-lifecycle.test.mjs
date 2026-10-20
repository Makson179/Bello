import assert from "node:assert/strict";
import { mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { PiWorkerRuntime } from "../src/runtime.mjs";
import { createFakeSdk, temporaryLayout, TEST_TOOL, waitFor } from "./helpers.mjs";

function worker(t) {
  const sdk = createFakeSdk();
  const events = [];
  const runtime = new PiWorkerRuntime({
    sdk,
    emit: async (event) => events.push(event),
    hostCalls: { async call() { throw new Error("No host tools expected in this neutral test"); } },
  });
  t.after(() => runtime.close());
  return { runtime, sdk, events };
}

async function initialize(runtime, layout) {
  await runtime.dispatch("initialize", { stateDir: layout.stateDir, agentDir: layout.agentDir });
}

async function start(runtime, threadId, cwd) {
  return runtime.dispatch("thread/start", {
    threadId, cwd, provider: "openai-codex", model: "gpt-test", effort: "high", tools: [TEST_TOOL],
  });
}

async function archivedFixture(t) {
  const layout = temporaryLayout(t);
  const temporaryCwd = join(layout.root, "neutral-startup-review");
  mkdirSync(temporaryCwd);
  const first = worker(t);
  await initialize(first.runtime, layout);
  await start(first.runtime, "review", temporaryCwd);
  await first.runtime.dispatch("thread/archive", { threadId: "review" });
  await first.runtime.close();
  rmSync(temporaryCwd, { recursive: true });
  return { ...layout, temporaryCwd };
}

test("worker restart loads archived review history after its temporary cwd is removed", async (t) => {
  const layout = await archivedFixture(t);
  const { runtime, sdk, events } = worker(t);
  await initialize(runtime, layout);
  assert.equal(sdk.created.length, 0, "metadata loading must not reopen archived sessions");
  const history = await runtime.dispatch("thread/read", { threadId: "review" });
  assert.equal(history.thread.status, "archived");
  assert.equal((await runtime.dispatch("thread/list", {})).data.length, 0);
  assert.equal((await runtime.dispatch("thread/list", { includeArchived: true })).data.length, 1);
  await assert.rejects(runtime.dispatch("thread/resume", { threadId: "review" }), {
    code: "invalid_params", message: /thread cwd is not accessible/,
  });
  assert.equal(runtime.getRecord("review").meta.archived, true);
  assert.equal(runtime.getRecord("review").meta.status, "archived");
  assert.equal(sdk.created.length, 0);
  await assert.rejects(runtime.dispatch("turn/start", {
    threadId: "review", turnId: "never", input: "neutral test",
  }), { code: "thread_archived" });

  // An unrelated live workspace still resumes and executes with its exact
  // original scope and tool contract; the missing review cwd is no blocker.
  await start(runtime, "coder", layout.workspace);
  const record = runtime.getRecord("coder");
  const originalCwd = realpathSync(layout.workspace);
  await runtime.dispatch("thread/resume", { threadId: "coder", cwd: layout.workspace, tools: [TEST_TOOL] });
  assert.equal(record.meta.cwd, originalCwd);
  assert.deepEqual(record.meta.tools, [TEST_TOOL]);
  await runtime.dispatch("turn/start", { threadId: "coder", turnId: "neutral", input: "Say done" });
  await waitFor(() => events.some((event) => event.method === "turn/completed"));
  assert.equal(record.meta.turns[0].status, "completed");
  assert.equal(sdk.created[0].options.cwd, originalCwd);
});

test("archived missing cwd exception still rejects relative paths", async (t) => {
  const layout = await archivedFixture(t);
  const path = join(layout.stateDir, "threads", "review.json");
  const meta = JSON.parse(readFileSync(path, "utf8"));
  meta.cwd = "../relative-workspace";
  writeFileSync(path, JSON.stringify(meta));
  const { runtime, sdk } = worker(t);
  await assert.rejects(initialize(runtime, layout), { code: "invalid_params", message: /cwd must be absolute/ });
  assert.equal(sdk.created.length, 0);
});

test("nonarchived missing cwd still fails metadata validation", async (t) => {
  const layout = temporaryLayout(t);
  const first = worker(t);
  await initialize(first.runtime, layout);
  await start(first.runtime, "coder", layout.workspace);
  await first.runtime.close();
  rmSync(layout.workspace, { recursive: true });
  const { runtime, sdk } = worker(t);
  await assert.rejects(initialize(runtime, layout), { code: "invalid_params", message: /cwd is not accessible/ });
  assert.equal(sdk.created.length, 0);
});

for (const replacement of ["removed", "redirected"]) {
  test(`execution checks ${replacement} cwd even with an already loaded session`, async (t) => {
    const layout = temporaryLayout(t);
    const { runtime, sdk } = worker(t);
    await initialize(runtime, layout);
    await start(runtime, "coder", layout.workspace);
    const record = runtime.getRecord("coder");
    const originalCwd = record.meta.cwd;
    const originalMetadata = readFileSync(runtime.metadataPath("coder"), "utf8");
    rmSync(layout.workspace, { recursive: true });
    if (replacement === "redirected") {
      const other = join(layout.root, "different-workspace");
      mkdirSync(other);
      try {
        symlinkSync(other, layout.workspace, "dir");
      } catch (error) {
        // Some hosts (for example Windows without symlink rights) cannot create one.
        if (error?.code === "EPERM" || error?.code === "EACCES") return t.skip("directory symlinks are not permitted on this host");
        throw error;
      }
    }
    const code = replacement === "removed" ? "invalid_params" : "scope_mismatch";
    await assert.rejects(runtime.loadSession(record), { code });
    await assert.rejects(runtime.dispatch("thread/resume", { threadId: "coder" }), { code });
    await assert.rejects(runtime.dispatch("turn/start", {
      threadId: "coder", turnId: "never", input: "neutral test",
    }), { code });
    assert.equal(record.meta.cwd, originalCwd);
    assert.equal(record.meta.turns.length, 0);
    assert.equal(record.active, undefined);
    assert.equal(sdk.created.length, 1, "no new session or model turn may run");
    assert.equal(readFileSync(runtime.metadataPath("coder"), "utf8"), originalMetadata);
  });
}

test("archived workspace may be restored at its original path before explicit resume", async (t) => {
  const layout = await archivedFixture(t);
  const { runtime, sdk } = worker(t);
  await initialize(runtime, layout);
  mkdirSync(layout.temporaryCwd);
  await runtime.dispatch("thread/resume", { threadId: "review" });
  assert.equal(runtime.getRecord("review").meta.archived, false);
  assert.equal(runtime.getRecord("review").meta.cwd, realpathSync(layout.temporaryCwd));
  assert.equal(sdk.created.length, 1);
});
