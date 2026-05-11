#!/usr/bin/env python3
"""Real-terminal smoke tests for the Rust TUI.

This intentionally tests the app through tmux instead of Ratatui's TestBackend.
The goal is to catch bugs that only appear with a live terminal viewport:
duplicated panels in scrollback, broken bracketed paste, and stale redraws.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = Path("/tmp/but-design-loop")


def run(cmd: list[str], *, check: bool = True, text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        input=text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def tmux(*args: str, check: bool = True) -> str:
    return run(["tmux", *args], check=check).stdout


def tmux_send(session: str, *keys: str) -> None:
    run(["tmux", "send-keys", "-t", session, *keys])


def tmux_send_literal(session: str, value: str) -> None:
    run(["tmux", "send-keys", "-t", session, "-l", value])


def tmux_send_shift_enter(session: str) -> None:
    # Crossterm decodes the kitty/CSI-u enhanced keyboard encoding that the
    # TUI enables at startup. tmux's symbolic "S-Enter" is not reliable across
    # terminal builds and can arrive as plain Enter.
    tmux_send_literal(session, "\x1b[13;2u")


def capture(session: str, name: str) -> str:
    text = tmux("capture-pane", "-t", session, "-p", "-S", "-200")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / f"tui-terminal-smoke-{name}.txt").write_text(text)
    return text


def capture_visible(session: str, name: str) -> str:
    text = tmux("capture-pane", "-t", session, "-p")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / f"tui-terminal-smoke-{name}.txt").write_text(text)
    return text


def wait_for(session: str, needle: str, name: str, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = capture(session, name)
        if needle in last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {needle!r}\n\n{last}")


def capture_after_idle(session: str, name: str, delay: float = 0.5, *, visible_only: bool = False) -> str:
    time.sleep(delay)
    if visible_only:
        return capture_visible(session, name)
    return capture(session, name)


def assert_contains(text: str, needle: str, context: str) -> None:
    if needle not in text:
        raise AssertionError(f"{context}: expected {needle!r}\n\n{text}")


def assert_not_contains(text: str, needle: str, context: str) -> None:
    if needle in text:
        raise AssertionError(f"{context}: unexpected {needle!r}\n\n{text}")


def assert_count(text: str, needle: str, expected: int, context: str) -> None:
    count = text.count(needle)
    if count != expected:
        raise AssertionError(f"{context}: expected {expected} x {needle!r}, saw {count}\n\n{text}")


def assert_regex_count(text: str, pattern: str, expected: int, context: str) -> None:
    count = len(re.findall(pattern, text, flags=re.MULTILINE))
    if count != expected:
        raise AssertionError(f"{context}: expected {expected} x /{pattern}/, saw {count}\n\n{text}")


def assert_no_ansi(text: str, context: str) -> None:
    if re.search(r"\x1b\[[0-?]*[ -/]*[@-~]", text):
        raise AssertionError(f"{context}: output contained ANSI escapes\n\n{text!r}")


def build_binary() -> Path:
    run(["cargo", "build", "-q", "-p", "browser-use-tui", "--bin", "but"])
    binary = ROOT / "target" / "debug" / "but"
    if not binary.exists():
        raise AssertionError(f"missing built binary: {binary}")
    return binary


def start_session(
    session: str,
    binary: Path,
    state_dir: Path,
    *,
    seed_demo: str = "running",
    select_latest: bool = True,
) -> None:
    tmux("kill-session", "-t", session, check=False)
    tmux("new-session", "-d", "-s", session, "-x", "120", "-y", "28")
    tmux("resize-window", "-t", session, "-x", "120", "-y", "28")
    select_arg = "--select-latest " if select_latest else ""
    command = (
        f"cd {ROOT} && {binary} "
        f"--state-dir {state_dir} --seed-demo {seed_demo} {select_arg}--agent none --height 28"
    )
    tmux_send(session, command, "C-m")
    wait_for(session, "browser-use", f"initial-{seed_demo}")


def smoke_interactive_terminal(binary: Path) -> None:
    session = f"but-smoke-{os.getpid()}"
    state_dir = Path(tempfile.mkdtemp(prefix="but-tui-smoke-"))
    try:
        start_session(session, binary, state_dir)
        wait_for(session, "+- working", "initial-running")

        tmux_send(session, "Tab", "Down", "Down", "Down")
        history = wait_for(session, "browser-use / previous work", "history")
        assert_count(history, "browser-use / previous work", 1, "history should be live, not appended repeatedly")
        assert_not_contains(history, "^[[B", "arrow keys should be consumed by the TUI")

        tmux_send(session, "Escape")
        wait_for(session, "+- working", "main-after-history")

        tmux_send_literal(session, "alpha")
        tmux_send_shift_enter(session)
        tmux_send_literal(session, "beta")
        multiline = wait_for(session, "beta", "shift-enter-newline")
        assert_contains(multiline, "> alpha", "multiline input first line")
        assert_contains(multiline, "  beta", "multiline input second line")
        assert_not_contains(multiline, "Follow-up\n    alpha", "shift-enter must not submit")
        assert_not_contains(multiline, "alpha|", "composer should use the terminal cursor, not a fake pipe")
        assert_not_contains(multiline, "beta|", "composer should use the terminal cursor, not a fake pipe")
        assert_regex_count(multiline, r"^  browser-use\b", 1, "multiline edit should not append duplicate app screens")

        tmux_send(session, "C-c")
        wait_for(session, "+- working", "main-after-clear")

        bracketed = "\x1b[200~paste one\npaste two\x1b[201~"
        tmux_send_literal(session, bracketed)
        pasted = wait_for(session, "paste two", "bracketed-paste")
        assert_contains(pasted, "paste one", "bracketed paste first line")
        assert_contains(pasted, "paste two", "bracketed paste second line")
        assert_not_contains(pasted, "^[[200~", "bracketed paste markers should not leak")
        assert_not_contains(pasted, "paste two|", "paste should use the terminal cursor, not a fake pipe")
        assert_regex_count(pasted, r"^  browser-use\b", 1, "paste should not append duplicate app screens")

        tmux_send(session, "C-c", "/")
        wait_for(session, "Actions", "actions-open")
        tmux_send(session, "b", "r", "o")
        actions = wait_for(session, "filter  bro", "actions-filter")
        assert_contains(actions, "Open browser", "actions filter should show matching command")
        assert_not_contains(actions, "filter  b\n", "actions filter should redraw in place")
        assert_not_contains(actions, "filter  br\n", "actions filter should redraw in place")

        tmux_send(session, "Escape")
        wait_for(session, "+- working", "main-after-actions")
        tmux_send(session, "F2")
        browser = wait_for(session, "Current browser", "browser-panel")
        assert_count(browser, "browser-use / browser", 1, "browser panel should be live, not appended repeatedly")

        tmux("resize-window", "-t", session, "-x", "100", "-y", "22")
        resized_small = capture_after_idle(session, "resize-100x22", visible_only=True)
        assert_contains(resized_small, "Current browser", "resize should keep the live app visible")
        assert_regex_count(resized_small, r"^  browser-use / browser\b", 1, "resize shrink should redraw in place")
        assert_not_contains(resized_small, "^[[", "resize shrink should not leak escape sequences")

        tmux("resize-window", "-t", session, "-x", "120", "-y", "28")
        resized_large = capture_after_idle(session, "resize-120x28", visible_only=True)
        assert_contains(resized_large, "Current browser", "resize grow should keep the live app visible")
        assert_regex_count(resized_large, r"^  browser-use / browser\b", 1, "resize grow should redraw in place")
    finally:
        tmux("kill-session", "-t", session, check=False)
        shutil.rmtree(state_dir, ignore_errors=True)


def smoke_history_selection_stays_live(binary: Path) -> None:
    session = f"but-smoke-history-{os.getpid()}"
    state_dir = Path(tempfile.mkdtemp(prefix="but-tui-smoke-history-"))
    try:
        start_session(
            session,
            binary,
            state_dir,
            seed_demo="cancelled",
            select_latest=False,
        )
        wait_for(session, "What should the browser do?", "history-start-ready")
        tmux_send(session, "Tab")
        wait_for(session, "browser-use / previous work", "history-open-cancelled")
        tmux_send(session, "Enter")
        selected = wait_for(session, "+- stopped", "history-select-cancelled")
        assert_regex_count(selected, r"^  browser-use\b", 1, "history selection should stay in one live viewport")
        assert_regex_count(selected, r"^Task$", 0, "history selection must not print a native transcript below the TUI")
        assert_not_contains(selected, "\n> Continue with a follow-up\n  Start a new task\n  Previous work", "plain transcript should not be appended")
    finally:
        tmux("kill-session", "-t", session, check=False)
        shutil.rmtree(state_dir, ignore_errors=True)


def smoke_tall_terminal_uses_available_height(binary: Path) -> None:
    session = f"but-smoke-height-{os.getpid()}"
    state_dir = Path(tempfile.mkdtemp(prefix="but-tui-smoke-height-"))
    try:
        tmux("kill-session", "-t", session, check=False)
        tmux("new-session", "-d", "-s", session, "-x", "120", "-y", "40")
        command = (
            f"cd {ROOT} && {binary} "
            f"--state-dir {state_dir} --seed-demo running --select-latest --agent none"
        )
        tmux_send(session, command, "C-m")
        wait_for(session, "+- working", "height-120x40-history")
        visible = capture_visible(session, "height-120x40")
        assert_regex_count(visible, r"^  browser-use\b", 1, "tall terminal should have one live app")
        footer_rows = [
            idx for idx, line in enumerate(visible.splitlines()) if "enter steer" in line
        ]
        if not footer_rows or max(footer_rows) < 34:
            raise AssertionError(
                "tall terminal should use the available height instead of stopping at 28 rows\n\n"
                + visible
            )
    finally:
        tmux("kill-session", "-t", session, check=False)
        shutil.rmtree(state_dir, ignore_errors=True)


def smoke_completed_plain_output(binary: Path) -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="but-tui-smoke-done-"))
    try:
        result = run(
            [
                str(binary),
                "--state-dir",
                str(state_dir),
                "--seed-demo",
                "long",
                "--select-latest",
                "--agent",
                "none",
            ]
        ).stdout
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        (ARTIFACT_DIR / "tui-terminal-smoke-completed-output.txt").write_text(result)
        assert_contains(result, "scroll check line 60", "completed result should print full plain transcript")
        assert_contains(result, "+- source", "completed result should include source section")
        assert_no_ansi(result, "completed result should be selectable plain text")
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true", help="reuse target/debug/but")
    args = parser.parse_args()

    if shutil.which("tmux") is None:
        print("tmux is required for real terminal smoke tests", file=sys.stderr)
        return 2

    binary = ROOT / "target" / "debug" / "but" if args.skip_build else build_binary()
    smoke_interactive_terminal(binary)
    smoke_history_selection_stays_live(binary)
    smoke_tall_terminal_uses_available_height(binary)
    smoke_completed_plain_output(binary)
    print("tui terminal smoke passed")
    print(f"captures: {ARTIFACT_DIR}/tui-terminal-smoke-*.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
