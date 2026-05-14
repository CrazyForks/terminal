# Overnight Experiment Report

Worktree: `/Users/greg/Documents/browser-use/experiments/overnight-experiment`

Branch: `overnight-experiment`

Protocol: `docs/overnight-experiment-loop.md`

This is the living scientific log for the autonomous eval-and-improve loop. Append every experiment, including negative results, failed commands, reverted changes, skipped runs, and environment problems.

## Dashboard

| Field | Current State |
| --- | --- |
| Recommended branch state | Selection/ranking audit guard implemented; focused task 6 rerun improved strict output |
| Latest `real_v8` strict/manual score | Not run in this worktree yet |
| Latest `real_v14_short` strict/manual score | Focus tasks 6/10/16: runner 3/3; strict manual 1 pass / 1 partial / 1 fail; focused task 6 after selection-audit: runner pass / manual pass |
| Latest `BU_Bench_V1` strict/manual score | Not run in this worktree yet |
| Most important improvement | Selection audit forced task 6 from first-visible/weak ranking to audited top-by-duration selection over 107 candidates |
| Worst regression | None yet |
| Open root-cause clusters | Source-scope fallback/locality proof for directory aggregation; exact metric unavailable vs proxy disclosure |
| Next experiment | Implement general source-scope/locality audit for broadened searches, then rerun task 10 and full `real_v14_short`/`real_v8` |

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

### Run: `overnight-real-v14-short-cloud-20260513-220555`

- Dataset: `real_v14_short`
- Root: `/tmp/overnight-real-v14-short-cloud-20260513-220555`
- Manifest: `/tmp/overnight-real-v14-short-cloud-20260513-220555/state/dataset-runs/overnight-real-v14-short-cloud-20260513-220555.json`
- Command: `dataset-run-codex real_v14_short --all --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 25 --browser-mode cloud`
- Runner result: `10/10` passed, `0` failed, `0` pending
- Manual strict result: `7` pass, `3` partial, `0` fail
- Browser helper evidence:
  - Missing API key errors: `0`
  - `browser.state` events: `184`
  - `tool.image` events: `73`
  - Host hard-timeout events: `1`
  - Python alarm timeout events: `1`
- Token usage: `8,405,288` total tokens, cost missing

Manual judging:

| Task | Runner | Manual | Notes |
| ---: | --- | --- | --- |
| 2 | pass | pass | Covered first two FERC rows and file set with URLs and summaries. |
| 4 | pass | pass | Extracted `682` unique Ollie's locations; earlier no-key run had `668` and stayed pending. |
| 5 | pass | pass | Captured all five requested telecom source groups, `103` total records. |
| 6 | pass | partial | Returned 5 ads and screenshots, but some copy was truncated and "best performing" relied on inference rather than verified engagement. |
| 8 | pass | pass | 28 SSD records; 4 have fewer than 3 offers, apparently because fewer were available. |
| 9 | pass | pass | Readable screenshot of full SBI table. |
| 10 | pass | partial | 126 surgeons, but only 12 from ASPS, 114 ABPS unknown, Hair Restoration only 22 candidates vs requested 40. |
| 11 | pass | pass | All 7 grantee codes with counts and evidence snippets. |
| 13 | pass | pass | 1,367 unique WakeMed profile URLs with broad metadata. |
| 16 | pass | partial | 19 categories and 271 rows, but only 162 unique item/price pairs despite "no duplicates". |

Comparison against no-key smoke:

- Task `4`: pending/pass artifact became runner pass with complete `682` stores.
- Task `5`: hard fail became pass.
- Task `6`: improved from partial with missing creative evidence to partial with actual creative/detail screenshots.
- Task `10`: hard fail/effectively no useful records became partial with a large record set.
- Task `16`: coverage improved but dedupe still failed.

Interpretation:

- The cloud credential was a major run-hygiene fix. The first smoke was not a trustworthy cloud-browser benchmark.
- The host-side hard-timeout intervention is working in live runs: hard timeout fired without wedging the scheduler.
- Remaining short-dataset failures are quality/finalization problems rather than browser/runtime failures.

Next generalizable hypothesis:

- The model needs a stronger pre-final self-review contract that explicitly checks requested count thresholds, uniqueness/dedupe requirements, source/selection caveats, and evidence for inferred ranking criteria before it calls `done`.

### Intervention: General Final Self-Review Contract

- Hypothesis: Remaining `real_v14_short` partials are mostly task-contract failures, not browser failures. A small general prompt intervention should push the model to review count targets, dedupe requirements, hard filters, source scope, and ranking evidence before finalization.
- Intervention:
  - Clarify that the overnight loop has no convergence stop condition; convergence is only logged and the user manually stops the loop.
  - Add a dataset-case final self-review contract covering per-bucket counts, dedupe, hard filters, source scope, ranking proxies, and explicit gap reporting.
  - Add a system final self-review rule that asks for compact artifact summaries/counts instead of giant JSON output.
- Expected movement:
  - Task `6`: better disclosure of the "best performing" proxy or deeper evidence before selecting ads.
  - Task `10`: stronger pressure to satisfy requested per-specialty/per-source counts or report real source gaps.
  - Task `16`: fewer duplicate menu items before finalization.
- Regression risk:
  - Extra review could consume turns on already-finished tasks.
  - The prompt may cause conservative partial disclosure instead of confident completion.
- Verification:
  - `cargo fmt --check`: passed
  - `cargo test`: passed
  - `uv run --with pytest python -m pytest -q`: passed, `15 passed`
- Decision: Keep as first revision; focused rerun showed improvement but not enough for strict pass.

### Focused Rerun: `overnight-real-v14-self-review-focus-20260513-223203`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-self-review-focus-20260513-223203`
- Manifest: `/tmp/overnight-real-v14-self-review-focus-20260513-223203/state/dataset-runs/overnight-real-v14-self-review-focus-20260513-223203.json`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `0` pass, `3` partial, `0` fail
- Token usage: `1,549,556` total tokens, cost missing

Manual judging:

