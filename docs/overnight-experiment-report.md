# Overnight Experiment Report

Worktree: `/Users/greg/Documents/browser-use/experiments/overnight-experiment`

Branch: `overnight-experiment`

Protocol: `docs/overnight-experiment-loop.md`

This is the living scientific log for the autonomous eval-and-improve loop. Append every experiment, including negative results, failed commands, reverted changes, skipped runs, and environment problems.

## Dashboard

| Field | Current State |
| --- | --- |
| Recommended branch state | Latest semantic run improves immediate previous full v8 from 67/29/4 to 71/26/3; next intervention in progress: zero-record audit guard, not-ready final-answer bypass guard, and stronger semantic/frontier prompt |
| Latest `real_v8` strict/manual score | `71` pass / `26` partial / `3` fail, weighted `84.0/100`, root `/tmp/overnight-real-v8-semantic-20260514-061215` |
| Latest `real_v14_short` strict/manual score | `7` pass / `3` partial / `0` fail, root `/tmp/overnight-real-v14-semantic-20260514-054705` |
| Latest `BU_Bench_V1` strict/manual score | `74` pass / `9` partial / `17` fail, weighted `78.5/100`, root `/tmp/overnight-bu-bench-v1-20260514-072225` |
| Most important improvement | Final answer persistence fixed preview/status/max-turn failures; semantic prompt recovered v8 tasks 8, 19, 51, 57, 60 and improved 99/100 |
| Worst regression | Semantic regressions remain in dynamic-offer/frontier tasks (`47`, `49`, `50`) and source-state tasks (`21`, `40`) |
| Open root-cause clusters | Stateful page proof, source-route fidelity, zero-record readiness, required-field provenance, full-frontier evidence, visual/lazy section extraction, price/discount semantics |
| Next experiment | Verify/commit zero-record and not-ready bypass guards; focused retest v8 tasks `9`, `15`, `21`, `27`, `47`, `49`, `50`, `99`, `100`, and v14 task `6` |

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

### Full Rerun: `overnight-real-v14-all-20260514-022303`

- Dataset: `real_v14_short`
- Root: `/tmp/overnight-real-v14-all-20260514-022303`
- Command: `dataset-run-codex real_v14_short --all --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 25 --browser-mode cloud`
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `10/10` passed.
- Manual strict result: `5/10` pass, `4/10` partial, `1/10` fail.
- Token usage: `9,036,815` total tokens across `270` model invocations.

Manual scorecard:

| Task | Runner | Manual | Reason |
| ---: | --- | --- | --- |
| 2 | pass | pass | FERC answer covered 11 filename links from the first two rows, excluded Generate PDF, and returned markdown only. |
| 4 | pass | pass | Ollie's output had 682 official-store records with name/address, no missing required fields, and no duplicates. |
| 5 | pass | partial | Extracted a plausible 97-row telecom artifact, but final answer returned only a summary/path instead of the requested JSON list; 12 rows still had null contract length. |
| 6 | pass | partial | Returned five Meta ads and screenshots, but candidate-pool evidence only covered six loaded candidates, too weak for "best performing"; final screenshot references pointed at cropped cards rather than full detail screenshots. |
| 8 | pass | partial | Produced 28 SSD comparisons, but product matching is weak: heatsink/non-heatsink and model-title mismatches appear in PriceRunner matches. |
| 9 | pass | pass | Screenshot exists, is nonblank, and captures the requested SBI table. |
| 10 | pass | fail | Source-scope audit was dishonest: procedure-search rows added candidates and many "scoped" ASPS rows had non-Beverly-Hills addresses. Removing procedure-only candidates drops at least one specialty below 40. |
| 11 | pass | partial | Correct `result.json` and persisted final answer exist, but run-level final result is the protocol string `Final answer persisted. Need done use.` |
| 13 | pass | pass | WakeMed extraction has 1,367 unique provider URLs and rich visible metadata; missing optional fields look absent on source profiles. |
| 16 | pass | pass | McDonald's location/menu artifact has source evidence, 128 priced items, categories, currency, and no duplicate item/price pairs. |

Failure modes found:

1. **Persisted-final-answer misuse**: task `11` proves the model can produce the correct artifact, call `set_final_answer`, then finalize with the tool status string instead of the stored answer. This is a harness/protocol weakness because the stored answer is already present and valid.
2. **Primary-result artifact not surfaced**: task `5` saved the real JSON list but returned a summary artifact. The benchmark asks for the data itself, so the finalization contract needs to bias toward the primary result artifact/content, not a convenience summary.
3. **Insufficient candidate-pool evidence for ranking**: task `6` passed audit shape but only loaded six candidates before selecting top five. The audit should ask for candidate-pool size and coverage evidence when a task uses "best", "top", "highest", or "longest".
4. **Product/entity match quality not audited**: task `8` needs a generic entity-match audit: compare source product attributes with matched result attributes before claiming "same product".
5. **Self-reported source scope is not enough**: task `10` shows that letting the model self-report `out_of_scope_record_count=0` is insufficient. The audit needs machine-checkable provenance fields or computed address/source-label checks.

