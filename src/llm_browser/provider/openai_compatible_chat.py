from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from llm_browser.browser.instructions import BROWSER_AGENT_INSTRUCTIONS
from llm_browser.provider.tool_content import tool_output_text
from llm_browser.provider.types import ModelEvent, ToolCall
from llm_browser.session.usage import ModelTokenUsage


class OpenAICompatibleChatProvider:
    """OpenAI-compatible /chat/completions provider for Z.ai, Qwen, and similar APIs."""

    def __init__(
        self,
        *,
        api_key: Optional[str],
        model: str,
        provider_label: str,
        base_url: str,
        supports_images: bool = False,
        extra_body: Optional[Dict[str, Any]] = None,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        instructions: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.provider_label = provider_label
        self.base_url = base_url.rstrip("/")
        self.supports_images = supports_images
        self.extra_body = extra_body or {}
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.instructions = instructions or BROWSER_AGENT_INSTRUCTIONS

    def set_instructions(self, instructions: str) -> None:
        self.instructions = instructions

    def reset_session(self) -> None:
        return None

    def supports_remote_compaction(self) -> bool:
        return False

    def start_turn(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Iterable[ModelEvent]:
        if not self.api_key:
            raise RuntimeError(f"{self.provider_label} API key missing. Configure auth with `but auth api-key set {self.provider_label}`.")

        payload = self._build_payload(messages, tools)
        response = self._post_payload(payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"{self.provider_label} chat request failed: HTTP {response.status_code}: {response.text[:1000]}"
            )
        data = response.json()
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            reasoning = message.get("reasoning_content") or message.get("reasoning_text")
            if reasoning and not message.get("content"):
                # Keep reasoning out of the visible transcript unless it is the only text returned.
                yield ModelEvent.text(str(reasoning))
            content = message.get("content")
            if content:
                yield ModelEvent.text(str(content))
            for call in message.get("tool_calls") or []:
                yield self._event_from_tool_call(call)
        usage = ModelTokenUsage.from_openai_usage(data.get("usage"))
        if usage is not None:
            yield ModelEvent.usage(usage, model=self.model, provider=self.provider_label)

    def _post_payload(self, payload: Dict[str, Any]) -> requests.Response:
        last_exc: Optional[BaseException] = None
        transient_statuses = {408, 409, 429, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout_s,
                )
                if response.status_code not in transient_statuses or attempt >= self.max_retries:
                    return response
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
            time.sleep(min(2.0, 0.25 * (2**attempt)))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"{self.provider_label} chat request failed before receiving a response")

    def _build_payload(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.instructions}],
        }
        payload["messages"].extend(self._convert_messages(messages))
        if tools:
            payload["tools"] = [self._convert_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        payload.update(self.extra_body)
        return payload

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                converted.append({"role": "user", "content": self._convert_user_content(message.get("content", ""))})
            elif role == "assistant":
                tool_calls = []
                for call in message.get("tool_calls") or []:
                    tool_calls.append(
                        {
                            "id": str(call["id"]),
                            "type": "function",
                            "function": {
                                "name": str(call["name"]),
                                "arguments": json.dumps(call.get("arguments") or {}),
                            },
                        }
                    )
                converted.append(
                    {
                        "role": "assistant",
                        "content": str(message.get("content") or "") or None,
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    }
                )
            elif role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(message["tool_call_id"]),
                        "content": tool_output_text(message.get("content", "")),
                    }
                )
            elif role == "provider_item":
                converted.append(
                    {
                        "role": "user",
                        "content": "[provider-specific compacted context item retained from prior history]",
                    }
                )
        return converted

    def _convert_user_content(self, content: Any) -> Any:
        if not self.supports_images:
            return _content_text(content)
        if not isinstance(content, list):
            return str(content)
        parts: List[Dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "input_text":
                parts.append({"type": "text", "text": str(item.get("text") or "")})
            elif item.get("type") == "input_image":
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": str(item.get("image_url") or ""),
                            "detail": str(item.get("detail") or "auto"),
                        },
                    }
                )
        return parts or _content_text(content)

    def _convert_tool(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        }

    def _event_from_tool_call(self, call: Dict[str, Any]) -> ModelEvent:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = {"_raw": str(raw_arguments)}
        return ModelEvent.call(
            ToolCall(
                id=str(call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {"_raw": arguments},
            )
        )


def _content_text(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
            elif item.get("type") == "input_text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "input_image":
                detail = str(item.get("detail") or "auto")
                parts.append(f"[Image attached to tool output; detail={detail}]")
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)