| Task | Runner | Manual | Delta vs previous cloud run | Evidence |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | Improved selection disclosure and duration fields; still failed screenshot artifact validity. | `outputs/result.json`, `outputs/ad_*_creative.png`, `outputs/search_results_top.png` |
| 10 | pass | partial | Improved ASPS extraction from 12-ish useful ASPS records to `173` ASPS results and reached `40` candidates per specialty; still has blank practice fields and `187/272` combined surgeons without specialties. | `outputs/result.json`, `artifacts/images/shot-3.png` |
| 16 | pass | partial | Did not fix dedupe. Coverage was lower than previous cloud run: `18` categories / `218` rows / `140` unique item-price pairs vs previous `19` / `271` / `162`. | `outputs/result.json`, `outputs/cache.json` |

Concrete checks:

- Task `6` visual artifacts:
  - Every per-ad `card` and `creative` PNG was a single solid color, `(240, 242, 245)`, with one sampled color.
  - Full-page screenshots had hundreds of sampled colors and showed real page content.
  - Root cause: the model treated file existence and dimensions as sufficient, but did not visually/pixel-check that clipped screenshots contained the target ad.
- Task `10` source/field completeness:
  - `topplasticsurgeonreviews_full_list`: `114`
  - `asps_results`: `173`
  - `combined_surgeons`: `272`
  - specialty lists: all `13` specialties had `40` candidates.
  - `abps_board_certified`: `191` true, `81` null.
  - Important caveat: many top-review-only records had no specialties, and ASPS practice fields were blank.
- Task `16` dedupe:
  - `18` categories, `218` item rows, `140` unique item-price pairs, `78` duplicate item-price pairs.
  - Duplicates were absent within individual categories but repeated globally across category sections such as `Most Ordered`, `Burgers`, and `Extra Value Meals`.

Interpretation:

- The first self-review prompt moved the model in the right direction for task `6` ranking disclosure and task `10` count targets.
- It did not make the model inspect visual artifact validity.
- It did not define dedupe scope strongly enough; the model likely interpreted "no duplicates" as no duplicates within each category, not no duplicate items in the whole returned JSON.
- It did not force enough per-field coverage review when the task asks "for each surgeon, identify specialties" and practice/ABPS details.

Decision:

- Keep `553b343`, because it improved useful behavior without adding deterministic benchmark logic.
- Revise the prompt again with general self-review checks for:
  - visual artifact nonblank/content verification;
  - global dedupe scope unless the task explicitly scopes dedupe more narrowly;
  - missing-field counts for per-record required fields;
  - final answers pointing to the full artifact, not only a summary artifact.

### Intervention: Self-Review Revision For Artifacts, Dedupe, And Missing Fields

- Hypothesis: The first self-review prompt was too abstract. The remaining misses need general but concrete checks for visual artifact content, global dedupe scope, and required-field coverage.
- Intervention:
  - Treat `no duplicates` as global across the returned artifact unless explicitly scoped otherwise.
  - Tell the model to verify screenshots/files contain requested content, not just that a file exists.
  - Tell the model to report missing-field counts for `for each record` tasks and revisit source/detail pages when many values are blank.
  - Tell the model to point final answers at the full artifact path first when a summary is also provided.
- Expected movement:
  - Task `6`: avoid blank per-ad screenshot crops or report unavailable screenshots.
  - Task `10`: better caveats and/or deeper per-record field enrichment.
  - Task `16`: global dedupe across repeated menu sections.
- Verification:
  - `cargo fmt --check`: passed
  - `cargo test`: passed
  - `uv run --with pytest python -m pytest -q`: passed, `15 passed`
- Decision: Mixed. Keep the visual-artifact clause conceptually, but do not treat this revision as sufficient.

### Focused Rerun: `overnight-real-v14-self-review-r2-focus-20260513-224908`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-self-review-r2-focus-20260513-224908`
- Manifest: `/tmp/overnight-real-v14-self-review-r2-focus-20260513-224908/state/dataset-runs/overnight-real-v14-self-review-r2-focus-20260513-224908.json`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `0` pass, `3` partial, `0` fail
- Token usage: `2,575,039` total tokens, cost missing

Manual judging:

| Task | Runner | Manual | Delta vs first focused rerun | Evidence |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | Improved visual artifacts: per-ad creatives are real JPG media with thousands of sampled colors instead of blank crops. Still lacks detailed screenshot evidence for each selected ad and selection evidence remains weak. | `outputs/result.json`, `outputs/ad_*_creative_1.jpg`, `outputs/search_results_overview.png` |
| 10 | pass | partial | More honest about gaps and better ASPS practice/address extraction for scraped ASPS records. Regressed on coverage: `159` records vs `272`, ASPS `76` vs `173`, and only some specialties reached 40. | `outputs/result.json`, `outputs/result.csv`, DB events around task `10` seq `745-775` |
| 16 | pass | partial | No improvement. Global duplicate count stayed `78` and store address regressed from requested `94103` to `94110`. | `outputs/result.json`, `outputs/menu_nodes.json` |

Concrete checks:

- Task `6` visual artifacts:
  - Five `ad_*_creative_1.jpg` files exist and are real media: each had thousands of sampled colors.
  - The prompt caused the model to fetch media directly instead of trusting blank clips.
  - Remaining weakness: `search_results_overview.png` and `viewport_verify.png` are not enough to prove detailed per-ad screenshot coverage.
- Task `10` missing-field / per-bucket audit:
  - `record_count`: `159`
  - `topplasticsurgeonreviews_count`: `114`
  - `asps_unique_scraped`: `76`
  - Specialty counts: Breast `42`, Rhinoplasty `42`, Face Lift `42`, Facial Reconstruction `42`, Liposuction `47`, Eyelid `42`, Tummy Tuck `33`, Mommy Makeover `20`, Injectors `0`, Cosmetic/Anti-Aging `0`, BBL `0`, Cosmetic Laser `0`, Hair Restoration `0`.
  - Missing practice count: `83`; missing specialties count: `92`.
  - Good behavior: final answer explicitly exposed gaps rather than claiming completion.
  - Bad behavior: the runner still marked it pass because the final answer was honest but incomplete.
- Task `16` dedupe:
  - `18` categories, `218` rows, `140` unique item-price pairs, `78` duplicate item-price pairs.
  - Same duplicate count as first focused rerun.
  - Store address was output as `302 Potrero Ave, San Francisco, CA 94110`, while the task requested `94103`.

Interpretation:

- More prose in the prompt has diminishing returns.
- The visual-artifact clause is useful and should stay.
- The dedupe and missing-field clauses did not become operational. The model needs to compute an explicit pre-final audit summary, not just "remember to check".
- Runner `ok` remains a weak signal: all three focused reruns passed in the manifest while all three were partial manually.

Decision:

- Keep `629e78b` for now, because it improved task `6` and made task `10` more honest.
- Next generalizable intervention should not be benchmark-specific validation. It should add a generic pre-final audit convention/tooling:
  - artifact path;
  - total records;
  - duplicate count and dedupe key;
  - required-field missing counts;
  - per-bucket target counts;
  - visual/file artifact validity checks;
  - explicit `ready_for_done: true/false`.

### Intervention: Operational Pre-Final Artifact Audit Helper

- Hypothesis: Prompt prose alone is too easy for the model to skip or interpret loosely. A generic helper that computes duplicate counts, missing-field counts, bucket targets, and visual-file sanity should make self-review concrete without encoding benchmark answers.
- Intervention:
  - Added `audit_artifact(...)` to the Python tool surface.
  - The helper accepts records or a JSON/CSV artifact path, optional `record_path`, required fields, dedupe fields, bucket targets, and visual files.
  - It writes `/home/user/outputs/artifact_audit.json` plus an artifact copy, and returns a compact audit with `ready_for_done`.
  - Updated system/dataset/tool prompts to tell the model to run this audit before finalizing large or artifact-heavy tasks.
- Why this is generalizable:
  - It does not know dataset IDs, expected answers, websites, or benchmark strings.
  - It does not block `done`; the model still decides whether to fix or report gaps.
  - It turns the model's own task contract into computed evidence that survives context compaction.
- Expected movement:
  - Task `16`: if the model calls `audit_artifact(records=[item for category in menu["categories"] for item in category["items"]], dedupe_fields=["item_name","item_price"])`, duplicate count becomes explicit before finalization.
  - Task `10`: per-specialty target misses and blank required fields become explicit before finalization.
  - Task `6`: blank/single-color screenshot files become explicit before finalization.
- Verification:
  - `uv run --with pytest python -m pytest -q`: passed, `16 passed`
  - `cargo fmt --check`: passed
  - `cargo test`: passed
- Decision: Helper itself works, but prompt-only adoption failed in the first focused rerun.

### Focused Rerun: `overnight-real-v14-audit-helper-focus-20260513-231435`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-audit-helper-focus-20260513-231435`
- Manifest: `/tmp/overnight-real-v14-audit-helper-focus-20260513-231435/state/dataset-runs/overnight-real-v14-audit-helper-focus-20260513-231435.json`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict local read: approximately `1` pass, `2` partial, `0` fail
- Token usage: `3,430,891` total tokens, cost missing
- Audit adoption: `0` `audit_artifact(...)` calls found; `0` `artifact_audit.json` files produced.

Manual judging:

| Task | Runner | Manual | Delta vs previous focused run | Evidence |
| ---: | --- | --- | --- | --- |
| 6 | pass | pass | Visual artifacts stayed fixed and cleaner: 5 selected ad screenshots are nonblank PNGs, plus a full search-results screenshot. | `outputs/result.json`, `outputs/ad_*.png`, `outputs/search_results_full.png` |
| 10 | pass | partial | Large improvement in coverage: `977` records, `896` ASPS unique, every specialty count above `40`; still has `100` records with empty `specialties_offered` and `81` missing practices. | `outputs/result.json`, `outputs/result.csv` |
| 16 | pass | partial | Store address corrected back to `94103`, but dedupe still failed: `248` rows, `162` unique item-price pairs, `86` duplicate item-price rows. | `outputs/result.json` |

Concrete checks:

- Task `6`:
  - 5 ad screenshots had nonblank image content.
  - `search_results_full.png` existed and had `1009` sampled colors.
  - This is the first focused rerun where the visual artifact failure appears solved.
- Task `10`:
  - Specialty candidate counts: Breast `359`, Rhinoplasty `250`, Face Lift `354`, Facial Reconstruction `402`, Liposuction `199`, Eyelid `238`, Tummy Tuck `239`, Mommy Makeover `130`, Injectors `263`, Cosmetic/Anti-Aging `276`, BBL `134`, Cosmetic Laser `187`, Hair Restoration `82`.
  - ABPS values: `889` true, `7` false, `81` null.
  - Missing-field issue remains: `100` records with no specialties and `81` missing practices.
- Task `16`:
  - `store_address`: `302 Potrero Ave, San Francisco, CA 94103, USA`
  - `18` categories, `248` item rows, `162` unique item-price pairs, `86` duplicate item-price pairs.
  - Dedupe still not global.

Interpretation:

- The audit helper was not used at all. Prompt exposure plus helper availability is not enough.
- Quality still improved on tasks `6` and `10`, likely due the accumulated prompt language, but task `16` proves the model still skips computed global checks.
- The next intervention should make audit state visible in the final-answer path. This remains non-blocking and general: `set_final_answer` can embed the last audit if present and emit `audit=missing` for large structured results without one.

### Intervention: Surface Audit State In `set_final_answer`

- Hypothesis: The model ignores optional helper calls unless the finalization path makes audit state visible. If `set_final_answer` embeds audit readiness or reports `audit=missing`, the next model turn has an explicit cue before `done`.
- Intervention:
  - `audit_artifact(...)` now stores `last_artifact_audit` in the Python namespace.
  - `set_final_answer(..., audit=audit)` accepts an explicit audit, and otherwise attaches the last audit if one exists.
  - For large structured results without an audit, `set_final_answer` includes an `audit_note` and emits `audit=missing`.
  - Prompt examples now show `audit = audit_artifact(...); set_final_answer(..., audit=audit)`.
- Verification:
  - `uv run --with pytest python -m pytest -q`: passed, `17 passed`
  - `cargo fmt --check && cargo test`: passed
- Decision: Focused rerun pending.

### Focused Rerun: `overnight-real-v14-audit-surfacing-focus-20260513-233407`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-audit-surfacing-focus-20260513-233407`
- Manifest: `/tmp/overnight-real-v14-audit-surfacing-focus-20260513-233407/state/dataset-runs/overnight-real-v14-audit-surfacing-focus-20260513-233407.json`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `0` pass, `3` partial, `0` fail
- Audit adoption: `0` `audit_artifact(...)` calls; `0` `artifact_audit.json` files.