Interpretation:

- The chunked-extraction prompt helped task `10` finish within the full run, but it did not solve the deeper correctness issue. The model now reaches a polished artifact faster, which is useful, but the artifact can still be wrong if provenance is not represented in the data model.
- The source-scope guard was too trust-based. It catches honest broadening, but not cases where the model labels broadened rows as scoped.
- Highest-priority next change: make `done` robust to persisted-answer status-string misuse and tighten the finalization prompt. This should be general, low-risk, and directly recovers task `11`.
- Next highest-impact design change: require auditable provenance columns for scoped/ranked/matched artifacts. This should not encode dataset answers; it should force the model to leave enough structured evidence for the audit and for manual review.

### Full Rerun: `overnight-real-v8-all-20260514-024405`

- Dataset: `real_v8`
- Root: `/tmp/overnight-real-v8-all-20260514-024405`
- Command: `dataset-run-codex real_v8 --all --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 25 --browser-mode cloud`
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `100/100` passed.
- Manual strict result from 5 judging agents: `73/100` pass, `23/100` partial, `4/100` fail.
- Token usage: `59,231,351` total tokens across `1,988` model invocations.

Manual score by range:

| Tasks | Pass | Partial | Fail | Notes |
| --- | ---: | ---: | ---: | --- |
| 1-20 | 18 | 1 | 1 | Fail was HostGenius location pages returned instead of property listings; partial was missing product descriptions/brands. |
| 21-40 | 14 | 6 | 0 | Main issues were source drift, missing contract/venue fields, and JSON output where a table was requested. |
| 41-60 | 17 | 3 | 0 | Main issues were weak Meta ranking completeness, missing contract length, and one task without a saved structured artifact. |
| 61-80 | 11 | 8 | 1 | Fail was missing operator ID; partials include listing/detail ambiguity, missing emails/practice names, incomplete sitemap menu extraction, and SSD comparison incompleteness. |
| 81-100 | 13 | 5 | 2 | Fails were 7/200 food trucks and task `100` returning a path/count/sample instead of the requested JSON object. |

Largest manual failure modes:

1. **Source/entity drift**: The model often found adjacent records and treated them as in-scope. Examples: task `9` returned HostGenius location pages instead of property listings; task `23` included broad "relativity" procurement matches; task `39` extracted Tek comparison API data instead of the guide page; task `88` changed ASPS scope to `q=90210`/fallback rows.
2. **Output-shape mismatch**: The model saved correct artifacts but did not always return the requested shape. Examples: task `33` returned JSON instead of the requested table; task `100` returned a path/count/sample summary when the prompt asked for a JSON object with three arrays.
3. **Finalization meta-text**: Some final answers included internal status phrases even when `.final_answer.json` existed. Examples: v14 task `11` returned `Final answer persisted. Need done use.`; v8 task `57` started with `Need final done...`; v8 task `100` started with `Need final with done...`.
4. **Missing required fields accepted as complete**: Examples: task `15` missing descriptions/brands, task `27` missing contract lengths, task `36` missing venue/conference, task `46` missing contract length, task `68` missing verified emails, task `75` missing practice names, task `95` missing one CRL URL.
5. **Weak selection/ranking evidence**: Meta Ads tasks can produce five plausible ads but with too little candidate-pool evidence to support "best performing" or "longest active" claims.
6. **Quota/coverage early stops**: Task `87` returned 7 food trucks against a target of 200. Task `88` honestly had only 1 facial reconstruction candidate in a scoped sub-artifact but still summarized a broad dataset.

What this says about the branch:

- The remote-browser/concurrency path is mechanically healthy. The `real_v8` run launched all 100 tasks, kept roughly 25 active until the tail, and did not open local Chrome.
- Runner pass rate is not a quality metric yet. It only measures whether the agent produced some final result.
- The best immediate gains are not deterministic benchmark validators. They are better finalization and self-review contracts: preserve requested output shape, do not surface internal status text, and force source/entity evidence to be represented in the data.

### Intervention: Finalization Status Text Replacement

- Hypothesis: When a persisted final answer exists, internal status text like `Final answer persisted. Need done use.` and `Need final with done...` should be treated like `Done.` and replaced with the persisted answer.
- Intervention:
  - `done` placeholder detection now treats persisted-final-answer status sentences as placeholders when a persisted answer with positive count exists.
  - Prompt language now explicitly says not to use status sentences such as "final answer persisted", "need final done", or "need done use" as the user-facing result.
- Why this is generalizable:
  - It is not task-specific and does not inspect benchmark content.
  - It only fires when the model has already created a persisted final answer and then submits a meta-status sentence instead of the answer.
