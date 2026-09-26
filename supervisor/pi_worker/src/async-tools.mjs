// Per-turn command futures. A placeholder is immutable once returned to Pi;
// completed late output is a new message, never a replacement in old history.
export class AsyncToolCoordinator {
  constructor({ graceMs = 1000 } = {}) {
    this.graceMs = graceMs;
    this.jobs = new Map();
    this.batch = undefined;
    this.waiters = new Set();
    this.cancelled = false;
    this.externalInputPending = false;
  }

  beginBatch(callIds) {
    this.batch = { ids: new Set(callIds), started: Date.now() };
  }

  changed() {
    for (const wake of this.waiters) wake();
    this.waiters.clear();
  }

  notifyExternalInput() {
    this.externalInputPending = true;
    this.changed();
  }

  async wait(timeout) {
    if (this.cancelled) throw new DOMException("Turn interrupted", "AbortError");
    await new Promise((resolve) => {
      let timer;
      const wake = () => {
        if (timer !== undefined) clearTimeout(timer);
        this.waiters.delete(wake);
        resolve();
      };
      this.waiters.add(wake);
      if (timeout !== undefined) timer = setTimeout(wake, Math.max(0, timeout));
    });
    if (this.cancelled) throw new DOMException("Turn interrupted", "AbortError");
  }

  get pending() {
    return [...this.jobs.values()].some((job) => !job.delivered);
  }

  async execute(callId, name, execute, signal) {
    const batch = this.batch;
    if (!batch?.ids.has(callId)) return execute(signal);
    if (this.jobs.has(callId)) throw new Error(`Duplicate asynchronous tool call: ${callId}`);
    const controller = new AbortController();
    const abort = () => {
      controller.abort();
      this.changed();
    };
    if (signal?.aborted) abort();
    signal?.addEventListener("abort", abort, { once: true });
    const job = { callId, name, batch, controller, done: false, delivered: false, placeholder: false };
    this.jobs.set(callId, job);
    job.promise = Promise.resolve().then(() => execute(controller.signal)).then(
      (result) => { job.result = result; },
      (error) => {
        job.error = error;
        job.result = { content: [{ type: "text", text: error instanceof Error ? error.message : String(error) }], details: {}, isError: true };
      },
    ).finally(() => {
      job.done = true;
      signal?.removeEventListener("abort", abort);
      this.changed();
    });
    while (!this.cancelled && !signal?.aborted) {
      const completed = [...this.jobs.values()].filter((entry) => entry.batch === batch && entry.done).length;
      const earlierResultReady = [...this.jobs.values()].some((entry) => entry.batch !== batch && entry.placeholder && entry.done && !entry.delivered);
      const remaining = this.graceMs - (Date.now() - batch.started);
      // No model wakeup with only "still running" results, even after grace.
      if (this.externalInputPending || completed === batch.ids.size || ((completed > 0 || earlierResultReady) && remaining <= 0)) break;
      await this.wait(remaining > 0 ? remaining : undefined);
    }
    if (this.cancelled || signal?.aborted) throw new DOMException("Turn interrupted", "AbortError");
    if (job.done) {
      job.delivered = true;
      if (job.error) throw job.error;
      return job.result;
    }
    job.placeholder = true;
    return {
      content: [{ type: "text", text: `Tool call ${callId} is still running. Its result arrives automatically in a later message. Continue necessary independent work, or end your turn to wait. Do not poll.` }],
      details: { asyncToolCallId: callId, status: "running" },
    };
  }

  async ready({ wait = false } = {}) {
    while (wait && this.pending && !this.externalInputPending && ![...this.jobs.values()].some((job) => job.placeholder && job.done && !job.delivered)) {
      await this.wait();
    }
    this.externalInputPending = false;
    const ready = [];
    for (const job of this.jobs.values()) {
      if (job.placeholder && job.done && !job.delivered) {
        job.delivered = true;
        ready.push({ callId: job.callId, name: job.name, ...job.result });
      }
    }
    return ready;
  }

  async cancel() {
    this.cancelled = true;
    for (const job of this.jobs.values()) if (!job.done) job.controller.abort();
    this.changed();
    await Promise.all([...this.jobs.values()].map((job) => job.promise));
  }
}

export function lateToolMessage(results) {
  return {
    customType: "bello-async-tool-results",
    content: results.flatMap((result) => [
      { type: "text", text: `Completed tool call ${result.callId} (${result.name})${result.isError ? " — failed" : ""}:` },
      ...result.content,
    ]),
    display: false,
    details: { callIds: results.map((result) => result.callId) },
  };
}
