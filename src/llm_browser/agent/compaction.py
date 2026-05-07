from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MAX_SUMMARY_CHARS = 18000
MAX_KEPT_TEXT_CHARS = 7000
INPUT_IMAGE_CONTEXT_UNITS = 20000
DEFAULT_MAX_KEPT_IMAGES = 2
IMAGE_OMITTED_TEXT = "[screenshot image omitted by context pruning; see screenshot timeline/artifacts in summary]"
PRUNE_MINIMUM_CONTEXT_UNITS = 20_000
PRUNE_PROTECT_CONTEXT_UNITS = 40_000
PRUNE_PROTECTED_TOOLS = {"skill"}
OLD_TOOL_RESULT_TEXT = "[Old tool result content cleared]"
COMPACT_USER_MESSAGE_MAX_CHARS = 80_000
COMPACTION_SCHEMA_VERSION = 1


def _read_prompt(name: str) -> str:
    return resources.files("llm_browser.browser").joinpath("prompts", name).read_text(encoding="utf-8").rstrip("\n")


COMPACTION_PROMPT = _read_prompt("compact-prompt.md")
COMPACTION_SUMMARY_PREFIX = _read_prompt("compact-summary-prefix.md")


def message_chars(messages: List[Dict[str, Any]]) -> int:
    return sum(len(_message_text(message)) for message in messages)


def message_context_units(messages: List[Dict[str, Any]]) -> int:
    """Estimate provider context pressure.

    Text length alone misses screenshots because reconstructed tool outputs
    carry base64 image data in input_image blocks. Count each image as a large
    fixed unit and count other strings normally, while ignoring raw image_url
    bytes so the estimate is stable across image dimensions.
    """

    return sum(_context_units(message) for message in messages)


def message_image_count(messages: List[Dict[str, Any]]) -> int:
    return sum(_image_count(message) for message in messages)


def trim_message_images(
    messages: List[Dict[str, Any]],
    max_images: int = DEFAULT_MAX_KEPT_IMAGES,
) -> List[Dict[str, Any]]:
    """Keep only the newest attached images in replay context.

    Screenshots are persisted as artifacts/events; replaying every base64 image
    makes provider request bodies explode before normal text compaction kicks in.
    """

    budget = max(0, int(max_images))
    if message_image_count(messages) <= budget:
        return messages
    trimmed = deepcopy(messages)
    for message in reversed(trimmed):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for index in range(len(content) - 1, -1, -1):
            item = content[index]
            if not isinstance(item, dict) or item.get("type") != "input_image":
                continue
            if budget > 0:
                budget -= 1
            else:
                content[index] = {"type": "input_text", "text": IMAGE_OMITTED_TEXT}
    return trimmed


def prune_old_tool_outputs(
    messages: List[Dict[str, Any]],
    protect_context_units: int = PRUNE_PROTECT_CONTEXT_UNITS,
    minimum_pruned_units: int = PRUNE_MINIMUM_CONTEXT_UNITS,
    protected_tools: set[str] = PRUNE_PROTECTED_TOOLS,
) -> List[Dict[str, Any]]:
    """Clear older tool output after preserving recent turns and a tool-output budget.

    This mirrors OpenCode's first phase: keep the latest two user turns intact,
    then walk older tool outputs backwards and clear content once a protected
    budget has already been retained.
    """

    total = 0
    pruned = 0
    turns = 0
    to_prune: List[int] = []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_compaction_summary_message(message):
            break
        if message.get("role") == "user":
            turns += 1
        if turns < 2:
            continue
        if message.get("role") != "tool":
            continue
        if str(message.get("name") or "") in protected_tools:
            continue
        estimate = _context_units(message.get("content", ""))
        total += estimate
        if total <= protect_context_units:
            continue
        pruned += estimate
        to_prune.append(index)
    if pruned <= minimum_pruned_units or not to_prune:
        return messages
    trimmed = deepcopy(messages)
    for index in to_prune:
        trimmed[index]["content"] = OLD_TOOL_RESULT_TEXT
    return trimmed


def new_compaction_id() -> str:
    return uuid.uuid4().hex[:12]