- Expected movement:
  - Recovers v14 task `11`.
  - Prevents v8 task `57` and `100` from exposing internal finalization text, though task `100` still needs output-shape discipline to be fully strict.
- Verification:
  - `cargo test -p browser-use-core done_replaces` passed with two focused tests:
    - `done_replaces_persisted_final_answer_status_text`
    - `done_replaces_need_final_done_status_text`
  - `cargo fmt --check` passed.

### Intervention: Explicit Output-Shape Contract

- Hypothesis: The model sometimes optimizes for artifact hygiene and loses the exact requested response shape. Explicitly separating "save the full artifact" from "return the requested shape" should reduce JSON/table finalization failures without adding deterministic validators.
- Intervention:
  - Dataset prompt now says that if the task asks for `Return JSON`, `JSON only`, a JSON array/object, or a table/markdown template, the final answer must follow that shape unless the task explicitly asks for a file path.
  - It still allows saving large artifacts under `/home/user/outputs`, but says a path/count/sample summary is not a substitute for the requested JSON/table.
- Why this is generalizable:
  - It applies to any explicit output-format request.
  - It does not require knowing expected rows or task-specific schemas.
- Expected movement:
  - Improve v8 task `33` and `100`.
  - Reduce "summary artifact instead of actual data" partials in v14 task `5` and similar large JSON/list tasks.

Next experiment:

- Rebuild `browser-use-terminal`, run focused retests for v14 task `11`, v8 task `57`, v8 task `100`, then run full `real_v14_short` and `real_v8` again with cloud browser and 25-way concurrency.
- If the focused retests show no regression, commit this intervention before the next full run so the scientific log has a clean boundary.

### Focused Reruns: Finalization And Output Shape

Shared setup:

- Browser mode: cloud only. `BU_CDP_URL`, `BU_CDP_WS`, and `BU_BROWSER_ID` were unset. `LLM_BROWSER_BROWSER_MODE=cloud`, `LLM_BROWSER_AUTO_CHROME=0`, and `LLM_BROWSER_OPEN_CLOUD_LIVE_VIEW=0` were set.
- Binary: rebuilt `./target/debug/browser-use-terminal` after prompt/code changes.
- Model/provider: `gpt-5.5` via `codex`.

Results:

| Run | Task | Prior failure | Focused result | Decision |
| --- | --- | --- | --- | --- |
| `/tmp/overnight-focused-v14-t11-20260514-032957` | `real_v14_short` task `11` | Final result was `Final answer persisted. Need done use.` despite valid FCC JSON artifact. | Pass. Final result is the actual JSON array of 7 grantee-code/count rows. | Status-text replacement works for the exact failure. |
| `/tmp/overnight-focused-v8-t57-20260514-032957` | `real_v8` task `57` | Final result leaked `Need final done...` status text before the saved review data. | Pass/improved. Final result is the actual structured review output, with no internal status prefix. | Status-text replacement works on a second independent case. |
| `/tmp/overnight-focused-v8-t100-20260514-032957` | `real_v8` task `100` | Final result was a path/count/sample summary rather than the requested JSON object with `amazon_de`, `galaxus_de`, and `kaufland_de` arrays. | Fail on strict output shape. The status prefix disappeared, but the final answer still returned `{artifact_path, record_count, counts, schema, note}`. | Prompt-only status fix was insufficient for explicit JSON-object tasks. |
| `/tmp/overnight-focused-v8-t100-shape-20260514-034539` | `real_v8` task `100` | Same output-shape failure. | Pass on the targeted issue. Final `done` used the actual JSON object with top-level `amazon_de`, `galaxus_de`, and `kaufland_de`; each array had 20 rows. | The stricter output-shape contract changed behavior in the desired general direction. |

Interpretation:

- The finalization-status replacement is clearly useful and low-risk: it only substitutes a persisted final answer when the submitted `done` payload is internal protocol/status text.
- The first task `100` retest showed that status cleanup alone does not solve response-shape drift. The model can still treat an artifact summary as the answer.
- The second task `100` retest shows the output-shape contract can recover a strict failure without a deterministic task-specific validator. The model saved artifacts and still returned the requested object.
- This intervention is worth keeping before the next full dataset pass.

Verification before commit:

- `cargo fmt --check` passed.
- `cargo test` passed.
- `uv run --with pytest python -m pytest -q` passed, `28 passed`.
- `cargo build --bin browser-use-terminal` passed.
- Commit this intervention, then run the next full `real_v14_short` and `real_v8` pass with cloud browser and 25-way concurrency.

### Full Rerun: `overnight-real-v14-after-finalshape-20260514-040718`

