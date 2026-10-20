import { ProtocolError } from "./protocol.mjs";

// The No Need For Context policy is validated by the worker runtime, which must
// stay free of a static Pi SDK import. Compaction itself lives in
// context-compaction.mjs, which depends on the SDK and re-exports this.
export function normalizeContextPolicy(value) {
  if (value === undefined || value === null) return { enabled: false, thresholdTokens: 200000, retainPercent: 10 };
  if (typeof value !== "object" || Array.isArray(value)
      || typeof value.enabled !== "boolean"
      || Object.keys(value).some((key) => !["enabled", "thresholdTokens", "retainPercent"].includes(key))) {
    throw new ProtocolError("noNeedForContext must contain enabled, thresholdTokens and retainPercent", "invalid_params");
  }
  const thresholdTokens = value.thresholdTokens ?? 200000;
  const retainPercent = value.retainPercent ?? 10;
  if (!Number.isSafeInteger(thresholdTokens) || thresholdTokens <= 0
      || !Number.isFinite(retainPercent) || retainPercent <= 0 || retainPercent > 50) {
    throw new ProtocolError("noNeedForContext requires a positive integer threshold and retainPercent in (0, 50]", "invalid_params");
  }
  return { enabled: value.enabled, thresholdTokens, retainPercent };
}
