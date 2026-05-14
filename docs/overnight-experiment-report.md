# Overnight Experiment Report

Worktree: `/Users/greg/Documents/browser-use/experiments/overnight-experiment`

Branch: `overnight-experiment`

Protocol: `docs/overnight-experiment-loop.md`

This is the living scientific log for the autonomous eval-and-improve loop. Append every experiment, including negative results, failed commands, reverted changes, skipped runs, and environment problems.

## Dashboard

| Field | Current State |
| --- | --- |
| Recommended branch state | Starting baseline at `57af289` |
| Latest `real_v8` strict/manual score | Not run in this worktree yet |
| Latest `real_v14_short` strict/manual score | Smoke run: runner 8/10 before interruption; manual strict 2 pass / 6 partial / 2 fail |
| Latest `BU_Bench_V1` strict/manual score | Not run in this worktree yet |
| Most important improvement | Host-side hard timeout for Python worker calls |
| Worst regression | None yet |
| Open root-cause clusters | Field-quality validation, over-broad extraction, artifact finalization after output exists |
| Next experiment | Repeat `real_v14_short` smoke with `.env` Browser Use cloud credentials present |

## Experiment 20260513-01: Baseline Remote-Browser Runs

- Hypothesis: The current `overnight-experiment` baseline needs a fresh remote-browser measurement before making changes.
- Intervention: No code changes. Run datasets with Browser Use cloud/remote browser only and 25-way concurrency.
- Expected movement: Establish current scores and failure modes for this worktree.
- Datasets/runs: Pending.
- Metrics: Pending.
- Failure-mode changes: Pending.
- Regressions: Pending.
- Code/prompt diff summary: None.
- Decision: Pending.
- Next: Run `real_v14_short`, `real_v8`, and optionally `BU_Bench_V1`; then judge failures with subagents.

### Run: `overnight-real-v14-short-20260513-212314`

- Dataset: `real_v14_short`
- Root: `/tmp/overnight-real-v14-short-20260513-212314`
- Manifest: `/tmp/overnight-real-v14-short-20260513-212314/state/dataset-runs/overnight-real-v14-short-20260513-212314.json`
- Command: `dataset-run-codex real_v14_short --all --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 25 --browser-mode cloud`
- Browser mode: cloud/remote browser only
- Local Chrome/CDP: no evidence this run opened local Chrome; active children were `browser-use-terminal` and `llm_browser_worker.worker`
- Runner summary before manual interruption:
  - Passed: 8
  - Failed: 1
  - Pending: 1
  - Passed IDs: `2, 6, 8, 9, 10, 11, 13, 16`
  - Failed IDs: `5`
  - Pending IDs: `4`
  - Tokens recorded by manifest: `16,310,744` total, cost missing

The run was manually interrupted because task `4` stalled indefinitely after already writing `outputs/result.json` and `.final_answer.json`. The last DB event was `tool.started` for a Python snippet making broad threaded/network location API requests. No `tool.finished`, `tool.failed`, or terminal session event followed.

Manual judging by subagent:

| Task | Runner | Manual | Notes |
| ---: | --- | --- | --- |
| 2 | done | partial | Rows/files found, but several summaries report full document text could not be extracted before timeout. |
| 4 | pending/running | pass | `outputs/result.json` has 668 Ollie's stores; runner never finalized after artifact was ready. |
| 5 | failed | fail | No final artifact; exceeded provider turns while reverse-engineering telecom APIs. |
| 6 | done | partial | Five ads/screenshots, but one selected ad lacks creative image and browser-filtered Ads Library flow was not clearly completed. |
| 8 | done | partial | 28 SSD comparisons, but some products have fewer than three comparison offers. |
| 9 | done | pass | Screenshot artifact shows the requested SBI home-loan table. |
| 10 | done | fail | Surgeon candidates over-broadened beyond Beverly Hills; sample profile is Minneapolis and `javascript:;`. |
| 11 | done | partial | Counts returned, but evidence came from FCCID.io mirror rather than the requested FCC UI snippets. |
| 13 | done | pass | 1,367 provider records with rich metadata. |
| 16 | done | partial | JSON has menu categories/prices but includes duplicate item names despite "no duplicates". |

Manual strict score for this smoke run:

- Pass: 2
- Partial: 6
- Fail: 2

Important failure clusters:

