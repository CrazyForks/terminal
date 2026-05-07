from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List


DEFAULT_MAX_VISUAL_CONTEXT_IMAGES = 2
VISUAL_CONTEXT_OMITTED_TEXT = (
    "[older screenshot image omitted before provider request; see screenshot artifacts or ask to reattach it]"
)


def tool_output_text(content: Any) -> str:
    """Return the string payload for a function_call_output item.

    The Responses API function output remains text, while screenshot images are
    sent as a following user message so the model sees them in the same turn.
    """

    if not isinstance(content, list):
        return str(content or "")

    text_parts: List[str] = []
    image_count = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "input_text" and item.get("text"):
            text_parts.append(str(item["text"]))
        elif item.get("type") == "input_image":
            image_count += 1

    if image_count:
        text_parts.append(f"[{image_count} screenshot image(s) attached in the following visual context message]")
    return "\n".join(text_parts)


def visual_context_messages(content: Any, call_id: str, tool_name: str) -> Iterable[Dict[str, Any]]:
    if not isinstance(content, list):
        return []

    images = [
        item
        for item in content
        if isinstance(item, dict) and item.get("type") == "input_image" and item.get("image_url")
    ]
    if not images:
        return []

    message_content: List[Dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                f"Visual context from tool call {call_id} ({tool_name}). "
                "Use these screenshots to verify the browser state before continuing."
            ),
        }
    ]
    message_content.extend(images)
    return [{"role": "user", "content": message_content}]


def trim_visual_context_images(
    input_items: List[Dict[str, Any]],
    max_images: int = DEFAULT_MAX_VISUAL_CONTEXT_IMAGES,
) -> List[Dict[str, Any]]:
    budget = max(0, int(max_images))
    if _input_image_count(input_items) <= budget:
        return input_items
    trimmed = deepcopy(input_items)
    for item in reversed(trimmed):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for index in range(len(content) - 1, -1, -1):
            content_item = content[index]
            if not isinstance(content_item, dict) or content_item.get("type") != "input_image":
                continue
            if budget > 0:
                budget -= 1
            else:
                content[index] = {"type": "input_text", "text": VISUAL_CONTEXT_OMITTED_TEXT}
    return trimmed


def _input_image_count(value: Any) -> int:
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return 1
        return sum(_input_image_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_input_image_count(item) for item in value)
    return 0