- Dataset: `real_v14_short`
- Root: `/tmp/overnight-real-v14-after-finalshape-20260514-040718`
- Commit under test: `926c0b0` (`Harden final answer shape and status handling`)
- Command: `browser-use-terminal --state-dir <run>/state dataset-run-codex real_v14_short --all --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 25 --browser-mode cloud`
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `10/10` passed.
- Manual strict result: `6/10` pass, `4/10` partial, `0/10` fail.
- Previous strict result: `5/10` pass, `4/10` partial, `1/10` fail.

Manual scorecard:

| Task | Manual | Vs Previous | Reason |
| ---: | --- | --- | --- |
| 2 | pass | no-change | FERC markdown covers the first two rows and file links. |
| 4 | pass | no-change | `682` Ollie's stores with name/address and no duplicate pairs. |
| 5 | partial | improved | Final answer is now the actual JSON list, `99` rows, and no missing contract length. Still has semantic extraction errors such as provider `4G forbindelse ikon`. |
| 6 | pass | improved | Candidate pool expanded to `26`; selected top 5 by longest visible active duration; usable screenshots exist. |
| 8 | partial | no-change/slight regression | `27` SSDs; some products have fewer than 3 offers and variant/heatsink matching remains weak. |
| 9 | pass | no-change | SBI table screenshot exists and is nonblank. |
| 10 | partial | improved from fail | Output is now honest about fallback/scope: audit says `ready_for_done=false`, `706` out-of-scope records, and counts rely on broadened ASPS rows. |
| 11 | pass | improved | Final result is the actual FCC JSON array; the old `Final answer persisted. Need done use.` failure is gone. |
| 13 | pass | no-change | `1,367` WakeMed provider URLs with rich profile metadata. |
| 16 | partial | regression | Menu extraction is rich, but selected store address drifted to `302 Potrero Ave, San Francisco, CA 94110` instead of requested `94103`. |

Interpretation:

- The final-answer status/output-shape intervention worked on its intended v14 failures. Task `11` moved from partial to pass, task `5` now returns the JSON list directly, and no status-wrapper final answers were found.
- The remaining v14 issues are not finalization mechanics. They are semantic correctness: source scope, entity/product matching, field plausibility, and location fidelity.

### Full Rerun: `overnight-real-v8-after-finalshape-20260514-042538`

- Dataset: `real_v8`
- Root: `/tmp/overnight-real-v8-after-finalshape-20260514-042538`
- Commit under test: `926c0b0` (`Harden final answer shape and status handling`)
- Command: `browser-use-terminal --state-dir <run>/state dataset-run-codex real_v8 --all --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 25 --browser-mode cloud`
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `99/100` passed, task `72` failed with `agent exceeded maximum provider turns`.
- Manual strict result from 5 judging agents: `67/100` pass, `29/100` partial, `4/100` fail.
- Previous manual strict result: `73/100` pass, `23/100` partial, `4/100` fail.
- Weighted score with partial = 0.5: `81.5/100`, down from `84.5/100`.

Manual score by range:

| Tasks | Pass | Partial | Fail | Delta vs previous |
| --- | ---: | ---: | ---: | --- |
| 1-20 | 16 | 3 | 1 | Worse. Task `8` over-pruned an intersection result; task `19` missed newer Japanese official IR docs. |
| 21-40 | 14 | 5 | 1 | Mixed. Output shape improved on `23`, `24`, `38`, but task `21` remains fail and `27`, `34`, `40` regressed. |
| 41-60 | 13 | 7 | 0 | Worse. Task `57` fixed, but coverage/finalization issues appeared on `45`, `51`, `54`, `60`. |
| 61-80 | 11 | 8 | 1 | Same strict shape as previous, but different failures. `71` and `79` improved; `65`, `68`, `69`, `74` regressed. |
| 81-100 | 13 | 6 | 1 | Slight weighted improvement. Task `100` moved fail to partial; task `87` still fail. |

Confirmed wins from `926c0b0`:

- Output-shape/finalization improved tasks `2`, `5`, `11`, `23`, `24`, `38`, `42`, `57`, `71`, `79`, and `100`.
- Task `100` no longer returns a path/count/sample summary; it returns the requested object with `amazon_de`, `galaxus_de`, and `kaufland_de` arrays of length `20`.
- Task `57` no longer leaks `Need final done...`.

Regressions and persistent failures:

- **Entity/page-type drift**: task `9` still returns HostGenius location pages instead of property listings; task `99` accepted balls/grip tape/backpacks as padel rackets.
- **Exact-source/latest drift**: task `19` stayed on English CellSeed IR pages and missed fresher Japanese official docs; task `91` accepted `KRAS altered` as if it were exact `KRAS G12D`.
- **Required-field audit gaps**: task `95` omitted `crl_file_url` from the required audit; task `100` omitted `price`; task `34` left developer website URLs null despite visible links.
- **Nulls without proof**: task `21` returned null Booking prices even though trace evidence showed room rows with "Show prices"; task `36` still finalized with missing venue fields.
- **Coverage/frontier regressions**: task `45` dropped sitemap coverage from `272` to `236`; task `54` dropped ScrapeCreators docs from `153` to `125`; task `60` included out-of-range/unlabeled vendors.
- **Preview-vs-full persisted result**: task `51` persisted a full 41-row final answer and audit, but the final `done` result contained only the preview/first rows.
- **Last-turn finalization**: task `72` wrote a partial final answer with `operator_id: null`, then hit the provider turn limit before calling `done`.

