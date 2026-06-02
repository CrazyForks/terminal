# Cascade Lookups

Use this when the task asks you to find a set of items, then do the same follow-up lookup for each item, or compare the same field across several pages/sites.

Trigger this skill before opening the first detail page when any of these are true:

- The task says "for each", "each of these", "all products", "all listings", "all files", or "compare across" and there are 5+ items.
- The task has a cascade shape: find X, then for each X find Y, then for each Y summarize or verify Z.
- The task has 3+ independent sources/sites or many document/detail pages.

Workflow:

1. Use the parent browser or HTTP helpers only to discover the item manifest: names, URLs, source labels, and required output fields.
2. Write or keep a compact manifest mapping each item to its helper id/name.
3. Spawn one helper per independent item before visiting detail pages locally.
4. Give each helper exactly one item and a strict output schema.
5. Continue light parent work only if it does not duplicate helper work.
6. Collect helper results with `wait_agent`, merge completed results, and repeat for unfinished helpers.
7. Before `done`, audit count, required fields, and dedupe keys with `audit_artifact(...)` or equivalent Python checks.

V1 call template, when `spawn_agent` has no `task_name` field:

```text
spawn_agent(message="Handle only item 3/9 for the parent task: <name or URL>. Extract <fields>. Use browser/data tools as needed. Return exactly one JSON object via done(result=...) with keys: item, source_url, <fields>, evidence.")
```

V2 call template, when `task_name` is required:

```text
spawn_agent(task_name="item_3", message="Handle only item 3/9 for the parent task: <name or URL>. Extract <fields>. Use browser/data tools as needed. Return exactly one JSON object via done(result=...) with keys: item, source_url, <fields>, evidence.")
```

Do not do a sequential detail-page walk for these tasks. Sequential browsing is the fallback only after a helper fails and the missing item is still required.
