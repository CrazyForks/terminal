from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from llm_browser.session.usage import ModelTokenUsage


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ModelEvent:
    type: str
    text: str = ""
    tool_call: Optional[ToolCall] = None
    token_usage: Optional["ModelTokenUsage"] = None
    model: Optional[str] = None
    provider: Optional[str] = None

    @classmethod
    def text(cls, text: str) -> "ModelEvent":
        return cls(type="text_delta", text=text)

    @classmethod
    def call(cls, tool_call: ToolCall) -> "ModelEvent":
        return cls(type="tool_call", tool_call=tool_call)

    @classmethod
    def usage(cls, usage: "ModelTokenUsage", *, model: Optional[str] = None, provider: Optional[str] = None) -> "ModelEvent":
        return cls(type="usage", token_usage=usage, model=model, provider=provider)

    @classmethod
    def done(cls) -> "ModelEvent":
        return cls(type="done")
