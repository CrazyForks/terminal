from __future__ import annotations

from typing import Any, Dict

from llm_browser.harness.api import HelperAPI


SKILL = {
    "name": "tracing",
    "description": "CDP event, console, network, dialog, and trace export helpers.",
    "exports": [
        "pending_dialog",
        "drain_cdp_events",
        "drain_events",
        "recent_cdp_events",
        "recent_console",
        "recent_network",
        "recent_network_failures",
        "save_browser_trace",
    ],
}


def install(api: HelperAPI) -> Dict[str, Any]:
    runtime = api.runtime
    return {
        "pending_dialog": getattr(runtime, "pending_dialog_info", lambda *args, **kwargs: None),
        "drain_cdp_events": getattr(runtime, "drain_events", lambda *args, **kwargs: []),
        "drain_events": getattr(runtime, "drain_events", lambda *args, **kwargs: []),
        "recent_cdp_events": getattr(runtime, "recent_cdp_events", lambda *args, **kwargs: []),
        "recent_console": getattr(runtime, "recent_console_events", lambda *args, **kwargs: []),
        "recent_network": getattr(runtime, "recent_network_events", lambda *args, **kwargs: []),
        "recent_network_failures": getattr(runtime, "recent_network_failures", lambda *args, **kwargs: []),
        "save_browser_trace": getattr(
            runtime,
            "save_browser_trace",
            lambda *args, **kwargs: {"path": None, "event_count": 0, "drained_count": 0},
        ),
    }
