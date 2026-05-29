const readline = require("node:readline");

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

const jobs = new Map();
const pendingCdp = new Map();
const sessionState = {
  activeSessionId: null,
  listeners: new Set(),
  eventPump: null,
};
let callSeq = 1;

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeConsole(runId) {
  return {
    log: (...args) => send({ type: "run.log", run_id: runId, text: args.map(String).join(" ") + "\n" }),
    error: (...args) => send({ type: "run.log", run_id: runId, text: args.map(String).join(" ") + "\n" }),
    warn: (...args) => send({ type: "run.log", run_id: runId, text: args.map(String).join(" ") + "\n" }),
  };
}

function makeCdp(runId, job) {
  return async function cdp(method, params = {}, options = {}) {
    if (job.cancelled) {
      throw new Error("browser job cancelled");
    }
    const callId = `${runId}-${callSeq++}`;
    const wait = deferred();
    pendingCdp.set(callId, wait);
    send({
      type: "cdp.call",
      run_id: runId,
      call_id: callId,
      method,
      params,
      session_id: options.sessionId ?? options.session_id ?? null,
      timeout_ms: options.timeout_ms ?? options.timeoutMs ?? 20000,
    });
    const result = await Promise.race([wait.promise, job.cancelPromise]);
    if (job.cancelled) {
      throw new Error("browser job cancelled");
    }
    if (method === "Page.captureScreenshot" && result && result.data) {
      const format = params && params.format === "jpeg" ? "jpeg" : params && params.format === "webp" ? "webp" : "png";
      send({
        type: "run.image",
        run_id: runId,
        label: options.label || "screenshot",
        mime_type: format === "jpeg" ? "image/jpeg" : format === "webp" ? "image/webp" : "image/png",
        data: result.data,
        detail: options.detail || "auto",
      });
    }
    return result;
  };
}

function normalizeEvent(event) {
  return {
    method: event.method,
    params: event.params || {},
    sessionId: event.sessionId || event.session_id || null,
    raw: event,
  };
}

function dispatchEvents(events) {
  for (const event of events) {
    const normalized = normalizeEvent(event);
    for (const listener of [...sessionState.listeners]) {
      try {
        listener(normalized.method, normalized.params, normalized.sessionId, normalized.raw);
      } catch {
        // Event listeners are agent-owned convenience callbacks; one bad listener
        // should not break the event pump or another listener.
      }
    }
  }
}

async function drainEvents(cdp) {
  const result = await cdp("BrowserUse.drainEvents", {}, { sessionId: null, timeoutMs: 1000 });
  const events = Array.isArray(result && result.events) ? result.events : [];
  if (events.length) {
    dispatchEvents(events);
  }
  return events.map(normalizeEvent);
}

function startEventPump(cdp, job) {
  if (sessionState.eventPump) {
    return;
  }
  sessionState.eventPump = (async () => {
    try {
      while (!job.done && !job.cancelled && sessionState.listeners.size > 0) {
        try {
          await drainEvents(cdp);
        } catch {
          break;
        }
        await sleep(50);
      }
    } finally {
      sessionState.eventPump = null;
    }
  })();
}

