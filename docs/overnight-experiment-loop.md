# Overnight Experiment Loop

Worktree: `/Users/greg/Documents/browser-use/experiments/overnight-experiment`

Branch: `overnight-experiment`

Base commit: `8ce05eb` (`Merge TUI live state cache`)

This document captures the intended overnight eval/improvement loop. It should be followed by future agents without relying on chat memory.

This loop is inspired by Karpathy's `autoresearch` pattern: the Markdown protocol is part of the experiment surface. Agents should treat this file, the cumulative report, prompts, judging rubrics, and code changes as the "research org code" that can be iterated on deliberately.

## Objective

Run repeated eval/improvement cycles on the Rust rewrite browser agent until the results converge:

- solve as much of `real_v8` and `real_v14_short` as possible;
- optionally use `BU_Bench_V1` as an additional generalization dataset when `datasets/BU_Bench_V1.json` is available;
- understand every failure mode from traces, artifacts, database events, and code;
- compare failures against prior runs to distinguish variance from real regressions;
- implement only generalizable fixes;
- avoid benchmark-specific or deterministic answer checks;
- keep a single cumulative Markdown report explaining every experiment, result, decision, and code change.

The goal is not to make a benchmark harness that catches known bad answers. The goal is to improve the agent loop so the model naturally produces the correct output sooner, with less context waste, less lost progress, and better source discipline.

The desired output after an unattended run is a readable scientific log: what was tried, why it was tried, what happened, what got better or worse, what was kept or reverted, and what should be tried next.

## Hard Constraints

- Use Codex login as the provider auth.
- Use Browser Use cloud / remote browser only.
- Do not use local Chrome.
- Do not open local Chrome windows.
- Run dataset evals with 25-way concurrency unless there is a clear resource/time-out reason to lower it.
- Default eval datasets:
  - `real_v8`
  - `real_v14_short`
- Optional generalization dataset:
  - `BU_Bench_V1`, only if `datasets/BU_Bench_V1.json` exists or is supplied before the run
- Keep work in this worktree, not the original dirty UI worktree.
- Preserve every run artifact under `/tmp` with a stable run ID.
- Do not overfit to task IDs, websites, exact expected answers, or known bad strings.
- Prefer general model-loop improvements over deterministic validation.

## Research Org / Program-As-Code

Think of this worktree as a small autonomous research organization:

- This document is the top-level operating program.
- The prompts are agent behavior programs.
- The judging rubric is the measurement program.
- The cumulative report is the lab notebook.
- Runtime code changes are interventions that must be justified by evidence.

Every change should have an experiment shape:

1. Hypothesis: what failure mode should this improve?
2. Intervention: what prompt, code, harness, or workflow change was made?
3. Expected effect: what metric or failure cluster should move?
4. Measurement: which dataset run, focused subset, or manual rerun was used?
5. Result: what improved, regressed, or stayed flat?
6. Decision: keep, revert, revise, or investigate more.

Prompt and protocol changes are valid experiments. Do not only change Rust/Python if the failure is better addressed by clearer task-contract instructions, better judging instructions, or a more disciplined research loop.

## Anti-Overfit Rules

Do not implement:

- task-specific answer rules;
- website-specific expected values;
- dataset-specific count tables;
- special cases like "if task 46, answer No binding";
- hardcoded rejection of exact task-observed strings such as `Bestseller` or `4G forbindelse ikon`;
- tool-level benchmark validators that force a known answer shape for a specific dataset task.

Allowed generalizable fixes:

- better prompt/task-contract discipline;
- checkpoint-first extraction;
- source-depth prompting for missing requested fields;
- model-driven artifact self-review;
- context hygiene and output compaction;
- provider-buffer retry/compaction;
- turn-deadline consolidation;
- retry/resume from partial artifacts;
- general artifact summaries that ask the model to review its own output against the user request.

## Baseline Context

Known prior full runs:

- Previous cloud run: `/tmp/real-v8-codex-cloud-20260513-112315`
  - Report: `docs/real-v8-codex-cloud-run-20260513-report.md`
  - Manual strict score reported as `86/100`
  - Runner manifest score was `93/100`
- Later cloud run: `/tmp/real-v8-codex-cloud-20260513-174409`
  - Report exists in the original worktree as `docs/real-v8-codex-cloud-run-20260513-174409-report.md`
  - Runner manifest score was `98/100`
  - Manual strict score was lower because judging was more granular and penalized semantically weak artifacts.

