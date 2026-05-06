from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from llm_browser.harness.api import HelperAPI
from llm_browser.tool.web_fetch import (
    _browser_headers,
    _count_values,
    _crawl_site,
    _extract_cve_ids,
    _extract_fcc_grantee_codes,
    _fetch_text_result,
    _fetch_text_with_curl_cffi,
    _fetch_text_with_jina_reader,
    _html_to_readable_text,
    _jina_reader_url,
    _looks_like_external_result_url,
    _normalize_search_url,
    _parse_bing_results,
    _parse_brave_results,
    _parse_duckduckgo_results,
    _parse_generic_search_results,
    _parse_markdown_links,
    _query_looks_scholarly,
    _search_crossref_api,
    _search_cve_records,
    _search_fcc_grantee_records,
    _search_pubmed_api,
    _search_wikipedia_api,
)


SKILL = {
    "name": "research",
    "description": "HTTP fetch, readable text, web search, crawling, and bulk fetch helpers.",
    "exports": ["http_get", "fetch_text", "fetch_readable_text", "fetch_many_text", "search_web", "crawl_site"],
}


def install(api: HelperAPI) -> Dict[str, Any]:
    fetch_text = make_fetch_text(api)
    fetch_many_text = make_fetch_many_text(api, fetch_text)

    def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 20.0) -> str:
        try:
            import requests
        except Exception as exc:
            raise RuntimeError("requests is not installed") from exc
        request_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            request_headers.update(headers)
        api.check_cancel()
        response = requests.get(url, headers=request_headers, timeout=timeout)
        api.check_cancel()
        response.raise_for_status()
        return response.text

    def fetch_readable_text(
        url: str,
        max_chars: int = 30000,
        use_jina: Any = "auto",
        timeout: float = 20.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        result = fetch_text(url, max_chars=max_chars * 4, use_jina=use_jina, timeout=timeout, headers=headers)
        raw_text = str(result.get("text") or "")
        content_type = str(result.get("content_type") or "").lower()
        looks_like_html = "<html" in raw_text[:1000].lower() or "<body" in raw_text[:2000].lower() or "text/html" in content_type
        readable = _html_to_readable_text(raw_text, max_chars=max_chars) if looks_like_html else re.sub(r"\s+", " ", raw_text).strip()[:max_chars]
        cleaned = dict(result)
        cleaned["text"] = readable
        cleaned["chars"] = len(readable)
        cleaned["raw_chars"] = len(raw_text)
        cleaned["readable"] = True
        return cleaned

    def search_web(
        query: str,
        max_results: int = 8,
        timeout: float = 20.0,
        save_raw: Any = "auto",
        include_specialized: Any = "auto",
    ) -> Dict[str, Any]:
        try:
            import requests
        except Exception as exc:
            raise RuntimeError("requests is not installed") from exc

        raw_mode = str(save_raw).lower()
        save_raw_always = save_raw is True or raw_mode in {"1", "true", "yes", "always", "all"}
        save_raw_auto = raw_mode in {"auto", "failed", "empty"}
        urls = [
            ("bing", f"https://www.bing.com/search?q={quote_plus(query)}"),
            ("duckduckgo_html", f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"),
            ("duckduckgo_lite", f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}"),
            ("brave", f"https://search.brave.com/search?q={quote_plus(query)}"),
            ("google_reader", _jina_reader_url(f"https://www.google.com/search?q={quote_plus(query)}")),
            ("bing_reader", _jina_reader_url(f"https://www.bing.com/search?q={quote_plus(query)}")),
        ]
        results: List[Dict[str, str]] = []
        attempts: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()

        def add_results(candidates: List[Dict[str, str]]) -> List[Dict[str, str]]:
            added: List[Dict[str, str]] = []
            for candidate in candidates:
                url = _normalize_search_url(candidate.get("url", ""))
                if not url or url in seen_urls or not _looks_like_external_result_url(url):
                    continue
                seen_urls.add(url)
                item = dict(candidate)
                item["url"] = url
                results.append(item)
                added.append(item)
                if len(results) >= max_results:
                    break
            return added

        def save_search_page(source: str, text: str) -> str:
            search_dir = api.cwd / "search_pages"
            search_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", query).strip("-")[:80] or "query"
            path = search_dir / f"{int(time.time() * 1000)}-{source}-{slug}.html"
            path.write_text(text, encoding="utf-8", errors="replace")
            return str(path)

        cve_ids = _extract_cve_ids(query)
        if cve_ids:
            found, attempt = _search_cve_records(cve_ids, limit=max_results)
            added = add_results(found)
            attempt["parsed"] = len(added)
            attempts.append(attempt)

        fcc_codes = _extract_fcc_grantee_codes(query)
        if fcc_codes and len(results) < max_results:
            found, attempt = _search_fcc_grantee_records(fcc_codes, limit=max_results - len(results))
            added = add_results(found)
            attempt["parsed"] = len(added)
            attempts.append(attempt)

        for source, search_url in urls:
            if len(results) >= max_results:
                break
            try:
                api.check_cancel()
                source_timeout = min(timeout, 12.0 if source.endswith("_reader") else 6.0)
                response = requests.get(search_url, headers=_browser_headers(), timeout=source_timeout)
                api.check_cancel()
                text = response.text
                if source == "bing":
                    parsed = _parse_bing_results(text, limit=max_results - len(results))
                elif source.startswith("duckduckgo"):
                    parsed = _parse_duckduckgo_results(text, limit=max_results - len(results))
                elif source == "brave":
                    parsed = _parse_brave_results(text, limit=max_results - len(results))
                elif source.endswith("_reader"):
                    parsed = _parse_markdown_links(text, limit=max_results - len(results), source=source)
                else:
                    parsed = _parse_generic_search_results(text, source=source, limit=max_results - len(results))
                added = add_results(parsed)
                attempt: Dict[str, Any] = {
                    "source": source,
                    "status": response.status_code,
                    "url": response.url,
                    "chars": len(text),
                    "parsed": len(added),
                }
                if save_raw_always or (save_raw_auto and not added):
                    attempt["raw_path"] = save_search_page(source, text)
                attempts.append(attempt)
            except Exception as exc:
                attempts.append({"source": source, "url": search_url, "error": str(exc)})
            if len(results) >= max_results:
                break

        specialized_mode = str(include_specialized).lower()
        specialized_enabled = (
            include_specialized is True
            or specialized_mode in {"1", "true", "yes", "always"}
            or (specialized_mode == "auto" and _query_looks_scholarly(query))
        )
        if specialized_enabled and len(results) < max_results:
            for source, searcher in (
                ("wikipedia_api", _search_wikipedia_api),
                ("pubmed_api", _search_pubmed_api),
                ("crossref_api", _search_crossref_api),
            ):
                try:
                    found, attempt = searcher(query, limit=max_results - len(results), timeout=timeout)
                    api.check_cancel()
                    added = add_results(found)
                    attempt["parsed"] = len(added)
                    attempts.append(attempt)
                except Exception as exc:
                    attempts.append({"source": source, "error": str(exc)})
                if len(results) >= max_results:
                    break
        return {"query": query, "results": results[:max_results], "attempts": attempts}

    def crawl_site(
        start_url: str,
        max_pages: int = 12,
        timeout: float = 12.0,
        max_workers: int = 6,
        max_chars_per_page: int = 120000,
        use_jina: Any = "auto",
        same_site: bool = True,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        purpose: str = "contact",
    ) -> Dict[str, Any]:
        return _crawl_site(
            fetch_many_text=fetch_many_text,
            start_url=start_url,
            max_pages=max_pages,
            timeout=timeout,
            max_workers=max_workers,
            max_chars_per_page=max_chars_per_page,
            use_jina=use_jina,
            same_site=same_site,
            include=include,
            exclude=exclude,
            purpose=purpose,
        )

    return {
        "http_get": http_get,
        "fetch_text": fetch_text,
        "fetch_readable_text": fetch_readable_text,
        "fetch_many_text": fetch_many_text,
        "search_web": search_web,
        "crawl_site": crawl_site,
    }


def make_fetch_text(api: HelperAPI):
    def fetch_text(
        url: str,
        max_chars: int = 20000,
        use_jina: Any = "auto",
        timeout: float = 20.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        try:
            import requests
        except Exception as exc:
            raise RuntimeError("requests is not installed") from exc

        mode = str(use_jina).lower()
        force_jina = use_jina is True or mode in {"1", "true", "yes", "always", "jina", "reader"}
        disable_jina = use_jina is False or mode in {"0", "false", "no", "never", "direct"}
        request_headers = _browser_headers()
        if headers:
            request_headers.update(headers)

        direct_error: Optional[str] = None
        if not force_jina:
            try:
                api.check_cancel()
                response = requests.get(url, headers=request_headers, timeout=timeout)
                api.check_cancel()
                text = response.text
                result = _fetch_text_result(url, response.url, response.status_code, text, "direct", max_chars)
                if response.ok and text.strip():
                    return result
                direct_error = f"HTTP {response.status_code}"
                if disable_jina:
                    return result
            except Exception as exc:
                direct_error = str(exc)
                if disable_jina:
                    return {
                        "ok": False,
                        "url": url,
                        "source": "direct",
                        "error": direct_error,
                        "text": "",
                        "truncated": False,
                    }

            api.check_cancel()
            curl_result = _compat_fetch_text_with_curl_cffi(url, max_chars=max_chars, timeout=timeout, headers=request_headers)
            api.check_cancel()
            if curl_result is not None:
                if direct_error:
                    curl_result["direct_error"] = direct_error
                if curl_result.get("ok") and str(curl_result.get("text") or "").strip():
                    return curl_result
                if not direct_error:
                    direct_error = f"curl_cffi HTTP {curl_result.get('status')}"

        api.check_cancel()
        return _fetch_text_with_jina_reader(
            url,
            max_chars=max_chars,
            timeout=timeout,
            headers=request_headers,
            direct_error=direct_error,
        )

    return fetch_text


def make_fetch_many_text(api: HelperAPI, fetch_text):
    def fetch_many_text(
        urls: List[str],
        max_workers: int = 8,
        max_chars: int = 20000,
        use_jina: Any = "auto",
        timeout: float = 20.0,
        headers: Optional[Dict[str, str]] = None,
        save_to: Optional[str] = None,
        requests_per_minute: Optional[float] = None,
        rate_limit_retries: int = 3,
    ) -> Dict[str, Any]:
        url_list = [str(url) for url in urls]
        worker_count = max(1, min(int(max_workers), 64, len(url_list) or 1))
        results: List[Optional[Dict[str, Any]]] = [None] * len(url_list)

        def save_results() -> Optional[Path]:
            if not save_to:
                return None
            target = Path(save_to).expanduser()
            if not target.is_absolute():
                target = api.cwd / target
            target.parent.mkdir(parents=True, exist_ok=True)
            compact = [item or {"ok": False, "error": "missing result", "text": ""} for item in results]
            target.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
            return target

        def fetch_one(index_and_url: tuple[int, str]) -> tuple[int, Dict[str, Any]]:
            index, item_url = index_and_url
            try:
                api.check_cancel()
                return index, fetch_text(
                    item_url,
                    max_chars=max_chars,
                    use_jina=use_jina,
                    timeout=timeout,
                    headers=headers,
                )
            except Exception as exc:
                return index, {
                    "ok": False,
                    "url": item_url,
                    "source": "fetch_many_text",
                    "error": str(exc),
                    "text": "",
                    "truncated": False,
                }

        rpm = None
        if requests_per_minute is not None:
            try:
                rpm = float(requests_per_minute)
            except (TypeError, ValueError):
                raise ValueError("requests_per_minute must be a positive number") from None
            if rpm <= 0:
                raise ValueError("requests_per_minute must be a positive number")

        if rpm is not None:
            min_interval_s = 60.0 / rpm
            next_allowed_at = time.monotonic()
            retry_count = max(0, int(rate_limit_retries))
            for index, item_url in enumerate(url_list):
                attempt = 0
                while True:
                    api.check_cancel()
                    delay = next_allowed_at - time.monotonic()
                    if delay > 0:
                        api.sleep(delay)
                    _, result_item = fetch_one((index, item_url))
                    results[index] = result_item
                    save_results()
                    next_allowed_at = time.monotonic() + min_interval_s
                    if not result_item.get("rate_limited") or attempt >= retry_count:
                        break
                    retry_after = result_item.get("retry_after_s")
                    try:
                        wait_s = max(float(retry_after), min_interval_s)
                    except (TypeError, ValueError):
                        wait_s = max(5.0, min_interval_s)
                    api.sleep(min(wait_s, 90.0))
                    next_allowed_at = time.monotonic()
                    attempt += 1
        else:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
            pending: set[concurrent.futures.Future[tuple[int, Dict[str, Any]]]] = set()
            try:
                pending = {executor.submit(fetch_one, item) for item in enumerate(url_list)}
                while pending:
                    done, pending = concurrent.futures.wait(
                        pending,
                        timeout=0.05,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    api.check_cancel()
                    for future in done:
                        index, result_item = future.result()
                        results[index] = result_item
            except BaseException:
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
            save_results()

        compact_results = [item or {"ok": False, "error": "missing result", "text": ""} for item in results]
        summary: Dict[str, Any] = {
            "count": len(compact_results),
            "ok": sum(1 for item in compact_results if item.get("ok")),
            "failed": sum(1 for item in compact_results if not item.get("ok")),
            "truncated": sum(1 for item in compact_results if item.get("truncated")),
            "rate_limited": sum(1 for item in compact_results if item.get("rate_limited")),
            "sources": _count_values(str(item.get("source") or "") for item in compact_results),
        }
        if save_to:
            target = save_results()
            summary["path"] = str(target)
            return summary
        summary["results"] = compact_results
        return summary

    return fetch_many_text


def _compat_fetch_text_with_curl_cffi(url: str, *, max_chars: int, timeout: float, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    python_browser = sys.modules.get("llm_browser.tool.python_browser")
    handler = getattr(python_browser, "_fetch_text_with_curl_cffi", _fetch_text_with_curl_cffi)
    return handler(url, max_chars=max_chars, timeout=timeout, headers=headers)
