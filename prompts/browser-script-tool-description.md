Run Python for browser page interaction through the Rust-held CDP connection.

This is the browser interaction tool and page/data-plane tool. Use it for navigation, page inspection, clicks, typing, scrolling, screenshots, downloads, uploads, network inspection, extraction, browser-backed verification, artifacts, and final answers.

Use the `browser` tool for connection/runtime work first. If the browser is not connected, run `browser status --json` and then an explicit connect command such as `browser connect local`, `browser connect managed --headless`, or `browser remote start`.

Important execution model:

- Each `browser_script` call starts a fresh Python process.
- Python variables do not persist across calls.
- Browser/CDP state persists in Rust.
- Fast calls return their final result immediately. Long calls return `status: running` with a `run_id`; keep observing that same run until it finishes, fails, or is cancelled.
- To listen to a running script, call this tool with `action="observe"`, the returned `run_id`, and optionally `observe_timeout_ms`. If observe reports no new output, wait/back off instead of polling in a tight loop.
- To stop a running script, call this tool with `action="cancel"` and the `run_id`. Partial images and artifacts emitted before cancellation are preserved.
- A failed `browser_script` call may include a short diagnosis. Read that diagnosis first: if it says the browser is still connected or the same page is usable, continue from the same page instead of reconnecting.
- Helpers are preimported; you do not need imports for normal browser work.
- CDP is the source of truth. If a helper is incomplete, use `cdp(...)` directly.
- Keep browser actions sequential and deliberate.
- Do not import Playwright, Selenium, or Pyppeteer.

Preimported helpers:

```python
cdp(method, session_id=None, **params)
cdp_batch(calls)
js(expression, returnByValue=True)
browser_fetch(url, headers=None, method="GET", body=None, timeout=20.0, binary=False)
browser_fetch_many(urls, headers=None, method="GET", body=None, timeout=20.0, binary=False, max_concurrent=8)

new_tab(url="about:blank")
goto_url(url)
page_info()
navigation_snapshot(keywords=None, limit=80)
embedded_data_snapshot(limit=80, max_sources=12)
network_resources_snapshot(limit=80, keywords=None)
repeated_items_snapshot(min_count=3, limit=8, include_prices=True)
extract_repeated_items(selector, limit=50, include_html=False)
pricing_cards_snapshot(limit=50)
rows_snapshot(limit=8)
extract_grid_rows(selector=None, limit=50, include_html=False)
read_document_text(source, headers=None, timeout=30.0, max_chars=120000, binary=None)
arxiv_query(search_query="cat:cs.AI", start=0, max_results=20, sort_by="submittedDate", sort_order="descending", timeout=20.0)

capture_screenshot(...)
screenshot(label="screenshot", full=False)
screenshot_clip(label, x, y, width, height)

click_at_xy(x, y)
fill_input(selector, text, clear=True)
form_fields_snapshot(limit=30)
fill_form_field(label_or_placeholder, value, clear=True, timeout=3.0)
autocomplete_suggestions_snapshot(query=None, limit=20)
select_autocomplete(label_or_placeholder, query, match_text=None, timeout=3.0)
action_controls_snapshot(limit=30)
click_button(label_or_text, timeout=3.0)
overlay_actions_snapshot(limit=20)
dismiss_overlay(prefer="accept", timeout=1.0)
pagination_controls_snapshot(limit=20)
click_pagination(label_or_text="next", timeout=2.0)
result_count_snapshot(limit=12)
form_controls_snapshot(limit=30)
toggle_form_control(label_or_text, checked=True, timeout=1.0)
select_controls_snapshot(limit=20, option_limit=30)
select_option(label_or_placeholder, option_text_or_value, timeout=1.0)
type_text(text)
press_key(key, modifiers=0)  # accepts chords like "Meta+A"; modifiers: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8
scroll(x=0, y=600)

wait_for_load(timeout=10)
wait_for_element(selector, timeout=10)
wait_for_network_idle(timeout=10)

current_tab()
list_tabs()
switch_tab(target_id)
ensure_real_tab()

upload_file(...)
drain_events()
http_get(url, **kwargs)
http_get_many(urls, headers=None, timeout=20.0, binary=False, max_workers=8)

copy_artifact(path, kind="file")
emit_output(value, label=None)
emit_image(path, label=None)
artifact_root()
outputs_dir()
session_metadata()
audit_artifact(data=None, min_count=None, exact_count=None, required_fields=None, unique_by=None, path=None, records_key=None, records_path=None, nonempty_file=False, **requirements)  # path can point to JSON, JSONL, CSV, or text
load_agent_helpers()
agent_workspace()
domain_skills_for_url(url_or_domain, include_content=False)
last_domain_skills(include_content=False)
```

Usage guidance:

- First navigation should usually be `new_tab(url)`, not `goto_url(url)`, because `goto_url(url)` mutates the current controlled tab.
- Keep keyboard semantics browser-harness/Rod aligned: `press_key(...)` simulates physical keys or shortcuts, while `type_text(...)` inserts/pastes text into the focused element with `Input.insertText`.
- For React/Vue/Svelte/controlled inputs, prefer `fill_input(selector, text)` over direct DOM value assignment. It focuses the element, clears with Cmd/Ctrl+A plus Backspace, types through physical key events, then fires final `input`/`change` events.
- On first page load, if a cookie/privacy/modal overlay blocks content or intercepts clicks, call `overlay_actions_snapshot()` when uncertain and `dismiss_overlay(prefer="accept")` or `dismiss_overlay(prefer="reject")` to click the rendered consent/dismissal control.
- For forms with labeled fields or split address fields, call `form_fields_snapshot()` before coordinate guessing. Use `fill_form_field(label_or_placeholder, value)` to target visible/rendered fields by label, placeholder, name, selector, or nearby text while preserving real browser input events.
- For autocomplete/typeahead fields where a valid suggestion must be selected before Search/Submit is enabled, use `select_autocomplete(label_or_placeholder, query, match_text=...)`; it fills the rendered field, inspects visible listbox/menu suggestions with `autocomplete_suggestions_snapshot(...)`, and clicks the rendered suggestion center.
- For named actions after filling/search/filter forms, call `action_controls_snapshot()` when uncertain and use `click_button(label_or_text)` for visible buttons/links such as Search, Submit, Apply, Next, Download, or Save. It clicks the rendered control center through browser input events instead of synthesizing DOM clicks.
- For labeled checkboxes, radios, and switches, call `form_controls_snapshot()` when uncertain and use `toggle_form_control(label_or_text, checked=True/False)` to click only when the current state differs.
- For native selects and combobox-like dropdowns, call `select_controls_snapshot()` when uncertain and use `select_option(label_or_placeholder, option_text_or_value)` to choose by visible label and option text/value.
- Do not combine `Input.dispatchKeyEvent` carrying printable `text` with a manual `char` event for the same character; that double-inserts text in Chrome.
- If the task is site-specific, call `domain_skills_for_url(url, include_content=True)` before inventing selectors, private API routes, or flows. `goto_url(url)` also returns matching `domain_skills` metadata when a skill root is available.
- Use screenshots as labeled temporal checkpoints: initial load, before/after meaningful clicks, scrolls, route changes, dialogs, uploads, downloads, and final verification.
- Before deciding a site lacks a property/listing/document/investor/results page, call `navigation_snapshot(keywords=[...])`. It returns visible links plus collapsed menu/toggle controls with `aria-expanded`/`aria-controls`, stable selectors, keyword matches, and relevance scores. If it shows a collapsed menu/toggle control, click or press that control, wait, then call `navigation_snapshot` again before giving up.
- On ecommerce, directory, product, investor/document, and SPA listing pages, call `embedded_data_snapshot()` before brittle visual scraping. It extracts bounded records from JSON-LD, `__NEXT_DATA__`, Nuxt payloads, JSON script tags, and product/document meta tags, including normalized names, URLs, images, prices, dates, brands, descriptions, and document links.
- After navigation, search/filter actions, SPA route changes, or downloads, call `network_resources_snapshot(keywords=[...])` to discover XHR/API, document/download, pagination, form, and export URLs before manual page walking. Use promising URLs with `http_get_many(...)`, `browser_fetch_many(...)`, or `read_document_text(...)`.
- For search-result tables, docket grids, comparison tables, or SPA row lists where row-scoped links/buttons matter, call `rows_snapshot()` and then `extract_grid_rows(selector=...)`. It returns header-labeled cells, description-like fields, row-scoped links/buttons, coordinates, and `file_actions` so filenames/download buttons stay associated with the correct row.
- For PDFs, DOCX files, downloaded filings, investor reports, and earnings documents, use `read_document_text(url_or_path, max_chars=...)` after discovering a direct file URL/path. It fetches or reads the file and extracts bounded text using available PDF/DOCX parsers or `pdftotext`, with a low-fidelity PDF byte-string fallback.
- For arXiv recent-list or paper-search tasks, use `arxiv_query(search_query=..., start=..., max_results=...)` instead of browsing arXiv list/abstract pages one by one. It returns normalized title, authors, abstract, abs/pdf URLs, categories, DOI/journal/comment metadata, and first-version submission time when exposed by arXiv.
- When repeated product/listing/package/ticket cards or rows are visible, call `repeated_items_snapshot()` first. It can recommend class, data-attribute, role, and schema selectors for SPA cards. If it returns `recommended_action: "extract_repeated_items"`, call `extract_repeated_items(selector=...)` and use those records instead of taking more screenshots or visiting cards one by one. If it returns `fanout_recommended: true`, spawn one child agent per detail link/item before opening detail pages in the parent. The extracted records include compact text, stable item attributes, table/list cells with semantic headers when available, headings, labels, prices, links with action labels, buttons, and image metadata including lazy `data-src`/`srcset`/`picture source` fields.
- For pricing/product/package/ticket cards, call `pricing_cards_snapshot()` to get visible commercial records with price tokens, speed/data tokens, contract hints, offer-type hints, links, and images before writing custom extraction JavaScript.
- For paginated listings, result pages, and "Load more" flows, call `pagination_controls_snapshot()` before guessing. Use `click_pagination("next")` or `click_pagination("load more")`, then wait and re-run the relevant extraction helper.
- For result-count or page-count evidence such as "Matches 1 - 25 of 58", "Records 1 through 10 of N", "Page 1 of 3", or "710 exhibitors", call `result_count_snapshot()` and keep its `evidence` text with the parsed count.
- The common screenshot call is `screenshot(label)`, for example `screenshot("before_submit")`.
- Screenshot/image artifacts are sent as `input_image` content to the next model turn. The user does not see those pixels inline in the terminal; describe what you see or provide the saved artifact path when the user asks for the screenshot.
- If a script emits screenshots/images and then fails, the next model turn still receives the images alongside the failure diagnosis. Use those pixels to decide the next smaller retry.
- If a running script emits screenshots/images before it finishes, `observe` returns those images as soon as they are available. Use the pixels to guide the next observe/retry.
- Use `emit_output(value, label="...")` for structured observations that the next model turn may need, such as `page_info()`, extracted rows, selected DOM state, or API responses. The full value stays model-visible.
- When a script emits labeled structured output, add a `# browser_summary:` JSON comment block at the top of the script that maps each emitted label to the compact transcript summary. Write the code/labels first mentally, then place or update this block before submitting the tool call; the runtime parses the whole script before execution.
- Summary values may be literals, JSONPath-like selectors such as `$.url`, or template strings such as `Read ${$.length} employee rows`. Missing summary specs fall back to a generic `Recorded <label>` summary while preserving the full output.
- Prefer this pattern over printing page or extraction objects:

