from __future__ import annotations

from typing import Any, Dict, List, Optional

from llm_browser.harness.api import HelperAPI
from llm_browser.tool.browser_state import (
    clear_cookies as _clear_cookies,
    clear_storage as _clear_storage,
    get_cookies as _get_cookies,
    grant_permissions as _grant_permissions,
    reset_permissions as _reset_permissions,
    set_cookie as _set_cookie,
    storage_state as _storage_state,
)


SKILL = {
    "name": "cookies",
    "description": "Cookie, storage, and permission helpers.",
    "exports": [
        "get_cookies",
        "set_cookie",
        "clear_cookies",
        "storage_state",
        "clear_storage",
        "grant_permissions",
        "reset_permissions",
    ],
}


def install(api: HelperAPI) -> Dict[str, Any]:
    runtime = api.runtime

    def get_cookies(urls: Optional[List[str]] = None) -> Dict[str, Any]:
        return _get_cookies(runtime, api.check_cancel, urls=urls)

    def set_cookie(cookie: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        return _set_cookie(runtime, api.check_cancel, cookie=cookie, **kwargs)

    def clear_cookies() -> Dict[str, Any]:
        return _clear_cookies(runtime, api.check_cancel)

    def storage_state(include_cookies: bool = True) -> Dict[str, Any]:
        return _storage_state(runtime, api.check_cancel, include_cookies=include_cookies)

    def clear_storage(origin: Optional[str] = None, storage_types: str = "all") -> Dict[str, Any]:
        return _clear_storage(runtime, api.check_cancel, origin=origin, storage_types=storage_types)

    def grant_permissions(
        permissions: List[str],
        origin: Optional[str] = None,
        browser_context_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return _grant_permissions(runtime, api.check_cancel, permissions, origin=origin, browser_context_id=browser_context_id)

    def reset_permissions(browser_context_id: Optional[str] = None) -> Dict[str, Any]:
        return _reset_permissions(runtime, api.check_cancel, browser_context_id=browser_context_id)

    return {
        "get_cookies": get_cookies,
        "set_cookie": set_cookie,
        "clear_cookies": clear_cookies,
        "storage_state": storage_state,
        "clear_storage": clear_storage,
        "grant_permissions": grant_permissions,
        "reset_permissions": reset_permissions,
    }
