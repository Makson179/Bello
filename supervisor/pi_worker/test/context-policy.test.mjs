import assert from "node:assert/strict";
import test from "node:test";
import { contextPoliciesMatch, normalizeContextPolicy } from "../src/context-policy.mjs";

test("an omitted Pi context policy stays off with a 244800 threshold and 24800 soft tail", () => {
  for (const value of [undefined, null]) {
    const policy = normalizeContextPolicy(value);
    assert.deepEqual(policy, { enabled: false, thresholdTokens: 244800, retainTokens: 24800 });
  }
});

test("sparse Pi context policies use the new default without changing enabled", () => {
  for (const enabled of [false, true]) {
    assert.deepEqual(normalizeContextPolicy({ enabled }), {
      enabled, thresholdTokens: 244800, retainTokens: 24800,
    });
  }
});

test("explicit absolute Pi tail targets are preserved independently of the trigger", () => {
  for (const retainTokens of [1, 24800, 12345]) {
    const policy = { enabled: true, thresholdTokens: 244800, retainTokens };
    assert.deepEqual(normalizeContextPolicy(policy), policy);
  }
});

test("Pi rejects ambiguous retention policies even when disabled", () => {
  for (const enabled of [false, true]) {
    assert.throws(() => normalizeContextPolicy({ enabled, retainTokens: 24800, retainPercent: 10 }), /not both/);
  }
});

test("Pi rejects invalid absolute retention targets", () => {
  for (const retainTokens of [null, undefined, true, 0, -1, 2.5, "24800", NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => normalizeContextPolicy({ enabled: true, retainTokens }), /retainTokens/);
  }
});

test("explicit legacy Pi thresholds survive normalization unchanged", () => {
  for (const enabled of [false, true]) {
    const policy = { enabled, thresholdTokens: 249000, retainPercent: 10 };
    assert.deepEqual(normalizeContextPolicy(policy), policy);
  }
});

test("only disabled Pi policies ignore changed inactive defaults", () => {
  const legacy = { enabled: false, thresholdTokens: 200000, retainPercent: 10 };
  const current = { enabled: false, thresholdTokens: 244800, retainTokens: 24800 };
  assert.equal(contextPoliciesMatch(legacy, current), true);
  assert.equal(contextPoliciesMatch({ ...legacy, enabled: true }, { ...current, enabled: true }), false);
  assert.equal(contextPoliciesMatch(legacy, { ...current, enabled: true }), false);
  for (const enabled of [0, null, "false"]) {
    assert.throws(() => contextPoliciesMatch(legacy, { ...current, enabled }), /noNeedForContext/);
  }
});