Important interpretation from prior analysis:

- The branch did not obviously regress remote browser connectivity.
- The biggest remaining failures are model-loop quality failures: long extraction drift, lost progress, context blowup, weak source depth, and semantically bad artifacts that still look structurally valid.
- The likely code-risk area is the core agent loop after `codex-native-browser-harness-impl`, especially broad recover-and-continue behavior, context overflow handling, and weak final artifact quality gates.

## Eval Commands

Build first if needed:

```bash
cargo build -p browser-use-cli
```

List available datasets before starting a cycle:

```bash
./target/debug/browser-use-terminal dataset-list
test -f datasets/BU_Bench_V1.json && echo "BU_Bench_V1 available" || echo "BU_Bench_V1 unavailable"
```

Run `real_v8`:

```bash
RUN_ID="overnight-real-v8-$(date +%Y%m%d-%H%M%S)"
ROOT="/tmp/$RUN_ID"
mkdir -p "$ROOT"

LLM_BROWSER_BROWSER_MODE=cloud \
LLM_BROWSER_AUTO_CHROME=0 \
LLM_BROWSER_OPEN_CLOUD_LIVE_VIEW=0 \
BU_CDP_URL= \
BU_CDP_WS= \
BU_BROWSER_ID= \
./target/debug/browser-use-terminal \
  --state-dir "$ROOT/state" \
  dataset-run-codex real_v8 \
  --all \
  --model gpt-5.5 \
  --max-turns 80 \
  --python-timeout-seconds 180 \
  --max-attempts 2 \
  --concurrency 25 \
  --browser-mode cloud \
  --run-id "$RUN_ID" 2>&1 | tee "$ROOT/dataset-run.log"
```

Run `real_v14_short`:

```bash
RUN_ID="overnight-real-v14-short-$(date +%Y%m%d-%H%M%S)"
ROOT="/tmp/$RUN_ID"
mkdir -p "$ROOT"

LLM_BROWSER_BROWSER_MODE=cloud \
LLM_BROWSER_AUTO_CHROME=0 \
LLM_BROWSER_OPEN_CLOUD_LIVE_VIEW=0 \
BU_CDP_URL= \
BU_CDP_WS= \
BU_BROWSER_ID= \
./target/debug/browser-use-terminal \
  --state-dir "$ROOT/state" \
  dataset-run-codex real_v14_short \
  --all \
  --model gpt-5.5 \
  --max-turns 80 \
  --python-timeout-seconds 180 \
  --max-attempts 2 \
  --concurrency 25 \
  --browser-mode cloud \
  --run-id "$RUN_ID" 2>&1 | tee "$ROOT/dataset-run.log"
```

Run optional `BU_Bench_V1` only if `dataset-list` shows it:

```bash
RUN_ID="overnight-bu-bench-v1-$(date +%Y%m%d-%H%M%S)"
ROOT="/tmp/$RUN_ID"
mkdir -p "$ROOT"

LLM_BROWSER_BROWSER_MODE=cloud \
LLM_BROWSER_AUTO_CHROME=0 \
LLM_BROWSER_OPEN_CLOUD_LIVE_VIEW=0 \
BU_CDP_URL= \
BU_CDP_WS= \
BU_BROWSER_ID= \
./target/debug/browser-use-terminal \
  --state-dir "$ROOT/state" \
  dataset-run-codex BU_Bench_V1 \
  --all \
  --model gpt-5.5 \
  --max-turns 80 \
  --python-timeout-seconds 180 \
  --max-attempts 2 \
  --concurrency 25 \
  --browser-mode cloud \
  --run-id "$RUN_ID" 2>&1 | tee "$ROOT/dataset-run.log"
```

Dataset strategy:

- Use `real_v8` as the broad baseline.
- Use `real_v14_short` as the fast hard smoke test.
- Use `BU_Bench_V1`, when present, as an out-of-distribution check against overfitting to the Browser Use benchmark tasks.
- In inner loops, focused subsets are allowed for speed.
- At milestone points, run full datasets again before claiming a change helped.

If a full run is too expensive or slow, use a focused subset. Throwing out tasks is allowed only for triage, and only under explicit criteria:

- the task repeatedly passes cleanly across recent runs;
- the task is operationally trivial and unrelated to current failure modes;
- the task would waste time while investigating a known failure cluster.

