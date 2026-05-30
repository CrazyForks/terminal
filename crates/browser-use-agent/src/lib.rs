//! `browser-use-agent` — the async agent engine (rearchitecture Milestone 3).
//!
//! Strategy **B (parallel rewrite)**: this crate is built alongside the legacy
//! synchronous `browser-use-core`, on top of the async, provider-neutral
//! `browser-use-llm`. Subsystems are ported here one at a time, each with
//! codex-parity tests, and the TUI/CLI are switched over to this engine only
//! once parity is reached. Until then `browser-use-core` remains the live engine.
//!
//! WP 3.1 (this commit) is an empty, compiling scaffold — no behavior yet.
//!
//! Planned module layout (added per later WPs):
//! - `turn`    — the async turn loop (codex parity: unbounded loop on
//!               needs-follow-up, `CancellationToken`, `FuturesOrdered` tool sched)
//! - `context` — context manager with REAL token accounting (per-provider `Usage`)
//! - `orchestrator` — `ToolOrchestrator` + `ToolRuntime`/`Approvable`/`Sandboxable`
//! - `session` — session lifecycle + resume over SQLite as a write-sink

#[cfg(test)]
mod tests {
    /// Scaffold smoke test: the crate builds and the async test harness runs.
    #[tokio::test]
    async fn crate_builds_and_async_harness_runs() {
        assert_eq!(2 + 2, 4);
    }
}