Manual judging:

| Task | Runner | Manual | Delta vs previous focused run | Evidence |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | Regressed from the previous local judgment: 5 records exist, but several `*_card.png` files are blank/single-color and the detailed screenshots mostly show grid/detail views rather than each requested creative. | `outputs/result.json`, `outputs/ad_*.png`, `outputs/search_results_page.png` |
| 10 | pass | partial | Improved vs self-review-r2 and smaller but cleaner than audit-helper run: `410` unique surgeons, all specialty candidate buckets reach `40`, but `26` records still lack specialties, `81` lack address/phone, and `37` lack ABPS status. | `outputs/result.json`, `outputs/result.csv` |
| 16 | pass | partial | Address is correct and prices are present, but the explicit `no duplicates` requirement still fails: `207` rows, `128` unique item-price pairs, `79` duplicate rows. | `outputs/result.json` |

Concrete checks:

- Task `6` screenshot audit:
  - `ad_3_*_card.png`, `ad_4_*_card.png`, and `ad_5_*_card.png` were single-color `(240, 242, 245)` images.
  - `result.json` points to screenshot paths for all 5 ads, so file existence alone is misleading.
  - This validates the need for visual artifact checks, but the model did not call the helper.
- Task `10` artifact audit:
  - `records`: `410`; `unique_names`: `410`
  - `sources`: `329` ASPS rows, `114` topplastics rows
  - Missing fields: `address=81`, `phone=81`, `specialties=26`, `abps_board_certification=37`
  - Specialty counts all exceeded `40`, and `candidates_by_specialty` had exactly `40` each.
  - Still not strict because the task asks for names/practices/ratings and ABPS certification for each surgeon, not only enough rows per specialty.
- Task `16` dedupe:
  - `14` categories, `207` item rows, `128` unique item-price pairs, `79` duplicate item-price rows.
  - Repeated examples include `Sausage McMuffin® with Egg`, `Sausage Egg McMuffin® Meal`, and `Sausage, Egg and Cheese McGriddles® Meal`.
  - The model deduped locally or not at all; it did not flatten globally across categories before finalizing.

Interpretation:

- The `set_final_answer` audit surfacing intervention did not activate because the missing-audit heuristic only looked at shallow top-level list counts.
- Task `10` final data was a compact summary with `record_count: 410` and output paths, so shallow list counting returned `null`.
- Task `16` had a top-level `categories` list of length `14`; the actual `207` item records were nested under `categories[].items`.
- Task `6` had only 5 logical records but many visual artifact paths, which should also trigger audit recommendations.

Decision:

- Revise the intervention, not revert it.
- Keep the non-blocking design: do not reject `done` and do not benchmark-validate answers.
- Make `set_final_answer` detect audit-worthy outputs through nested records, explicit `record_count`/`item_count` fields, external structured artifact paths, and many visual artifact paths.

### Intervention: Recursive Missing-Audit Recommendation

- Hypothesis: The prior audit-surfacing mechanism was right in spirit but too shallow. The finalization path should recommend an audit when the answer is nested, references a large external artifact, or contains multiple screenshot/media paths.
- Intervention:
  - Added recursive final-answer inspection inside `set_final_answer`.
  - It now estimates record scale from nested record-like lists such as `items`, `rows`, `records`, `surgeons`, `ads`, `stores`, `products`, and `candidates`.
  - It recognizes explicit count fields such as `record_count`, `item_count`, `row_count`, `total_count`, and `total`.
  - It counts visual artifact paths (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) and structured artifact paths (`.json`, `.csv`, `.tsv`, `.xlsx`, `.pdf`, `.txt`).
  - When no audit is attached and any of these signals are large/artifact-heavy, `set_final_answer` emits `audit=missing` and stores an `audit_recommendation` with reasons.
- Why this is generalizable:
  - It still does not block finalization.
  - It does not know benchmark answers, task IDs, sites, expected counts, or special schemas.
  - It only gives the model a visible, computed prompt to perform its own audit before calling `done`.
- Expected movement:
  - Task `16`: nested `categories[].items` should surface `large_structured_result_estimate=...` before `done`.
  - Task `10`: compact summaries with `record_count` and `output_path` should surface missing audit state.
  - Task `6`: multiple screenshot paths should surface visual-artifact audit need even when record count is small.
- Verification:
  - `uv run --with pytest python -m pytest -q`: passed, `19 passed`
  - `cargo fmt --check && cargo test`: passed
- Decision: Accepted for the next eval iteration.

### Focused Rerun: `overnight-real-v14-recursive-audit-focus-20260513-235306`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-recursive-audit-focus-20260513-235306`
- Manifest: `/tmp/overnight-real-v14-recursive-audit-focus-20260513-235306/state/dataset-runs/overnight-real-v14-recursive-audit-focus-20260513-235306.json`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `0` pass, `1` partial, `2` fail
- Audit adoption: `0` `audit_artifact(...)` calls; `0` `artifact_audit.json` files.
- Audit surfacing: `audit=missing` surfaced for all three tasks.

Manual judging:

| Task | Runner | Manual | What changed | Evidence |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | `audit=missing` surfaced, but the model ignored it and finished. Output has 5 ads, but several creative screenshots are blank or almost blank, and the ranking is still a proxy. | `outputs/result.json`, `outputs/ad_*_creative.png`, DB seq `1049-1064` |
| 10 | pass | fail | `audit=missing` surfaced twice. The model continued once, then ended with plain `Done.`. Output grew to `877` records but failed explicit per-specialty targets for Rhinoplasty (`12`) and Liposuction (`24`), and broadened beyond the scoped source population. | `outputs/result.json`, DB seq `1089`, `1277-1289` |
| 16 | pass | fail | `audit=missing` surfaced for nested item count, but the model ignored it and finished. Output has correct address text but appears to use DoorDash delivery destination, not proven restaurant source, and still has `81` duplicate item-price rows. | `outputs/result.json`, DB seq `1239-1251` |

Concrete checks:

