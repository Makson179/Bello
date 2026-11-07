// Bello reserves openai/* for API billing; ChatGPT uses native openai-codex/*.
// Pi 1.0 also offers subscription OAuth under openai. Never silently use it.
import { ORCAROUTER_PROVIDER_ID, createAuthHealth } from "./orcarouter.mjs";

export const OPENAI_API_ROUTE_ERROR =
  "Bello openai/* requires API-key authentication; stored Pi OpenAI OAuth is not " +
  "used or replaced automatically. Choose native openai-codex/* for ChatGPT " +
  "subscription access, or explicitly configure Pi openai with an API key.";

export function billingRouteError(runtime, provider) {
  return provider === "openai" && runtime.isUsingOAuth?.(provider)
    ? OPENAI_API_ROUTE_ERROR : undefined;
}

function assertCredentialRoute(provider, credential) {
  if (provider === "openai" && credential?.type === "oauth") {
    const error = new Error(OPENAI_API_ROUTE_ERROR);
    error.code = "billing_route_conflict";
    throw error;
  }
}

export const ORCAROUTER_REAUTH_ERROR =
  "OrcaRouter rejected the stored credential (HTTP 401). OrcaRouter keys are durable and " +
  "Bello never falls back to another key or clears it automatically: run " +
  "`bello runtime login orcarouter` again for this account.";

/**
 * A 401 from OrcaRouter means the stored key for the account that made the
 * request was revoked or replaced. Report it for that exact credential only: a
 * late failure from a superseded key never marks the key now in use. Bello does
 * not refresh (there is no refresh grant), delete the credential, or retry with
 * another key; the next request fails with an explicit re-login request while
 * the credential stays recoverable.
 */
export function enforceOrcaRouterReauth(runtime, orca = ORCAROUTER_PROVIDER_ID, health = createAuthHealth()) {
  const api = runtime.models ?? runtime;
  if (typeof api.getProvider !== "function") return { runtime, health };
  const providerId = ORCAROUTER_PROVIDER_ID;
  // Observe each request credential on its way to the wire. `getAuth` is not
  // instrumented: it also serves display-only account reads.
  const originalGetProvider = api.getProvider.bind(api);
  api.getProvider = (id) => {
    const provider = originalGetProvider(id);
    if (id !== providerId || !provider?.auth?.apiKey?.resolve || provider.auth.apiKey.__orcaObserved) return provider;
    const auth = {
      ...provider.auth,
      apiKey: {
        ...provider.auth.apiKey,
        __orcaObserved: true,
        resolve: async (input) => {
          const result = await provider.auth.apiKey.resolve(input);
          const key = result?.auth?.apiKey;
          if (health.status({ type: "api_key", key })?.needsReauth) {
            const error = new Error(ORCAROUTER_REAUTH_ERROR);
            error.code = "orcarouter_needs_reauth";
            throw error;
          }
          health.observe(key ? { type: "api_key", key } : undefined);
          return result;
        },
      },
    };
    return { ...provider, auth };
  };
  return { runtime, health };
}

export function enforceExecutionBillingRoute(runtime) {
  // Guard the actual SDK credential read, not merely the discovery snapshot or
  // session startup. Another process can replace auth while a session is idle
  // or between requests. Throw before OAuth derivation/refresh; returning no
  // credential here would silently fall back to an ambient API key.
  const credentials = runtime.credentials;
  const read = credentials.read.bind(credentials);
  credentials.read = async (provider, options) => {
    const credential = await read(provider, options);
    assertCredentialRoute(provider, credential);
    return credential;
  };
  const modify = credentials.modify.bind(credentials);
  credentials.modify = (provider, update, options) => modify(provider, async (current) => {
    assertCredentialRoute(provider, current);
    const next = await update(current);
    assertCredentialRoute(provider, next);
    return next;
  }, options);
  const getAuth = runtime.getAuth.bind(runtime);
  runtime.getAuth = async (...args) => {
    try {
      return await getAuth(...args);
    } catch (error) {
      // Pi wraps credential-store errors. Preserve our safe, actionable message.
      for (let cause = error, depth = 0; cause && depth < 8; cause = cause.cause, depth++) {
        if (cause.code === "billing_route_conflict") throw cause;
      }
      throw error;
    }
  };
  return runtime;
}
