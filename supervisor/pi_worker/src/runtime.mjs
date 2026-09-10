import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";

import { ProtocolError } from "./protocol.mjs";

const STATE_VERSION = 1;
const ID_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$/;
const TOOL_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;
const STANDARD_EFFORTS = new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);
const RESERVED_TOOL_NAME = "submit_result";

function requireString(value, name) {
  if (typeof value !== "string" || !value.trim()) {
    throw new ProtocolError(`${name} must be a nonempty string`, "invalid_params");
  }
  return value;
}

function requireId(value, name) {
  const id = requireString(value, name);
  if (!ID_PATTERN.test(id)) {
    throw new ProtocolError(
      `${name} must start and end with an alphanumeric character and contain only letters, digits, '.', '_' or '-'`,
      "invalid_params",
    );
  }
  return id;
}

function requireObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolError(`${name} must be an object`, "invalid_params");
  }
  return value;
}

function cloneJson(value, name = "value") {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (error) {
    throw new ProtocolError(`${name} must be JSON-serializable: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function canonicalPath(path, name, mustExist = true) {
  const raw = requireString(path, name);
  if (!isAbsolute(raw)) throw new ProtocolError(`${name} must be absolute`, "invalid_params");
  const resolved = resolve(raw);
  if (!mustExist) return resolved;
  try {
    return realpathSync(resolved);
  } catch (error) {
    throw new ProtocolError(`${name} is not accessible: ${error instanceof Error ? error.message : String(error)}`, "invalid_params");
  }
}

function isInside(parent, child) {
  const path = relative(parent, child);
  return path === "" || (!path.startsWith("..") && !isAbsolute(path));
}

function timestamp() {
  return new Date().toISOString();
}

function normalizeInput(value) {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) throw new ProtocolError("input must be a string or an array of text blocks", "invalid_params");
  const parts = [];
  for (const block of value) {
    if (typeof block === "string") {
      parts.push(block);
    } else if (block && typeof block === "object" && typeof block.text === "string") {
      parts.push(block.text);
    } else {
      throw new ProtocolError("input contains a non-text block", "invalid_params");
    }
  }
  const text = parts.join("\n");
  if (!text) throw new ProtocolError("input must contain text", "invalid_params");
  return text;
}

function normalizeTools(value) {
  if (!Array.isArray(value)) throw new ProtocolError("tools must be an array", "invalid_params");
  const names = new Set();
  return value.map((raw, index) => {
    const tool = requireObject(raw, `tools[${index}]`);
    const name = requireString(tool.name, `tools[${index}].name`);
    if (!TOOL_NAME_PATTERN.test(name)) {
      throw new ProtocolError(`invalid tool name: ${name}`, "invalid_params");
    }
    if (name === RESERVED_TOOL_NAME) {
      throw new ProtocolError(`${RESERVED_TOOL_NAME} is reserved by the worker`, "invalid_params");
    }
    if (names.has(name)) throw new ProtocolError(`duplicate tool name: ${name}`, "invalid_params");
    names.add(name);
    const description = requireString(tool.description, `tools[${index}].description`);
    const parameters = requireObject(tool.parameters, `tools[${index}].parameters`);
    return { name, description, parameters: cloneJson(parameters, `${name} parameters`) };
  });
}

function inputModalities(model) {
  if (!Array.isArray(model?.input)) return [];
  return [...new Set(model.input.filter((value) => typeof value === "string" && value))];
}

function toolsForModel(tools, model) {
  if (inputModalities(model).includes("image")) return tools;
  return tools.filter((tool) => tool.name !== "view_image");
}

function extractText(message) {
  if (!message || message.role !== "assistant" || !Array.isArray(message.content)) return "";
  return message.content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

function resultText(result) {
  if (!result || !Array.isArray(result.content)) return "Host tool failed without a textual result.";
  const text = result.content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n");
  return text || "Host tool failed without a textual result.";
}

function reportedNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function assistantUsage(message, itemId) {
  if (!message || message.role !== "assistant" || !message.usage || typeof message.usage !== "object") {
    return undefined;
  }
  const usage = {};
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "cacheWrite1h", "reasoning", "totalTokens"]) {
    const value = reportedNumber(message.usage[key]);
    if (value !== undefined) usage[key] = value;
  }
  if (message.usage.cost && typeof message.usage.cost === "object") {
    const cost = {};
    for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
      const value = reportedNumber(message.usage.cost[key]);
      if (value !== undefined) cost[key] = value;
    }
    if (Object.keys(cost).length > 0) usage.cost = cost;
  }
  if (Object.keys(usage).length === 0) return undefined;
  const response = { itemId, usage };
  for (const key of ["provider", "model", "api", "responseModel", "providerThinkingLevel"]) {
    if (typeof message[key] === "string" && message[key]) response[key] = message[key];
  }
  return response;
}

function aggregateUsage(responses) {
  const totals = {};
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "cacheWrite1h", "reasoning", "totalTokens"]) {
    const reported = responses
      .map((response) => response.usage[key])
      .filter((value) => typeof value === "number");
    if (reported.length > 0) totals[key] = reported.reduce((sum, value) => sum + value, 0);
  }
  const cost = {};
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
    const reported = responses
      .map((response) => response.usage.cost?.[key])
      .filter((value) => typeof value === "number");
    if (reported.length > 0) cost[key] = reported.reduce((sum, value) => sum + value, 0);
  }
  if (Object.keys(cost).length > 0) totals.cost = cost;
  return { ...totals, responses: cloneJson(responses, "provider usage responses") };
}

function normalizeHostResult(result) {
  const value = requireObject(result, "host tool result");
  if (!Array.isArray(value.content) || value.content.length === 0) {
    throw new ProtocolError("host tool result.content must be a nonempty array", "host_tool_error");
  }
  const content = value.content.map((raw, index) => {
    const block = requireObject(raw, `host tool result.content[${index}]`);
    if (block.type === "text" && typeof block.text === "string") return { type: "text", text: block.text };
    if (block.type === "image" && typeof block.data === "string" && typeof block.mimeType === "string") {
      return { type: "image", data: block.data, mimeType: block.mimeType };
    }
    throw new ProtocolError("host tool result contains an unsupported content block", "host_tool_error");
  });
  return {
    content,
    details: value.details === undefined ? {} : cloneJson(value.details, "host tool result.details"),
    isError: value.isError === true,
  };
}

function publicTurn(turn) {
  return cloneJson({
    id: turn.id,
    status: turn.status,
    items: turn.items,
    startedAt: turn.startedAt,
    ...(turn.completedAt ? { completedAt: turn.completedAt } : {}),
    ...(turn.error ? { error: turn.error } : {}),
    ...(turn.structuredResult !== undefined ? { structuredResult: turn.structuredResult } : {}),
    ...(turn.usage ? { usage: turn.usage } : {}),
  });
}

function publicThread(meta, includeTurns = true) {
  return cloneJson({
    id: meta.threadId,
    status: meta.archived ? "archived" : meta.status,
    cwd: meta.cwd,
    provider: meta.provider,
    model: meta.model,
    qualifiedModel: `${meta.provider}/${meta.model}`,
    reasoningEffort: meta.effort,
    ...(meta.parentThreadId ? { parentThreadId: meta.parentThreadId } : {}),
    ...(includeTurns ? { turns: meta.turns.map(publicTurn) } : {}),
  });
}

function normalizeServiceTier(value) {
  if (value === undefined || value === null) return undefined;
  return requireString(value, "serviceTier");
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export class PiWorkerRuntime {
  constructor({ sdk, emit, hostCalls, schedule = setImmediate }) {
    this.sdk = sdk;
    this.emit = emit;
    this.hostCalls = hostCalls;
    this.schedule = schedule;
    this.initialized = false;
    this.stateDir = undefined;
    this.agentDir = undefined;
    this.threadsDir = undefined;
    this.sessionsDir = undefined;
    this.modelRuntime = undefined;
    this.threads = new Map();
    this.writeSequence = 0;
    this.closing = false;
  }

  async dispatch(method, params) {
    if (method === "initialize") return this.initialize(params);
    if (!this.initialized) throw new ProtocolError("initialize must be called first", "not_initialized");
    const methods = {
      "model/list": () => this.modelList(params),
      "model/validate": () => this.modelValidate(params),
      "account/read": () => this.accountRead(params),
      "thread/start": () => this.threadStart(params),
      "thread/resume": () => this.threadResume(params),
      "thread/read": () => this.threadRead(params),
      "thread/list": () => this.threadList(params),
      "thread/turns/list": () => this.threadTurnsList(params),
      "thread/archive": () => this.threadArchive(params),
      "thread/unsubscribe": () => this.threadUnsubscribe(params),
      "turn/start": () => this.turnStart(params),
      "turn/steer": () => this.turnSteer(params),
      "turn/interrupt": () => this.turnInterrupt(params),
    };
    const handler = methods[method];
    if (!handler) throw new ProtocolError(`unsupported worker method: ${method}`, "method_not_found");
    return handler();
  }

  async initialize(params) {
    if (this.initialized) {
      const requested = canonicalPath(params.stateDir, "stateDir", false);
      if (requested !== this.stateDir) throw new ProtocolError("worker is already initialized with a different stateDir");
      return this.initializeResult();
    }
    const stateDir = canonicalPath(params.stateDir, "stateDir", false);
    mkdirSync(stateDir, { recursive: true, mode: 0o700 });
    this.stateDir = realpathSync(stateDir);
    this.threadsDir = join(this.stateDir, "threads");
    this.sessionsDir = join(this.stateDir, "sessions");
    mkdirSync(this.threadsDir, { recursive: true, mode: 0o700 });
    mkdirSync(this.sessionsDir, { recursive: true, mode: 0o700 });
    const defaultAgentDir = process.env.PI_CODING_AGENT_DIR || join(process.env.HOME || this.stateDir, ".pi", "agent");
    this.agentDir = canonicalPath(params.agentDir ?? defaultAgentDir, "agentDir", false);
    const allowModelNetwork = params.allowModelNetwork === true;
    this.modelRuntime = await this.sdk.createModelRuntime({ agentDir: this.agentDir, allowModelNetwork });
    this.loadMetadata();
    this.initialized = true;
    return this.initializeResult();
  }

  initializeResult() {
    return {
      serverInfo: { name: "bello-pi-worker", version: "0.6.0", piSdkVersion: this.sdk.version },
      protocolVersion: 1,
      capabilities: {
        multiplex: true,
        persistentSessions: true,
        hostTools: true,
        structuredOutputTool: RESERVED_TOOL_NAME,
        modelCapabilityPreflight: true,
        hostChosenThreadIds: true,
        hostChosenTurnIds: true,
      },
    };
  }

  loadMetadata() {
    for (const name of readdirSync(this.threadsDir)) {
      if (!name.endsWith(".json")) continue;
      const path = join(this.threadsDir, name);
      let meta;
      try {
        meta = JSON.parse(readFileSync(path, "utf8"));
      } catch (error) {
        throw new ProtocolError(`cannot read Pi thread metadata ${name}: ${error instanceof Error ? error.message : String(error)}`, "state_corrupt");
      }
      this.validateMetadata(meta, name);
      let changed = false;
      for (const turn of meta.turns) {
        if (turn.status === "inProgress") {
          turn.status = "interrupted";
          turn.completedAt = timestamp();
          turn.error = { message: "Pi worker restarted while this turn was active; it was not replayed." };
          for (const item of turn.items) {
            if (item && item.status === "inProgress") item.status = "interrupted";
          }
          changed = true;
        }
      }
      if (!meta.archived && meta.status === "inProgress") {
        meta.status = "idle";
        changed = true;
      }
      const record = this.makeRecord(meta);
      this.threads.set(meta.threadId, record);
      if (changed) this.save(record);
    }
  }

  validateMetadata(meta, filename) {
    requireObject(meta, `thread metadata ${filename}`);
    if (meta.version !== STATE_VERSION) throw new ProtocolError(`unsupported thread metadata version in ${filename}`, "state_corrupt");
    const threadId = requireId(meta.threadId, `thread metadata ${filename} id`);
    if (`${threadId}.json` !== filename) throw new ProtocolError(`thread metadata filename does not match its id: ${filename}`, "state_corrupt");
    canonicalPath(meta.cwd, `thread metadata ${filename} cwd`);
    requireString(meta.provider, `thread metadata ${filename} provider`);
    requireString(meta.model, `thread metadata ${filename} model`);
    normalizeTools(meta.tools);
    if (!Array.isArray(meta.turns)) throw new ProtocolError(`thread metadata ${filename} turns must be an array`, "state_corrupt");
    if (meta.sessionFile !== null && meta.sessionFile !== undefined) {
      const sessionFile = canonicalPath(meta.sessionFile, `thread metadata ${filename} sessionFile`, false);
      if (!isInside(this.sessionsDir, sessionFile)) {
        throw new ProtocolError(`thread metadata ${filename} points outside the private session directory`, "state_corrupt");
      }
    }
  }

  makeRecord(meta) {
    return {
      meta,
      session: undefined,
      unsubscribe: undefined,
      loadPromise: undefined,
      requestOptions: { current: undefined },
      active: undefined,
      assistantSequence: 0,
      currentAssistantItemId: undefined,
    };
  }

  metadataPath(threadId) {
    return join(this.threadsDir, `${requireId(threadId, "threadId")}.json`);
  }

  save(record) {
    record.meta.updatedAt = timestamp();
    const path = this.metadataPath(record.meta.threadId);
    const temporary = `${path}.${process.pid}.${++this.writeSequence}.tmp`;
    try {
      writeFileSync(temporary, `${JSON.stringify(record.meta, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
      renameSync(temporary, path);
    } catch (error) {
      try {
        if (existsSync(temporary)) unlinkSync(temporary);
      } catch {
        // Preserve the primary error.
      }
      throw error;
    }
  }

  getRecord(threadId) {
    const id = requireId(threadId, "threadId");
    const record = this.threads.get(id);
    if (!record) throw new ProtocolError(`unknown Pi thread: ${id}`, "thread_not_found");
    return record;
  }

  isModelAvailable(model) {
    return this.modelRuntime.getAvailableSnapshot().some(
      (candidate) => candidate.provider === model.provider && candidate.id === model.id,
    );
  }

  modelDescriptor(model, configured = this.isModelAvailable(model)) {
    const supportedEfforts = this.sdk.supportedEfforts(model);
    const effortCapabilitySources = Object.fromEntries(
      supportedEfforts.map((effort) => [effort, this.sdk.effortCapabilitySource(model, effort)]),
    );
    const effortRoutes = Object.fromEntries(
      supportedEfforts.map((effort) => [effort, this.sdk.effortRoute(model, effort)]),
    );
    return {
      id: model.id,
      model: model.id,
      provider: model.provider,
      qualifiedId: `${model.provider}/${model.id}`,
      name: model.name,
      api: model.api,
      reasoning: model.reasoning === true,
      inputModalities: inputModalities(model),
      supportedEfforts,
      defaultEffort: this.sdk.defaultEffort(model) ?? null,
      effortCapabilitySources,
      effortRoutes,
      supportsServiceTier: this.sdk.supportsServiceTier(model),
      supportedServiceTiers: this.sdk.supportedServiceTiers(model),
      configured,
    };
  }

  providerDescriptors() {
    if (typeof this.modelRuntime.getProviders !== "function") return [];
    return this.modelRuntime.getProviders().map((provider) => {
      const status = typeof this.modelRuntime.getProviderAuthStatus === "function"
        ? this.modelRuntime.getProviderAuthStatus(provider.id)
        : { configured: this.modelRuntime.hasConfiguredAuth(provider.id) };
      const loginMethods = [];
      if (provider.auth?.oauth?.login) loginMethods.push("oauth");
      if (provider.auth?.apiKey?.login) loginMethods.push("api_key");
      return {
        id: provider.id,
        name: provider.name,
        configured: status.configured === true,
        ...(typeof status.source === "string" ? { source: status.source } : {}),
        ...(typeof status.label === "string" ? { label: status.label } : {}),
        loginMethods,
      };
    });
  }

  async modelList(params = {}) {
    const available = this.modelRuntime.getAvailableSnapshot();
    const availableIds = new Set(available.map((model) => `${model.provider}\0${model.id}`));
    const result = {
      data: available.map((model) => this.modelDescriptor(model, true)),
      providers: this.providerDescriptors(),
      catalogError: this.modelRuntime.getError?.() ?? null,
    };
    if (params.includeUnconfigured === true) {
      result.catalog = this.modelRuntime.getModels().map((model) => (
        this.modelDescriptor(model, availableIds.has(`${model.provider}\0${model.id}`))
      ));
    }
    return result;
  }

  async accountRead() {
    const providerMetadata = this.providerDescriptors();
    const providers = providerMetadata.filter((provider) => provider.configured).map((provider) => provider.id).sort();
    return {
      account: {
        type: "pi",
        configured: providers.length > 0,
        configuredProviders: providers,
        providers: providerMetadata,
      },
      requiresOpenaiAuth: false,
    };
  }

  selection(params) {
    let provider = params.provider;
    let modelId = params.model;
    requireString(modelId, "model");
    if (typeof provider !== "string" || !provider) {
      if (modelId.includes("/")) {
        [provider, modelId] = [modelId.slice(0, modelId.indexOf("/")), modelId.slice(modelId.indexOf("/") + 1)];
      } else if (modelId.startsWith("gpt-")) {
        provider = "openai-codex";
      } else {
        throw new ProtocolError("model requires an explicit provider/model id", "invalid_model");
      }
    }
    requireString(provider, "provider");
    requireString(modelId, "model");
    const model = this.modelRuntime.getModel(provider, modelId);
    if (!model) throw new ProtocolError(`unknown Pi model: ${provider}/${modelId}`, "model_not_found");
    return { provider, modelId, model };
  }

  async ensureConfigured(model) {
    if (this.modelRuntime.hasConfiguredAuth(model.provider)) return;
    const auth = await this.modelRuntime.checkAuth(model.provider);
    if (!auth) throw new ProtocolError(`provider ${model.provider} is not authenticated`, "provider_not_configured");
  }

  ensureAvailable(model) {
    if (!this.isModelAvailable(model)) {
      throw new ProtocolError(
        `model ${model.provider}/${model.id} is not available for the configured provider account`,
        "model_not_available",
      );
    }
  }

  validateEffort(model, effort) {
    const value = effort ?? this.sdk.defaultEffort(model);
    if (value === undefined) {
      throw new ProtocolError(
        `model ${model.provider}/${model.id} has no exact default reasoning effort`,
        "unsupported_effort",
      );
    }
    if (typeof value !== "string" || (!STANDARD_EFFORTS.has(value) && value !== "ultra")) {
      throw new ProtocolError(`invalid reasoning effort: ${String(value)}`, "unsupported_effort");
    }
    const supported = this.sdk.supportedEfforts(model);
    if (!supported.includes(value)) {
      const catalog = typeof this.sdk.catalogEfforts === "function"
        ? this.sdk.catalogEfforts(model)
        : supported;
      if (catalog.includes(value)) {
        const route = this.sdk.effortRoute(model, value);
        if (route.mappingStatus === "alias") {
          throw new ProtocolError(
            `reasoning effort ${value} is only an inexact alias to provider effort ${route.providerReasoningEffort} for ${model.provider}/${model.id}`,
            "unsupported_effort",
          );
        }
      }
      throw new ProtocolError(
        `reasoning effort ${value} is not supported by ${model.provider}/${model.id}; available: ${supported.join(", ") || "none"}`,
        "unsupported_effort",
      );
    }
    return value;
  }

  validateServiceTier(model, tier) {
    const value = normalizeServiceTier(tier);
    const supported = this.sdk.supportedServiceTiers(model);
    if (value !== undefined && !supported.includes(value)) {
      throw new ProtocolError(
        `serviceTier ${value} is not supported by ${model.provider}/${model.id}; available: ${supported.join(", ") || "none"}`,
        "unsupported_service_tier",
      );
    }
    return value;
  }

  async modelValidate(params) {
    const { model } = this.selection(params);
    // Capability checks deliberately precede authentication.  They are pure
    // catalog checks and therefore fail before any provider-backed self-test.
    const effort = this.validateEffort(model, params.effort);
    const serviceTier = this.validateServiceTier(model, params.serviceTier);
    await this.ensureConfigured(model);
    this.ensureAvailable(model);
    return {
      valid: true,
      model: this.modelDescriptor(model, true),
      requested: {
        effort,
        effortDefaulted: params.effort === undefined || params.effort === null,
        serviceTier: serviceTier ?? null,
      },
      execution: {
        ...this.sdk.effortRoute(model, effort),
        effortCapabilitySource: this.sdk.effortCapabilitySource(model, effort),
      },
    };
  }

  async threadStart(params) {
    const threadId = requireId(params.threadId, "threadId");
    if (this.threads.has(threadId)) throw new ProtocolError(`duplicate Pi thread: ${threadId}`, "thread_exists");
    const cwd = canonicalPath(params.cwd, "cwd");
    if (isInside(cwd, this.stateDir)) {
      throw new ProtocolError("agent workspace contains the private Pi state directory", "unsafe_state_directory");
    }
    const { provider, modelId, model } = this.selection(params);
    const effort = this.validateEffort(model, params.effort);
    const serviceTier = this.validateServiceTier(model, params.serviceTier);
    await this.ensureConfigured(model);
    this.ensureAvailable(model);
    const tools = normalizeTools(params.tools);
    const now = timestamp();
    const meta = {
      version: STATE_VERSION,
      threadId,
      cwd,
      provider,
      model: modelId,
      effort,
      effortDefaulted: params.effort === undefined || params.effort === null,
      serviceTier: serviceTier ?? null,
      tools,
      developerInstructions: typeof params.developerInstructions === "string" ? params.developerInstructions : null,
      systemPrompt: typeof params.systemPrompt === "string" ? params.systemPrompt : null,
      approvalPolicy: typeof params.approvalPolicy === "string" ? params.approvalPolicy : null,
      sandbox: typeof params.sandbox === "string" ? params.sandbox : null,
      parentThreadId: typeof params.parentThreadId === "string" ? params.parentThreadId : null,
      sessionFile: null,
      status: "starting",
      archived: false,
      turns: [],
      createdAt: now,
      updatedAt: now,
    };
    const record = this.makeRecord(meta);
    this.threads.set(threadId, record);
    this.save(record);
    try {
      await this.loadSession(record, model);
      meta.status = "idle";
      this.save(record);
    } catch (error) {
      meta.status = "failed";
      meta.error = { message: error instanceof Error ? error.message : String(error) };
      this.save(record);
      throw error;
    }
    return { thread: publicThread(meta) };
  }

  async loadSession(record, knownModel) {
    if (record.session) return record.session;
    if (record.loadPromise) return record.loadPromise;
    record.loadPromise = (async () => {
      const meta = record.meta;
      const model = knownModel ?? this.modelRuntime.getModel(meta.provider, meta.model);
      if (!model) throw new ProtocolError(`persisted model is unavailable: ${meta.provider}/${meta.model}`, "model_not_found");
      await this.ensureConfigured(model);
      this.ensureAvailable(model);
      this.validateEffort(model, meta.effort);
      this.validateServiceTier(model, meta.serviceTier);
      let sessionFile = meta.sessionFile;
      if (sessionFile && !existsSync(sessionFile)) sessionFile = null;
      const sessionManager = this.sdk.createSessionManager({
        cwd: meta.cwd,
        sessionDir: this.sessionsDir,
        threadId: meta.threadId,
        sessionFile,
      });
      if (sessionManager.getSessionId() !== meta.threadId) {
        throw new ProtocolError("persisted Pi session id does not match Bello thread id", "state_corrupt");
      }
      meta.sessionFile = sessionManager.getSessionFile();
      this.save(record);
      const modelTools = toolsForModel(meta.tools, model);
      const customTools = this.customTools(record, modelTools);
      const session = await this.sdk.createSession({
        cwd: meta.cwd,
        agentDir: this.agentDir,
        modelRuntime: this.modelRuntime,
        model,
        thinkingLevel: this.sdk.bootstrapEffort(model, meta.effort),
        sessionManager,
        customTools,
        activeToolNames: modelTools.map((tool) => tool.name),
        developerInstructions: meta.developerInstructions,
        systemPrompt: meta.systemPrompt,
        requestOptions: record.requestOptions,
      });
      record.session = session;
      record.unsubscribe = session.subscribe((event) => this.onAgentEvent(record, event));
      return session;
    })();
    try {
      return await record.loadPromise;
    } finally {
      record.loadPromise = undefined;
    }
  }

  customTools(record, tools = record.meta.tools) {
    const hostTools = tools.map((tool) => ({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      executionMode: "sequential",
      execute: (callId, args, signal) => this.executeHostTool(record, tool.name, callId, args, signal),
    }));
    hostTools.push({
      name: RESERVED_TOOL_NAME,
      label: "Submit result",
      description: "Optional: submit the final JSON matching this schema and stop. You may instead return the same JSON as your final assistant message. Bello validates the decision in either case.",
      parameters: { type: "object", additionalProperties: true },
      executionMode: "sequential",
      execute: async (_callId, args) => {
        const active = record.active;
        if (!active || !active.outputSchema) throw new Error("submit_result is unavailable for this turn");
        if (active.structuredResult !== undefined) throw new Error("submit_result may only be called once");
        const errors = this.sdk.validateSchema(active.outputSchema, args);
        if (errors.length > 0) {
          throw new Error(`structured result failed schema validation: ${errors.map((entry) => `${entry.path || "/"}: ${entry.message}`).join("; ")}`);
        }
        active.structuredResult = cloneJson(args, "structured result");
        return {
          content: [{ type: "text", text: "Structured result received; Bello will validate the decision." }],
          details: { accepted: true },
          terminate: true,
        };
      },
    });
    return hostTools;
  }

  async executeHostTool(record, name, callId, args, signal) {
    const active = record.active;
    if (!active) throw new Error("tool call belongs to an inactive turn");
    if (name === "view_image") {
      const model = this.modelRuntime.getModel(record.meta.provider, record.meta.model);
      if (!inputModalities(model).includes("image")) {
        throw new ProtocolError(
          `view_image is unavailable because ${record.meta.provider}/${record.meta.model} does not accept image input`,
          "unsupported_input_modality",
        );
      }
    }
    requireString(callId, "Pi tool call id");
    const item = {
      id: callId,
      type: "hostTool",
      name,
      arguments: cloneJson(args, "tool arguments"),
      status: "inProgress",
      startedAt: timestamp(),
    };
    active.turn.items.push(item);
    this.save(record);
    try {
      const raw = await this.hostCalls.call({
        threadId: record.meta.threadId,
        turnId: active.turn.id,
        callId,
        name,
        arguments: args,
      }, signal);
      const result = normalizeHostResult(raw);
      item.status = result.isError ? "failed" : "completed";
      item.completedAt = timestamp();
      item.result = cloneJson(result);
      this.save(record);
      if (result.isError) throw new Error(resultText(result));
      return { content: result.content, details: result.details };
    } catch (error) {
      if (item.status === "inProgress") {
        item.status = signal?.aborted ? "interrupted" : "failed";
        item.completedAt = timestamp();
        item.error = { message: error instanceof Error ? error.message : String(error) };
        this.save(record);
      }
      throw error;
    }
  }

  onAgentEvent(record, event) {
    const active = record.active;
    if (!active) return;
    if (event.type === "message_start" && event.message?.role === "assistant") {
      const id = `${active.turn.id}-message-${++record.assistantSequence}`;
      record.currentAssistantItemId = id;
      void this.emit({
        method: "item/started",
        params: {
          threadId: record.meta.threadId,
          turnId: active.turn.id,
          itemId: id,
          item: { id, type: "agentMessage", text: "", status: "inProgress" },
        },
      });
      return;
    }
    if (event.type === "message_end" && event.message?.role === "assistant") {
      const id = record.currentAssistantItemId ?? `${active.turn.id}-message-${++record.assistantSequence}`;
      record.currentAssistantItemId = undefined;
      const item = { id, type: "agentMessage", text: extractText(event.message), status: "completed" };
      const reportedUsage = assistantUsage(event.message, id);
      if (reportedUsage) {
        item.usage = cloneJson(reportedUsage.usage, "assistant message usage");
        item.usageProvenance = cloneJson(
          Object.fromEntries(Object.entries(reportedUsage).filter(([key]) => !["itemId", "usage"].includes(key))),
          "assistant message usage provenance",
        );
        active.usageResponses.push(reportedUsage);
        active.turn.usage = aggregateUsage(active.usageResponses);
      }
      active.turn.items.push(item);
      active.lastAssistant = {
        stopReason: event.message.stopReason,
        errorMessage: event.message.errorMessage,
      };
      this.save(record);
      void this.emit({
        method: "item/completed",
        params: { threadId: record.meta.threadId, turnId: active.turn.id, itemId: id, item: cloneJson(item) },
      });
    }
  }

  async threadResume(params) {
    const record = this.getRecord(params.threadId);
    const meta = record.meta;
    if (params.cwd !== undefined && canonicalPath(params.cwd, "cwd") !== meta.cwd) {
      throw new ProtocolError("thread/resume cannot change cwd", "scope_mismatch");
    }
    if (params.provider !== undefined || params.model !== undefined) {
      const selection = this.selection({ provider: params.provider ?? meta.provider, model: params.model ?? meta.model });
      if (selection.provider !== meta.provider || selection.modelId !== meta.model) {
        throw new ProtocolError("thread/resume cannot change model", "model_mismatch");
      }
    }
    if (params.tools !== undefined) {
      const tools = normalizeTools(params.tools);
      if (!sameJson(tools, meta.tools)) throw new ProtocolError("thread/resume cannot change its tool contract", "scope_mismatch");
    }
    if (params.serviceTier !== undefined) {
      const model = this.modelRuntime.getModel(meta.provider, meta.model);
      meta.serviceTier = this.validateServiceTier(model, params.serviceTier) ?? null;
    }
    meta.archived = false;
    meta.status = "idle";
    delete meta.error;
    this.save(record);
    await this.loadSession(record);
    return { thread: publicThread(meta) };
  }

  async threadRead(params) {
    const record = this.getRecord(params.threadId);
    return { thread: publicThread(record.meta, params.includeTurns !== false) };
  }

  async threadList(params) {
    const includeArchived = params.includeArchived === true;
    return {
      data: [...this.threads.values()]
        .filter((record) => includeArchived || !record.meta.archived)
        .map((record) => publicThread(record.meta, false)),
    };
  }

  async threadTurnsList(params) {
    const record = this.getRecord(params.threadId);
    const limit = Number.isInteger(params.limit) && params.limit > 0 ? Math.min(params.limit, 1000) : 10;
    const offset = typeof params.cursor === "string" && /^\d+$/.test(params.cursor) ? Number(params.cursor) : 0;
    if (!Number.isSafeInteger(offset) || offset < 0) throw new ProtocolError("invalid turns cursor", "invalid_params");
    const ordered = [...record.meta.turns];
    if (params.sortDirection !== "asc") ordered.reverse();
    const data = ordered.slice(offset, offset + limit).map(publicTurn);
    const nextOffset = offset + data.length;
    return { data, nextCursor: nextOffset < ordered.length ? String(nextOffset) : null };
  }

  async disposeRecord(record) {
    if (!record.session) return;
    record.unsubscribe?.();
    record.unsubscribe = undefined;
    record.session.dispose();
    record.session = undefined;
  }

  async threadArchive(params) {
    const record = this.getRecord(params.threadId);
    if (record.active) await this.interruptRecord(record, record.active.turn.id);
    await this.disposeRecord(record);
    record.meta.archived = true;
    record.meta.status = "archived";
    this.save(record);
    return {};
  }

  async threadUnsubscribe(params) {
    return this.threadArchive(params);
  }

  async turnStart(params) {
    const record = this.getRecord(params.threadId);
    if (record.meta.archived) throw new ProtocolError("cannot start a turn on an archived thread", "thread_archived");
    if (record.active) throw new ProtocolError("thread already has an active turn", "turn_active");
    const turnId = requireId(params.turnId, "turnId");
    if (record.meta.turns.some((turn) => turn.id === turnId)) throw new ProtocolError(`duplicate turn id: ${turnId}`, "turn_exists");
    const input = normalizeInput(params.input);
    const model = this.modelRuntime.getModel(record.meta.provider, record.meta.model);
    if (!model) throw new ProtocolError(`thread model is unavailable: ${record.meta.provider}/${record.meta.model}`, "model_not_found");
    const effort = this.validateEffort(model, params.effort ?? record.meta.effort);
    const tierInput = Object.hasOwn(params, "serviceTier") ? params.serviceTier : record.meta.serviceTier;
    const serviceTier = this.validateServiceTier(model, tierInput);
    let outputSchema;
    if (params.outputSchema !== undefined && params.outputSchema !== null) {
      outputSchema = cloneJson(requireObject(params.outputSchema, "outputSchema"), "outputSchema");
    }
    const turn = {
      id: turnId,
      status: "inProgress",
      items: [],
      startedAt: timestamp(),
    };
    record.meta.turns.push(turn);
    record.meta.status = "inProgress";
    record.meta.effort = effort;
    record.active = {
      turn,
      input,
      effort,
      serviceTier,
      outputSchema,
      structuredResult: undefined,
      usageResponses: [],
      interrupted: false,
      lastAssistant: undefined,
    };
    this.save(record);
    this.schedule(() => {
      void this.runTurn(record).catch((error) => {
        process.stderr.write(`Pi turn finalization error: ${error instanceof Error ? error.stack ?? error.message : String(error)}\n`);
      });
    });
    return { turn: publicTurn(turn) };
  }

  async runTurn(record) {
    const active = record.active;
    if (!active) return;
    await this.emit({
      method: "turn/started",
      params: { threadId: record.meta.threadId, turnId: active.turn.id, turn: publicTurn(active.turn) },
    });
    try {
      const session = await this.loadSession(record);
      const model = this.modelRuntime.getModel(record.meta.provider, record.meta.model);
      const bootstrap = this.sdk.bootstrapEffort(model, active.effort);
      session.setThinkingLevel(bootstrap);
      if (session.thinkingLevel !== bootstrap) {
        throw new ProtocolError(`Pi changed requested effort ${bootstrap} to ${session.thinkingLevel}`, "unsupported_effort");
      }
      const activeTools = toolsForModel(record.meta.tools, model).map((tool) => tool.name);
      if (active.outputSchema) activeTools.push(RESERVED_TOOL_NAME);
      session.setActiveToolsByName(activeTools);
      if (active.outputSchema) {
        const submitTool = session.state.tools.find((tool) => tool.name === RESERVED_TOOL_NAME);
        if (!submitTool) throw new ProtocolError("Pi did not activate submit_result", "internal_error");
        submitTool.parameters = active.outputSchema;
      }
      record.requestOptions.current = {
        nativeEffort: this.sdk.nativeEffort(model, active.effort),
        serviceTier: active.serviceTier,
      };
      await session.prompt(active.input, { expandPromptTemplates: false });
      if (active.interrupted || active.lastAssistant?.stopReason === "aborted") {
        this.finishTurn(record, active, "interrupted", "Turn was interrupted.");
      } else if (active.lastAssistant?.stopReason === "error") {
        this.finishTurn(record, active, "failed", active.lastAssistant.errorMessage || "Provider returned an error.");
      } else {
        // A normal final JSON message is also a valid delivery path, as in
        // Bello 0.5.2. Leave its text intact for the existing Python decision
        // parsers, validation and repair loop; completed is not decision accept.
        if (active.structuredResult !== undefined) {
          const text = JSON.stringify(active.structuredResult);
          const item = {
            id: `${active.turn.id}-structured-result`,
            type: "agentMessage",
            text,
            status: "completed",
          };
          active.turn.items.push(item);
          active.turn.structuredResult = cloneJson(active.structuredResult);
          await this.emit({
            method: "item/completed",
            params: {
              threadId: record.meta.threadId,
              turnId: active.turn.id,
              itemId: item.id,
              item: cloneJson(item),
            },
          });
        }
        this.finishTurn(record, active, "completed");
      }
    } catch (error) {
      const status = active.interrupted || (error instanceof Error && error.name === "AbortError") ? "interrupted" : "failed";
      this.finishTurn(record, active, status, error instanceof Error ? error.message : String(error));
    } finally {
      record.requestOptions.current = undefined;
    }
  }

  finishTurn(record, active, status, errorMessage) {
    if (active.turn.status !== "inProgress") return;
    active.turn.status = status;
    active.turn.completedAt = timestamp();
    if (errorMessage) active.turn.error = { message: errorMessage };
    record.meta.status = record.meta.archived ? "archived" : "idle";
    if (record.active === active) record.active = undefined;
    this.save(record);
    void this.emit({
      method: "turn/completed",
      params: {
        threadId: record.meta.threadId,
        turnId: active.turn.id,
        turn: publicTurn(active.turn),
      },
    });
  }

  async turnSteer(params) {
    const record = this.getRecord(params.threadId);
    const expectedTurnId = requireId(params.expectedTurnId, "expectedTurnId");
    if (!record.active || record.active.turn.id !== expectedTurnId) {
      throw new ProtocolError("cannot steer an inactive or stale turn", "turn_not_active");
    }
    const text = normalizeInput(params.input);
    const session = await this.loadSession(record);
    await session.steer(text);
    return {};
  }

  async interruptRecord(record, turnId) {
    const active = record.active;
    if (!active) return;
    if (active.turn.id !== turnId) throw new ProtocolError("turn id does not match the active turn", "turn_mismatch");
    active.interrupted = true;
    if (record.session) await record.session.abort();
    if (active.turn.status === "inProgress") this.finishTurn(record, active, "interrupted", "Turn was interrupted.");
  }

  async turnInterrupt(params) {
    const record = this.getRecord(params.threadId);
    const turnId = requireId(params.turnId, "turnId");
    await this.interruptRecord(record, turnId);
    return {};
  }

  async close() {
    if (this.closing) return;
    this.closing = true;
    for (const record of this.threads.values()) {
      if (record.active) {
        try {
          await this.interruptRecord(record, record.active.turn.id);
        } catch {
          // Continue closing the remaining sessions.
        }
      }
      await this.disposeRecord(record);
    }
  }
}
