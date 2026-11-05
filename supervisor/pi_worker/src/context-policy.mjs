import { ProtocolError } from "./protocol.mjs";

// The No Need For Context policy is validated by the worker runtime, which must
// stay free of a static Pi SDK import. Compaction itself lives in
// context-compaction.mjs, which depends on the SDK and re-exports this.
export function normalizeContextPolicy(value) {
  if (value === undefined || value === null) return { enabled: false, thresholdTokens: 244800, retainTokens: 24800 };
  if (typeof value !== "object" || Array.isArray(value)
      || typeof value.enabled !== "boolean"
      || Object.keys(value).some((key) => !["enabled", "thresholdTokens", "retainPercent", "retainTokens"].includes(key))) {
    throw new ProtocolError("noNeedForContext must contain enabled, thresholdTokens and one retention target", "invalid_params");
  }
  const thresholdTokens = value.thresholdTokens ?? 244800;
  if (!Number.isSafeInteger(thresholdTokens) || thresholdTokens <= 0) {
    throw new ProtocolError("noNeedForContext requires a positive integer threshold", "invalid_params");
  }
  if (Object.hasOwn(value, "retainTokens") && Object.hasOwn(value, "retainPercent")) {
    throw new ProtocolError("noNeedForContext must select retainTokens or retainPercent, not both", "invalid_params");
  }
  if (Object.hasOwn(value, "retainPercent")) {
    const retainPercent = value.retainPercent;
    if (!Number.isFinite(retainPercent) || retainPercent <= 0 || retainPercent > 50) {
      throw new ProtocolError("noNeedForContext requires retainPercent in (0, 50]", "invalid_params");
    }
    return { enabled: value.enabled, thresholdTokens, retainPercent };
  }
  const retainTokens = Object.hasOwn(value, "retainTokens") ? value.retainTokens : 24800;
  if (!Number.isSafeInteger(retainTokens) || retainTokens <= 0) {
    throw new ProtocolError("noNeedForContext requires positive integer retainTokens", "invalid_params");
  }
  return { enabled: value.enabled, thresholdTokens, retainTokens };
}

// Inactive defaults may change across releases without migrating a saved thread.
// Enabled policies still require an exact normalized match.
export function contextPoliciesMatch(saved, requested) {
  const oldPolicy = normalizeContextPolicy(saved);
  const newPolicy = normalizeContextPolicy(requested);
  return (oldPolicy.enabled === false && newPolicy.enabled === false)
    || JSON.stringify(oldPolicy) === JSON.stringify(newPolicy);
}
