# Connection & Tab Visibility

## Browser setup is Rust-owned

Do not connect from inside `browser_execute`. The browser is already connected
before JavaScript runs. Use `browser_status` for diagnostics, and use
`browser_configure` only when the user explicitly asks to change local,
managed, remote-CDP, or cloud browser setup.

`session.connect()` exists only as a no-op compatibility shim. Do not rely on it
for setup, profile selection, CDP URL changes, or recovery.

## The omnibox popup problem

When Chrome opens fresh, the only CDP `type: "page"` targets may be
`chrome://inspect` and `chrome://omnibox-popup.top-chrome/` (a 1px invisible
viewport). If you attach to the omnibox popup, every subsequent action happens
on a tab the user cannot see.

`listPageTargets()` already filters `chrome://` and `devtools://` URLs. If you
call `Target.getTargets` directly, filter these manually:

```js
const { targetInfos } = await session.Target.getTargets({})
const realTabs = targetInfos.filter(t =>
  t.type === 'page' &&
  !t.url.startsWith('chrome://') &&
  !t.url.startsWith('devtools://')
)
```

If no real pages exist yet, create one instead of attaching to nothing:

```js
const tabs = await listPageTargets()
let targetId = tabs[0]?.targetId
if (!targetId) {
  ({ targetId } = await session.Target.createTarget({ url: 'about:blank' }))
}
await session.use(targetId)
```

## Startup sequence inside browser_execute

1. `const tabs = await listPageTargets()` - see what real pages exist.
2. Create `about:blank` if no real page exists.
3. `await session.use(targetId)` - route Page/DOM/Runtime/Network calls to that target.
4. `await session.Target.activateTarget({ targetId })` - bring the tab visually to front.
5. Enable the domains you need: `await session.Page.enable()`, `await session.Network.enable({})`, etc.

## CDP target order is not visible tab-strip order

When the user says "the first tab I can see", do not trust the order of
`Target.getTargets`. Use:

- A screenshot (`session.Page.captureScreenshot()`) to identify visually.
- Page title / URL heuristics.
- Or platform UI automation (macOS: AppleScript; Linux: `xdotool`/`wmctrl`).

`Target.activateTarget` only switches to a targetId you already know; it cannot
resolve "leftmost tab".

## Bringing Chrome to front

```bash
# macOS - prefer AppleScript over `open -a` (reuses current profile, avoids the profile picker)
osascript -e 'tell application "Google Chrome" to activate'

# Linux (X11) - use wmctrl or xdotool
wmctrl -a 'Google Chrome'
xdotool search --name 'Google Chrome' windowactivate

# Windows (PowerShell)
powershell -NoProfile -Command "(New-Object -ComObject WScript.Shell).AppActivate('Google Chrome')"
```
