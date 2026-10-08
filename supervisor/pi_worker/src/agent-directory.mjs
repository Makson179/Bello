import { homedir } from "node:os";
import { isAbsolute, join, resolve, sep } from "node:path";

// Login and worker startup must address the same provider credentials/catalog.
// Resolve before the worker changes cwd; Python supplies its resolved path.
export function piAgentDirectory({ env = process.env, cwd = process.cwd(), home = homedir() } = {}) {
  let configured = env.BELLO_PI_AGENT_DIR || env.PI_CODING_AGENT_DIR || join(home, ".pi", "agent");
  if (configured === "~") configured = home;
  else if (configured.startsWith(`~${sep}`) || configured.startsWith("~/")) {
    configured = join(home, configured.slice(2));
  }
  return isAbsolute(configured) ? resolve(configured) : resolve(cwd, configured);
}