Decision:

- Keep `926c0b0`. It clearly fixes a real class of finalization and output-shape failures, especially on v14.
- The v8 strict score regression means the next intervention should not add more schema pressure alone. The next change should target general semantic correctness and finalization mechanics:
  - required fields must be derived from the task/schema, not handpicked;
  - `null`/unknown values need source evidence or an explicit incomplete status;
  - entity/page type and exact-source constraints must be checked before extraction/finalization;
  - load-more/page-range tasks need frontier/exhaustion evidence;
  - `done` should use the full persisted answer when the model accidentally passes a compact preview;
  - if a ready persisted final answer exists at max turns, the harness should finish with it instead of failing the run.

### Intervention: Finalization Fallback And Semantic Audit Prompt

- Hypothesis: The finalization fix improved shape but exposed the next bottleneck: the model can still finalize a preview, omit required fields from the audit, accept adjacent entities, stop pagination early, or fail after persisting a last-turn answer.
- Intervention:
  - Harness:
    - If `done` receives a JSON preview/prefix of a persisted final answer, replace it with the full persisted final answer.
    - If the agent exhausts provider turns after writing a ready persisted final answer, emit `session.done` from that answer instead of failing the run.
  - Prompts:
    - Tell the model not to paste `set_final_answer(...)` previews into `done`.
    - Require `required_fields` to be derived literally from the task/schema.
    - Treat `null`, `unknown`, `not specified`, and `unavailable` as gaps unless there is source evidence for that exact record.
    - Require page/entity-type checks before list extraction.
    - Require frontier/exhaustion evidence for all/load-more/page-range/sitemap tasks.
- Why this is generalizable:
  - It is not URL- or dataset-specific.
  - It targets classes of mistakes seen across both datasets: preview finalization, last-turn loss, source/entity drift, missing-field audit gaps, and early coverage stops.
- Expected movement:
  - Recover runner failure on task `72` if a final answer is ready on the last turn.
  - Recover task `51`-style preview finalization.
  - Improve tasks `21`, `34`, `36`, `45`, `54`, `60`, `95`, `99`, and `100` if the model follows the stronger audit and provenance contract.
  - Risk: stricter field/source language may increase honest partials or make the model spend more turns on recovery. This is acceptable if it reduces wrong complete answers.
- Verification:
  - `cargo fmt --check` passed.
  - `cargo test -p browser-use-core persisted_final_answer` passed, including the new preview-replacement and max-turn persisted-answer tests.
  - `cargo test` passed, `58` browser-use-core tests plus the full workspace suite.
  - `uv run --with pytest python -m pytest -q` passed, `28 passed`.
  - `cargo build --bin browser-use-terminal` passed.

### Focused Rerun: `overnight-focused-v8-semantic-20260514-052602`

- Dataset: `real_v8`
- Tasks: `21`, `51`, `72`, `99`, `100`
- Root: `/tmp/overnight-focused-v8-semantic-20260514-052602`
- Commit under test: `59a42b4` (`Add semantic audit and finalization fallback`)
- Command: `dataset-run-codex real_v8 --task-id 21 --task-id 51 --task-id 72 --task-id 99 --task-id 100 --model gpt-5.5 --max-turns 80 --python-timeout-seconds 180 --max-attempts 1 --concurrency 5 --browser-mode cloud`
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `5/5` passed.

Focused outcomes:

| Task | Prior issue | Focused outcome | Judgment |
| ---: | --- | --- | --- |
| 21 | Returned null Booking prices without proving exact-date unavailability. | Still returns null prices for all 13 dates, with no saved final artifact. | No improvement. Prompt-only null-proofing did not make the model perform the price reveal flow. |
| 51 | Full 41-row persisted answer existed, but `done` returned only a 3-row preview. | Final result is the full `41`-row JSON list; `session.final_answer_used` fired and `done` used the persisted final answer. | Fixed. The finalization fallback/prompt works for this class. |
| 72 | Hit max provider turns after writing a partial final answer; strict semantic fail due missing operator ID. | Runner now finishes cleanly with an honest best-available answer. It still says operator ID is unknown/not verified. | Runner failure fixed; semantic task remains fail. This needs better site/search strategy, not finalization. |
| 99 | Accepted accessories/non-rackets as padel rackets. | Returned `20` racket-like products; visible names are all rackets/paddles rather than balls/grips/bags. | Improved; likely pass or much stronger partial depending product URL/name consistency. |
| 100 | Correct shape but missing Amazon prices and audit omitted `price`. | Final JSON has `20/20/20` platform buckets and `0` missing `price`, `image_url`, or `supplier`; audit includes `price`. | Improved; targeted missing-field issue fixed. |

