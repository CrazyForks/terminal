# HANDOFF — engine cutover (browser-use-core → browser-use-agent)

## STATUS (2026-05-31): CUTOVER COMPLETE + browser/python tools wired. `decodex` @ a7648c1, pushed.

The legacy `browser-use-core` engine (~62k LOC) is **deleted**. `browser-use-tui` and
`browser-use-cli` run entirely on the new async `browser-use-agent` engine. Codex/ChatGPT
backend is **cut** from every run path. Merged to `decodex` and pushed to origin.

### Verified state
- `cargo build --workspace` — green, **core deleted**.
- `cargo test --workspace` — **1241 passed, 0 failed**.
- tui + cli: **zero** `browser_use_core` references.
- Codex: no `chatgpt.com/backend-api` in default build; auth reader gated behind non-default
  `codex-dev` feature; `ProviderBackend::Codex` → typed "cut" error. Codex credential-import
  CLI commands kept (harmless; no run path).
- **Tools that execute through the turn loop (9):** shell, apply_patch, view_image, update_plan,
  request_user_input, tool_search, web_search, **browser**, **python**. Each verified registered
  + reachable through the orchestrator seam (not stubs); python worker started once per real run
  with typed-error-on-failure + Drop teardown (no leaked process).
- **Live model layer proven** (real round-trips): OpenAI **gpt-5.5**, Anthropic **claude-sonnet-4-6**,
  OpenRouter **openai/gpt-4o-mini**. (A duplicate-Content-Type bug that broke ALL OpenAI calls was fixed.)

## REMAINING GAPS (real, not yet done — priority order)
1. **MCP tool — DEFERRED** (user: "we don't need it yet"). The `mcp` handler + `McpClient` trait
   exist but are NOT registered; the real stdio JSON-RPC transport (was in the deleted
   `core/mcp.rs`) needs to be built. Trait seam is ready; register it in
   `entrypoint::provider::build_tool_dispatcher` once a transport exists.
2. **Workspace-context section assembly** not ported — the entrypoint seeds only a minimal
   `<environment_context><cwd>…` block; legacy assembled AGENTS.md instructions + permissions +
   collaboration-mode + multi-agent hints. So **AGENTS.md guidance/permissions are not seeded**
   into sessions. (entrypoint/mod.rs `environment_context_content`.)
3. **Provider creds read from process ENV only**, not store settings. `auth login <provider>
   --api-key` (store) + ClaudeCode-OAuth no longer feed runs. (entrypoint/provider.rs::resolve_provider)
4. **Minimal TurnState** — no token-accounting / mid-turn compaction, no pending-input/steer queue
   (StoreTurnState in entrypoint/mod.rs). UI sink is a DiscardSink.
5. **MessageHistorySettings always default (SaveAll)** — AGENTS.md-derived history settings
   not re-resolved; local-image inlining emits a text placeholder (no base64); model catalog is a
   minimal mirror (no model_catalog_json override / full presets).
6. **Dead code:** `CodexResponsesProvider` (~349 refs) still physically present in
   `browser-use-providers` but unreachable (no run path constructs it). A future cleanup can delete it.

## Honest live-run status
Model HTTP layer proven live for all 3 providers. browser/python tool dispatch is proven OFFLINE
(network-free reachability tests through the real orchestrator seam). A full **live** app turn
(model + browser tool, end-to-end) has NOT been run — needs a non-codex key in env; test keys were
used only for the model-layer smoke and then securely wiped. To run it: set OPENAI_API_KEY (or
ANTHROPIC_API_KEY) and exercise a browser task through the entrypoint.

## Suggested next work (priority)
1. Live end-to-end browser turn (needs a key in env) — prove the full loop, not just the model layer.
2. Workspace-context section assembler (gap #2) — so AGENTS.md/permissions reach the model.
3. Real ContextManager-backed TurnState with token accounting + compaction (gap #4).
4. Decide store-vs-env credential resolution (gap #3).
5. MCP transport + registration (gap #1) when needed.
6. Delete the dead CodexResponsesProvider code (gap #6).
