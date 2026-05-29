# Browser Interaction Redesign

This document is the implementation contract for replacing the current
`browser_script` interaction layer.

## Goals

- Keep Rust as the browser lifecycle and CDP transport owner.
- Remove the Python `browser_script` interaction surface.
- Add a persistent Bun JavaScript executor for model-authored browser code.
- Route every browser interaction CDP call through a Rust-owned broker.
- Give the model Codex-style execute/observe/cancel semantics with no explicit
  background flag.
- Stop reconnecting browsers after ordinary snippet, selector, timeout, or CDP
  method errors.

## Non-Goals

- Do not remove local, managed, cloud, or remote-CDP browser setup.
- Do not move browser ownership or recovery into Bun.
- Do not make Bun open a direct WebSocket to Chrome.
- Do not preserve Python helper compatibility.

## High-Level Architecture

```text
LLM
 |
 | browser_execute / browser_observe / browser_cancel
 v
Rust tool dispatcher
 |
 | ensure configured browser + active CDP broker
 v
Rust BrowserSession / BrowserManager
 |
 | owns endpoint, lifecycle, target/session state, recovery policy
 v
Rust CdpBroker
 |
 | owns the Chrome CDP WebSocket
 | demuxes responses, buffers events, applies timeouts/cancel
 v
Chrome / local browser / managed Chromium / Browser Use cloud / remote CDP

Bun executor
 |
 | persistent JS runtime and job registry only
 | requests CDP calls/events from Rust over IPC
 v
Rust CdpBroker
```

Rust is the single owner of browser lifecycle and CDP transport. Bun is a
persistent JavaScript runtime that evaluates model code and exposes convenient
browser helpers, but it does not own the browser.

## Rust Browser Manager: Keep

Keep the current Rust control plane:

- `BrowserSession` state.
- local browser discovery, setup, and profile scanning.
- managed Chromium launch and cleanup.
- Browser Use cloud start, stop, status, profiles, and live URL.
- remote CDP connect by HTTP or WebSocket URL.
- endpoint probing, redaction, and safety flags.
- status, doctor, ownership, and recovery internals.
- failure classification and diagnosis.
- artifact/image recording infrastructure when reusable.

The broad human/debug CLI can remain internally, but normal model-facing browser
lifecycle actions should be typed tools.

## Old Interaction Layer: Remove

Remove the current interaction plane:

- `browser_script` model-facing tool.
- `action=start|observe|cancel` inside a single browser interaction tool.
- fresh Python worker process per script.
- Python helper prelude and `browser_script_helpers.py`.
- Python-to-Rust TCP bridge.
- `browser script runs/cancel` model-facing behavior.
- browser-script-specific prompt text and stale Python interaction skills.
- tests that only validate the removed Python behavior.

Do not use the old implementation as the new runtime template. Keep a feature
inventory, but rebuild the interaction path around Rust-owned CDP broker
semantics.

## Rust CDP Broker

The current synchronous CDP path is lifecycle-grade, not interaction-grade. The
new interaction runtime needs a broker with these properties:

- owns the CDP WebSocket in Rust.
- uses one reader loop.
- demuxes responses by request id.
- retains unrelated events instead of dropping them.
- exposes event subscriptions to running JS jobs.
- supports an event ring buffer for late observers.
- routes `sessionId` explicitly.
- applies per-call timeout and job cancellation.
- classifies browser death, target loss, session loss, and ordinary CDP method
  errors separately.

Hard rules:

```text
Do not hold the global browser/session lock while waiting for a CDP response.
Do not discard unrelated CDP events while waiting for a response.
Do not reconnect after ordinary CDP method errors.
```

The old `CdpConnection::call()` may remain temporarily for lifecycle fallback,
but `browser_execute` jobs must use the broker.

## LLM-Facing Tools

The normal browser interaction tools are:

```text
browser_execute
browser_observe
browser_cancel
browser_status
browser_configure
browser_recover
done
```

### browser_execute

Starts JavaScript browser interaction code and waits only `yield_time_ms`.

```json
{
  "code": "await session.Page.navigate({ url: 'https://example.com' });",
  "yield_time_ms": 1000,
  "timeout_ms": 60000,
  "description": "Open example.com"
}
```

If the job finishes before `yield_time_ms`, return the final result. If it is
still running, return `status: "running"`, a `run_id`, and any partial logs,
checkpoints, images, artifacts, or summaries.

There is no explicit `background` flag.

### browser_observe

Observes a running browser job.

```json
{
  "run_id": "br-...",
  "yield_time_ms": 1000
}
```

It returns new output or the final result. Empty output is allowed when nothing
new happens before the yield deadline.

### browser_cancel

Cancels a stale or no-longer-needed browser job.

```json
{
  "run_id": "br-...",
  "reason": "Superseded by a narrower inspection"
}
```

Cancellation should propagate into Bun job state and Rust CDP calls. It must not
kill or reconnect the browser.

### browser_status

Returns compact diagnostics:

- selected mode.
- connection state.
- redacted endpoint.
- owner and safety flags.
- current target/session.
- active jobs.
- live URL.
- last issue and next step.

### browser_configure

Only use for explicit user-directed setup changes, such as:

- connect to this CDP URL.
- use this Browser Use API key.
- switch to local, managed, cloud, or remote-CDP mode.
- use this profile.

Do not use it as speculative recovery.

### browser_recover

Only use after a classified lifecycle failure:

- browser dead.
- WebSocket disconnected.
- target/session gone and page unusable.
- owned managed/cloud browser must be restarted/stopped.

Do not recover after JS exceptions, selector misses, ordinary CDP method errors,
or bounded job timeouts when the browser/page is still usable.

## Bun Executor

Bun owns persistent JavaScript job execution only.

It exposes:

```js
session.Page.navigate(...)
session.Runtime.evaluate(...)
session.Input.dispatchMouseEvent(...)
cdp(method, params, options)
listPageTargets()
session.use(targetId)
waitFor(predicateOrEvent, options)
onEvent(eventName, handler)
checkpoint(label, data)
sleep(ms)
console.log(...)
```

Bun communicates with Rust over a small IPC protocol:

```text
Bun -> Rust: cdp.call, event.subscribe, artifact.write, image.attach, checkpoint
Rust -> Bun: cdp.result, cdp.error, cdp.event, cancel, timeout, status
```

Bun never connects to Chrome directly.

## Output Model

Tool output should preserve:

- logs.
- checkpoints.
- structured result data.
- summary items.
- browser events.
- image attachments.
- artifact records.
- diagnosis for failures.

`Page.captureScreenshot` results should be auto-attached as images when possible.

## Prompt Contract

Keep the prompt short:

```text
Use browser_execute for page interaction. It auto-yields with run_id if still
running. Use browser_observe for running jobs and browser_cancel only for stale
or hung jobs. Use browser_configure only when the user explicitly asks to change
setup. Use browser_recover only after real browser lifecycle failure. Do not
reconnect after normal JS/CDP errors.
```

Interaction skills should be native skills, sourced primarily from
`browser-harness-js`, not hard-inlined Python/browser_script instructions.

## Verification

Run the standard repo checks after implementation:

```bash
cargo fmt --check
cargo test
uv run --with pytest python -m pytest -q
```

Focused browser checks:

- managed browser starts.
- remote CDP connects.
- `browser_execute` returns a fast result.
- `browser_execute` auto-yields `run_id`.
- `browser_observe` completes a job.
- `browser_cancel` cancels a job.
- CDP events are retained while responses are pending.
- screenshot output attaches an image.
- JS errors do not reconnect.
- ordinary CDP method errors do not reconnect.
- real browser death permits gated recovery.
