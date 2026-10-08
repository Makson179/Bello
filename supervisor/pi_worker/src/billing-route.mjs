// Bello reserves openai/* for API billing; ChatGPT uses native openai-codex/*.
// Pi 1.0 also offers subscription OAuth under openai. Never silently use it.
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
