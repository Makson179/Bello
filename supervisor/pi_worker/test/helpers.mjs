import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { realPiSdk } from "../src/pi-sdk.mjs";

export const TEST_TOOL = {
  name: "exec_command",
  description: "Run one test command through the Bello host.",
  parameters: {
    type: "object",
    properties: { command: { type: "string" } },
    required: ["command"],
    additionalProperties: false,
  },
};

export function temporaryLayout(t) {
  const root = mkdtempSync(join(tmpdir(), "bello-pi-worker-test-"));
  const workspace = join(root, "workspace");
  const stateDir = join(root, "private-state");
  const agentDir = join(root, "agent-config");
  mkdirSync(workspace);
  mkdirSync(agentDir);
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return { root, workspace, stateDir, agentDir };
}

export function fakeModel(overrides = {}) {
  return {
    id: "gpt-test",
    name: "GPT Test",
    provider: "openai-codex",
    api: "openai-codex-responses",
    reasoning: true,
    input: ["text", "image"],
    thinkingLevelMap: { xhigh: "xhigh", max: "max", ultra: "ultra" },
    supportedEfforts: ["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    ...overrides,
  };
}

class FakeModelRuntime {
  constructor(models) {
    this.models = models;
  }

  getAvailableSnapshot() {
    return this.models;
  }

  getModels() {
    return this.models;
  }

  getProviders() {
    return [...new Set(this.models.map((model) => model.provider))].map((id) => ({
      id,
      name: id,
      auth: { oauth: { login: async () => {} } },
    }));
  }

  getProviderAuthStatus(provider) {
    return { configured: this.hasConfiguredAuth(provider), source: "test" };
  }

  getModel(provider, id) {
    return this.models.find((model) => model.provider === provider && model.id === id);
  }

  hasConfiguredAuth(provider) {
    return this.models.some((model) => model.provider === provider);
  }

  async checkAuth(provider) {
    return this.hasConfiguredAuth(provider) ? { type: "test" } : undefined;
  }

  getError() {
    return undefined;
  }
}

class FakeSessionManager {
  constructor({ cwd, sessionDir, threadId, sessionFile }) {
    this.cwd = cwd;
    this.threadId = threadId;
    this.sessionFile = sessionFile ?? join(sessionDir, `${threadId}.jsonl`);
  }

  getSessionId() {
    return this.threadId;
  }

  getSessionFile() {
    return this.sessionFile;
  }
}

export class FakeSession {
  constructor(options, behavior) {
    this.options = options;
    this.registry = new Map(options.customTools.map((tool) => [tool.name, tool]));
    this.state = { tools: [] };
    this.thinkingLevel = options.thinkingLevel;
    this.listeners = new Set();
    this.behavior = behavior;
    this.abortController = undefined;
    this.promptPromise = undefined;
    this.steering = [];
    this.disposed = false;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(event) {
    for (const listener of this.listeners) listener(event);
  }

  emitAssistant(text, {
    stopReason = "stop",
    errorMessage,
    usage,
    provider,
    model,
    api,
    responseModel,
    providerThinkingLevel,
  } = {}) {
    const message = {
      role: "assistant",
      content: text === undefined ? [] : [{ type: "text", text }],
      stopReason,
      ...(errorMessage ? { errorMessage } : {}),
      ...(usage ? { usage } : {}),
      ...(provider ? { provider } : {}),
      ...(model ? { model } : {}),
      ...(api ? { api } : {}),
      ...(responseModel ? { responseModel } : {}),
      ...(providerThinkingLevel ? { providerThinkingLevel } : {}),
    };
    this.emit({ type: "message_start", message });
    this.emit({ type: "message_end", message });
  }

  setActiveToolsByName(names) {
    this.state.tools = names.flatMap((name) => {
      const tool = this.registry.get(name);
      return tool ? [tool] : [];
    });
  }

  setThinkingLevel(level) {
    this.thinkingLevel = level;
  }

  async prompt(input) {
    this.abortController = new AbortController();
    this.promptPromise = this.behavior(this, input, this.abortController.signal);
    return this.promptPromise;
  }

  async steer(text) {
    this.steering.push(text);
  }

  async abort() {
    this.abortController?.abort();
    await this.promptPromise?.catch(() => {});
  }

  dispose() {
    this.disposed = true;
    this.abortController?.abort();
  }
}

export function createFakeSdk({ models = [fakeModel()], behavior = async (session) => session.emitAssistant("done") } = {}) {
  const created = [];
  const modelRuntime = new FakeModelRuntime(models);
  return {
    version: "0.85.1-test",
    created,
    modelRuntime,
    async createModelRuntime() {
      return modelRuntime;
    },
    createSessionManager(options) {
      return new FakeSessionManager(options);
    },
    async createSession(options) {
      const session = new FakeSession(options, behavior);
      session.setActiveToolsByName(options.activeToolNames);
      created.push(session);
      return session;
    },
    supportedEfforts(model) {
      return this.catalogEfforts(model).filter(
        (effort) => this.effortRoute(model, effort).mappingStatus !== "alias",
      );
    },
    catalogEfforts(model) {
      return model.supportedEfforts;
    },
    defaultEffort(model) {
      const supported = this.supportedEfforts(model);
      return ["medium", "high", "xhigh", "max", "low", "minimal", "off"]
        .find((effort) => supported.includes(effort));
    },
    effortCapabilitySource(model, effort) {
      return effort === "ultra" && !Object.hasOwn(model.thinkingLevelMap ?? {}, "ultra")
        ? "bello-0.5.2-codex-compatibility"
        : "pi-catalog";
    },
    supportsServiceTier(model) {
      return model.api.includes("openai") && model.api.includes("responses");
    },
    supportedServiceTiers(model) {
      return this.supportsServiceTier(model) ? ["auto", "default", "flex", "scale", "priority"] : [];
    },
    nativeEffort(_model, effort) {
      return effort === "ultra" ? "ultra" : undefined;
    },
    bootstrapEffort(_model, effort) {
      return effort === "ultra" ? "max" : effort;
    },
    effortRoute(model, effort) {
      const piThinkingLevel = this.bootstrapEffort(model, effort);
      if (effort === "off") {
        return {
          piThinkingLevel,
          providerControl: "reasoning_omitted",
          providerReasoningEffort: null,
          mappingStatus: "exact",
        };
      }
      const providerReasoningEffort = this.nativeEffort(model, effort)
        ?? model.thinkingLevelMap?.[effort]
        ?? effort;
      return {
        piThinkingLevel,
        providerControl: "reasoning_effort",
        providerReasoningEffort,
        mappingStatus: providerReasoningEffort === effort ? "exact" : "alias",
      };
    },
    validateSchema(schema, value) {
      return realPiSdk.validateSchema(schema, value);
    },
  };
}

export async function waitFor(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = predicate();
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("timed out waiting for test condition");
}
