from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

from llm_browser.browser.runtime import BrowserRuntime


class JsRuntime(BrowserRuntime):
    def __init__(self, root_dir: Path, js_value: str) -> None:
        super().__init__(root_dir=root_dir)
        self.js_value = js_value

    def js(self, expression: str, await_promise: bool = False) -> Any:
        return self.js_value


class ScreenshotRuntime(BrowserRuntime):
    def __init__(self, root_dir: Path) -> None:
        super().__init__(root_dir=root_dir)
        self.target = {"url": "https://fallback.example", "title": "Fallback"}

    def cdp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"png-bytes").decode("ascii")}
        return {}

    def page_info(self) -> Dict[str, Any]:
        raise RuntimeError("document is not ready")


class BrowserRuntimeTest(unittest.TestCase):
    def test_page_info_handles_missing_document_elements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JsRuntime(
                Path(tmp),
                json.dumps(
                    {
                        "url": "about:blank",
                        "title": "",
                        "w": 0,
                        "h": 0,
                        "sx": 0,
                        "sy": 0,
                        "pw": 0,
                        "ph": 0,
                    }
                ),
            )

            self.assertEqual(runtime.page_info()["url"], "about:blank")

    def test_screenshot_writes_artifact_when_page_info_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ScreenshotRuntime(Path(tmp))

            image = runtime.screenshot("fallback", attach=True)

            self.assertTrue(Path(image.path).exists())
            self.assertEqual(image.url, "https://fallback.example")
            self.assertTrue(Path(image.path).with_suffix(".json").exists())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
