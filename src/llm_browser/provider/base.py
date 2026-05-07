from __future__ import annotations

from typing import Any, Dict, Iterable, List, Protocol

from llm_browser.provider.types import ModelEvent, ProviderCompactionResult


class Provider(Protocol):
    def start_turn(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Iterable[ModelEvent]:
        ...

    def reset_session(self) -> None:
        ...

    def supports_remote_compaction(self) -> bool:
        ...

    def compact_conversation_history(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> ProviderCompactionResult:
        ...
