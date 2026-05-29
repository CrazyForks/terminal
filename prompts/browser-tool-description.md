Browser runtime control tool.

This hidden compatibility tool is the browser control plane. Prefer the typed tools:

- `browser_status` for diagnostics.
- `browser_configure` for explicit user-requested setup changes.
- `browser_recover` for gated recovery after real lifecycle failure.
- `browser_execute`, `browser_observe`, and `browser_cancel` for page interaction jobs.

The input is a single CLI-like command string. The leading word `browser` is optional.

Useful commands:

```text
browser status --json
browser doctor --json
browser domain skills --domain <domain> [--include-content] --json
browser preference --json
browser preference use local|cloud|managed-headless|managed-headed
browser connect
browser connect local
browser connect managed [--headless|--headed] [--profile temp|<path>]
browser connect remote-cdp --url <http-url>
browser connect remote-cdp --ws <ws-url>
browser remote start [--profile-id <uuid>|--profile-name <name>] [--timeout <minutes>] [--proxy-country <iso2|none>]
browser remote stop
browser remote status --json
browser remote profiles --json
browser local list --json
browser local setup [--profile <profile-id>]
browser local profiles --json
browser local profiles inspect <profile-id-or-name> --domains-only
browser recover reconnect-websocket
browser recover reattach-same-target
browser recover restart-runtime
browser recover restart-owned-browser
browser recover stop-owned-remote
browser runtime logs
browser runtime ownership --json
browser runtime cleanup-stale
```

Rules:

- Rust owns the browser lifecycle and CDP connection.
- External user Chrome is never killed or relaunched.
- Nothing reloads, relaunches, closes, or switches tabs silently.
- Remote start means start and connect; do not copy its returned CDP URL into another command.
- Runtime setup and recovery are explicit; page interaction belongs to `browser_execute`.
- Use `browser runtime ownership --json` before stopping anything.