Interpretation:

- The harness-level finalization fixes are working: they recover preview finalization and avoid a runner failure when the model reaches a partial deliverable at the end.
- The prompt-level semantic audit changes helped tasks `99` and `100`, at least on this focused sample.
- Task `21` shows the limitation of prompt-only guidance: the model needs better action verification for interactive price reveal flows, not just stronger final-review language.
- Task `72` remains unsolved at the first-principles level. It likely needs a better discovery strategy for ND DMR operator IDs or accepting that the public site may not expose the requested ID.

Next loop:

- Run full `real_v14_short` and `real_v8` again on `59a42b4`.
- If full v8 confirms improvements on `51`, `99`, and `100` without further regressions, keep this intervention.
- If task `21` remains a hard fail, investigate interactive action verification and CDP/network event capture for flows where a visible button must be opened before a field can be considered unavailable.

### Full Rerun: `overnight-real-v14-semantic-20260514-054705`

- Dataset: `real_v14_short`
- Root: `/tmp/overnight-real-v14-semantic-20260514-054705`
- Commit under test: `59a42b4` (`Add semantic audit and finalization fallback`)
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `10/10` passed.
- Manual strict result: `7` pass / `3` partial / `0` fail.
- Previous full v14 result on `926c0b0`: `6` pass / `4` partial / `0` fail.

Manual deltas:

| Task | Manual | Delta | Notes |
| ---: | --- | --- | --- |
| 5 | pass | improved | Direct JSON list, `99` rows, all five source groups, no missing required fields, and the bad `4G forbindelse ikon` rows disappeared. |
| 6 | partial | regressed | Candidate pool and top-by-duration selection were good, but per-ad screenshot deliverables had null/missing detail artifacts. |
| 8 | partial | stable | `26` SSDs; eight products have fewer than three offers and same-product/variant matching remains weak. |
| 10 | partial | stable/improved honesty | `40` per specialty only through ASPS all-location fallback; audit correctly says not ready with out-of-scope rows. |
| 11 | pass | fixed/stable | Persisted FCC JSON result remains fixed. |
| 16 | pass | improved | Correct `94103` store address, `128` menu items, `14` categories. |

Interpretation:

- The semantic/finalization intervention is positive on v14: strict score improved from `6/4/0` to `7/3/0`.
- The most important remaining v14 issue is no longer output shape. It is artifact/content validity: when screenshots are requested per selected entity, each selected row needs a nonblank screenshot that maps to that row.
- The ASPS task confirms the source-scope audit is doing useful work by marking broad fallback results as partial rather than silently complete.

### Full Rerun: `overnight-real-v8-semantic-20260514-061215`

- Dataset: `real_v8`
- Root: `/tmp/overnight-real-v8-semantic-20260514-061215`
- Commit under test: `59a42b4` (`Add semantic audit and finalization fallback`)
- Browser mode: cloud only. Local CDP env vars were unset and local Chrome auto-open was disabled.
- Runner result: `100/100` passed.
- Manual strict result from five judging agents: `71` pass / `26` partial / `3` fail.
- Weighted score with partial = 0.5: `84.0/100`.
- Previous full v8 result on `926c0b0`: `67` pass / `29` partial / `4` fail, weighted `81.5/100`.
- Earlier high-water run before final-shape changes: `73` pass / `23` partial / `4` fail, weighted `84.5/100`.

Manual score by range:

| Tasks | Pass | Partial | Fail | Weighted | Read |
| --- | ---: | ---: | ---: | ---: | --- |
| 1-20 | 18 | 1 | 1 | 18.5 | Recovered tasks `8` and `19`; task `9` is still hard fail but now fails honestly as empty, not wrong entity type. |
| 21-40 | 14 | 5 | 1 | 16.5 | Task `21` still fails; `27`/`40` show state/frontier regressions; `29` improved. |
| 41-60 | 14 | 6 | 0 | 17.0 | `51`, `54`, `60` fixed/improved; `47`, `49`, `50` regressed from missing visual/lazy content and incomplete offer frontiers. |
| 61-80 | 11 | 8 | 1 | 15.0 | Runner reliability fixed, but task `72` remains semantic fail; optional-but-requested fields and field-type checks are weak. |
| 81-100 | 14 | 6 | 0 | 17.0 | `87` improved from fail to partial; `99`/`100` improved but still have semantic field issues. |

Confirmed wins:

