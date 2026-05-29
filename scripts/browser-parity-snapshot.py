#!/usr/bin/env python3
"""Capture a browser-harness parity snapshot for this repo.

This is intentionally lightweight: it compares agent-visible prompts, helper
names, and domain-skill corpus availability without launching a browser. Use it
as the first step before deeper real-browser conformance runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HOME = Path.home()
REFERENCE = Path(os.environ.get("BROWSER_HARNESS_REPO", HOME / "repos/browser-harness-js"))
OUT = Path(os.environ.get("BROWSER_PARITY_SNAPSHOT", "/tmp/but-design-loop/browser-parity-snapshot.json"))


def file_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def skill_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".py"}
    )


def main() -> None:
    local_prompt = "\n\n".join(
        [
            file_text(REPO / "prompts/browser-agent-system.md"),
            file_text(REPO / "prompts/browser-tool-description.md"),
        ]
    )
    reference_prompt = "\n\n".join(
        [
            file_text(REFERENCE / "SKILL.md"),
            file_text(REFERENCE / "interaction-skills/screenshots.md"),
            file_text(REFERENCE / "interaction-skills/tabs.md"),
            file_text(REFERENCE / "interaction-skills/profile-sync.md"),
            file_text(REFERENCE / "interaction-skills/network-requests.md"),
        ]
    )

    local_domain_roots = [
        REPO / "domain-skills",
        HOME / ".browser-use-terminal/agent-workspace/domain-skills",
    ]
    reference_domain_roots = [
        REFERENCE / "agent-workspace/domain-skills",
        REFERENCE / "domain-skills",
    ]

    snapshot = {
        "local_repo": str(REPO),
        "reference_repo": str(REFERENCE),
        "prompt_contract": {
            "local_has_screenshot_first": "screenshots as labeled temporal checkpoints" in local_prompt,
            "local_has_coordinate_click_bias": "coordinate clicks" in local_prompt,
            "local_has_first_navigation_new_tab": "First navigation should usually be `new_tab(url)`" in local_prompt,
            "local_has_domain_skills": "domain skills" in local_prompt.lower(),
            "reference_mentions_domain_skills": "Domain skills" in reference_prompt,
            "reference_mentions_fetch_proxy": "fetch-use proxy" in reference_prompt or "BROWSER_USE_API_KEY" in reference_prompt,
        },
        "domain_skill_corpus": {
            "local_roots": {str(root): skill_files(root)[:200] for root in local_domain_roots},
            "reference_roots": {str(root): skill_files(root)[:200] for root in reference_domain_roots},
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
