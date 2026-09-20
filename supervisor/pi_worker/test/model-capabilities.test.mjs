import assert from "node:assert/strict";
import test from "node:test";

import { realPiSdk, transformProviderPayload } from "../src/pi-sdk.mjs";

function model(id, overrides = {}) {
  return {
    provider: "openai-codex",
    id,
    api: "openai-codex-responses",
    reasoning: true,
    thinkingLevelMap: { xhigh: "xhigh", max: "max", minimal: "low" },
    ...overrides,
  };
}

test("Bello 0.5.2 Codex ultra routes stay provider-qualified and literal", () => {
  for (const id of ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"]) {
    const selected = model(id);
    assert.equal(realPiSdk.supportedEfforts(selected).includes("ultra"), true);
    assert.equal(realPiSdk.nativeEffort(selected, "ultra"), "ultra");
    assert.equal(realPiSdk.bootstrapEffort(selected, "ultra"), "max");
    assert.equal(realPiSdk.effortCapabilitySource(selected, "ultra"), "bello-0.5.2-codex-compatibility");
  }
  assert.equal(realPiSdk.supportedEfforts(model("gpt-5.6-luna")).includes("ultra"), false);
  assert.equal(realPiSdk.supportedEfforts(model("gpt-5.6-sol", { provider: "openai" })).includes("ultra"), false);
  assert.equal(realPiSdk.supportsServiceTier(model("gpt-5.6-sol")), true);
  assert.equal(realPiSdk.supportsServiceTier(model("gpt-5.6-sol", { provider: "github-copilot", api: "openai-responses" })), false);
});

test("OpenAI reasoning aliases are not advertised as exact efforts", () => {
  const selected = model("gpt-5.6-sol");
  assert.equal(realPiSdk.catalogEfforts(selected).includes("minimal"), true);
  assert.equal(realPiSdk.supportedEfforts(selected).includes("minimal"), false);
  assert.deepEqual(realPiSdk.effortRoute(selected, "minimal"), {
    piThinkingLevel: "minimal",
    providerControl: "reasoning_effort",
    providerReasoningEffort: "low",
    mappingStatus: "alias",
  });
  assert.deepEqual(realPiSdk.effortRoute(selected, "low"), {
    piThinkingLevel: "low",
    providerControl: "reasoning_effort",
    providerReasoningEffort: "low",
    mappingStatus: "exact",
  });
});

test("non-OpenAI provider controls stay explicitly adapter-defined", () => {
  const selected = model("claude-test", {
    provider: "anthropic",
    api: "anthropic-messages",
    thinkingLevelMap: { minimal: "low" },
  });
  assert.equal(realPiSdk.supportedEfforts(selected).includes("minimal"), true);
  assert.deepEqual(realPiSdk.effortRoute(selected, "minimal"), {
    piThinkingLevel: "minimal",
    providerControl: "adapter_defined",
    providerReasoningEffort: null,
    catalogProviderValue: "low",
    mappingStatus: "adapter_defined",
  });
});

test("OpenAI off routes report whether the provider receives none or no control", () => {
  assert.deepEqual(realPiSdk.effortRoute(model("gpt-5.6-sol"), "off"), {
    piThinkingLevel: "off",
    providerControl: "reasoning_omitted",
    providerReasoningEffort: null,
    mappingStatus: "exact",
  });
  assert.deepEqual(realPiSdk.effortRoute(model("gpt-5.6-sol", {
    provider: "openai",
    api: "openai-responses",
    thinkingLevelMap: { off: "none" },
  }), "off"), {
    piThinkingLevel: "off",
    providerControl: "reasoning_effort",
    providerReasoningEffort: "none",
    mappingStatus: "exact",
  });
});

test("provider payload transformation sends literal ultra and service tier without mutating input", () => {
  const payload = { reasoning: { summary: "auto", effort: "max" }, input: [] };
  const transformed = transformProviderPayload(payload, { nativeEffort: "ultra", serviceTier: "priority" });
  assert.deepEqual(transformed, {
    reasoning: { summary: "auto", effort: "ultra" },
    service_tier: "priority",
    input: [],
  });
  assert.equal(payload.reasoning.effort, "max");
});