```python
# browser_summary:
# {
#   "page_info": {
#     "kind": "page",
#     "url": "$.url",
#     "title": "$.title"
#   },
#   "employee_rows": {
#     "kind": "extracted",
#     "message": "Read ${$.length} employee rows"
#   }
# }

info = page_info()
emit_output(info, label="page_info")

rows = [{"name": "Ada"}, {"name": "Grace"}]
emit_output(rows, label="employee_rows")
```

- Keep `print(...)` for short debug/status text only. Do not print large page, DOM, network, or extraction objects when `emit_output(...)` can carry the full value.
- Prefer coordinate clicks for visible UI: screenshot, inspect pixels, `click_at_xy(x, y)`, wait, screenshot again.
- Use `js(...)` for DOM inspection and raw `cdp(...)` for lower-level browser actions.
- For real user forms, act like a browser user: screenshot, click the visible field/control, type with `type_text(...)`, `press_key(...)`, or `fill_input(...)`, then screenshot or otherwise verify. Use coordinate clicks for checkboxes, radios, buttons, dropdowns, and custom controls. Do not assign `element.value`, `element.checked`, `selectedIndex`, React private state, or MutationObserver restore loops on live forms. Do not synthesize `input`, `change`, `click`, or keyboard events in page JavaScript to make a form look filled. Those anti-patterns can desynchronize framework state from the visible DOM.
- Use `http_get(...)` for static pages and APIs after the browser reveals stable endpoints. It returns the response body as a string by default, or bytes with `binary=True`; the returned body also exposes `.status_code`, `.headers`, `.url`, `.text`, `.content`, and `.json()` for convenience. For URL batches, use `http_get_many(...)`; it preserves input order and returns compact per-URL records with `ok`, `status_code`, `text` or `content_base64`, and `error` fields. If direct HTTP hits bot or login protection but the page is loaded, retry with `browser_fetch_many(...)` or `browser_fetch(...)` so requests run in the page context with browser cookies/session; otherwise use site-specific headers/cookies or the configured Browser Use fetch proxy.
- Save complete generated result files under `outputs_dir()` or relative paths in the current working directory. Files written there are collected as artifacts automatically; `copy_artifact(...)` is for files created elsewhere.
- For large structured results, write the full JSON/CSV/text to a file. If the task asks for an exact inline final format, return that content with `done(result=...)` and optionally include `result_file=path`; otherwise finish with `done(result_file=path)`.
- Before finalizing explicit structured/file requirements, call `audit_artifact(...)` with concrete checks such as `min_count`, `exact_count`, `required_fields`, `unique_by`, `path`, and `nonempty_file=True`. You can pass `audit_artifact(path="result.json", ...)`, `audit_artifact(path="result.jsonl", ...)`, or `audit_artifact(path="result.csv", ...)` directly; it loads the file and applies count/field/dedupe checks. For object-wrapped JSON like `{"items": [...]}` or `{"payload": {"results": [...]}}`, use `records_key="items"` or `records_path="payload.results"` when needed. `required_fields` and `unique_by` accept dotted nested fields such as `"source.url"`; `unique_by` also fails missing or blank dedupe keys. If `ready_for_done` is false, fix the result or clearly finalize as partial.
- For long extraction or verification loops, prefer bounded chunks with checkpoints written to files. If a chunk fails with a usable-page diagnosis, shrink the next chunk and resume from the last checkpoint.

Do not call runtime-management helpers here. There is no `browser_connect`, `browser_status`, `browser_doctor`, or `browser_recover` helper in this tool. Those are intentionally only in the `browser` tool so the model can reason about browser lifecycle explicitly.
