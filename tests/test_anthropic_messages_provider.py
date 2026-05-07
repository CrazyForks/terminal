from __future__ import annotations

import unittest
from unittest.mock import patch

from llm_browser.provider.anthropic_messages import AnthropicMessagesProvider


class FakeResponse:
    def __init__(self, status_code: int, data: dict, text: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self) -> dict:
        return self._data


class AnthropicMessagesProviderTest(unittest.TestCase):
    def test_oauth_headers_tool_mapping_and_usage(self) -> None:
        provider = AnthropicMessagesProvider(
            api_key="sk-ant-oat-test",
            credential_type="oauth",
            model="claude-sonnet-4-6",
            instructions="custom instructions",
        )
        response = FakeResponse(
            200,
            {
                "content": [
                    {"type": "text", "text": "need shell"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "pwd"}},
                ],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                },
            },
        )

        with patch("llm_browser.provider.anthropic_messages.requests.post", return_value=response) as post:
            events = list(
                provider.start_turn(
                    [{"role": "user", "content": "run pwd"}],
                    [{"type": "function", "name": "shell", "description": "", "parameters": {"type": "object"}}],
                )
            )

        headers = post.call_args.kwargs["headers"]
        payload = post.call_args.kwargs["json"]
        self.assertIn("Authorization", headers)
        self.assertIn("claude-code-20250219", headers["anthropic-beta"])
        self.assertEqual(payload["tools"][0]["name"], "Bash")
        self.assertIn("Claude Code", payload["system"][0]["text"])
        self.assertEqual(events[0].text, "need shell")
        self.assertEqual(events[1].tool_call.name, "shell")
        self.assertEqual(events[1].tool_call.arguments, {"cmd": "pwd"})
        self.assertEqual(events[-1].type, "usage")
        self.assertEqual(events[-1].token_usage.cache_read_tokens, 3)
        self.assertEqual(events[-1].token_usage.cache_write_tokens, 2)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
