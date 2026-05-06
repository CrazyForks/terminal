from __future__ import annotations

from typing import Any, Dict, Optional

from llm_browser.harness.api import HelperAPI
from llm_browser.tool.browser_state import wait_for_download as _wait_for_download


SKILL = {
    "name": "downloads",
    "description": "Download inspection and wait helpers.",
    "exports": ["download_info", "wait_for_download"],
}


def install(api: HelperAPI) -> Dict[str, Any]:
    runtime = api.runtime

    def download_info(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return getattr(
            runtime,
            "download_info",
            lambda *a, **k: {"downloads_dir": str(api.download_dir), "files": [], "events": []},
        )(*args, **kwargs)

    def wait_for_download(
        pattern: Optional[str] = None,
        timeout_s: float = 30.0,
        poll_s: float = 0.25,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if timeout is not None:
            timeout_s = timeout
        return _wait_for_download(runtime, api.check_cancel, pattern=pattern, timeout_s=timeout_s, poll_s=poll_s)

    return {"download_info": download_info, "wait_for_download": wait_for_download}