- Task `6`:
  - `ad_3_*_creative.png`, `ad_4_*_creative.png`, and `ad_5_*_creative.png` were single-color `(240, 242, 245)` images.
  - The helper correctly flagged visual-artifact audit need via `visual_artifact_paths=3`.
  - Failure mode: visible cue was not strong enough to stop finalization.
- Task `10`:
  - `records`: `877`; `metadata.asps_unique_count`: `797`.
  - Missing fields from artifact shape: `rating=877`, `address=877`, `phone=877`, `abps_board_certification=877`, `specialties=82`.
  - Specialty counts included `Rhinoplasty=12` and `Liposuction=24`, below the requested `40`.
  - The page evidence showed ASPS was falling back outside the requested location, so volume was inflated by broadening scope.
  - The model produced plain final text `Done.` at the end; because that path bypassed the `done` tool, it did not use the persisted final answer.
- Task `16`:
  - `14` categories, `209` item rows, `128` unique item-price pairs, `81` duplicate item-price rows.
  - DB/browser evidence showed DoorDash pages for another McDonald's title while `302 Potrero` appeared as delivery address.
  - Failure mode: confused delivery address with selected restaurant/menu source and preserved repeated recommendation categories.

Interpretation:

- Recursive audit detection works: all three target shapes surfaced `audit=missing` with useful reasons.
- Prompt-only response to `audit=missing` still fails.
- There is a second generic bug: direct assistant text `Done.` bypasses the `done` tool's persisted-final-answer replacement logic.
- The next intervention should keep the general principle but make it operational:
  - missing audit should set `ready_for_done=false`;
  - `done(use_final_answer=true)` should reject persisted final answers whose own summary says an audit is missing;
  - plain placeholder text like `Done.` should not bypass the same missing-audit guard.

### Intervention: Guard Missing-Audit Finalization And Placeholder Done

- Hypothesis: The model needs a hard generic protocol edge only when it tries to finish with a persisted final answer that the worker itself marked audit-missing. This is not benchmark validation; it enforces the artifact protocol.
- Intervention:
  - `set_final_answer(...)` now sets `ready_for_done=false` when an audit is recommended but missing, and emits `audit=missing ready_for_done=False`.
  - The `done` tool rejects `use_final_answer=true` or placeholder replacement when the persisted final answer has `audit_recommendation.recommended=true` and no attached audit.
  - Direct assistant text placeholders such as `Done.` now go through the same guard when a persisted final answer exists, preventing them from bypassing tool validation.
  - Prompts now explicitly say not to finish when `audit=missing ready_for_done=False` appears.
- Why this is generalizable:
  - It does not inspect task IDs, expected answers, websites, or result values.
  - It only enforces consistency between the model's own persisted final answer and the generic audit protocol.
  - It still permits an explicit final result that reports unresolved gaps, so impossible tasks can terminate honestly.
- Expected movement:
  - Task `10`: plain `Done.` after an audit-missing artifact should be rejected and force another turn.
  - Task `16`: `done(use_final_answer=true)` after nested duplicate-prone output should be rejected until an audit is attached or gaps are explicitly reported.
  - Task `6`: visual screenshot artifacts should require either an audit or explicit caveat.
- Verification:
  - `uv run --with pytest python -m pytest -q`: passed, `19 passed`
  - `cargo test`: passed, including new missing-audit finalization tests; `browser-use-core` now has `53` tests.
  - `cargo fmt --check`: passed
- Decision: Accepted for another focused eval iteration before full real_v8.

### Invalid Focused Rerun: `overnight-real-v14-audit-guard-focus-20260514-001823`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-audit-guard-focus-20260514-001823`
- Result: discarded.
- Reason: launched after committing `fa72e0c` but before rebuilding `target/debug/browser-use-terminal`, so the run used a stale binary. It accepted `done(use_final_answer=true)` despite `ready_for_done=false`, proving the run was not exercising the committed guard.
- Decision: Do not use for scoring or regression analysis.

### Focused Rerun: `overnight-real-v14-audit-guard-valid-focus-20260514-002920`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-audit-guard-valid-focus-20260514-002920`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `1` pass, `1` partial, `1` fail
- Token usage: `2,700,133` total tokens for the three focused tasks.

Manual judging:

| Task | Runner | Manual | What changed | Remaining problem |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | The model called `audit_artifact`, found blank detail screenshots, repaired them, reran the audit, and finished with `ready_for_done=true`. | Still selected US-default results rather than proven global results; top-5 ranking is only a visible-results proxy; ads 2-5 have card crops rather than full modal details. |
| 10 | pass | pass | The model produced `293` unique records, complete TopPlastic ranks `1-114`, `211` ASPS candidates, and `45` candidates for each specialty. | `practice` blank for all rows; broad ASPS geography; some ASPS rows without specialty evidence. |
| 16 | pass | fail | The model produced `19` categories and `225` rows with no category-scoped duplicates. | It conflated delivery destination with restaurant/store source: DoorDash source was `McDonald's (7413-SAN FRAN/BAYSHR)`, store id `662393`, zip `94124`, while the final answer claimed `302 Potrero Ave`. Global item-price duplicates remain (`225` rows, `140` unique item-price pairs). |

Interpretation:

- The finalization guard is valuable: it converted task `6` from blank-image failure to usable visual artifacts.
- The guard is still too shallow: a shape/file audit can say `ready_for_done=true` while the artifact is semantically tied to the wrong source entity.
- Task `16` exposes the first-principles issue:
  - entered/requested locations, delivery destinations, filters, and search text are not the same as the selected source entity;
  - the model needs to preserve both roles explicitly and verify the selected source before extracting bulk data;
  - dedupe scope must match user wording, not whatever key makes the audit pass.

### Intervention: Source-Provenance Audit Protocol

- Hypothesis: The next generalizable failure class is not missing artifacts; it is unverified source identity. For entity/location-bound extraction, the model must prove which source entity produced the artifact before finalization.
- Intervention:
  - `audit_artifact(...)` now accepts `source_evidence` and `required_source_fields`.
  - `set_final_answer(...)` detects top-level source/entity/location claims such as `store_address` in nontrivial structured outputs. If an attached audit lacks `source_evidence`, the persisted final answer is marked `ready_for_done=false`.
  - `done(use_final_answer=true)` and placeholder text finalization now reject any persisted final answer whose summary has `ready_for_done=false`, not only missing-audit cases.
  - The Python tool prompt now explicitly distinguishes selected source identity from search inputs, delivery destinations, filters, and user-provided claims.
  - The prompt now tells the model to choose dedupe keys from the user's wording; unqualified "no duplicates" means global entity/item dedupe, not category-scoped dedupe.
