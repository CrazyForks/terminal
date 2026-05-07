from __future__ import annotations

from importlib import resources


def _read_prompt(name: str) -> str:
    return resources.files("llm_browser.browser").joinpath("prompts", name).read_text(encoding="utf-8").rstrip("\n")


CODEX_AGENT_INSTRUCTIONS = _read_prompt("codex-agent-instructions.md")
BROWSER_AGENT_INSTRUCTIONS = _read_prompt("browser-agent-instructions.md")
BROWSER_HELP_PLAYBOOK = _read_prompt("browser-help-playbook.md")


CODEX_TASK_PATTERNS = (
    "what is in this repo",
    "codebase",
    "repo",
    "repository",
    "implementation",
    "implement",
    "refactor",
    "unit test",
    "tests",
    "commit",
    "git",
    "diff",
    "pull request",
    "review",
    "source code",
)


def select_agent_instructions(task: str, mode: str = "auto") -> str:
    normalized = (mode or "auto").strip().lower()
    if normalized == "codex":
        return CODEX_AGENT_INSTRUCTIONS
    if normalized == "browser":
        return BROWSER_AGENT_INSTRUCTIONS
    text = (task or "").strip().lower()
    if any(pattern in text for pattern in CODEX_TASK_PATTERNS):
        return CODEX_AGENT_INSTRUCTIONS
    return BROWSER_AGENT_INSTRUCTIONS
