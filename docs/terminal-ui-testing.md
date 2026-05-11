# Terminal UI Testing

Terminal UI changes are not tested by compilation alone. A Ratatui screen is a terminal protocol plus keyboard behavior, so a useful test must cross the terminal boundary.

The working heuristic is:

1. Start the app in a real terminal, PTY, or tmux session at a known size.
2. Wait for a visible landmark.
3. Send real key sequences for the workflow being changed.
4. Capture the visible pane after meaningful actions.
5. Assert both presence and absence:
   - expected title, state, selection, prompt, or result text appears
   - duplicate app chrome does not appear in scrollback
   - raw escape sequences such as `^[[A` and `^[[B` do not leak
   - bracketed paste markers do not leak
   - transient redraw states are not left behind
   - completed plain output has no ANSI escapes when it should be selectable text
6. Resize and re-capture when layout behavior changed.
7. Save captures under `/tmp/but-design-loop/` and inspect them before finalizing.

For this repo, the required full loop is:

```bash
scripts/verify-terminal-ui.sh
```

For focused iteration on live terminal behavior:

```bash
scripts/tui-terminal-smoke.py
```

Deterministic Ratatui dumps and TestBackend tests are still useful, but they do not replace a live terminal check for input handling, focus, overlays, paste, resize, scrollback, alternate-screen behavior, redraws, or completed terminal output.