- Why this is generalizable:
  - It does not know McDonald's, DoorDash, task IDs, addresses, expected menus, or benchmark answers.
  - It applies to any extraction where a final artifact claims a specific source entity/location.
  - It still allows honest explicit completion with caveats when source identity cannot be verified.
- Expected movement:
  - Task `16` should either select the actual requested store/menu source or refuse to claim the menu belongs to it.
  - Task `6` may improve modal/source proof if the model treats ad details as source evidence rather than only screenshots.
  - Task `10` should not regress; its compact final summary has no top-level source address claim, and its existing audit remains focused on fields/buckets.
- Verification:
  - Focused Python tests for audit/source evidence: passed, `21 passed`.
  - `cargo fmt --check`: passed.
  - `cargo test`: passed, including `browser-use-core` `54` tests.
  - Full Python suite: passed, `21 passed`.
- Decision: Keep. Focused rerun fixed the task `16` source-entity failure without regressing task `10`.

### Focused Rerun: `overnight-real-v14-source-provenance-focus-20260514-004948`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-source-provenance-focus-20260514-004948`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `2` pass, `1` partial, `0` fail
- Token usage: `3,352,608` total tokens for the three focused tasks.

Manual judging:

| Task | Runner | Manual | What changed | Remaining problem |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | Returned 5 relevant SIM/eSIM ads with IDs, copy, and nontrivial creative/detail artifacts. | Required fields were filled with unavailable placeholders: all `deployment_duration` values say "Not visible..." and platforms are generic filter prose, not per-ad platform evidence. Ranking proof is still weak. |
| 10 | pass | pass | Produced the full TopPlastic ranks `1-114`, `85` ASPS general records, and `40` candidates for each of the 13 specialties via ASPS procedure-filtered evidence. ASPS rows have ABPS noted. | TopPlastic-only records do not have ASPS-derived specialties/ABPS, which is an honest source-coverage caveat rather than a fabricated fill. |
| 16 | pass | pass | Fixed the prior first-principles failure. `artifact_audit.json` separates requested location from selected source entity and proves `McDonald's (16932-POTRERO HILL)` at `302 Potrero Ave, San Francisco, CA 94110`; output has `13` categories and `128` globally unique item-price rows. | ZIP mismatch remains as a source-data caveat: user text said `94103`, actual source store record reports `94110`. |

Concrete checks:

- Task `6` output `result.json` has 5 ads, but `deployment_duration` is `"Not visible in captured result/detail card; ad status was Active."` for every row and `platforms` is `"Exact per-ad platform icons not visible..."`.
- Task `6` final answer used a hand-written audit dictionary with `ready_for_done=true`; it did not use the computed `audit_artifact(...)` shape.
- Task `10` primary `records` contains `167` unique surgeons:
  - `114` TopPlastic records;
  - `85` ASPS records;
  - `32` records present in both sources;
  - all 13 specialty arrays have count `40`;
  - ASPS records have ABPS certification noted.
- Task `16` audit evidence:
  - `source_url`: `https://mcdonalds.order.online/store/mcdonald-s-san-francisco-653629/?delivery=true`
  - `source_page_title`: `McDonald's 302 Potrero Avenue - Order pickup and delivery`
  - `selected_source_entity_name`: `McDonald's (16932-POTRERO HILL)`
  - `selected_source_entity_address`: `302 Potrero Ave, San Francisco, CA 94110, USA`
  - duplicate item-price rows: `0`

Interpretation:

- Source provenance was a successful generalizable intervention. It fixed the task `16` wrong-store/source conflation without a McDonald's-specific validator.
- The remaining task `6` failure is a different generic protocol gap:
  - unavailable prose such as "not visible" is being treated as a present required value;
  - a hand-written audit dictionary can bypass computed missing-field/visual-file checks;
  - the model needs to use `audit_artifact(...)` output, not merely declare an audit passed.

Decision:

- Keep `9549a81`.
- Implement a general missing-placeholder and computed-audit guard before another focused rerun.

### Intervention: Placeholder Missing Values And Computed Audit Guard

- Hypothesis: Task `6` remains partial because the pre-final audit protocol accepts semantic placeholders and ad-hoc audit dictionaries. A general guard should force real extraction or honest gap reporting without encoding benchmark answers.
- Intervention:
  - `_is_missing(...)` now treats common unavailable placeholders as missing values: `unknown`, `n/a`, `not visible`, `not available`, `unavailable`, `could not determine`, `not found`, `not shown`, `not provided`, and related variants.
  - `audit_artifact(...)` now marks its output with `generated_by: audit_artifact` and `schema_version: 1`.
  - `set_final_answer(...)` now rejects audit-worthy final answers that attach a hand-written audit dictionary rather than computed `audit_artifact(...)` output.
  - If a final answer references multiple image artifacts, an attached computed audit must include `visual_files`.
  - If a final answer has large structured output, the computed audit must include at least one structured check: `missing_fields`, `dedupe`, or `buckets`.
  - If a final answer claims a source/entity/location, the computed audit must include `source_evidence`.
  - The Python tool prompt now says unavailable placeholders are gaps, not completed fields, and tells the model not to pass hand-written audit dictionaries for audit-worthy artifacts.
- Why this is generalizable:
  - It does not inspect task IDs, websites, expected answers, or benchmark-specific fields.
  - It enforces the worker's own audit protocol and common data-quality semantics.
  - It still permits honest explicit completion with stated gaps when a source truly does not expose a field.
- Expected movement:
  - Task `6`: `deployment_duration: "Not visible..."` and generic platform prose should cause `audit_artifact(required_fields=[...])` to fail, forcing either deeper extraction or explicit partial completion.
  - Task `10`: no expected regression; existing successful run used computed bucket/missing/dedupe audit.
  - Task `16`: no expected regression; existing successful run used computed dedupe plus source-evidence audit.
- Verification:
  - Focused worker tests: passed, `23 passed`.
  - Full Python tests: passed, `23 passed`.
  - `cargo fmt --check`: passed.
  - `cargo test`: passed.
  - `cargo build --bin browser-use-terminal`: passed.