def heuristic_summary(messages: List[Dict[str, Any]], session_events: Optional[List[Dict[str, Any]]] = None) -> str:
    return _summary(messages, session_events or [])


def collect_user_messages(messages: List[Dict[str, Any]]) -> List[str]:
    collected: List[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _message_text(message).strip()
        if not text:
            continue
        if is_compaction_summary_text(text) or text == COMPACTION_PROMPT:
            continue
        collected.append(text)
    return collected


def is_compaction_summary_text(text: str) -> bool:
    return text.startswith(f"{COMPACTION_SUMMARY_PREFIX}\n") or text.startswith("Conversation was compacted")


def build_compacted_history(
    user_messages: List[str],
    summary_text: str,
    max_user_chars: int = COMPACT_USER_MESSAGE_MAX_CHARS,
) -> List[Dict[str, Any]]:
    selected: List[str] = []
    remaining = max(0, int(max_user_chars))
    for message in reversed(user_messages):
        if remaining <= 0:
            break
        if len(message) <= remaining:
            selected.append(message)
            remaining -= len(message)
            continue
        selected.append(_compact_text(message, remaining))
        break
    selected.reverse()

    history = [{"role": "user", "content": message} for message in selected]
    history.append({"role": "user", "content": summary_text or "(no summary available)"})
    return history


def compaction_checkpoint_payload(
    *,
    compaction_id: str,
    phase: str,
    reason: str,
    message: str,
    replacement_history: List[Dict[str, Any]],
    before_messages: int,
    path: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema_version": COMPACTION_SCHEMA_VERSION,
        "compaction_id": compaction_id,
        "phase": phase,
        "reason": reason,
        "message": message,
        "replacement_history": _strip_images_for_persistence(replacement_history),
        "before_messages": before_messages,
        "after_messages": len(replacement_history),
        "before_context_units": 0,
        "after_context_units": message_context_units(replacement_history),
    }
    if path is not None:
        payload["path"] = str(path)
    if extra:
        payload.update(extra)
    return payload


def write_compaction_artifact(
    artifact_dir: Path,
    payload: Dict[str, Any],
) -> Path:
    compaction_dir = artifact_dir / "compactions"
    compaction_dir.mkdir(parents=True, exist_ok=True)
    path = compaction_dir / f"{len(list(compaction_dir.glob('*.json'))) + 1:03d}.json"
    artifact_payload = dict(payload)
    artifact_payload["replacement_history"] = _strip_images_for_persistence(
        artifact_payload.get("replacement_history") or []
    )
    path.write_text(json.dumps(artifact_payload, indent=2) + "\n", encoding="utf-8")
    return path


def replay_messages_from_compaction_payload(payload: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    replacement_history = payload.get("replacement_history")
    if isinstance(replacement_history, list) and all(isinstance(message, dict) for message in replacement_history):
        return deepcopy(replacement_history)

    # Legacy artifact-backed compaction event.
    path = payload.get("path")
    if not path:
        return None
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    replay = data.get("replay_messages") or data.get("replacement_history")
    if not isinstance(replay, list) or not all(isinstance(message, dict) for message in replay):
        return None
    return deepcopy(replay)


def compact_messages(
    messages: List[Dict[str, Any]],
    artifact_dir: Path,
    keep_last: int = 12,
    session_events: Optional[List[Dict[str, Any]]] = None,
    max_kept_images: int = DEFAULT_MAX_KEPT_IMAGES,
) -> Tuple[List[Dict[str, Any]], Path]:
    if len(messages) <= keep_last + 1:
        kept = _trim_kept_messages(messages, max_images=max_kept_images)
        if message_context_units(kept) >= message_context_units(messages):
            return messages, artifact_dir / "compactions" / "noop.json"
        keep_start = 0
    else:
        keep_start = _valid_suffix_start(messages, max(0, len(messages) - keep_last))
        kept = _trim_kept_messages(messages[keep_start:], max_images=max_kept_images)

    summary = _summary(messages[:keep_start], session_events=session_events or [])
    compacted = [
        {
            "role": "user",
            "content": (
                "Conversation was compacted by browser use terminal. "
                "Use this summary plus the recent messages and artifact paths to continue.\n\n"
                f"{summary}"
            ),
        }
    ]
    compacted.extend(kept)

    compaction_dir = artifact_dir / "compactions"
    compaction_dir.mkdir(parents=True, exist_ok=True)
    path = compaction_dir / f"{len(list(compaction_dir.glob('*.json'))) + 1:03d}.json"
    compacted[0]["content"] += f"\n\nFull compaction artifact: {path}"
    payload = {
        "summary": summary,
        "kept_messages": len(kept),
        "original_messages": len(messages),
        "max_kept_images": max_kept_images,
        "before_context_units": message_context_units(messages),
        "after_context_units": message_context_units(compacted),
        "replay_messages": _strip_images_for_persistence(compacted),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return compacted, path


def _valid_suffix_start(messages: List[Dict[str, Any]], desired_start: int) -> int:
    """Move the compaction boundary to a provider-valid message boundary.

    Responses-style providers reject a function_call_output unless the same
    request also contains the matching function_call, or the request is chained
    to the response that produced that call. After local compaction we replay a
    compacted transcript, so a suffix that starts with a tool message is invalid.
    Move the boundary back to the assistant turn that created the leading tool
    output and keep that whole assistant/tool block intact.
    """

    if desired_start <= 0:
        return 0
    if desired_start >= len(messages):
        return len(messages)
    start = desired_start
    while start > 0 and messages[start].get("role") == "tool":
        previous = start - 1
        while previous >= 0 and messages[previous].get("role") == "tool":
            previous -= 1
        if previous >= 0 and messages[previous].get("role") == "assistant":
            return previous
        start += 1
        if start >= len(messages):
            return len(messages)
    return start


def _summary(messages: List[Dict[str, Any]], session_events: List[Dict[str, Any]]) -> str:
    first_user = ""
    recent_tools = []
    tool_refs = []
    errors = []
    paths = []
    for message in messages:
        role = message.get("role")
        text = _message_text(message)
        if role == "user" and not first_user:
            first_user = text[:3000]
        if role == "tool":
            if text:
                recent_tools.append(_compact_text(text, 2600))
            if "output_path" in text or "artifact" in text or "screenshots" in text:
                tool_refs.append(_compact_text(text, 1600))
            if "tool error" in text or "'ok': False" in text or '"ok": false' in text:
                errors.append(_compact_text(text, 1600))
        for path in _extract_paths(text):
            if path not in paths:
                paths.append(path)
    parts = []
    if first_user:
        parts.append(f"Original user/task goal:\n{first_user}")
    if recent_tools:
        parts.append("Recent tool results before compaction:\n" + "\n\n".join(recent_tools[-10:]))
    if tool_refs:
        parts.append("Important tool/artifact references:\n" + "\n\n".join(tool_refs[-8:]))
    if paths:
        parts.append("Known artifact/file paths:\n" + "\n".join(paths[-40:]))
    if errors:
        parts.append("Recent recoverable errors:\n" + "\n\n".join(errors[-5:]))
    event_summary = _event_summary(session_events)
    if event_summary:
        parts.append(event_summary)
    if not parts:
        parts.append(f"Compacted {len(messages)} older message(s). Continue from recent context.")
    return _compact_text("\n\n".join(parts), MAX_SUMMARY_CHARS)


def _event_summary(events: List[Dict[str, Any]]) -> str:
    if not events:
        return ""
    image_lines: List[str] = []
    rehydrate_lines: List[str] = []
    browser_lines: List[str] = []
    status_lines: List[str] = []
    for event in events:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "tool.image":
            image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
            label = str(image.get("label") or "screenshot")
            path = str(image.get("path") or "")
            url = str(image.get("url") or "")
            title = str(image.get("title") or "")
            bits = [label]
            if title:
                bits.append(f"title={title[:120]}")
            if url:
                bits.append(f"url={url[:180]}")
            if path:
                bits.append(f"path={path}")
                rehydrate_lines.append(f"attach_image({path!r}, label={label!r})")
            image_lines.append(" | ".join(bits))
        elif event_type in {"tool.failed", "session.failed", "session.cancelled"}:
            text = str(payload.get("error") or payload.get("reason") or "")
            status_lines.append(f"{event_type}: {text[:300]}")
        elif event_type == "tool.finished":
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
            data = output.get("data") if isinstance(output.get("data"), dict) else {}
            trace_path = data.get("path") or data.get("output_path")
            if trace_path:
                browser_lines.append(f"{payload.get('name', 'tool')} artifact: {trace_path}")
    parts = []
    if image_lines:
        parts.append("Recent screenshot timeline:\n" + "\n".join(image_lines[-16:]))
    if rehydrate_lines:
        parts.append("Screenshot rehydration helpers:\n" + "\n".join(rehydrate_lines[-8:]))
    if browser_lines:
        parts.append("Recent trace/output artifacts:\n" + "\n".join(browser_lines[-12:]))
    if status_lines:
        parts.append("Recent status/error events:\n" + "\n".join(status_lines[-8:]))
    return "\n\n".join(parts)


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "input_text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "input_image":
                    parts.append("[input_image]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _trim_kept_messages(messages: List[Dict[str, Any]], max_images: int) -> List[Dict[str, Any]]:
    budget = max(0, int(max_images))
    trimmed_reversed: List[Dict[str, Any]] = []
    for message in reversed(messages):
        trimmed, budget = _trim_message_with_image_budget(message, budget)
        trimmed_reversed.append(trimmed)
    return list(reversed(trimmed_reversed))


def _trim_message_with_image_budget(message: Dict[str, Any], image_budget: int) -> Tuple[Dict[str, Any], int]:
    trimmed = deepcopy(message)
    content = trimmed.get("content")
    if isinstance(content, str):
        trimmed["content"] = _compact_text(content, MAX_KEPT_TEXT_CHARS)
        return trimmed, image_budget
    if not isinstance(content, list):
        return trimmed, image_budget

    next_reversed: List[Any] = []
    for item in reversed(content):
        if isinstance(item, dict) and item.get("type") == "input_image":
            if image_budget > 0:
                next_reversed.append(item)
                image_budget -= 1
            else:
                next_reversed.append(
                    {
                        "type": "input_text",
                        "text": "[screenshot image omitted by compaction; see screenshot timeline/artifacts in summary]",
                    }
                )
            continue
        if isinstance(item, dict) and item.get("type") == "input_text":
            next_item = dict(item)
            next_item["text"] = _compact_text(str(next_item.get("text") or ""), MAX_KEPT_TEXT_CHARS)
            next_reversed.append(next_item)
            continue
        next_reversed.append(item)
    trimmed["content"] = list(reversed(next_reversed))
    return trimmed, image_budget


def _context_units(value: Any) -> int:
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return INPUT_IMAGE_CONTEXT_UNITS
        total = 0
        for item in value.values():
            total += _context_units(item)
        return total
    if isinstance(value, list):
        return sum(_context_units(item) for item in value)
    if isinstance(value, str):
        return len(value)
    if value is None:
        return 0
    return len(str(value))


def _image_count(value: Any) -> int:
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return 1
        return sum(_image_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_image_count(item) for item in value)
    return 0


def _is_compaction_summary_message(message: Dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.startswith("Conversation was compacted")


def _strip_images_for_persistence(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_images_for_persistence(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return {
                "type": "input_text",
                "text": "[screenshot image omitted from persisted compaction replay; see screenshot timeline/artifacts in summary]",
            }
        return {key: _strip_images_for_persistence(item) for key, item in value.items()}
    return value


def _compact_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return f"{text[:head]}\n\n[... omitted {omitted} chars during compaction ...]\n\n{text[-tail:]}"


def _extract_paths(text: str) -> List[str]:
    pattern = re.compile(r"(/[^\s\]\)\"']+\.(?:txt|json|jsonl|png|jpg|jpeg|webp|pdf|csv|tsv|xlsx|html|md|docx))")
    return [match.group(1) for match in pattern.finditer(text)]
