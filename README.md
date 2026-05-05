# llm-browser

A browser-specific LLM harness built around raw Chrome DevTools Protocol access, durable sessions, editable helpers, and screenshot timelines.

Current status: implementation has started. See:

- `docs/browser-agent-harness-plan.md`
- `docs/browser-agent-harness-learnings.md`
- `docs/implementation-roadmap.md`

## Local Commands

```bash
python3 -m llm_browser.cli doctor
python3 -m llm_browser.cli run "Open example.com"
python3 -m llm_browser.cli sessions list
```

By default runtime state is stored under `.llm-browser/`.
