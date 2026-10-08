import assert from "node:assert/strict";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { piAgentDirectory } from "../src/agent-directory.mjs";
import { authMain } from "../src/auth-cli.mjs";
import { PiWorkerRuntime } from "../src/runtime.mjs";
import { createFakeSdk, temporaryLayout } from "./helpers.mjs";

for (const [label, env, suffix] of [
  ["Bello override", { BELLO_PI_AGENT_DIR: "bello", PI_CODING_AGENT_DIR: "pi" }, "bello"],
  ["Pi override", { PI_CODING_AGENT_DIR: "pi" }, "pi"],
  ["empty Bello override", { BELLO_PI_AGENT_DIR: "", PI_CODING_AGENT_DIR: "pi" }, "pi"],
  ["default", {}, ".pi/agent"],
  ["empty overrides", { BELLO_PI_AGENT_DIR: "", PI_CODING_AGENT_DIR: "" }, ".pi/agent"],
  ["tilde", { BELLO_PI_AGENT_DIR: "~/custom" }, "custom"],
]) {
  test(`agent directory resolver honors ${label}`, () => {
    const base = resolve("isolated-home");
    assert.equal(piAgentDirectory({ env, cwd: base, home: base }), resolve(base, suffix));
  });
}

for (const selected of ["bello", "pi"]) {
  test(`login and direct worker share the ${selected} override`, async (t) => {
    const layout = temporaryLayout(t);
    const env = {
      BELLO_PI_AGENT_DIR: selected === "bello" ? layout.agentDir : "",
      PI_CODING_AGENT_DIR: selected === "bello" ? join(layout.root, "unselected") : layout.agentDir,
    };
    const previous = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]));
    Object.assign(process.env, env);
    t.after(() => {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    });
    let loginOptions;
    let loginProvider;
    const status = await authMain({ argv: ["bello-local"], env, output: { write() {} },
      runtimeFactory: async (options) => {
        loginOptions = options;
        return { getProvider: () => ({ name: "Local", auth: { apiKey: { login() {} } } }),
          login: async (provider, method) => { loginProvider = [provider, method]; } };
      },
    });
    const sdk = createFakeSdk();
    let runtimeOptions;
    const create = sdk.createModelRuntime;
    sdk.createModelRuntime = async (options) => { runtimeOptions = options; return create(options); };
    const runtime = new PiWorkerRuntime({ sdk, emit: async () => {}, hostCalls: {} });
    t.after(() => runtime.close());
    await runtime.dispatch("initialize", { stateDir: layout.stateDir });
    assert.equal(status, 0);
    assert.deepEqual(loginProvider, ["bello-local", "api_key"]);
    assert.equal(loginOptions.authPath, join(runtimeOptions.agentDir, "auth.json"));
    assert.equal(loginOptions.modelsPath, join(runtimeOptions.agentDir, "models.json"));
    assert.equal(runtimeOptions.agentDir, layout.agentDir);
    assert.equal(loginOptions.allowModelNetwork, false);
    assert.equal(loginOptions.refreshOnCreate, false);
    assert.equal(runtimeOptions.allowModelNetwork, false);
  });
}

test("default uses the OS home rather than worker state or a missing HOME variable", () => {
  assert.equal(piAgentDirectory({ env: {} }), join(homedir(), ".pi", "agent"));
});