- Finalization mechanics are substantially healthier: tasks `51` and `57` no longer expose previews/status text, and `72` no longer runner-fails after persisting a best-available answer.
- Semantic prompt changes recovered `8` and `19`, showing the exact-source/entity guidance can improve real behavior.
- Task `99` moved from accessory pollution to mostly actual padel rackets.
- Task `100` now has a correct 20/20/20 bucket artifact with Amazon prices present; remaining issues are semantic filtering and rating/review gaps rather than total shape failure.

Persistent and regressed failures:

- `9`: zero-record artifact was treated as ready because missing-field checks passed vacuously. This is a general audit bug.
- `15`, `34`, `36`, `65`, `75`, `96`, `97`: requested fields remain missing or optional-but-requested fields are skipped in audits/final presentation.
- `21`, `40`: interactive state is not proven before extraction. The model reaches a relevant page but does not prove requested dates/address/filters are accepted and bound to results.
- `27`, `49`, `50`: all/load-more/offer-frontier proof is too weak. The model stops after visible primary groups without proving tabs, hidden sections, carousels, or plan families are exhausted.
- `46`: field-completeness pressure caused a plausible default (`No binding`) instead of source-backed `Not specified`.
- `47`: lazy/visual product-page sections such as Amazon A+ content were missed after DOM text looked empty.
- `63`: sitemap/frontier output contains samples/counts but not the full discovered frontier.
- `72`: returned an identifier without explicit evidence it was an operator ID.
- `99`: discount semantics are wrong when `discounted_price == price`.

Decision:

- Keep `59a42b4`; it improves the immediately previous full v8 run and v14.
- The next change should stay general and should not introduce task-specific validators. The highest-value general changes are:
  - make zero-record record-level audits fail unless the model explicitly proves an empty result is valid;
  - prevent a clean explicit `done` answer from bypassing a not-ready persisted final answer;
  - sharpen prompts for interactive state proof, source-route fidelity, full-frontier evidence, semantic field values, and price/discount semantics.

### Intervention In Progress: Audit And Semantic Finalization Guard

- Hypothesis: Many remaining partials are caused by the model presenting a result as complete after its own audit either failed, was vacuous, or did not check the right semantic evidence.
- Intervention:
  - `audit_artifact(...)` now marks zero-record record-level audits as not ready unless `allow_empty=True` is explicitly passed after proving a genuine no-match result.
  - `done` now rejects clean explicit completion text when a persisted final answer exists but its audit is not ready. The model must either fix/rerun `set_final_answer(..., audit=audit)` or finalize with an explicit partial/incomplete answer that names gaps.
  - The same not-ready bypass guard applies to free-text assistant completions, not only the `done` tool path.
  - Prompts now call out interactive state proof, source-route fidelity, no guessed normalized fields, full frontier evidence, audit-after-final-write, and price/discount semantics.
- Why this is generalizable:
  - It does not know task IDs, URLs, expected counts, or expected answers.
  - It turns the model's own audit into a real pre-final contract and fixes a vacuous truth bug.
  - It still allows honest partial completion, which matters when a site genuinely does not expose a requested field or source scope.
- Expected movement:
  - Task `9` should no longer treat `[]` as a complete audited extraction unless it proves empty-source evidence.
  - Tasks `15`, `36`, `88`, `99`, and `100` should be more likely to finalize as explicit partials when their audits are false instead of sounding complete.
  - Tasks `21`, `27`, `40`, `47`, `49`, and `50` may improve if the prompt makes the model prove state/frontier/visual sections before extraction.
  - Risk: stricter finalization may turn some runner passes into explicit partials or extra retries. That is acceptable if wrong-complete answers go down.
- Verification so far:
  - `uv run --with pytest python -m pytest -q python/tests/test_worker_package.py -q`: passed, `29` tests.
  - `cargo test -p browser-use-core bypass`: passed, `2` tests.
- Next:
  - Run full verification (`cargo fmt --check`, `cargo test`, Python tests, build).
  - Commit if clean.
  - Focused retest likely failure probes: v8 `9`, `15`, `21`, `27`, `47`, `49`, `50`, `99`, `100`, plus v14 `6`.

## Experiment 20260514-04: BU_Bench_V1 Remote Run And Manual Judging

- Dataset: `BU_Bench_V1`
- Root: `/tmp/overnight-bu-bench-v1-20260514-072225`
- Manifest: `/tmp/overnight-bu-bench-v1-20260514-072225/state/dataset-runs/BU_Bench_V1-1778768545971.json`
- Judge packets: `/tmp/overnight-bu-bench-v1-20260514-072225/judge-packets`
- Commit under test: `c498a66` (`Add human-readable overnight report`)
- Browser mode: cloud only. Local CDP env vars were unset, local Chrome auto-open was disabled, and no local browser was opened for judging.
- Runner result: `95` passed / `5` failed / `0` pending.
- Manual strict result from five judging agents: `74` pass / `9` partial / `17` fail.
- Weighted score with partial = 0.5: `78.5/100`.