Do not permanently remove tasks from the benchmark score. Mark skipped tasks clearly in the report.

## Parallel Judging

After each run, use subagents in parallel to judge artifacts and traces.

Suggested split:

- For `real_v8`: 5 subagents, roughly 20 tasks each.
- For `real_v14_short`: one or two subagents are enough unless the traces are large.

Each judge should inspect:

- manifest result;
- final answer;
- output files under `outputs/`;
- artifact files;
- session DB events;
- failure/error events;
- signs of partial output, wrong source, missing fields, context blowup, or no checkpointing.

Judges should return:

- strict pass/partial/fail;
- runner-vs-manual mismatch;
- concise failure mode;
- evidence path;
- suspected root cause;
- whether this is variance, regression, or persistent current-branch weakness.

## Deep Failure Analysis

For every failed/partial task and every runner/manual mismatch:

1. Inspect the output artifact.
2. Inspect the final answer.
3. Query `state.db` events.
4. Identify the exact last good state.
5. Identify whether useful data existed but was not saved.
6. Compare against previous runs when available.
7. Read relevant code paths.
8. Decide whether the problem is:
   - model strategy;
   - prompt/task-contract weakness;
   - browser/runtime issue;
   - provider/context issue;
   - artifact/final-answer contract issue;
   - scheduler/retry/checkpoint issue;
   - external site variance.

Useful DB commands:

```bash
sqlite3 "$ROOT/state/state.db" \
  "select id,status,cwd,artifact_root from sessions order by created_ms;"

sqlite3 "$ROOT/state/state.db" \
  "select seq,type,substr(payload_json,1,500) from events where session_id='$SESSION' order by seq;"

sqlite3 "$ROOT/state/state.db" \
  "select type,count(*) from events where session_id='$SESSION' group by type order by count(*) desc;"
```

## Regression Analysis

For each failure, compare to prior runs:

- Did the same task pass before?
- Did it pass only under looser judging?
- Did the output improve structurally but degrade semantically?
- Did it fail for the same reason or a different reason?
- Did the current branch convert a hard failure into a weak completion?
- Does the git diff plausibly explain the change?

The main code boundaries to compare:

- before current branch experiment: `6df7527`
- current base: `8ce05eb`
- key merge: `1a2e232`
- key branch commit: `5400414`

Useful diff commands:

```bash
git diff 6df7527..1a2e232 --stat
git diff 6df7527..1a2e232 -- crates/browser-use-core/src/lib.rs
git diff 6df7527..1a2e232 -- prompts/browser-agent-system.md prompts/python-tool-description.md
git diff 6df7527..1a2e232 -- crates/browser-use-protocol/src/lib.rs
git diff 6df7527..1a2e232 -- crates/browser-use-core/src/tools/command.rs
```

## Generalizable Fix Candidates

Prefer these categories.

### Task Contract Discipline

Have the model derive a task contract from the user request:

- required deliverable;
- required fields;
- minimum or approximate count if the user says one;
- source of truth;
- acceptable unknowns and evidence required for unknowns.

Before finishing, the model should review its artifact against that contract.

### Checkpoint-First Extraction

For long extraction tasks:

- write partial results early;
- update output files every page or every batch;
- never hold the only useful copy in Python memory;
- once a minimum target is reached, save before optional enrichment.

### Source-Depth For Missing Fields

If a requested field is absent from visible cards/tables, the model should inspect:

- detail pages;
- terms/legal pages;
- PDFs;
- FAQ;
- network/API responses;
- source HTML/data blobs.

Only then should it use `unknown` / `not specified`, ideally with evidence.

### Model-Driven Artifact Self-Review

Before `done`, ask the model to inspect a compact artifact summary and look for semantic problems:

- UI chrome accidentally used as data;
- all rows have the same suspicious missing value;
- product names look like badges or actions;
- image fields look like icons/placeholders;
- counts do not match the requested target;
- fields are empty despite the source likely containing the data.

This should be a model review step, not a deterministic benchmark validator.

### Context Hygiene

Avoid giant printed JSON/DOM/log output in the model context:

- write large data to files;
- use `set_final_answer(...)`;
- include only compact previews;
- spill large tool output;
- summarize event traces before feeding them back to the model.

### Provider Buffer Handling

Treat provider errors such as:

