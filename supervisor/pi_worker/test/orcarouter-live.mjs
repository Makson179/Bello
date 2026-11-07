// Opt-in live check for the implemented OrcaRouter provider path. Skipped
// unless ORCA_LIVE=1 and the key is supplied through the ORCAROUTER_API_KEY
// environment variable; the key value is never printed or asserted.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import test from "node:test";

import { realPiSdk } from "../src/pi-sdk.mjs";
import { discoveryUrl, ORCAROUTER_SEED } from "../src/orcarouter-catalog.mjs";

const LIVE = process.env.ORCA_LIVE === "1";

async function streamText(modelRuntime, model) {
  const stream = await modelRuntime.models.stream(model, {
    messages: [{ role: "user", content: "Reply with the single word OK." }],
  }, {});
  let text = "";
  let error;
  for await (const event of stream) {
    if (event.type === "text_delta") text += event.delta ?? "";
    if (event.type === "error") error = event.error?.errorMessage;
    if (event.type === "done" || event.type === "error") break;
  }
  return { text, error };
}

test("OrcaRouter live: catalog and inference through Bello's named provider", {
  skip: LIVE ? false : "set ORCA_LIVE=1 with a key in the environment",
  timeout: 120_000,
}, async () => {
  assert.ok(process.env.ORCAROUTER_API_KEY, "the OrcaRouter key must be set in the environment");
  const agentDir = mkdtempSync("/tmp/bello-orca-live-");
  const runtime = await realPiSdk.createModelRuntime({ agentDir, allowModelNetwork: true });
  const provider = runtime.getProvider("orcarouter");
  assert.equal(provider.name, "OrcaRouter Cyber Inference");
  assert.equal(provider.baseUrl, "https://api.orcarouter.ai/v1");
  const refreshed = await runtime.refresh({ allowNetwork: true });
  assert.equal(refreshed.errors.size, 0, [...refreshed.errors.values()].map(String).join("; "));
  const models = runtime.getModels("orcarouter");
  const ids = models.map((model) => model.id);
  console.log(`discovery ${discoveryUrl()} -> ${ids.length} chat models, e.g. ${ids.slice(0, 4).join(", ")}`);
  assert.ok(ids.length > 0);
  // The key's grant decides which models it may call. Use the first discovered
  // model this key can reach; a scope rejection is not an integration failure.
  let outcome;
  let chosen;
  for (chosen of models) {
    outcome = await streamText(runtime, chosen);
    if (outcome.text.trim() || !/403|model_access_denied/u.test(outcome.error ?? "")) break;
  }
  console.log(`inference ${chosen.id}: ${JSON.stringify(outcome.text.slice(0, 80))}`);
  assert.ok(outcome.text.trim().length > 0, `no model produced text: ${outcome.error}`);
});