- Runner/tool reliability: task `4` produced valid artifacts but then hung inside Python, leaving the manifest pending.
- Turn-budget/model strategy: task `5` ran out of provider turns while exploring APIs.
- Field/source fidelity: tasks `6`, `8`, `10`, `11`, and `16` had structurally useful outputs but failed strict semantic/source requirements.
- Finalization discipline: task `4` continued exploration after `session.final_answer_ready` instead of finishing.

Root-cause analysis for the stall:

- `--python-timeout-seconds` was only passed into the Python worker.
- Python implemented it as `SIGALRM` around `exec(...)`.
- The Rust side blocked forever on `stdout.read_line()` waiting for the worker's final JSON response.
- Model-written Python can defeat cooperative signal timeouts through blocking network calls, DNS/SSL, C extension calls, or `ThreadPoolExecutor` cleanup waiting for worker threads.
- The dataset scheduler waits on `rx.recv()` for active task completion, so a stuck agent thread prevents manifest completion.

### Intervention: Host-Side Python Tool Hard Timeout

- Hypothesis: A host-enforced Python tool deadline will prevent one stuck model-written Python snippet from hanging a dataset slot forever.
- Change:
  - Store Python worker launch configuration.
  - Run each worker in its own process group.
  - Add a Rust-side deadline around worker stdout response reads.
  - On timeout, kill the whole worker process group, restart the worker, return a failed Python tool response to the agent, and continue the loop.
  - Add a regression test for `ThreadPoolExecutor` shutdown hangs.
- Files changed:
  - `crates/browser-use-python-worker/src/lib.rs`
  - `crates/browser-use-python-worker/Cargo.toml`
- Verification:
  - `cargo fmt --check`: passed
  - `cargo test`: passed
  - `uv run --with pytest python -m pytest -q`: passed, `15 passed`
- Decision: Keep. This is generalizable runtime reliability, not benchmark-specific validation.
- Next:
  - Run a focused `real_v14_short` task `4` rerun to verify the scheduler no longer hangs on the same class.
  - Then rerun `real_v14_short` smoke and compare runner completion plus manual quality.

### Targeted Check: Task 4 Hard-Timeout Path

First attempt:

- Run ID: `overnight-real-v14-task4-timeout-check-20260513-215244`
- Result: invalid as a verification run because it was launched before rebuilding `target/debug/browser-use-terminal` after the library change.
- Action: interrupted and discarded as evidence.

Second attempt:

- Run ID: `overnight-real-v14-task4-hard-timeout-check-20260513-220114`
- Command: `dataset-run-codex real_v14_short --task-id 4 --max-turns 20 --python-timeout-seconds 5 --max-attempts 1 --concurrency 1 --browser-mode cloud`
- Result: runner completed cleanly with `failed: 1`, `pending: 0`.
- Failure reason: `agent exceeded maximum provider turns`.
- Artifact result: `outputs/result.json` existed but had `stores: []`.
- Important read: the scheduler did not hang; the attempt ended as a normal manifest failure.
- Hard-timeout event: not triggered in this focused rerun because the model's requests raised normal `requests` timeout/connection errors before the host hard timeout had to kill the worker.

Additional run-hygiene finding:

- Both the smoke and targeted rerun show `browser_harness_error: "Browser Use cloud selected, but BROWSER_USE_API_KEY is not set"`.
- That means the Python browser helpers were unavailable, so the model fell back to raw HTTP/API scraping.
- This likely depresses strict benchmark quality and must be fixed before trusting a full overnight benchmark comparison.

Decision after targeted check:

- Keep the host-side Python hard-timeout change because unit tests reproduce the first-principles hang and the focused eval no longer left a pending scheduler slot.
- Before running full `real_v8`/`BU_Bench_V1`, fix or document the missing Browser Use cloud API credential so remote browser access is actually available.

### Run Hygiene Fix: Cloud Credential In Worktree

- Finding: the overnight worktree had no `.env`, while the original worktree had a gitignored `.env` containing `BROWSER_USE_API_KEY`.
- Action: copied the original `.env` into the overnight worktree.
- Git status: `.env` remains ignored and must not be committed.
- Expected effect: subsequent cloud-mode runs should have actual browser helpers instead of `browser_harness_error: "Browser Use cloud selected, but BROWSER_USE_API_KEY is not set"`.
- Next measurement: repeat `real_v14_short` before running larger datasets.
