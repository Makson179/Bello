import { join } from "node:path";

import { getSupportedThinkingLevels } from "@earendil-works/pi-ai";
import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { Check, Errors } from "typebox/value";

export const PI_SDK_VERSION = "0.85.1";
const DIRECT_OPENAI_RESPONSE_ROUTES = new Set([
  "openai/openai-responses",
  "openai-codex/openai-codex-responses",
  "azure-openai-responses/azure-openai-responses",
]);
const OPENAI_REASONING_EFFORT_APIS = new Set([
  "openai-responses",
  "openai-codex-responses",
  "azure-openai-responses",
]);
const OPENAI_SERVICE_TIERS = ["auto", "default", "flex", "scale", "priority"];
// Pi's SDK default is medium.  If a model cannot use medium, Pi clamps upward
// first and then downward.  Bello resolves that effective default before
// session creation so an omitted effort is visible and never mistaken for an
// explicitly requested `off`.
const PI_DEFAULT_EFFORT_ORDER = ["medium", "high", "xhigh", "max", "low", "minimal", "off"];
// Pi 0.85.1 predates the `ultra` spelling in its ModelThinkingLevel type and
// therefore cannot advertise it from getSupportedThinkingLevels().  These are
// the exact Codex routes for which Bello 0.5.2 already allowed ultra.  Keep the
// compatibility supplement deliberately provider-qualified: similarly named
// API-key, gateway, Bedrock, or Copilot routes must use their own catalog.
const BELLO_CODEX_ULTRA_MODELS = new Set([
  "openai-codex/gpt-6-astra",
  "openai-codex/gpt-5.6-sol",
  "openai-codex/gpt-5.6-terra",
]);

function qualifiedModelId(model) {
  return `${model.provider}/${model.id}`;
}

export function transformProviderPayload(payload, current) {
  if (!current || (!current.nativeEffort && current.serviceTier === undefined)) return undefined;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
  const transformed = { ...payload };
  if (current.nativeEffort) {
    const previous = transformed.reasoning;
    transformed.reasoning = {
      ...(previous && typeof previous === "object" && !Array.isArray(previous) ? previous : {}),
      effort: current.nativeEffort,
    };
  }
  if (current.serviceTier !== undefined) transformed.service_tier = current.serviceTier;
  return transformed;
}

function providerPayloadExtension(requestOptions) {
  return (pi) => {
    pi.on("before_provider_request", (event) => {
      return transformProviderPayload(event.payload, requestOptions.current);
    });
  };
}

