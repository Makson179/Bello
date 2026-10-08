import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";

function discoveryKey(key, env = {}) {
  // Match Pi's literal/environment templates, but leave commands inert. A
  // command-backed key is configured; only execution can prove its value.
  if (key === undefined || key.startsWith("!")) return key;
  let result = "";
  for (let index = 0; index < key.length;) {
    if (key[index] !== "$") { result += key[index++]; continue; }
    const next = key[index + 1];
    if (next === "$" || next === "!") { result += next; index += 2; continue; }
    const braced = next === "{";
    const end = braced ? key.indexOf("}", index + 2) : -1;
    const name = braced
      ? (end < 0 ? undefined : key.slice(index + 2, end))
      : key.slice(index + 1).match(/^[A-Za-z_][A-Za-z0-9_]*/u)?.[0];
    if (!name || !/^[A-Za-z_][A-Za-z0-9_]*$/u.test(name)) {
      if (braced && end >= 0) { result += key.slice(index, end + 1); index = end + 1; }
      else result += key[index++];
      continue;
    }
    const value = env[name] || process.env[name];
    if (!value) return undefined;
    result += value;
    index = braced ? end + 1 : index + name.length + 1;
  }
  return result;
}

// Pi's execution stores acquire write locks, create missing files, and resolve
// credential commands on read. Discovery needs only configured metadata. Keep
// these snapshots separate from the normal SDK stores used by login/sessions.
class LocalSnapshot {
  constructor(path, credentials = false) {
    this.path = path;
    this.credentials = credentials;
    this.reload();
  }

  reload() {
    let data;
    try {
      data = JSON.parse(readFileSync(this.path, "utf8").replace(/^\uFEFF/u, ""));
    } catch (error) {
      if (error.code !== "ENOENT") throw new Error("Cannot read Pi local catalog/auth metadata");
      data = {};
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("Pi local catalog/auth metadata must be an object");
    }
    if (this.credentials) {
      for (const credential of Object.values(data)) {
        const valid = credential && typeof credential === "object" && !Array.isArray(credential)
          && ((credential.type === "oauth" && typeof credential.access === "string"
            && typeof credential.refresh === "string" && Number.isFinite(credential.expires))
          || (credential.type === "api_key" && (credential.key === undefined || typeof credential.key === "string")
            && (credential.env === undefined || (credential.env && typeof credential.env === "object"
              && !Array.isArray(credential.env) && Object.values(credential.env).every((value) => typeof value === "string")))));
        if (!valid) throw new Error("Pi local auth metadata contains an invalid credential");
      }
    }
    this.data = data;
  }

  async read(providerId, options) {
    options?.signal?.throwIfAborted();
    const value = Object.hasOwn(this.data, providerId) ? structuredClone(this.data[providerId]) : undefined;
    if (this.credentials && value?.type === "api_key") value.key = discoveryKey(value.key, value.env);
    return value;
  }

  async list(options) {
    options?.signal?.throwIfAborted();
    return Object.entries(this.data).map(([providerId, credential]) => ({ providerId, type: credential?.type }));
  }

  async modify() { throw new Error("Pi catalog discovery cannot modify credentials"); }
  async write() { throw new Error("Pi catalog discovery cannot write model caches"); }
  async delete() { throw new Error("Pi catalog discovery cannot delete stored metadata"); }
}

export async function createCatalogRuntime({ agentDir }) {
  try {
    const credentials = new LocalSnapshot(join(agentDir, "auth.json"), true);
    const modelsStore = new LocalSnapshot(join(agentDir, "models-store.json"));
    const runtime = await ModelRuntime.create({
      credentials,
      modelsStore,
      modelsPath: join(agentDir, "models.json"),
      allowModelNetwork: false,
      // create() discards provider refresh errors. Inspect the actual result
      // before this candidate can become the worker's published snapshot.
      refreshOnCreate: false,
    });
    const refresh = runtime.refresh.bind(runtime);
    runtime.refresh = async (options = {}) => {
      if (options.allowNetwork === true) throw new Error("Pi catalog refresh is local only");
      return refresh({ ...options, allowNetwork: false });
    };
    const result = await runtime.refresh({ allowNetwork: false });
    if (result.aborted || result.errors.size || runtime.getError()) {
      throw new Error("Invalid Pi local catalog");
    }
    return runtime;
  } catch {
    // SDK parse/provider errors may contain keys, headers, or file contents.
    throw new Error("Pi local catalog could not be loaded; check the agent directory configuration");
  }
}
