import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline/promises";
import { Writable } from "node:stream";

import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { piAgentDirectory } from "./agent-directory.mjs";
import { OPENAI_API_ROUTE_ERROR } from "./billing-route.mjs";
import { ORCAROUTER_PROVIDER_ID, createOrcaConnectLogin, registerLoginProviders, registerOrcaRouter } from "./orcarouter.mjs";

const NEVER_ABORTED = new AbortController().signal;
const inputQueue = new Map();

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
  const previous = inputQueue.get(input) ?? Promise.resolve();
  const current = previous.catch(() => {}).then(() => promptLine(input, output, text, signal));
  inputQueue.set(input, current);
  try {
    return await current;
  } finally {
    if (inputQueue.get(input) === current) inputQueue.delete(input);
  }
}

async function promptLine(input, output, text, signal) {
  // Closing this interface must not close the caller's shared stdin.
  const lines = createInterface({ input, output, terminal: Boolean(input.isTTY) });
  try {
    // One reader at a time: a prompt already waiting on the shared stdin must
    // not consume the next prompt's line. Keep it armed during an abort so a
    // pending read settles instead of becoming an unsettled top-level await.
    return await lines.question(text, { signal: AbortSignal.any([signal, NEVER_ABORTED]) });
  } finally {
    lines.close();
    if (!input.isTTY) input.resume?.();
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
  if (provider.id !== "openai" && provider.auth?.oauth?.login) types.push("oauth");
  if (provider.auth?.apiKey?.login) types.push("api_key");
  return types;
}

export async function chooseAuthType(provider, requestedType, interaction) {
  if (provider.id === "openai" && requestedType === "oauth") throw new Error(OPENAI_API_ROUTE_ERROR);
  // OrcaRouter's single login adapter already offers "Connect with OrcaRouter"
  // (OAuth 2.0 + PKCE) and its API key side by side, so Pi's generic
  // oauth/api_key prompt would only repeat the same two choices.
  const available = availableAuthTypes(provider);
  if (provider.id === ORCAROUTER_PROVIDER_ID && requestedType === undefined) return "oauth";
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
  loginOptions = {},
} = {}) {
  const args = parseArgs(argv);
  if (args.help) {
    output.write("Usage: node auth.mjs PROVIDER [--method oauth|api_key]\n");
    return 0;
  }
  const agentDir = piAgentDirectory({ env });
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
    // Login addresses the same named providers the execution runtime uses.
    const interaction = createTerminalInteraction({ input, output, signal: controller.signal });
    let method;
    if (args.providerId === ORCAROUTER_PROVIDER_ID) {
      // OrcaRouter's Connect (OAuth 2.0 + PKCE) and its API key are one login
      // adapter with a single choice list, so --method maps to that list
      // instead of adding a second, duplicate method prompt.
      const connect = createOrcaConnectLogin(loginOptions);
      const adapterLogin = (cb) => connect.login({
        signal: cb.signal,
        notify: (event) => cb.notify?.(event),
        prompt: (prompt) => (typeof cb.prompt === "function"
          ? cb.prompt(prompt)
          : interaction.prompt(prompt)),
      });
      const apiMethod = {
        login: (callback) => adapterLogin({
          ...callback,
          prompt: async (prompt) => (prompt.type === "select" ? "api_key" : interaction.prompt(prompt)),
        }),
      };
      await registerOrcaRouter(runtime, {
        loginOptions: { fetchImpl: loginOptions.fetchImpl },
        login: args.method === undefined ? adapterLogin : apiMethod,
      });
      const provider = runtime.getProvider(args.providerId);
      if (!provider) throw new Error(`Unknown Pi provider: ${args.providerId}`);
      method = await chooseAuthType(provider, args.method, interaction);
    } else {
      await registerLoginProviders(runtime, { loginOptions: { fetchImpl: loginOptions.fetchImpl } });
      const provider = runtime.getProvider(args.providerId);
      if (!provider) throw new Error(`Unknown Pi provider: ${args.providerId}`);
      method = await chooseAuthType(provider, args.method, interaction);
    }
    if (!runtime.getProvider(args.providerId)) throw new Error(`Unknown Pi provider: ${args.providerId}`);
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
