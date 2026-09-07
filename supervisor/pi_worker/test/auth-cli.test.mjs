import assert from "node:assert/strict";
import test from "node:test";

import { availableAuthTypes, chooseAuthType, redactErrorMessage } from "../src/auth-cli.mjs";

function provider(auth) {
  return { id: "test", name: "Test Provider", auth };
}

test("auth method discovery uses only official interactive provider methods", async () => {
  const both = provider({
    oauth: { name: "Subscription", login: async () => {} },
    apiKey: { name: "API key", login: async () => {} },
  });
  assert.deepEqual(availableAuthTypes(both), ["oauth", "api_key"]);
  const selected = await chooseAuthType(both, undefined, {
    async prompt(prompt) {
      assert.deepEqual(prompt.options.map((option) => option.id), ["oauth", "api_key"]);
      return "api_key";
    },
  });
  assert.equal(selected, "api_key");
  assert.equal(await chooseAuthType(both, "oauth", {}), "oauth");
});

test("auth error reporting redacts credentials and URL query parameters", () => {
  const message = redactErrorMessage(new Error(
    "Bearer secret-token-value https://login.example/callback?code=private sk-example1234567890",
  ));
  assert.equal(message.includes("secret-token-value"), false);
  assert.equal(message.includes("code=private"), false);
  assert.equal(message.includes("sk-example1234567890"), false);
});