- Decision: Keep, with the next intervention focused on ranking/selection proof.

### Focused Rerun: `overnight-real-v14-audit-tightening-focus-20260514-011412`

- Dataset: `real_v14_short`
- Tasks: `6`, `10`, `16`
- Root: `/tmp/overnight-real-v14-audit-tightening-focus-20260514-011412`
- Command: `dataset-run-codex real_v14_short --task-id 6 --task-id 10 --task-id 16 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 2 --concurrency 3 --browser-mode cloud`
- Runner result: `3/3` passed, `0` failed, `0` pending
- Manual strict result: `1` pass, `1` partial, `1` fail
- Token usage: `3,093,306` total tokens for the three focused tasks.

Manual judging:

| Task | Runner | Manual | What changed | Remaining problem |
| ---: | --- | --- | --- | --- |
| 6 | pass | partial | Placeholder guard worked: `deployment_duration` became real computed durations, screenshot files were nonblank, and audit used computed `audit_artifact`. | Selection was still weak: output chose first visible/result-order eSIM cards, while `raw_cards.json` contained longer-running eSIM candidates later in the loaded pool. Per-ad card screenshots were exact duplicate hashes in the first version, then repaired only after audit pressure. |
| 10 | pass | fail | Structurally large output: `530` unique records and `40` candidates per specialty. | Strict source-scope failure: the Beverly Hills ASPS URL returned no selected-location results, then the model filled specialty buckets from broad/global ASPS procedure pages. This satisfies counts but not a reliable Beverly Hills surgeon/specialty/ABPS list. |
| 16 | pass | pass | The first no-source-evidence finalization was blocked. The repair final answer proved `source_url` `https://mcdonalds.order.online/store/-653629?delivery=false`, page title `McDonald's 302 Potrero Avenue`, and source entity `McDonald's (16932-POTRERO HILL)`. | Empty visible categories are preserved; acceptable for "include every visible category." |

Concrete checks:

- Task `6` selected IDs: `634468225627175`, `812639694250559`, `789931336696572`, `1288150293297474`, `1695860041574404`.
- `raw_cards.json` showed better longest-active candidates in the loaded eSIM-like pool, including multiple `Sparks_esim` ads at `804` days and `SimOptions` at `332` days.
- Task `6` audit was structurally clean but lacked a check comparing selected rows against the raw candidate pool by the declared metric.
- Task `10` result included `114` TopPlastic records and broad ASPS specialty buckets. A stricter manual read treats the broadening as a fail because the prompt specifically starts from Beverly Hills sources.
- Task `16` final result had `19` categories, `138` item rows, `138` unique item-price pairs, and source evidence that matches the requested Potrero store.

Interpretation:

- The placeholder/ad-hoc audit guard was successful for missing-field honesty and source finalization.
- It exposed the next first-principles failure: a model can pass missing/dedupe/visual audits while making a weak comparative selection.
- Source scope needs its own protocol. Count targets must not be filled by broadening the source silently when the requested source reports no local results.

### Intervention: Selection/Ranking Audit Guard

- Hypothesis: For comparative tasks, the model needs to compute a compact selection proof against the candidate pool. Otherwise it can claim "top" or "best" based on visible order without proving that selected rows are top by the declared metric.
- Intervention:
  - `audit_artifact(...)` now accepts `selection_metric_field`, `selection_order`, `selection_limit`, `selection_pool_records`, and `selection_key_fields`.
  - The selection check reports missing metric counts, order violations, candidate-pool top preview, missing top candidates, and selected records outside the top pool.
  - `set_final_answer(...)` detects ranking/selection claims such as `ranking_basis`, `selection_method`, `sort_basis`, or top/best/highest/longest prose. If an audit-worthy final answer has such claims but no `selection` check, `ready_for_done=false`.
  - `audit_artifact(...)` also supports `unique_visual_files=True` so per-record screenshot deliverables can catch exact duplicate image files.
  - Prompts now tell the model to prefer an observed numeric metric over result order for top/best/highest/longest selections unless the page visibly labels the current sort.
- Why this is generalizable:
  - It does not know Facebook, eSIM, task IDs, expected IDs, or benchmark answers.
  - It only forces the model to connect its own selection claim to its own candidate pool and metric.
  - It leaves proxy choice to the model but makes the proxy auditable.
- Verification:
  - Focused worker tests: passed, `26 passed`.
  - Full Python tests: passed, `26 passed`.
  - `cargo fmt --check`: passed.
  - `cargo test`: passed.
  - `cargo build --bin browser-use-terminal`: passed.

### Focused Rerun: `overnight-real-v14-selection-audit-task6-20260514-013345`

- Dataset: `real_v14_short`
- Task: `6`
- Root: `/tmp/overnight-real-v14-selection-audit-task6-20260514-013345`
- Command: `dataset-run-codex real_v14_short --task-id 6 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 1 --browser-mode cloud`
- Runner result: `1/1` passed.
- Manual strict result: pass.
- Token usage: `722,411` total tokens.

Manual judging:

| Task | Runner | Manual | What changed | Remaining problem |
| ---: | --- | --- | --- | --- |
| 6 | pass | pass | The model selected five `Sparks_esim` ads all tied at `804` active days from `Mar 1, 2024`, with a `selection` audit over `107` loaded candidate cards. The first finalization had `ready_for_done=false`; the model repaired blank screenshot crops and finalized only after `artifact_audit.json` had `ready_for_done=true`. | Engagement metrics still were not visible, so the answer correctly uses longest active duration as proxy. Country remained United States/default because the UI did not expose a global option in the run. |

Concrete checks:

- Final selected IDs: `812639694250559`, `398867029407187`, `1456501448274040`, `928317988902166`, `934513628063294`.
- Final audit:
  - `record_count`: `5`
  - `candidate_pool_count`: `107`
  - `candidate_pool_metric_count`: `107`
  - `missing_top_candidate_count`: `0`
  - `selected_outside_top_count`: `0`
  - `visual_file_uniqueness.duplicate_hash_group_count`: `0`
  - all visual files nonblank.
- The audit blocked the first result with blank crops (`ready_for_done=false`) and the agent repaired the images before `done`.