Manual score by category:

| Category | Pass | Partial | Fail | Weighted | Read |
| --- | ---: | ---: | ---: | ---: | --- |
| WebBenchREAD | 15 | 4 | 1 | 17.0 | Mostly strong artifact-backed extraction; one content mismatch and a few weak source/order proofs. |
| OM2W2 | 17 | 3 | 0 | 18.5 | Strongest general web-research category; partials are factual precision and evidence-depth risks. |
| InteractionTests | 18 | 2 | 0 | 19.0 | Browser interaction is healthy; partials are weak final evidence (`Done.` without proof), not visible runtime failures. |
| GAIA | 10 | 0 | 10 | 10.0 | Exact-answer tasks are harsh; many completed but answered the wrong entity/value/count. |
| BrowseComp | 14 | 0 | 6 | 14.0 | Four failures were max-turn no-answer cases; two completed with wrong entities. |

Runner failures:

| Index | Category | Task ID | Cause |
| ---: | --- | --- | --- |
| 63 | GAIA | `d7eea2b1-7d31-425e-977c-b803ffd28250` | Provider rejected invalid image input. |
| 83 | BrowseComp | `cd22ee34-6f65-4bf2-acb1-34bc32a735af` | Exceeded max provider turns. |
| 90 | BrowseComp | `8d92dbe1-bf45-4daf-ac05-02d0bd2cf9c8` | Exceeded max provider turns. |
| 95 | BrowseComp | `71906b5a-577d-48c4-a253-c756790afa64` | Exceeded max provider turns. |
| 98 | BrowseComp | `55371adf-23f9-4f35-88d4-f342c972bf8e` | Exceeded max provider turns. |

Notable semantic failures despite runner success:

| Index | Category | Expected | Final | Failure mode |
| ---: | --- | --- | --- | --- |
| 60 | GAIA | `6` | `3` | Count/reasoning error. |
| 62 | GAIA | `6` | `79` | Counted occurrence records instead of requested animals. |
| 66 | GAIA | `2018` | `1987` | Reconstructed wrong stock-price crossing semantics. |
| 67 | GAIA | `Mapping Human Oriented Information to Software Agents for Online Systems Usage` | `A New Software Agent 'Learning' Algorithm` | Wrong paper/entity. |
| 72 | GAIA | `mice` | `mice and humans` caveat | Added extra answer beyond requested exact output. |
| 74 | GAIA | `pears, bananas` | `pears, plums, bananas` | Included extra item. |
| 76 | GAIA | `Claude Shannon` | `Jerome Wiesner` | Wrong entity. |
| 78 | GAIA | `17` | `17000` | Unit/format mismatch; answered hours instead of thousands of hours. |
| 79 | GAIA | `backtick` | `dot` | Wrong symbol. |
| 82 | BrowseComp | `Mallorca, Spain` | `Brno, Czech Republic` | Wrong location/entity chain. |
| 94 | BrowseComp | `Galacta: The Battle for Saturn` | `ShadowCaster` | Wrong game. |

Partial clusters:

- Weak final evidence: InteractionTests `41` and `57` likely completed, but final output was only `Done.` with no success text, secret, or artifact proof.
- Source/order proof: WebBenchREAD `1`, `3`, and `12` returned plausible outputs without enough evidence that they were the requested first results, Review Bytes, or map-view filtered results.
- Site/access fallback: WebBenchREAD `15` hit VRBO blocking and used Expedia fallback, so the task was useful but not source-faithful.
- Factual precision/evidence depth: OM2W2 `23`, `29`, and `34` had useful outputs but unresolved factual thresholds, exact model gaps, or weak route/distance proof.

Interpretation:

- BU_Bench is not a pure browser-runtime benchmark. The browser interaction categories did well, but exact-answer reasoning categories exposed substantial model/research quality gaps.
- The gap between runner success (`95%`) and manual strict pass (`74%`) is large. The harness currently measures task completion/finalization much more than semantic correctness.
- The main generalizable fixes are not task-specific validators. They are:
  - stronger answer-shape pressure for exact-answer tasks: final answer should be only the requested unit/entity/value and should not include extra candidates;
  - better source-chain verification before finalizing answer-heavy research tasks;
  - invalid image hardening before sending screenshots/images to the provider;
  - max-turn salvage for BrowseComp-style long searches, where a best current candidate plus evidence may be better than no answer;
  - final evidence requirement for interaction tasks, so `Done.` carries visible success text or a captured artifact.

Decision:

- Treat BU_Bench as a useful complementary benchmark, not a replacement for `real_v8`/`real_v14`.
- No immediate code change was made from this judging pass. The next implementation should prioritize general final-answer/evidence discipline and provider image validation, then rerun the failed/partial BU_Bench probes alongside the existing v8/v14 probes.
