import { spawn } from "node:child_process";

// Test-owned children must not outlive a failed assertion or startup timeout.
export function spawnTestProcess(t, executable, args, { env = process.env } = {}) {
  const child = spawn(executable, args, { stdio: ["pipe", "pipe", "pipe"], env });
  let stdout = "";
  let stderr = "";
  let failure;
  let closed;
  let cleanupPromise;
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.on("error", (error) => { failure = error; });
  child.stdin.on("error", (error) => { failure ??= error; });
  child.once("close", (code, signal) => { closed = { code, signal }; });

  const diagnostic = (message) => new Error(
    `${message}; pid=${child.pid ?? "not spawned"}; `
    + `exit=${closed ? JSON.stringify(closed) : "still open"}`
    + `${failure ? `; process error=${failure.message}` : ""}`
    + `\nstdout tail:\n${stdout.slice(-4000)}\nstderr tail:\n${stderr.slice(-4000)}`,
  );

  async function until(predicate, timeoutMs, description, allowFailedProcess = false) {
    const deadline = Date.now() + timeoutMs;
    while (true) {
      const result = predicate();
      if (result) return result;
      if (!allowFailedProcess && (failure || closed)) {
        throw diagnostic(`worker failed before ${description}`);
      }
      if (Date.now() >= deadline) throw diagnostic(`timed out after ${timeoutMs} ms waiting for ${description}`);
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }

  function cleanup() {
    cleanupPromise ??= (async () => {
      if (closed) return;
      child.stdin.destroy();
      try {
        child.kill("SIGKILL");
        await until(() => closed, 5000, "child cleanup", true);
      } finally {
        // Even a failed OS-level termination must not leave test-owned pipes
        // keeping the parent test runner alive indefinitely.
        child.stdout.destroy();
        child.stderr.destroy();
        child.unref();
      }
    })();
    return cleanupPromise;
  }

  // Register before callers can write, await startup, or make assertions.
  t.after(cleanup);
  return {
    child,
    cleanup,
    get stdout() { return stdout; },
    get stderr() { return stderr; },
    waitForOutput: (predicate, timeoutMs = 30_000) => until(
      () => predicate(stdout), timeoutMs, "worker output",
    ),
    // close is recorded from spawn onward, so an already-exited child cannot
    // race a late once(child, "close") subscription.
    waitForClose: (timeoutMs = 30_000) => until(
      () => closed, timeoutMs, "worker close", true,
    ),
  };
}
