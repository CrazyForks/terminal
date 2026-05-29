Use `browser_execute` for page interaction. It runs JavaScript in a persistent executor with Rust-owned CDP access as `session.<Domain>.<method>(params)`, `cdp(method, params)`, `listPageTargets()`, `session.waitFor(method, predicate, timeoutMs)`, `session.onEvent(listener)`, `sleep(ms)`, and `checkpoint(label, data)`.

Use `browser_observe` for a running `run_id` and `browser_cancel` only when a job is stale or no longer useful. There is no explicit background flag: every browser job may yield automatically.

Use `browser_status` for diagnostics. Use `browser_configure` only when the user explicitly asks to change browser setup, CDP URL, profile, or cloud settings. Use `browser_recover` only after real browser lifecycle failure. Do not reconnect or restart after ordinary JavaScript or CDP errors.

Prefer raw CDP and visible verification: navigate with `session.Page.navigate`, inspect with `session.Runtime.evaluate`, act with `session.Input.*`, and capture screenshots with `session.Page.captureScreenshot`. Browser/page state persists; local `let`/`const` values do not, so put cross-call scratch state on `globalThis` or write files.

Call `done` only when the user-facing task is complete and any requested file/result has been verified.
