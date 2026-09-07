import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { Writable } from "node:stream";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";

const PROVIDER_PATTERN = /^[a-z0-9][a-z0-9._-]*$/;
const AUTH_TYPES = new Set(["oauth", "api_key"]);

export function redactErrorMessage(value) {
  let message = value instanceof Error ? value.message : String(value);
  message = message.replace(/\b(?:sk|key|token)-[A-Za-z0-9._-]{12,}\b/giu, "[redacted]");
  message = message.replace(/\bBearer\s+\S+/giu, "Bearer [redacted]");
  message = message.replace(/https?:\/\/[^\s)]+/giu, (raw) => {
    try {
      const url = new URL(raw);
      if (url.search || url.hash) return `${url.origin}${url.pathname}?[redacted]`;
    } catch {
      return "[redacted URL]";
    }
    return raw;
  });
  return message;
}

function combinedSignal(primary, secondary) {
  if (!secondary) return primary;
  return AbortSignal.any([primary, secondary]);
}

async function question(input, output, text, signal) {
  const lines = createInterface({ input, output, terminal: Boolean(input.isTTY) });
  try {
    return await lines.question(text, { signal });
  } finally {
    lines.close();
  }
}

async function secretQuestion(input, output, text, signal) {
  if (!input.isTTY) return question(input, output, text, signal);
  output.write(text);
  const muted = new Writable({
    write(_chunk, _encoding, callback) {
      callback();
    },
  });
  try {
    return await question(input, muted, "", signal);
  } finally {
    output.write("\n");
  }
}

export function createTerminalInteraction({ input, output, signal }) {
  return {
    signal,
    async prompt(prompt) {
      const promptSignal = combinedSignal(signal, prompt.signal);
      if (prompt.type === "select") {
        output.write(`${prompt.message}\n`);
        prompt.options.forEach((option, index) => {
          output.write(`  ${index + 1}. ${option.label}${option.description ? ` — ${option.description}` : ""}\n`);
        });
        const answer = (await question(input, output, "Selection: ", promptSignal)).trim();
        const numeric = Number(answer);
        if (Number.isInteger(numeric) && numeric >= 1 && numeric <= prompt.options.length) {
          return prompt.options[numeric - 1].id;
        }
        const direct = prompt.options.find((option) => option.id === answer);
        if (direct) return direct.id;
        throw new Error("Invalid authentication selection");
      }
      const label = `${prompt.message}${prompt.placeholder ? ` (${prompt.placeholder})` : ""}: `;
      const answer = prompt.type === "secret"
        ? await secretQuestion(input, output, label, promptSignal)
        : await question(input, output, label, promptSignal);
      if (!answer.trim()) throw new Error("Authentication input cannot be empty");
      return answer.trim();
    },
    notify(event) {
      if (event.type === "auth_url") {
        output.write(`${event.instructions || "Open this URL to authenticate:"}\n${event.url}\n`);
      } else if (event.type === "device_code") {
        output.write(`Open ${event.verificationUri}\nDevice code: ${event.userCode}\n`);
      } else if (event.type === "info") {
        output.write(`${event.message}\n`);
        for (const link of event.links ?? []) output.write(`${link.label ? `${link.label}: ` : ""}${link.url}\n`);
      } else if (event.type === "progress") {
        output.write(`${event.message}\n`);
      }
    },
  };
}

export function availableAuthTypes(provider) {
  const types = [];
  if (provider.auth?.oauth?.login) types.push("oauth");
  if (provider.auth?.apiKey?.login) types.push("api_key");
  return types;
}

export async function chooseAuthType(provider, requestedType, interaction) {
  const available = availableAuthTypes(provider);
  if (available.length === 0) throw new Error(`${provider.name} has no interactive login method`);
  if (requestedType !== undefined) {
    if (!AUTH_TYPES.has(requestedType)) throw new Error("authentication method must be oauth or api_key");
    if (!available.includes(requestedType)) throw new Error(`${provider.name} does not support ${requestedType} login`);
    return requestedType;
  }
  if (available.length === 1) return available[0];
  return interaction.prompt({
    type: "select",
    message: `Select authentication for ${provider.name}:`,
    options: available.map((type) => ({
      id: type,
      label: type === "oauth" ? provider.auth.oauth.name : provider.auth.apiKey.name,
      description: type === "oauth" ? "provider subscription" : "provider API key",
    })),
  });
}

function parseArgs(argv) {
  if (argv.includes("--help") || argv.includes("-h")) return { help: true };
  const providerId = argv[0];
  if (typeof providerId !== "string" || !PROVIDER_PATTERN.test(providerId)) {
    throw new Error("usage: node auth.mjs PROVIDER [--method oauth|api_key]");
  }
  let method;
  if (argv.length > 1) {
    if (argv[1] !== "--method" || argv.length !== 3) {
      throw new Error("usage: node auth.mjs PROVIDER [--method oauth|api_key]");
    }
    method = argv[2];
  }
  return { providerId, method, help: false };
}

export async function authMain({
  argv = process.argv.slice(2),
  env = process.env,
  input = process.stdin,
  output = process.stderr,
  runtimeFactory = (options) => ModelRuntime.create(options),
} = {}) {
  const args = parseArgs(argv);
  if (args.help) {
    output.write("Usage: node auth.mjs PROVIDER [--method oauth|api_key]\n");
    return 0;
  }
  const configuredDir = env.BELLO_PI_AGENT_DIR || env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
  const agentDir = resolve(configuredDir);
  mkdirSync(agentDir, { recursive: true, mode: 0o700 });
  const controller = new AbortController();
  const onInterrupt = () => controller.abort(new Error("Authentication cancelled"));
  process.once("SIGINT", onInterrupt);
  try {
    const runtime = await runtimeFactory({
      authPath: join(agentDir, "auth.json"),
      modelsPath: join(agentDir, "models.json"),
      allowModelNetwork: false,
      refreshOnCreate: false,
      signal: controller.signal,
    });
    const provider = runtime.getProvider(args.providerId);
    if (!provider) throw new Error(`Unknown Pi provider: ${args.providerId}`);
    const interaction = createTerminalInteraction({ input, output, signal: controller.signal });
    const method = await chooseAuthType(provider, args.method, interaction);
    await runtime.login(args.providerId, method, interaction);
    output.write(`Authentication saved for ${args.providerId}.\n`);
    return 0;
  } catch (error) {
    output.write(`Authentication failed: ${redactErrorMessage(error)}\n`);
    return 1;
  } finally {
    process.removeListener("SIGINT", onInterrupt);
  }
}