Decision:

- Keep and commit. This is a real generalizable improvement: it converted a persistent task `6` partial into an audited pass without encoding task-specific expected IDs.
- Next intervention should target task `10`: a general source-scope/locality audit that distinguishes "requested source/location returned no local results" from "broadened fallback used to satisfy counts."

### Intervention: Source-Scope Audit Guard

- Hypothesis: Task `10` regressed because the model silently broadened a scoped source/location when the requested ASPS Beverly Hills URL returned fallback/global results. Count targets should not be satisfied by broadening the source unless the user explicitly allows fallback.
- Intervention:
  - `set_final_answer(...)` now detects broadened/fallback source-scope claims such as "all locations", "no results found", "selected location", "fallback", "outside scope", and "to meet target".
  - `audit_artifact(...)` now accepts `source_scope_evidence` and `required_scope_fields`.
  - The source-scope check records requested scope, actual scope, scope match, fallback-used/allowed flags, and out-of-scope counts.
  - Unapproved fallback, out-of-scope records, or partial/mismatched scope make `ready_for_done=false`.
  - Prompts now tell the model to preserve requested source/location/category scope, avoid fallback rows for scoped count targets, and explicitly report remaining scoped gaps.
- Why this is generalizable:
  - It does not encode ASPS, Beverly Hills, task IDs, expected specialty counts, or known URLs.
  - It applies to any source, location, category, directory, product, profile, or account where the website broadens results after a scoped lookup fails.
  - It still allows honest partial completion when the source cannot satisfy the target.
- Verification:
  - Focused worker tests: passed, `28 passed`.
  - Full Python tests: passed, `28 passed`.
  - `cargo fmt --check`: passed.
  - `cargo test`: passed.
  - `cargo build --bin browser-use-terminal`: passed.

### Focused Rerun: `overnight-real-v14-source-scope-task10-20260514-015107`

- Dataset: `real_v14_short`
- Task: `10`
- Root: `/tmp/overnight-real-v14-source-scope-task10-20260514-015107`
- Command: `dataset-run-codex real_v14_short --task-id 10 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 1 --browser-mode cloud`
- Runner result: `1/1` passed.
- Manual strict result: partial. The run did not satisfy all requested count targets, but it did not fabricate the missing scoped rows.
- Token usage: `1,893,400` total tokens.

Manual judging:

| Task | Runner | Manual | What changed | Remaining problem |
| ---: | --- | --- | --- | --- |
| 10 | pass | partial | The model detected that the requested ASPS `state=CA&city=Beverly Hills` path showed fallback/no-local behavior, switched to scoped Beverly Hills `zip=90210` and procedure-filtered `zip=90210` pages, and refused to broaden to global rows just to force all buckets to 40. | `Facial Reconstruction` only had `17` scoped matches, below the target of `40`. The final answer and audit correctly marked the gap. |

Concrete checks:

- Output files:
  - `result.json`: `759` combined unique surgeons.
  - `result.csv`: CSV export of the same artifact.
  - `artifact_audit.json`: computed `audit_artifact(...)` output.
- Counts:
  - TopPlasticSurgeonReviews Beverly Hills rows: `114`.
  - ASPS scoped/procedure-filtered unique records: `678`.
  - Combined unique surgeons: `759`.
  - Specialty target met for `12/13` specialties.
  - `Facial Reconstruction`: `17/40`.
- Audit:
  - `ready_for_done`: `false`.
  - `record_count`: `1873` flattened specialty candidate rows.
  - duplicate `(name, specialty)` rows: `0`.
  - missing `name`/`specialty`: `0`.
  - source-scope evidence:
    - requested scope: TopPlastic Beverly Hills plus ASPS Beverly Hills CA directory.
    - actual scope: TopPlastic Beverly Hills plus ASPS Beverly Hills `zip=90210` directory/procedure filters.
    - `scope_match`: `partial`.
    - `fallback_used`: `false`.
    - `out_of_scope_record_count`: `0`.
- The model first timed out a monolithic Python extraction and then recovered with a faster chunked script. That is useful evidence for the next general improvement.

Interpretation:

- This was a quality improvement even though the strict target remains unsolved. The previous task `10` run looked structurally complete but was wrong because it filled buckets with broad/global ASPS records. This run preserved source scope and surfaced the real scoped gap.
- The runner marked it pass because it completed with artifacts and an explicit final answer. Manual strict scoring should keep it partial because one target remains unmet.
- The next general failure mode is execution discipline for large extraction tasks:
  - avoid one giant Python tool call that can hit the 180-second cap;
  - checkpoint partial outputs after each page/source/bucket;
  - prefer bounded parallel fetch batches with progress summaries;
  - do not let the model burn many turns debugging shell/process state after a timeout.

Decision:

- Keep the source-scope guard. It prevents a first-principles correctness error and aligns with the user's stated preference to avoid wrong/fabricated rows over forcing benchmark counts.
- Next intervention: add a general long-extraction/chunking protocol to prompts, then rerun focused task `10` and full `real_v14_short`/`real_v8`.

### Intervention: Bounded Chunked Extraction Prompt

- Hypothesis: Large extraction tasks waste turns and lose progress when the model puts discovery, full crawl, detail expansion, audit, and finalization into one long Python/shell call. A general prompt rule should make the model checkpoint work and recover from failures without restarting.
- Intervention:
  - Python tool prompt now says to split bulk extraction by page/source/bucket, use per-request timeouts, write progress after each chunk, and avoid all-in-one calls that may approach the tool timeout.
  - Dataset prompt now has a long-extraction contract: discover endpoint/pagination first, fetch in batches, checkpoint under `/home/user/outputs`, print compact progress, and resume from checkpoints after failure.
  - Browser-agent system prompt now repeats the same rule in the bulk-extraction workflow.
- Why this is generalizable:
  - It applies to any large crawl, export, download set, detail-page fanout, or per-bucket extraction.
  - It does not enforce deterministic tool-specific counting or benchmark-specific URLs.
  - It changes model strategy rather than adding a hardcoded validator.
- Expected movement:
  - Task `10`: fewer wasted turns after timeouts; faster arrival at the final scoped artifact.
  - Full datasets: fewer failures where the model times out after collecting useful partial data but never writes a deliverable.