function makeSession(runId, job, cdp) {
  const session = {
    get activeSessionId() {
      return sessionState.activeSessionId;
    },
    setActiveSession(sessionId) {
      sessionState.activeSessionId = sessionId || null;
    },
    async connect(options = undefined) {
      if (options && Object.keys(options).length > 0) {
        throw new Error(
          "session.connect(options) is not supported in browser_execute. " +
          "Rust owns browser setup; use browser_configure to change the browser connection when explicitly requested.",
        );
      }
      return {
        status: "already-connected",
        activeSessionId: sessionState.activeSessionId,
        targets: await listPageTargets(cdp),
      };
    },
    async use(targetId) {
      const result = await cdp(
        "Target.attachToTarget",
        { targetId, flatten: true },
        { sessionId: null },
      );
      sessionState.activeSessionId = result.sessionId;
      send({
        type: "browser.set_current_target",
        run_id: runId,
        target_id: targetId,
        session_id: result.sessionId,
      });
      return result;
    },
    onEvent(listener) {
      if (typeof listener !== "function") {
        throw new Error("session.onEvent requires a function");
      }
      sessionState.listeners.add(listener);
      startEventPump(cdp, job);
      return () => {
        sessionState.listeners.delete(listener);
      };
    },
    async waitFor(method, predicate = () => true, timeoutMs = 10000) {
      if (typeof predicate === "number") {
        timeoutMs = predicate;
        predicate = () => true;
      }
      if (typeof predicate !== "function") {
        throw new Error("session.waitFor predicate must be a function when provided");
      }
      const deadline = Date.now() + Math.max(1, Number(timeoutMs || 10000));
      while (Date.now() < deadline) {
        const events = await drainEvents(cdp);
        for (const event of events) {
          if (event.method === method && predicate(event.params, event.sessionId, event.raw)) {
            return event;
          }
        }
        await sleep(Math.min(100, Math.max(1, deadline - Date.now())));
      }
      throw new Error(`Timed out waiting for ${method} after ${timeoutMs} ms`);
    },
  };
  return new Proxy(session, {
    get(target, prop) {
      if (prop in target) {
        return target[prop];
      }
      if (typeof prop !== "string") {
        return target[prop];
      }
      return new Proxy({}, {
        get(_domainTarget, method) {
          if (typeof method !== "string") {
            return undefined;
          }
          return (params = {}, options = {}) => {
            const fullMethod = `${prop}.${method}`;
            const browserLevel = fullMethod.startsWith("Target.") || fullMethod.startsWith("Browser.") || fullMethod.startsWith("Storage.");
            const callOptions = { ...options };
            if (!browserLevel && callOptions.sessionId === undefined && callOptions.session_id === undefined) {
              callOptions.sessionId = sessionState.activeSessionId;
            }
            return cdp(fullMethod, params, callOptions);
          };
        },
      });
    },
  });
}

async function listPageTargets(cdp) {
  const result = await cdp("Target.getTargets", {}, { sessionId: null });
  return (result.targetInfos || []).filter((target) => {
    if (target.type !== "page") {
      return false;
    }
    const url = target.url || "";
    return !url.startsWith("chrome://") && !url.startsWith("devtools://");
  });
}

function makeCheckpoint(runId) {
  return (label, data = null) => {
    send({ type: "run.checkpoint", run_id: runId, label, data });
  };
}

async function runJob(message) {
  const runId = message.run_id;
  const cancel = deferred();
  const job = {
    cancelled: false,
    done: false,
    cancelPromise: cancel.promise,
    cancelReject: cancel.reject,
  };
  jobs.set(runId, job);
  const cdp = makeCdp(runId, job);
  const session = makeSession(runId, job, cdp);
  const checkpoint = makeCheckpoint(runId);
  const scopedConsole = makeConsole(runId);
  try {
    const fn = new AsyncFunction(
      "session",
      "cdp",
      "listPageTargets",
      "sleep",
      "checkpoint",
      "console",
      `"use strict";\n${message.code}`,
    );
    const timeoutMs = Math.max(1, Number(message.timeout_ms || 60000));
    const timeout = new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`browser_execute timed out after ${timeoutMs} ms`)), timeoutMs);
    });
    const result = await Promise.race([
      fn(session, cdp, () => listPageTargets(cdp), sleep, checkpoint, scopedConsole),
      timeout,
      job.cancelPromise,
    ]);
    if (job.cancelled) {
      send({ type: "run.cancelled", run_id: runId });
    } else {
      send({ type: "run.result", run_id: runId, result: result === undefined ? null : result });
    }
  } catch (error) {
    if (job.cancelled) {
      send({ type: "run.cancelled", run_id: runId });
    } else {
      send({ type: "run.error", run_id: runId, error: error && error.stack ? error.stack : String(error) });
    }
  } finally {
    job.done = true;
    jobs.delete(runId);
  }
}

function cancelJob(message) {
  const job = jobs.get(message.run_id);
  if (!job) {
    send({ type: "run.cancelled", run_id: message.run_id });
    return;
  }
  job.cancelled = true;
  job.cancelReject(new Error(message.reason || "browser job cancelled"));
}

rl.on("line", (line) => {
  if (!line.trim()) {
    return;
  }
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    send({ type: "run.log", run_id: "unknown", text: `invalid host message: ${error}\n` });
    return;
  }
  if (message.type === "execute") {
    runJob(message);
  } else if (message.type === "cancel") {
    cancelJob(message);
  } else if (message.type === "cdp.result" || message.type === "cdp.error") {
    const pending = pendingCdp.get(message.call_id);
    if (!pending) {
      return;
    }
    pendingCdp.delete(message.call_id);
    if (message.type === "cdp.error") {
      pending.reject(new Error(message.error || "CDP call failed"));
    } else {
      pending.resolve(message.result);
    }
  }
});

send({ type: "ready" });
