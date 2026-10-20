const interrupted = () => new DOMException("Turn interrupted", "AbortError");

// Per-turn command futures. A placeholder is immutable once returned to Pi;
// completed late output is a new message, never a replacement in old history.
//
// Completion collection is per assistant tool batch. The first finished result
// of a batch opens a `graceMs` window and every further completion of that same
// batch restarts it, so siblings that finish together reach the model together.
// A batch whose calls have all finished is ready at once. Nothing but a real
// result ever opens a window: a batch of still-running calls never wakes the
// model, and another batch's activity can neither extend nor shorten a window.
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
    this.batch = { ids: new Set(callIds), completed: 0, readyAt: undefined, released: false };
  }

  batchReady(batch, now = performance.now()) {
    return batch.completed === batch.ids.size || (batch.readyAt !== undefined && batch.readyAt <= now);
  }

  // Only new completions move a deadline. Once a group is ready, it stays ready
  // until its collected results are consumed, even if its timer wakes late.
  completed(job) {
    const now = performance.now();
    const { batch } = job;
    const collecting = [...this.jobs.values()].some((entry) => entry.batch === batch && entry.done && !entry.delivered);
    if (!collecting || batch.readyAt > now) batch.readyAt = now + this.graceMs;
    job.done = true;
    batch.completed++;
    this.changed();
  }

  lateJobs() {
    return [...this.jobs.values()].filter((job) => job.placeholder && job.done && !job.delivered);
  }

  collectionDelay(batches, now = performance.now()) {
    const deadlines = [...batches].map((batch) => batch.readyAt).filter((deadline) => deadline !== undefined);
    return deadlines.length ? Math.max(0, Math.min(...deadlines) - now) : undefined;
  }

  changed() {
    for (const wake of this.waiters) wake();
    this.waiters.clear();
  }

  notifyExternalInput() {
    this.externalInputPending = true;
    this.changed();
  }

  // Pi can finish a call without ever invoking execute(): an unknown tool,
  // invalid arguments or a truncated response get an immediate error result.
  // Such a call can never produce a late result, so it must not keep an
  // otherwise finished batch open. Call it for a tool_execution_end event.
  skip(callId) {
    const { batch } = this;
    if (!batch?.ids.has(callId) || this.jobs.has(callId)) return;
    batch.ids.delete(callId);
    this.changed();
  }

  async wait(timeout) {
    if (this.cancelled) throw interrupted();
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
    if (this.cancelled) throw interrupted();
  }

  // Results awaiting delivery. Cancellation discards them: nothing is ever
  // delivered afterwards, so a cancelled coordinator is never pending.
  get pending() {
    return !this.cancelled && [...this.jobs.values()].some((job) => !job.delivered);
  }

  async execute(callId, name, execute, signal) {
    // Never start a tool after cancellation: nobody would await or abort it.
    if (this.cancelled) throw interrupted();
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
    job.promise = Promise.resolve().then(() => {
      // A call aborted before its turn to run (cancellation, Pi abort) is not started.
      controller.signal.throwIfAborted();
      return execute(controller.signal);
    }).then(
      (result) => { job.result = result; },
      (error) => {
        job.error = error;
        job.result = { content: [{ type: "text", text: error instanceof Error ? error.message : String(error) }], details: {}, isError: true };
      },
    ).finally(() => {
      signal?.removeEventListener("abort", abort);
      this.completed(job);
    });
    while (!this.cancelled && !signal?.aborted) {
      const batches = new Set([batch, ...this.lateJobs().map((entry) => entry.batch)]);
      const now = performance.now();
      if (batch.released || this.externalInputPending || [...batches].some((entry) => this.batchReady(entry, now))) {
        // Every sibling must observe the same release, including after another
        // execute() has already consumed the result that made this batch ready.
        batch.released = true;
        break;
      }
      // With no actual results there is no timer and no placeholder-only wake.
      await this.wait(this.collectionDelay(batches, now));
    }
    if (this.cancelled || signal?.aborted) {
      // The caller receives an error rather than a result, so nothing remains
      // to deliver for this call; it must not leave the turn pending forever.
      job.delivered = true;
      throw interrupted();
    }
    if (job.done && (this.externalInputPending || this.batchReady(batch))) {
      job.delivered = true;
      if (job.error) throw job.error;
      return job.result;
    }
    job.placeholder = true;
    return {
      content: [{ type: "text", text: `Tool call ${callId} ${job.done ? "has completed and is awaiting delivery with its sibling results" : "is still running"}. Its result arrives automatically in a later message. Continue necessary independent work, or end your turn to wait. Do not poll.` }],
      details: { asyncToolCallId: callId, status: job.done ? "awaiting_delivery" : "running" },
    };
  }

  async ready({ wait = false } = {}) {
    while (true) {
      if (this.cancelled) throw interrupted();
      const batches = new Set(this.lateJobs().map((job) => job.batch));
      const now = performance.now();
      if (!wait || !this.pending || this.externalInputPending || [...batches].some((batch) => this.batchReady(batch, now))) break;
      await this.wait(this.collectionDelay(batches, now));
    }
    const immediate = this.externalInputPending;
    this.externalInputPending = false;
    const ready = [];
    const now = performance.now();
    for (const job of this.lateJobs()) {
      if (immediate || this.batchReady(job.batch, now)) {
        job.delivered = true;
        ready.push({ callId: job.callId, name: job.name, ...job.result });
      }
    }
    return ready;
  }

  async cancel() {
    this.cancelled = true;
    for (const job of this.jobs.values()) {
      if (!job.done) job.controller.abort();
      // Nothing is delivered after cancellation, so nothing stays pending.
      job.delivered = true;
    }
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