export const realPiSdk = {
  version: PI_SDK_VERSION,

  async createModelRuntime({ agentDir, allowModelNetwork }) {
    return ModelRuntime.create({
      authPath: join(agentDir, "auth.json"),
      modelsPath: join(agentDir, "models.json"),
      allowModelNetwork,
    });
  },

  createSessionManager({ cwd, sessionDir, threadId, sessionFile }) {
    return sessionFile
      ? SessionManager.open(sessionFile, sessionDir, cwd)
      : SessionManager.create(cwd, sessionDir, { id: threadId });
  },

  async createSession({
    cwd,
    agentDir,
    modelRuntime,
    model,
    thinkingLevel,
    sessionManager,
    customTools,
    activeToolNames,
    developerInstructions,
    systemPrompt,
    requestOptions,
  }) {
    const settingsManager = SettingsManager.inMemory({
      defaultTools: [],
      steeringMode: "one-at-a-time",
      followUpMode: "one-at-a-time",
    });
    const appendSystemPrompt = typeof developerInstructions === "string" && developerInstructions.trim()
      ? [developerInstructions]
      : [];
    const resourceLoader = new DefaultResourceLoader({
      cwd,
      agentDir,
      settingsManager,
      noExtensions: true,
      noSkills: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: true,
      extensionFactories: [providerPayloadExtension(requestOptions)],
      ...(typeof systemPrompt === "string" && systemPrompt.trim() ? { systemPrompt } : {}),
      appendSystemPrompt,
    });
    await resourceLoader.reload();
    const { session, modelFallbackMessage } = await createAgentSession({
      cwd,
      agentDir,
      modelRuntime,
      model,
      thinkingLevel,
      noTools: "builtin",
      customTools,
      resourceLoader,
      sessionManager,
      settingsManager,
    });
    if (modelFallbackMessage) {
      session.dispose();
      throw new Error(`Pi unexpectedly selected a fallback model: ${modelFallbackMessage}`);
    }
    session.setActiveToolsByName(activeToolNames);
    return session;
  },

  catalogEfforts(model) {
    const efforts = [...getSupportedThinkingLevels(model)];
    const catalogUltra = model.thinkingLevelMap
      && Object.hasOwn(model.thinkingLevelMap, "ultra")
      && model.thinkingLevelMap.ultra !== null;
    if (
      OPENAI_REASONING_EFFORT_APIS.has(model.api)
      && (catalogUltra || BELLO_CODEX_ULTRA_MODELS.has(qualifiedModelId(model)))
    ) {
      efforts.push("ultra");
    }
    return [...new Set(efforts)];
  },

  supportedEfforts(model) {
    return this.catalogEfforts(model).filter(
      (effort) => this.effortRoute(model, effort).mappingStatus !== "alias",
    );
  },

  defaultEffort(model) {
    const supported = this.supportedEfforts(model);
    return PI_DEFAULT_EFFORT_ORDER.find((effort) => supported.includes(effort));
  },

  effortCapabilitySource(model, effort) {
    if (effort !== "ultra") return "pi-catalog";
    const catalogUltra = model.thinkingLevelMap
      && Object.hasOwn(model.thinkingLevelMap, "ultra")
      && model.thinkingLevelMap.ultra !== null;
    return catalogUltra ? "pi-catalog" : "bello-0.5.2-codex-compatibility";
  },

  supportsServiceTier(model) {
    return DIRECT_OPENAI_RESPONSE_ROUTES.has(`${model.provider}/${model.api}`);
  },

  supportedServiceTiers(model) {
    return this.supportsServiceTier(model) ? [...OPENAI_SERVICE_TIERS] : [];
  },

  nativeEffort(model, effort) {
    if (effort !== "ultra") return undefined;
    return model.thinkingLevelMap?.ultra ?? "ultra";
  },

  bootstrapEffort(model, effort) {
    if (effort !== "ultra") return effort;
    const supported = getSupportedThinkingLevels(model);
    return supported.at(-1) ?? "off";
  },

  effortRoute(model, effort) {
    const piThinkingLevel = this.bootstrapEffort(model, effort);
    const nativeEffort = this.nativeEffort(model, effort);
    if (nativeEffort !== undefined) {
      return {
        piThinkingLevel,
        providerControl: "reasoning_effort",
        providerReasoningEffort: nativeEffort,
        mappingStatus: nativeEffort === effort ? "exact" : "alias",
      };
    }

    if (OPENAI_REASONING_EFFORT_APIS.has(model.api)) {
      if (effort === "off") {
        const sendsDisabledEffort = model.reasoning === true
          && model.api !== "openai-codex-responses"
          && model.provider !== "github-copilot"
          && model.thinkingLevelMap?.off !== null;
        return {
          piThinkingLevel,
          providerControl: sendsDisabledEffort ? "reasoning_effort" : "reasoning_omitted",
          providerReasoningEffort: sendsDisabledEffort
            ? (model.thinkingLevelMap?.off ?? "none")
            : null,
          mappingStatus: "exact",
        };
      }
      const mapped = model.thinkingLevelMap?.[effort];
      const providerReasoningEffort = typeof mapped === "string" ? mapped : effort;
      return {
        piThinkingLevel,
        providerControl: "reasoning_effort",
        providerReasoningEffort,
        mappingStatus: providerReasoningEffort === effort ? "exact" : "alias",
      };
    }

    const catalogValue = model.thinkingLevelMap?.[effort];
    return {
      piThinkingLevel,
      // Pi adapters can translate a level into a token budget, an adaptive
      // effort, a boolean, or another provider-specific control.  Do not claim
      // a literal reasoning_effort value when the generic SDK cannot prove it.
      providerControl: "adapter_defined",
      providerReasoningEffort: null,
      catalogProviderValue: typeof catalogValue === "string" ? catalogValue : null,
      mappingStatus: "adapter_defined",
    };
  },

  validateSchema(schema, value) {
    if (Check(schema, value)) return [];
    return [...Errors(schema, value)].slice(0, 8).map((entry) => ({
      path: entry.path,
      message: entry.message,
    }));
  },
};
