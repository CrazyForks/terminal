from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_browser.harness.api import HelperAPI
from llm_browser.harness_skills.research import make_fetch_text
from llm_browser.tool.web_fetch import (
    _extract_email_records,
    _extract_links,
    _extract_markdown_link_blocks,
    _extract_store_locator_locations,
    _html_to_readable_text,
    _normalize_email_domains,
)


SKILL = {
    "name": "extraction",
    "description": "HTML/text extraction, sitemap parsing, email/link parsing, and store locator helpers.",
    "exports": [
        "html_to_text",
        "extract_links",
        "extract_emails",
        "extract_markdown_link_blocks",
        "read_sitemap",
        "extract_store_locator_locations",
        "store_locator_locations",
    ],
}


def install(api: HelperAPI) -> Dict[str, Any]:
    fetch_text = api.namespace.get("fetch_text")
    if not callable(fetch_text):
        fetch_text = make_fetch_text(api)

    def html_to_text(markup: str, max_chars: int = 30000, remove_chrome: bool = True) -> str:
        return _html_to_readable_text(str(markup or ""), max_chars=max_chars, remove_chrome=remove_chrome)

    def extract_links(text: str, pattern: Optional[str] = None, limit: int = 1000) -> List[str]:
        return _extract_links(str(text), pattern=pattern, limit=limit)

    def extract_markdown_link_blocks(
        text: str,
        url_pattern: Optional[str] = None,
        max_lines_after: int = 8,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        return _extract_markdown_link_blocks(
            str(text),
            url_pattern=url_pattern,
            max_lines_after=max_lines_after,
            limit=limit,
        )

    def extract_emails(
        text: str,
        domains: Optional[Any] = None,
        max_results: int = 200,
        include_context: bool = True,
    ) -> List[Dict[str, str]]:
        return _extract_email_records(
            str(text),
            domains=_normalize_email_domains(domains),
            max_results=max_results,
            include_context=include_context,
        )

    def read_sitemap(
        url: str,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        max_urls: int = 10000,
        timeout: float = 30.0,
        use_jina: Any = "auto",
    ) -> Dict[str, Any]:
        result = fetch_text(url, max_chars=2_000_000, use_jina=use_jina, timeout=timeout)
        text = str(result.get("text") or "")
        links = _extract_links(text, pattern=include, limit=max(max_urls * 2, max_urls))
        if exclude:
            exclude_re = re.compile(exclude)
            links = [link for link in links if not exclude_re.search(link)]
        return {
            "url": url,
            "source": result.get("source"),
            "status": result.get("status"),
            "chars": result.get("chars"),
            "truncated": result.get("truncated"),
            "links": links[:max_urls],
            "count": len(links),
        }

    def extract_store_locator_locations(
        target: str,
        provider: str = "auto",
        country_ids: Optional[Any] = None,
        max_locations: int = 10000,
        timeout: float = 30.0,
        save_to: Optional[str] = None,
        include_locations: bool = True,
    ) -> Dict[str, Any]:
        result = _extract_store_locator_locations(
            target,
            provider=provider,
            country_ids=country_ids,
            max_locations=max_locations,
            timeout=timeout,
        )
        locations = result.get("locations")
        if save_to and isinstance(locations, list):
            target_path = Path(save_to).expanduser()
            if not target_path.is_absolute():
                target_path = api.cwd / target_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(json.dumps(locations, ensure_ascii=False, indent=2), encoding="utf-8")
            result["path"] = str(target_path)
        if not include_locations:
            result.pop("locations", None)
        return result

    return {
        "html_to_text": html_to_text,
        "extract_links": extract_links,
        "extract_emails": extract_emails,
        "extract_markdown_link_blocks": extract_markdown_link_blocks,
        "read_sitemap": read_sitemap,
        "extract_store_locator_locations": extract_store_locator_locations,
        "store_locator_locations": extract_store_locator_locations,
    }
