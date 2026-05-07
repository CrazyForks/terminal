from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from llm_browser.events.event import now_ms
from llm_browser.provider.base import Provider
from llm_browser.provider.types import ModelEvent
from llm_browser.tool.result import ToolImage, ToolResult
from llm_browser.tool.spec import ToolSpec


def run_image_smoke(provider: Provider, artifact_dir: Path) -> Dict[str, Any]:
    """Send a synthetic two-frame visual timeline through a provider.

    This is intentionally a provider-level smoke, not a browser task. It proves
    that tool-result screenshots are transformed into model-visible image input
    on the next continuation.
    """

    artifact_dir.mkdir(parents=True, exist_ok=True)
    images = _write_probe_images(artifact_dir)
    tool_content = ToolResult(text="Synthetic browser screenshots captured in order.", images=images).to_provider_content()
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "You are validating browser-use-terminal screenshot transport. "
                "A tool call will return two image frames. Use the images, not assumptions."
            ),
        },
        {
            "role": "assistant",
            "tool_calls": [{"id": "call_image_smoke", "name": "image_probe", "arguments": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_image_smoke",
            "name": "image_probe",
            "content": tool_content,
        },
        {
            "role": "user",
            "content": (
                "Answer exactly with the dominant colors of the two attached frames in order, "
                "using this format: red then blue"
            ),
        },
    ]
    tools = [
        ToolSpec(
            name="image_probe",
            description="Synthetic tool used only for provider image smoke tests.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ).to_provider_tool()
    ]

    text_parts: List[str] = []
    tool_calls = []
    for event in provider.start_turn(messages, tools):
        if event.type == "text_delta":
            text_parts.append(event.text)
        elif event.type == "tool_call" and event.tool_call is not None:
            tool_calls.append(event.tool_call)
        elif event.type == "usage":
            continue
        elif event.type == "done":
            continue
        else:
            raise RuntimeError(f"unknown provider event type: {event.type}")

    text = "".join(text_parts).strip()
    lowered = text.lower()
    red_index = lowered.find("red")
    blue_index = lowered.find("blue")
    ok = red_index >= 0 and blue_index >= 0 and red_index < blue_index and not tool_calls
    result = {
        "ok": ok,
        "text": text,
        "expected": "red then blue",
        "tool_calls": [call.__dict__ for call in tool_calls],
        "images": [image.to_dict() for image in images],
    }
    (artifact_dir / "image-smoke-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _write_probe_images(artifact_dir: Path) -> List[ToolImage]:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        raise RuntimeError("provider image smoke requires Pillow") from exc

    specs = [("frame_1", "red", (220, 30, 30)), ("frame_2", "blue", (35, 85, 220))]
    images: List[ToolImage] = []
    for index, (label, word, color) in enumerate(specs, start=1):
        path = artifact_dir / f"{index:03d}_{label}.png"
        image = Image.new("RGB", (220, 120), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 82, 220, 120], fill=(255, 255, 255))
        draw.text((16, 94), word, fill=(0, 0, 0))
        image.save(path)
        images.append(
            ToolImage(
                label=label,
                path=str(path),
                order=index,
                ts_ms=now_ms(),
                viewport={"width": 220, "height": 120},
            )
        )
    return images
