import { once } from "node:events";

export class ProtocolError extends Error {
  constructor(message, code = "protocol_error", data) {
    super(message);
    this.name = "ProtocolError";
    this.code = code;
    this.data = data;
  }
}

export function errorPayload(error) {
  if (error instanceof ProtocolError) {
    return {
      code: error.code,
      message: error.message,
      ...(error.data === undefined ? {} : { data: error.data }),
    };
  }
  return {
    code: "internal_error",
    message: error instanceof Error ? error.message : String(error),
  };
}

export class JsonlWriter {
  constructor(stream) {
    this.stream = stream;
    this.tail = Promise.resolve();
  }

  send(value) {
    const line = `${JSON.stringify(value)}\n`;
    const write = async () => {
      if (this.stream.destroyed || this.stream.writableEnded) {
        throw new ProtocolError("worker output stream is closed", "stream_closed");
      }
      if (!this.stream.write(line, "utf8")) {
        await once(this.stream, "drain");
      }
    };
    this.tail = this.tail.then(write, write);
    return this.tail;
  }
}

function abortError() {
  const error = new Error("tool call was cancelled");
  error.name = "AbortError";
  return error;
}

export class HostCallBroker {
  constructor(send) {
    this.send = send;
    this.sequence = 0;
    this.pending = new Map();
    this.closed = false;
  }

  call(params, signal) {
    if (this.closed) {
      return Promise.reject(new ProtocolError("host call broker is closed", "stream_closed"));
    }
    const requestId = `tool-${++this.sequence}`;
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        signal?.removeEventListener("abort", onAbort);
        this.pending.delete(requestId);
        callback(value);
      };
      const onAbort = () => {
        void this.send({
          method: "bello/tool/cancel",
          params: {
            requestId,
            threadId: params.threadId,
            turnId: params.turnId,
            callId: params.callId,
          },
        });
        finish(reject, abortError());
      };
      if (signal?.aborted) {
        onAbort();
        return;
      }
      signal?.addEventListener("abort", onAbort, { once: true });
      this.pending.set(requestId, { resolve: (value) => finish(resolve, value), reject: (error) => finish(reject, error) });
      void this.send({ id: requestId, method: "bello/tool", params }).catch((error) => finish(reject, error));
    });
  }

  accept(message) {
    const pending = this.pending.get(message.id);
    if (!pending) return false;
    if (Object.hasOwn(message, "error")) {
      const raw = message.error;
      const detail = raw && typeof raw === "object" && typeof raw.message === "string" ? raw.message : String(raw);
      pending.reject(new ProtocolError(`host tool failed: ${detail}`, "host_tool_error"));
    } else if (message.result && typeof message.result === "object" && !Array.isArray(message.result)) {
      pending.resolve(message.result);
    } else {
      pending.reject(new ProtocolError("host tool response must contain an object result", "protocol_error"));
    }
    return true;
  }

  close(error = new ProtocolError("worker input stream closed", "stream_closed")) {
    this.closed = true;
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }
}

export class WorkerServer {
  constructor({ runtime, writer, hostCalls }) {
    this.runtime = runtime;
    this.writer = writer;
    this.hostCalls = hostCalls;
    this.pendingRequests = new Set();
  }

  accept(message) {
    if (!message || typeof message !== "object" || Array.isArray(message)) {
      void this.writer.send({ method: "worker/error", params: { message: "protocol frame must be an object" } });
      return;
    }
    if (!Object.hasOwn(message, "method")) {
      if (!this.hostCalls.accept(message)) {
        void this.writer.send({ method: "worker/error", params: { message: "unexpected response id" } });
      }
      return;
    }
    if (!Object.hasOwn(message, "id")) {
      void this.writer.send({ method: "worker/error", params: { message: "host notifications are not supported" } });
      return;
    }
    const request = this.handleRequest(message);
    this.pendingRequests.add(request);
    void request.finally(() => this.pendingRequests.delete(request));
  }

  async handleRequest(message) {
    const { id } = message;
    try {
      if (typeof message.method !== "string" || !message.method) {
        throw new ProtocolError("request method must be a nonempty string");
      }
      const params = message.params ?? {};
      if (!params || typeof params !== "object" || Array.isArray(params)) {
        throw new ProtocolError("request params must be an object");
      }
      const result = await this.runtime.dispatch(message.method, params);
      if (!result || typeof result !== "object" || Array.isArray(result)) {
        throw new ProtocolError("worker method returned a non-object result", "internal_error");
      }
      await this.writer.send({ id, result });
    } catch (error) {
      await this.writer.send({ id, error: errorPayload(error) });
    }
  }

  async waitForIdle() {
    await Promise.allSettled([...this.pendingRequests]);
    await this.writer.tail;
  }
}
