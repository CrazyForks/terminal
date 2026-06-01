# DECISIONS.md — settled choices & sanctioned divergences

Decision log for the rearchitecture (see `REARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`).
Append new entries as they're made.

## Sanctioned divergences from codex (intentional — preserve, justify, test)
- **D-DIV-1 Multi-provider** instead of OpenAI-only (the product needs OpenAI/Anthropic/local).
- **D-DIV-2 SQLite as a write-only sink, not a hot loader.** Dump events/state to SQLite for durability + debuggability + resume, but keep runtime state **in-memory**; **never read/poll SQLite on the hot path**; read it **only on resume**. (Reading it hot is "slow as hell".)
- **D-DIV-3 Sync/serial `view_image`** — blocking read, never parallel with browser actions, so screenshots are observed in order.
- **D-DIV-4 Browser + Python tool surface** (our product; browser layer = `browser-harness-js`, treated as a black box).
- **D-DIV-5 Drop the codex/ChatGPT backend** entirely (server-side, can't use) — its headers/OAuth/identifiers go with it.

Everything else = **EXTREME PARITY** with codex (mechanism, heuristics, thresholds, outputs), proven by tests.

## Settled open questions
- **D-1 Context messages:** align with codex's typed `reference_context` mechanism for parity; our extra kinds (permissions/personality/goal/collaboration/hook/mention/generated-image/browser) become additional typed items. *(User: "sure whatever" → pick the parity-aligned option.)*
- **D-2 Subagent wait:** **event-notify** (in-memory mailbox notification), not the 50ms SQLite poll. Messages still dumped to SQLite for the record. *(User: "I prefer event-notify"; consistent with D-DIV-2.)*
- **D-3 Crate granularity:** engine = one `browser-use-core` crate with submodules; separate crates for `browser-use-llm`, `browser-use-mcp`, `browser-use-sandbox`, `browser-use-guardian`. *(User: "whatever you think is prettier".)*
- **D-4 Async migration order:** top-down (runtime → loop → tools) behind the frozen Phase-0 interfaces, optimized for **correctness + testability, not speed**. *(User: "whatever will make you implement BETTER … focus is NOT speed … everything gets done, run and tested".)*
- **D-5 v1 providers:** OpenAI (Responses), Anthropic (Messages + Claude-Code OAuth), Ollama, **DeepSeek, OpenRouter, Fireworks** (via the `openai-chat` protocol where compatible). The protocol × provider design makes additional providers config-only — added freely later.

## Process decisions
- **D-P-1 No human in the loop.** The orchestrating agent does all coordination, review, gating, and the serial carve.
- **D-P-2 Tests first, commit first.** Each WP commits parity/e2e tests before implementing; frequent working-increment commits; Phase 0 commits per extraction step.
- **D-P-3 Run-and-test, not compile-only.** Acceptance = behavior runs + tests pass against a **live model**. E2E uses the **existing codex auth** as the live-model vehicle during the build (revisit once OpenAI/Anthropic direct auth is wired — see `IMPLEMENTATION_PLAN.md` §5).
- **D-P-4 Goal = completeness over speed.** Parallelism is for isolation/depth, not finishing fast.