- `507 Insufficient Storage`;
- `request buffer limit`;
- `input too large`;
- `context length`;
- `too many tokens`;

as context-management failures. Compact harder and retry once. If the retry fails, preserve artifacts and mark the failure clearly.

### Turn Deadline Consolidation

Near the max-turn budget:

- stop exploration;
- save the best current artifact;
- run self-review;
- finish with known gaps or fail explicitly.

This is general and prevents tasks from dying after useful work has already been done.

## Implementation Loop

Each cycle should follow this structure:

1. Run evals or focused subset.
2. Spawn parallel judges.
3. Aggregate strict/partial/fail outcomes.
4. Compare to previous runs.
5. Identify current failure modes.
6. Read code paths and DB traces deeply.
7. Decide if changes should be made.
8. Implement only generalizable fixes.
9. Run targeted regression tasks.
10. Run broader eval again.
11. Append results to the cumulative report.
12. Repeat until manually stopped by the user.

Do not stop automatically because the scores appear to have converged. If a cycle looks flat, document the plateau, formulate a new generalizable hypothesis, and continue. The user will manually stop the process when they want it stopped.

## Cumulative Report

Maintain one living report:

```text
docs/overnight-experiment-report.md
```

The report must work as a one-day catch-up document. Keep a top dashboard at the beginning with:

- current recommended branch state;
- best current score by dataset;
- latest strict/manual score by dataset;
- most important improvement;
- worst regression;
- currently open root-cause clusters;
- next recommended experiment.

Every experiment must append:

- experiment ID;
- date/time;
- git SHA;
- branch;
- hypothesis;
- intervention;
- expected outcome;
- command used;
- run IDs and artifact roots;
- datasets used;
- raw runner score;
- manual strict score;
- manual half-credit score if useful;
- runtime, cost, and token usage if available;
- skipped tasks and why;
- failure table;
- regression table;
- git-diff causality analysis;
- code changes made;
- prompt/protocol changes made;
- verification commands;
- targeted rerun results;
- decision: keep, revert, or revise change;
- next hypothesis.

Include negative results, failed commands, abandoned attempts, environment problems, and changes that were reverted. A failed experiment is useful if the report makes clear what it ruled out.

Use this experiment ledger shape:

```markdown
## Experiment YYYYMMDD-NN: short name

- Hypothesis:
- Intervention:
- Expected movement:
- Datasets/runs:
- Metrics:
- Failure-mode changes:
- Regressions:
- Code/prompt diff summary:
- Decision:
- Next:
```

The report should be written for a human reader who was away for a day and needs to understand what happened without opening every trace.

## Verification

After code changes, run the relevant checks.

For core/runtime/provider/worker changes:

```bash
cargo fmt --check
cargo test
uv run --with pytest python -m pytest -q
```

For TUI changes, follow `AGENTS.md` and run:

```bash
scripts/verify-terminal-ui.sh
```

Avoid TUI changes unless they are necessary for the eval loop.

## Initial Hypotheses To Test

1. Broad tool-error recovery improved raw completion but causes long wandering and weak semantic completions.
2. Provider buffer errors like `507 Insufficient Storage` are currently misclassified and should trigger compaction/retry.
3. The agent needs stronger prompt/runtime discipline around checkpointing and source-depth, not deterministic validators.
4. Runner `ok` is too weak as a benchmark-quality signal.
5. Turn-deadline consolidation should salvage useful partial work and reduce max-turn failures.

## First Implementation Candidates

Do not implement all at once. Prefer small experiments with targeted reruns.

1. Add provider-buffer error strings to context-overflow retry handling.
2. Add stronger prompt language for task contract, checkpoint-first extraction, and source-depth.
3. Add a model-driven pre-done self-review step or prompt convention.
4. Add deadline consolidation behavior near max turns.
5. Narrow recoverable tool errors so some failures trigger structured recovery instead of blind continuation.

Each candidate must be evaluated against both improvement and regression risk.

## Manual Control

The loop should continue until the user manually stops it.

If a cycle reaches an apparent plateau, do not stop. Instead:

- document the plateau;
- identify what classes of failures remain;
- separate generalizable problems from external/site variance;
- choose the next generalizable hypothesis;
- continue with another experiment.

If local Chrome or local CDP is touched, pause the eval loop immediately, kill the offending processes, document it, fix run hygiene, commit the hygiene fix if needed, and then continue.
