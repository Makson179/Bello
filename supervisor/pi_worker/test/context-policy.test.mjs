import assert from "node:assert/strict";
import test from "node:test";
import { normalizeContextPolicy } from "../src/context-policy.mjs";

test("an omitted Pi context policy stays off with a 200K threshold and 20K soft tail", () => {
  for (const value of [undefined, null]) {
    const policy = normalizeContextPolicy(value);
    assert.deepEqual(policy, { enabled: false, thresholdTokens: 200000, retainPercent: 10 });
    assert.equal(policy.thresholdTokens * policy.retainPercent / 100, 20000);
  }
});

test("sparse Pi context policies use the new default without changing enabled", () => {
  for (const enabled of [false, true]) {
    assert.deepEqual(normalizeContextPolicy({ enabled }), {
      enabled, thresholdTokens: 200000, retainPercent: 10,
    });
  }
});

test("explicit legacy Pi thresholds survive normalization unchanged", () => {
  for (const enabled of [false, true]) {
    const policy = { enabled, thresholdTokens: 249000, retainPercent: 10 };
    assert.deepEqual(normalizeContextPolicy(policy), policy);
  }
});
