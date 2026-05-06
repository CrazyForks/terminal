from __future__ import annotations

import base64
import html
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse


__all__: List[str]


def _browser_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


def _jina_reader_url(url: str) -> str:
    if url.startswith(("https://r.jina.ai/http://", "http://r.jina.ai/http://")):
        return url
    return "https://r.jina.ai/http://" + url


def _fetch_text_result(
    requested_url: str,
    final_url: str,
    status_code: int,
    text: str,
    source: str,
    max_chars: int,
) -> Dict[str, Any]:
    truncated = len(text) > max_chars
    return {
        "ok": 200 <= status_code < 400,
        "url": requested_url,
        "final_url": final_url,
        "status": status_code,
        "source": source,
        "text": text[:max_chars],
        "chars": len(text),
        "truncated": truncated,
    }


def _fetch_text_with_curl_cffi(
    url: str,
    *,
    max_chars: int,
    timeout: float,
    headers: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    try:
        from curl_cffi import requests as curl_requests
    except Exception:
        return None

    last_error: Optional[str] = None
    for impersonate in ("chrome136", "chrome124", "chrome120"):
        try:
            response = curl_requests.get(url, headers=headers, timeout=timeout, impersonate=impersonate)
            result = _fetch_text_result(url, response.url, response.status_code, response.text, "curl_cffi", max_chars)
            result["impersonate"] = impersonate
            return result
        except Exception as exc:
            last_error = str(exc)
    return {
        "ok": False,
        "url": url,
        "source": "curl_cffi",
        "error": last_error or "curl_cffi request failed",
        "text": "",
        "truncated": False,
    }


def _fetch_text_with_jina_reader(
    url: str,
    *,
    max_chars: int,
    timeout: float,
    headers: Dict[str, str],
    direct_error: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests is not installed") from exc

    reader_url = _jina_reader_url(url)
    last_error: Optional[str] = None
    for attempt in range(3):
        try:
            response = requests.get(reader_url, headers=headers, timeout=max(timeout, 30.0))
            retry_delay = _jina_retry_delay(response.text)
            if retry_delay is not None and attempt < 2:
                time.sleep(min(max(retry_delay, 1.0), 30.0))
                continue
            result = _fetch_text_result(url, response.url, response.status_code, response.text, "jina", max_chars)
            if retry_delay is not None:
                result["ok"] = False
                result["rate_limited"] = True
                result["retry_after_s"] = retry_delay
            if direct_error:
                result["direct_error"] = direct_error
            return result
        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(1.0 + attempt)
    return {
        "ok": False,
        "url": url,
        "source": "jina",
        "reader_url": reader_url,
        "direct_error": direct_error,
        "error": last_error or "jina reader request failed",
        "text": "",
        "truncated": False,
    }


def _jina_retry_delay(text: str) -> Optional[float]:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    code = payload.get("code")
    status = payload.get("status")
    code_text = str(code or "")
    status_text = str(status or "")
    message = str(payload.get("message") or "").lower()
    rate_limited = code_text.startswith("429") or status_text.startswith("429") or (
        "retryAfter" in payload and "rate limit" in message
    )
    if not rate_limited:
        return None
    retry_after = payload.get("retryAfter")
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return 5.0


def _count_values(values: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        if not value:
            value = "-"
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _parse_bing_results(page: str, limit: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    if limit <= 0:
        return results
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page, "html.parser")
        for item in soup.select("li.b_algo"):
            link = item.select_one("h2 a") or item.find("a")
            if not link:
                continue
            title = link.get_text(" ", strip=True)
            url = _normalize_search_url(str(link.get("href") or ""))
            snippet = item.get_text(" ", strip=True)
            _append_search_result(results, title=title, url=url, snippet=snippet, source="bing", limit=limit)
            if len(results) >= limit:
                return results
        for link in soup.select("h2 a[href]"):
            title = link.get_text(" ", strip=True)
            url = _normalize_search_url(str(link.get("href") or ""))
            _append_search_result(results, title=title, url=url, snippet="", source="bing", limit=limit)
            if len(results) >= limit:
                return results
    except Exception:
        pass

    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.I | re.S):
        url = _normalize_search_url(match.group(1))
        title = re.sub(r"<[^>]+>", " ", match.group(2))
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        _append_search_result(results, title=title, url=url, snippet="", source="bing", limit=limit)
        if len(results) >= limit:
            break
    return results


def _parse_duckduckgo_results(page: str, limit: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    if limit <= 0:
        return results
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page, "html.parser")
        blocks = soup.select(".result, .web-result, tr")
        for block in blocks:
            link = block.select_one("a.result__a, a.result-link, a[href]")
            if not link:
                continue
            title = link.get_text(" ", strip=True)
            url = _normalize_search_url(str(link.get("href") or ""))
            snippet_node = block.select_one(".result__snippet, .result-snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else block.get_text(" ", strip=True)
            _append_search_result(results, title=title, url=url, snippet=snippet, source="duckduckgo", limit=limit)
            if len(results) >= limit:
                return results
    except Exception:
        pass
    return _parse_generic_search_results(page, source="duckduckgo", limit=limit)


def _parse_brave_results(page: str, limit: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    if limit <= 0:
        return results
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page, "html.parser")
        for link in soup.select(".snippet a[href], a[href][data-testid='result-title-a'], a.result-header[href]"):
            title = link.get_text(" ", strip=True)
            url = _normalize_search_url(str(link.get("href") or ""))
            if not title:
                continue
            snippet = title
            parent = link
            for _ in range(4):
                parent = parent.parent
                if parent is None:
                    break
                parent_text = parent.get_text(" ", strip=True)
                if len(parent_text) > len(title):
                    snippet = parent_text
                    break
            _append_search_result(results, title=title, url=url, snippet=snippet, source="brave", limit=limit)
            if len(results) >= limit:
                return results
    except Exception:
        pass
    return results


def _parse_generic_search_results(page: str, *, source: str, limit: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    if limit <= 0:
        return results
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page, "html.parser")
        for link in soup.find_all("a", href=True):
            title = link.get_text(" ", strip=True)
            url = _normalize_search_url(str(link.get("href") or ""))
            _append_search_result(results, title=title, url=url, snippet="", source=source, limit=limit)
            if len(results) >= limit:
                break
    except Exception:
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.I | re.S):
            title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(2)))).strip()
            url = _normalize_search_url(match.group(1))
            _append_search_result(results, title=title, url=url, snippet="", source=source, limit=limit)
            if len(results) >= limit:
                break
    return results


def _parse_markdown_links(markdown: str, limit: int, source: str = "bing_reader") -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    if limit <= 0:
        return results
    seen = set()
    for match in re.finditer(r"\[([^\]]{1,220})\]\((https?://[^)\s]+)\)", markdown):
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        url = _normalize_search_url(match.group(2))
        if not title or url in seen:
            continue
        seen.add(url)
        _append_search_result(results, title=title, url=url, snippet="", source=source, limit=limit)
        if len(results) >= limit:
            break
    return results


def _append_search_result(
    results: List[Dict[str, str]],
    *,
    title: str,
    url: str,
    snippet: str,
    source: str,
    limit: int,
) -> None:
    if len(results) >= limit:
        return
    title = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    snippet = re.sub(r"\s+", " ", html.unescape(snippet or "")).strip()
    url = _normalize_search_url(url)
    if not title or not url or not _looks_like_external_result_url(url):
        return
    if any(existing.get("url") == url for existing in results):
        return
    results.append({"title": title[:300], "url": url, "snippet": snippet[:600], "source": source})


def _normalize_search_url(url: str) -> str:
    url = html.unescape(str(url or "")).strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/l/") and "uddg=" in url:
        query = parse_qs(urlparse("https://duckduckgo.com" + url).query)
        if "uddg" in query:
            return _normalize_search_url(query["uddg"][0])
    if url.startswith("/"):
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)
    if "duckduckgo.com" in host and "uddg" in query:
        return _normalize_search_url(query["uddg"][0])
    if "bing.com" in host and "u" in query:
        decoded = _decode_bing_redirect(query["u"][0])
        if decoded:
            return _normalize_search_url(decoded)
    if "google." in host and "url" in parsed.path and "q" in query:
        return _normalize_search_url(query["q"][0])
    return unquote(url)


def _decode_bing_redirect(value: str) -> str:
    value = unquote(value)
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("a1"):
        value = value[2:]
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return decoded if decoded.startswith(("http://", "https://")) else ""


def _looks_like_external_result_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    blocked_hosts = (
        "bing.com",
        "duckduckgo.com",
        "google.com",
        "google.",
        "startpage.com",
        "brave.com",
        "brave.app",
        "search.brave.com",
        "mojeek.com",
        "kagi.com",
        "r.jina.ai",
        "s.jina.ai",
        "gstatic.com",
        "googleusercontent.com",
        "encrypted-tbn",
    )
    if any(blocked in host for blocked in blocked_hosts):
        return False
    blocked_path_parts = ("/search", "/preferences", "/settings", "/account", "/signin", "/login", "/captcha")
    if any(part in path for part in blocked_path_parts):
        return False
    blocked_extensions = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")
    return not path.endswith(blocked_extensions)


def _query_looks_scholarly(query: str) -> bool:
    text = query.strip()
    lowered = text.lower()
    strong_scholarly_terms = (
        "pubmed",
        "pmid",
        "ncbi",
        "doi:",
        "doi.org",
        "arxiv",
        "clinical trial",
        "randomized",
        "double-blind",
        "placebo",
        "research paper",
        "scientific paper",
        "peer reviewed",
        "citation",
        "citations",
        "journal",
    )
    if any(term in lowered for term in strong_scholarly_terms):
        return True
    biology_context_terms = (
        "animal",
        "animals",
        "bacterium",
        "bacteria",
        "clinical",
        "gene",
        "genome",
        "infection",
        "microbial",
        "molecule",
        "organism",
        "pathogen",
        "plant",
        "protein",
        "species",
        "strain",
    )
    has_binomial = bool(re.search(r"\b[A-Z][a-z]{2,15}\s+[a-z]{3,15}\b", text))
    return has_binomial and any(term in lowered for term in biology_context_terms)


def _extract_cve_ids(text: str) -> List[str]:
    seen: set[str] = set()
    cve_ids: List[str] = []
    for match in re.finditer(r"\bCVE-\d{4}-\d{4,}\b", text, flags=re.IGNORECASE):
        cve_id = match.group(0).upper()
        if cve_id not in seen:
            seen.add(cve_id)
            cve_ids.append(cve_id)
    return cve_ids


def _extract_fcc_grantee_codes(text: str) -> List[str]:
    if not re.search(r"\b(fcc|fccid|grantee)\b", text or "", flags=re.I):
        return []
    stop_words = {
        "FCC",
        "FCCID",
        "ID",
        "IO",
        "SITE",
        "HTTP",
        "HTML",
        "THE",
        "AND",
        "FOR",
        "CODE",
        "CODES",
        "QUERY",
        "SEARCH",
    }
    seen: set[str] = set()
    codes: List[str] = []
    for token in re.findall(r"\b[A-Z0-9]{3,5}\b", (text or "").upper()):
        if token in stop_words:
            continue
        if len(token) == 5 and not token.startswith("2"):
            continue
        if not any(ch.isdigit() for ch in token) and len(token) != 3:
            continue
        if token not in seen:
            seen.add(token)
            codes.append(token)
    return codes


def _search_fcc_grantee_records(codes: List[str], *, limit: int) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    if limit <= 0:
        return [], {"source": "fcc_grantee_records", "skipped": True}
    results: List[Dict[str, str]] = []
    for code in codes:
        candidates = [
            {
                "title": f"FCCID.io grantee page - {code}",
                "url": f"https://fccid.io/{code}/",
                "snippet": f"FCCID.io grantee page listing FCC ID applications for grantee code {code}.",
                "source": "fcc_grantee_records",
            },
            {
                "title": f"FCCID.io company lookup - {code}",
                "url": f"https://fccid.io/company.php?grantee={code}",
                "snippet": f"FCCID.io company lookup for FCC grantee code {code}.",
                "source": "fcc_grantee_records",
            },
        ]
        for candidate in candidates:
            results.append(candidate)
            if len(results) >= limit:
                return results, {"source": "fcc_grantee_records", "fcc_grantee_codes": codes}
    return results, {"source": "fcc_grantee_records", "fcc_grantee_codes": codes}


def _normalize_email_domains(domains: Optional[Any]) -> Optional[set[str]]:
    if domains is None:
        return None
    if isinstance(domains, str):
        raw_domains = [part.strip() for part in re.split(r"[,;\s]+", domains) if part.strip()]
    else:
        raw_domains = [str(part).strip() for part in domains if str(part).strip()]
    normalized = {
        part.lower().lstrip("@").removeprefix("www.")
        for part in raw_domains
        if "." in part.lstrip("@")
    }
    return normalized or None


def _extract_email_records(
    text: str,
    *,
    domains: Optional[set[str]] = None,
    max_results: int = 200,
    include_context: bool = True,
) -> List[Dict[str, str]]:
    if max_results <= 0:
        return []
    seen: set[str] = set()
    records: List[Dict[str, str]] = []
    email_re = re.compile(r"(?<![A-Z0-9._%+\-])[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,24}\b", re.I)
    for match in email_re.finditer(text or ""):
        email = match.group(0).strip(".,;:!?)]}'\"").lower()
        if email in seen or _looks_like_noise_email(email):
            continue
        domain = email.rsplit("@", 1)[-1]
        if domains and not any(domain == item or domain.endswith("." + item) for item in domains):
            continue
        seen.add(email)
        record = {"email": email, "domain": domain}
        if include_context:
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 100)
            context = re.sub(r"\s+", " ", text[start:end]).strip()
            record["context"] = context[:260]
        records.append(record)
        if len(records) >= max_results:
            break
    return records


def _looks_like_noise_email(email: str) -> bool:
    if "@" not in email:
        return True
    local, domain = email.rsplit("@", 1)
    local = local.lower().strip(".")
    domain = domain.lower().strip(".")
    if not local or not domain or "." not in domain:
        return True
    tld = domain.rsplit(".", 1)[-1]
    blocked_tlds = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "woff", "woff2", "ico"}
    if tld in blocked_tlds:
        return True
    blocked_domains = {
        "example.com",
        "example.org",
        "example.net",
        "domain.com",
        "mysite.com",
        "yourdomain.com",
        "company.com",
        "duckduckgo.com",
    }
    if domain in blocked_domains or domain.endswith(".example.com"):
        return True
    blocked_exact = {
        "user@domain.com",
        "you@company.com",
        "name@example.com",
        "email@example.com",
        "test@example.com",
        "error-lite@duckduckgo.com",
    }
    if email in blocked_exact:
        return True
    blocked_locals = {
        "example",
        "test",
        "testing",
        "user",
        "username",
        "you",
        "yourname",
        "name",
        "firstname.lastname",
        "first.last",
        "email",
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
    }
    return local in blocked_locals


def _extract_store_locator_locations(
    target: str,
    *,
    provider: str = "auto",
    country_ids: Optional[Any] = None,
    max_locations: int = 10000,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    provider_name = str(provider or "auto").lower()
    if provider_name not in {"auto", "bullseye"}:
        return {"ok": False, "provider": provider, "error": f"unsupported provider: {provider}"}
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests is not installed") from exc

    attempts: List[Dict[str, Any]] = []
    candidates = _discover_bullseye_interface_names(str(target or ""), requests=requests, timeout=timeout, attempts=attempts)
    if not candidates:
        return {
            "ok": False,
            "provider": "bullseye",
            "target": target,
            "error": "no Bullseye interface name discovered",
            "attempts": attempts,
        }

    errors: List[str] = []
    for interface_name in candidates:
        config_url = "https://wswrapper.bullseyelocations.com/InterfaceConfiguration/GetInterfaceConfiguration"
        try:
            config_response = requests.get(
                config_url,
                params={"interfaceName": interface_name},
                headers=_browser_headers(),
                timeout=min(timeout, 30.0),
            )
            config_attempt: Dict[str, Any] = {
                "source": "bullseye_config",
                "interface_name": interface_name,
                "status": getattr(config_response, "status_code", None),
                "url": getattr(config_response, "url", config_url),
                "chars": len(getattr(config_response, "text", "") or ""),
            }
            config = config_response.json()
            if not isinstance(config, dict):
                config_attempt["error"] = "configuration response was not an object"
                attempts.append(config_attempt)
                continue
            client_id = config.get("clientId")
            api_key = config.get("apiKey")
            config_attempt["client_id"] = client_id
            attempts.append(config_attempt)
            if not client_id or not api_key:
                errors.append(f"{interface_name}: missing clientId/apiKey")
                continue
            locations_result = _fetch_bullseye_location_list(
                requests=requests,
                client_id=client_id,
                api_key=str(api_key),
                config=config,
                country_ids=country_ids,
                max_locations=max_locations,
                timeout=timeout,
            )
            attempts.append(locations_result.pop("attempt"))
            if locations_result.get("ok"):
                locations = locations_result.get("locations") or []
                return {
                    "ok": True,
                    "provider": "bullseye",
                    "target": target,
                    "interface_name": interface_name,
                    "client_id": client_id,
                    "country_ids": locations_result.get("country_ids"),
                    "count": len(locations),
                    "sample": locations[:3],
                    "locations": locations,
                    "attempts": attempts,
                }
            errors.append(f"{interface_name}: {locations_result.get('error', 'location list failed')}")
        except Exception as exc:
            attempts.append({"source": "bullseye_config", "interface_name": interface_name, "error": str(exc)})
            errors.append(f"{interface_name}: {exc}")

    return {
        "ok": False,
        "provider": "bullseye",
        "target": target,
        "interfaces": candidates,
        "error": "; ".join(errors[-5:]) or "all Bullseye attempts failed",
        "attempts": attempts,
    }


def _discover_bullseye_interface_names(
    target: str,
    *,
    requests: Any,
    timeout: float,
    attempts: List[Dict[str, Any]],
) -> List[str]:
    candidates: List[str] = []

    def add(value: Any) -> None:
        name = html.unescape(unquote(str(value or ""))).strip().strip("/?#&")
        name = name.split("?", 1)[0].split("#", 1)[0].strip("/")
        if not name:
            return
        if "/" in name:
            name = name.rstrip("/").rsplit("/", 1)[-1]
        name = name.lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,140}", name):
            return
        blocked = {"local", "list", "citylist", "pages", "page", "static", "resources", "resource", "error", "ie", "index"}
        if name in blocked or name.startswith("_next") or name.endswith(("-css", "-js")):
            return
        if name not in candidates:
            candidates.append(name)

    value = target.strip()
    parsed = urlparse(value)
    if parsed.query:
        query = parse_qs(parsed.query)
        for key in ("interfaceName", "interfacename", "interface_name"):
            for item in query.get(key, []):
                add(item)
    if parsed.scheme in {"http", "https"}:
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        for index, segment in enumerate(segments):
            previous = segments[index - 1].lower() if index else ""
            before_previous = segments[index - 2].lower() if index >= 2 else ""
            if previous in {"list", "citylist", "pages"} or (before_previous == "local" and previous in {"list", "citylist"}):
                add(segment)
        if segments:
            add(segments[-1])
    elif value and "/" not in value and " " not in value:
        add(value)

    texts = [value]
    if parsed.scheme in {"http", "https"}:
        try:
            response = requests.get(value, headers=_browser_headers(), timeout=min(timeout, 20.0))
            text = getattr(response, "text", "") or ""
            texts.append(text)
            attempts.append(
                {
                    "source": "locator_page",
                    "url": getattr(response, "url", value),
                    "status": getattr(response, "status_code", None),
                    "chars": len(text),
                }
            )
        except Exception as exc:
            attempts.append({"source": "locator_page", "url": value, "error": str(exc)})

    for text in texts:
        for pattern in (
            r"GetInterfaceConfiguration\?[^\"'<>]*?interfaceName=([^&\"'<>]+)",
            r"interfaceName[\"'\s:=]+([a-zA-Z0-9_-]{2,140})",
            r"interface_name[\"'\s:=]+([a-zA-Z0-9_-]{2,140})",
            r"/local/(?:list|citylist)/([a-zA-Z0-9][a-zA-Z0-9_-]{1,140})",
            r"/local/(?!static/|list/|citylist/|error/|ie\.html)([a-zA-Z0-9][a-zA-Z0-9_-]{1,140})(?:[/?\"'<>#]|$)",
            r"/pages/([a-zA-Z0-9][a-zA-Z0-9_-]{1,140})",
        ):
            for match in re.finditer(pattern, text):
                add(match.group(1))
    return candidates


def _fetch_bullseye_location_list(
    *,
    requests: Any,
    client_id: Any,
    api_key: str,
    config: Dict[str, Any],
    country_ids: Optional[Any],
    max_locations: int,
    timeout: float,
) -> Dict[str, Any]:
    country_value = _bullseye_country_ids(country_ids, config)
    location_identifier = config.get("locationIdentifier") or 1
    params = {
        "countryIds": country_value,
        "action": "json",
        "isSEO": "true",
        "isProxy": "true",
        "locationIdentifier": location_identifier,
        "callback": "results",
        "ClientId": client_id,
        "ApiKey": api_key,
    }
    url = "https://ws.bullseyelocations.com/RestSearch.svc/GetLocationList"
    try:
        response = requests.get(url, params=params, headers=_browser_headers(), timeout=min(timeout, 60.0))
        payload = _decode_bullseye_location_payload(response)
        locations = payload.get("locations")
        if not isinstance(locations, list):
            locations = payload.get("Locations")
        if not isinstance(locations, list):
            return {
                "ok": False,
                "error": "location payload did not contain a locations list",
                "country_ids": country_value,
                "attempt": {
                    "source": "bullseye_location_list",
                    "status": getattr(response, "status_code", None),
                    "url": getattr(response, "url", url),
                    "chars": len(getattr(response, "text", "") or ""),
                    "parsed": 0,
                },
            }
        limit = max(0, min(int(max_locations), len(locations)))
        trimmed = locations[:limit]
        return {
            "ok": True,
            "country_ids": country_value,
            "locations": trimmed,
            "attempt": {
                "source": "bullseye_location_list",
                "status": getattr(response, "status_code", None),
                "url": getattr(response, "url", url),
                "chars": len(getattr(response, "text", "") or ""),
                "parsed": len(trimmed),
                "total": len(locations),
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "country_ids": country_value,
            "attempt": {"source": "bullseye_location_list", "url": url, "error": str(exc), "parsed": 0},
        }


def _bullseye_country_ids(country_ids: Optional[Any], config: Dict[str, Any]) -> str:
    if country_ids is not None:
        if isinstance(country_ids, str):
            return country_ids
        try:
            return ",".join(str(item) for item in country_ids)
        except TypeError:
            return str(country_ids)
    countries = config.get("countries")
    if isinstance(countries, list):
        ids = [str(item.get("id")) for item in countries if isinstance(item, dict) and item.get("id")]
        if ids:
            return ",".join(ids)
    return "1"


def _decode_bullseye_location_payload(response: Any) -> Dict[str, Any]:
    try:
        raw: Any = response.json()
    except Exception:
        raw = getattr(response, "text", "")
    for _ in range(4):
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, list):
            return {"locations": raw}
        if not isinstance(raw, str):
            break
        text = raw.strip()
        jsonp = re.match(r"^[a-zA-Z_$][\w$]*\((.*)\)\s*;?$", text, re.S)
        if jsonp:
            text = jsonp.group(1).strip()
        try:
            raw = json.loads(text)
        except Exception:
            break
    return {}


def _crawl_site(
    *,
    fetch_many_text: Callable[..., Dict[str, Any]],
    start_url: str,
    max_pages: int,
    timeout: float,
    max_workers: int,
    max_chars_per_page: int,
    use_jina: Any,
    same_site: bool,
    include: Optional[str],
    exclude: Optional[str],
    purpose: str,
) -> Dict[str, Any]:
    root_url = _ensure_http_url(start_url)
    parsed_root = urlparse(root_url)
    if not parsed_root.netloc:
        raise ValueError(f"invalid start_url: {start_url}")
    site_key = _site_host_key(parsed_root.netloc)
    include_re = re.compile(include) if include else None
    exclude_re = re.compile(exclude) if exclude else None
    page_limit = max(1, min(int(max_pages), 100))
    worker_limit = max(1, min(int(max_workers), 32))
    candidates: Dict[str, Dict[str, Any]] = {}
    order = 0

    def add_candidate(raw_url: str, *, base: Optional[str] = None, reason: str = "seed", label: str = "") -> None:
        nonlocal order
        normalized = _normalize_crawl_url(raw_url, base_url=base or root_url)
        if not normalized:
            return
        parsed = urlparse(normalized)
        if same_site and not _url_matches_site(parsed, site_key):
            return
        if include_re and not include_re.search(normalized):
            return
        if exclude_re and exclude_re.search(normalized):
            return
        score = _crawl_url_score(normalized, label=label, purpose=purpose)
        existing = candidates.get(normalized)
        if existing:
            existing["score"] = max(int(existing["score"]), score)
            if reason not in existing["reasons"]:
                existing["reasons"].append(reason)
            return
        candidates[normalized] = {"url": normalized, "score": score, "reasons": [reason], "order": order}
        order += 1

    add_candidate(root_url, reason="start")
    for path in _COMMON_CRAWL_PATHS:
        add_candidate(urljoin(root_url, path), reason="common_path", label=path)

    fetched: set[str] = set()
    pages: List[Dict[str, Any]] = []
    aggregate_emails: Dict[str, Dict[str, str]] = {}
    social_links: Dict[str, str] = {}

    while len(fetched) < page_limit:
        remaining = [
            item
            for item in candidates.values()
            if item["url"] not in fetched
        ]
        if not remaining:
            break
        remaining.sort(key=lambda item: (-int(item["score"]), int(item["order"])))
        batch = remaining[: min(worker_limit, page_limit - len(fetched))]
        batch_urls = [item["url"] for item in batch]
        for item_url in batch_urls:
            fetched.add(item_url)
        fetched_results = fetch_many_text(
            batch_urls,
            max_workers=worker_limit,
            max_chars=max_chars_per_page,
            use_jina=use_jina,
            timeout=timeout,
        )
        raw_results = fetched_results.get("results") or []
        if not isinstance(raw_results, list):
            raw_results = []
        for index, result in enumerate(raw_results):
            if not isinstance(result, dict):
                continue
            page_url = str(result.get("final_url") or result.get("url") or batch_urls[index])
            text = str(result.get("text") or "")
            page_links = _extract_page_links(text, base_url=page_url, limit=500)
            for link in page_links:
                href = link["url"]
                if href.startswith("mailto:"):
                    continue
                add_candidate(href, base=page_url, reason="page_link", label=link.get("text", ""))
                if _looks_like_social_url(href):
                    social_links.setdefault(href, href)
            email_records = _extract_email_records(text, domains=None, max_results=80, include_context=True)
            for record in email_records:
                aggregate_emails.setdefault(record["email"], record)
            high_value_links = [
                link["url"]
                for link in sorted(page_links, key=lambda item: -_crawl_url_score(item["url"], label=item.get("text", ""), purpose=purpose))
                if not link["url"].startswith("mailto:") and _crawl_url_score(link["url"], label=link.get("text", ""), purpose=purpose) > 0
            ][:25]
            pages.append(
                {
                    "url": page_url,
                    "requested_url": batch_urls[index] if index < len(batch_urls) else page_url,
                    "ok": bool(result.get("ok")),
                    "status": result.get("status"),
                    "source": result.get("source"),
                    "chars": result.get("chars", len(text)),
                    "truncated": bool(result.get("truncated")),
                    "title": _extract_html_title(text),
                    "emails": [record["email"] for record in email_records],
                    "email_records": email_records[:20],
                    "links": high_value_links,
                }
            )

    return {
        "start_url": root_url,
        "site": site_key,
        "fetched": len(fetched),
        "pages": pages,
        "emails": list(aggregate_emails),
        "email_records": list(aggregate_emails.values()),
        "social_links": list(social_links),
        "candidate_count": len(candidates),
        "remaining_candidates": [
            item["url"]
            for item in sorted(candidates.values(), key=lambda value: (-int(value["score"]), int(value["order"])))
            if item["url"] not in fetched
        ][:50],
    }


_COMMON_CRAWL_PATHS = (
    "/",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/team",
    "/people",
    "/staff",
    "/leadership",
    "/privacy",
    "/privacy-policy",
    "/legal",
    "/impressum",
    "/sitemap.xml",
    "/robots.txt",
    "/llms.txt",
    "/.well-known/security.txt",
)


def _ensure_http_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise ValueError("url is empty")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value.lstrip("/")
    parsed = urlparse(value)
    if parsed.netloc and not parsed.path:
        return parsed._replace(path="/").geturl()
    return value


def _site_host_key(host: str) -> str:
    host = host.lower().split(":", 1)[0].strip(".")
    return host[4:] if host.startswith("www.") else host


def _url_matches_site(parsed_url: Any, site_key: str) -> bool:
    host = _site_host_key(parsed_url.netloc)
    return host == site_key or host.endswith("." + site_key)


def _normalize_crawl_url(raw_url: str, *, base_url: str) -> str:
    value = html.unescape(str(raw_url or "")).strip()
    if not value or value.startswith(("#", "javascript:", "tel:", "data:", "blob:")):
        return ""
    if value.startswith("mailto:"):
        return value
    url = urljoin(base_url, value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path_lower = parsed.path.lower()
    skip_extensions = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".avif",
        ".ico",
        ".css",
        ".js",
        ".mjs",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp4",
        ".webm",
        ".mov",
        ".zip",
    )
    if path_lower.endswith(skip_extensions):
        return ""
    return parsed._replace(fragment="").geturl()


def _crawl_url_score(url: str, *, label: str = "", purpose: str = "contact") -> int:
    parsed = urlparse(url)
    exact_path = parsed.path.rstrip("/") or "/"
    exact_scores = {
        "/": 200,
        "/contact": 165,
        "/contact-us": 155,
        "/team": 130,
        "/people": 130,
        "/staff": 130,
        "/leadership": 125,
        "/about": 115,
        "/about-us": 110,
        "/impressum": 105,
        "/privacy": 60,
        "/privacy-policy": 60,
        "/legal": 50,
        "/sitemap.xml": 45,
        "/.well-known/security.txt": 35,
        "/llms.txt": 30,
        "/robots.txt": 10,
    }
    depth = len([part for part in parsed.path.split("/") if part])
    if exact_path in exact_scores:
        return max(0, exact_scores[exact_path] - depth * 8)
    text = (parsed.path + " " + parsed.query + " " + label).lower()
    score = 0
    terms = {
        "contact": 120,
        "contact-us": 120,
        "about": 70,
        "about-us": 70,
        "team": 85,
        "people": 85,
        "staff": 85,
        "leadership": 85,
        "founder": 75,
        "owner": 75,
        "impressum": 75,
        "privacy": 35,
        "legal": 35,
        "sitemap": 25,
        "security.txt": 20,
        "llms.txt": 15,
    }
    if purpose and purpose.lower() not in {"contact", "contacts", "email", "emails"}:
        score += 10
    for term, value in terms.items():
        if term in text:
            score += value
    return max(0, score - depth * 8)


def _extract_page_links(text: str, *, base_url: str, limit: int = 500) -> List[Dict[str, str]]:
    seen: set[str] = set()
    links: List[Dict[str, str]] = []

    def add(raw_url: str, label: str = "") -> None:
        if len(links) >= limit:
            return
        normalized = _normalize_crawl_url(raw_url, base_url=base_url)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        links.append({"url": normalized, "text": re.sub(r"\s+", " ", label).strip()[:220]})

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text or "", "html.parser")
        for node in soup.select("a[href], area[href], link[href]"):
            add(str(node.get("href") or ""), node.get_text(" ", strip=True) or str(node.get("rel") or ""))
            if len(links) >= limit:
                return links
    except Exception:
        pass

    for match in re.finditer(r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", text or "", flags=re.I | re.S):
        label = re.sub(r"<[^>]+>", " ", match.group(2))
        add(match.group(1), label)
        if len(links) >= limit:
            return links
    for url in _extract_links(text or "", limit=max(0, limit - len(links))):
        add(url, "")
        if len(links) >= limit:
            return links
    for email_record in _extract_email_records(text or "", max_results=max(0, limit - len(links)), include_context=False):
        add("mailto:" + email_record["email"], email_record["email"])
    return links


def _extract_html_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text or "", flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))).strip()[:200]


def _html_to_readable_text(markup: str, *, max_chars: int = 30000, remove_chrome: bool = True) -> str:
    value = str(markup or "")
    if max_chars <= 0:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(value, "html.parser")
        selectors = ["script", "style", "noscript", "template", "svg"]
        if remove_chrome:
            selectors.extend(["header", "footer", "nav"])
        for node in soup.select(",".join(selectors)):
            node.decompose()
        for node in soup.select("br,p,li,h1,h2,h3,h4,h5,h6,tr"):
            node.append("\n")
        text = soup.get_text("\n")
    except Exception:
        text = re.sub(r"<(script|style|noscript|template|svg)\b.*?</\1>", " ", value, flags=re.I | re.S)
        text = re.sub(r"<br\s*/?>|</?(p|li|h[1-6]|tr|div|section|article)\b[^>]*>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    lines: List[str] = []
    seen: set[str] = set()
    for line in html.unescape(text).splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(normalized)
        if sum(len(item) + 1 for item in lines) >= max_chars:
            break
    return "\n".join(lines)[:max_chars]


def _looks_like_social_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    social_hosts = (
        "linkedin.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "github.com",
        "youtube.com",
        "tiktok.com",
        "crunchbase.com",
    )
    return any(host == item or host.endswith("." + item) for item in social_hosts)


def _search_cve_records(cve_ids: List[str], *, limit: int) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    if limit <= 0:
        return [], {"source": "cve_records", "skipped": True}
    results: List[Dict[str, str]] = []
    for cve_id in cve_ids:
        parts = cve_id.split("-")
        year = parts[1]
        number = parts[2]
        prefix = f"{number[:-3]}xxx" if len(number) > 3 else "0xxx"
        candidates = [
            {
                "title": f"NVD - {cve_id}",
                "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                "snippet": f"National Vulnerability Database detail page for {cve_id}.",
                "source": "cve_records",
            },
            {
                "title": f"CVE.org - {cve_id}",
                "url": f"https://www.cve.org/CVERecord?id={cve_id}",
                "snippet": f"Official CVE Program record for {cve_id}.",
                "source": "cve_records",
            },
            {
                "title": f"NVD API - {cve_id}",
                "url": f"https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cve_id}",
                "snippet": f"Machine-readable NVD JSON record for {cve_id}.",
                "source": "cve_records",
            },
            {
                "title": f"CVE List JSON - {cve_id}",
                "url": f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/{year}/{prefix}/{cve_id}.json",
                "snippet": f"Machine-readable CVE List v5 JSON record for {cve_id}.",
                "source": "cve_records",
            },
        ]
        for candidate in candidates:
            results.append(candidate)
            if len(results) >= limit:
                return results, {"source": "cve_records", "cve_ids": cve_ids}
    return results, {"source": "cve_records", "cve_ids": cve_ids}


def _search_wikipedia_api(query: str, *, limit: int, timeout: float) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    if limit <= 0:
        return [], {"source": "wikipedia_api", "skipped": True}
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests is not installed") from exc

    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "opensearch",
        "search": query,
        "limit": min(max(limit, 1), 10),
        "namespace": 0,
        "format": "json",
    }
    response = requests.get(url, params=params, headers=_browser_headers(), timeout=timeout)
    attempt = {"source": "wikipedia_api", "status": response.status_code, "url": response.url, "chars": len(response.text)}
    response.raise_for_status()
    payload = response.json()
    titles = payload[1] if len(payload) > 1 and isinstance(payload[1], list) else []
    snippets = payload[2] if len(payload) > 2 and isinstance(payload[2], list) else []
    urls = payload[3] if len(payload) > 3 and isinstance(payload[3], list) else []
    results: List[Dict[str, str]] = []
    for title, snippet, result_url in zip(titles, snippets, urls):
        _append_search_result(
            results,
            title=str(title),
            url=str(result_url),
            snippet=str(snippet),
            source="wikipedia_api",
            limit=limit,
        )
    return results, attempt


def _search_pubmed_api(query: str, *, limit: int, timeout: float) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    if limit <= 0:
        return [], {"source": "pubmed_api", "skipped": True}
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests is not installed") from exc

    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": min(max(limit, 1), 10),
        "sort": "relevance",
    }
    search_response = requests.get(search_url, params=search_params, headers=_browser_headers(), timeout=timeout)
    attempt = {
        "source": "pubmed_api",
        "status": search_response.status_code,
        "url": search_response.url,
        "chars": len(search_response.text),
    }
    search_response.raise_for_status()
    ids = (search_response.json().get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return [], attempt

    summary_response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        headers=_browser_headers(),
        timeout=timeout,
    )
    attempt["summary_status"] = summary_response.status_code
    attempt["summary_chars"] = len(summary_response.text)
    summary_response.raise_for_status()
    payload = summary_response.json().get("result") or {}
    results: List[Dict[str, str]] = []
    for pubmed_id in ids:
        item = payload.get(str(pubmed_id)) or {}
        title = str(item.get("title") or f"PubMed {pubmed_id}")
        pubdate = str(item.get("pubdate") or "")
        source = str(item.get("source") or "")
        snippet = " ".join(part for part in [source, pubdate] if part)
        _append_search_result(
            results,
            title=title,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_id}/",
            snippet=snippet,
            source="pubmed_api",
            limit=limit,
        )
    return results, attempt


def _search_crossref_api(query: str, *, limit: int, timeout: float) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    if limit <= 0:
        return [], {"source": "crossref_api", "skipped": True}
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests is not installed") from exc

    response = requests.get(
        "https://api.crossref.org/works",
        params={"query": query, "rows": min(max(limit, 1), 10), "select": "title,URL,DOI,container-title,published-print,published-online"},
        headers=_browser_headers(),
        timeout=timeout,
    )
    attempt = {"source": "crossref_api", "status": response.status_code, "url": response.url, "chars": len(response.text)}
    response.raise_for_status()
    items = ((response.json().get("message") or {}).get("items") or [])[:limit]
    results: List[Dict[str, str]] = []
    for item in items:
        title_values = item.get("title") or []
        title = str(title_values[0] if title_values else item.get("DOI") or "Crossref result")
        container_values = item.get("container-title") or []
        container = str(container_values[0] if container_values else "")
        result_url = str(item.get("URL") or "")
        _append_search_result(
            results,
            title=title,
            url=result_url,
            snippet=container,
            source="crossref_api",
            limit=limit,
        )
    return results, attempt


def _extract_links(text: str, pattern: Optional[str] = None, limit: int = 1000) -> List[str]:
    if limit <= 0:
        return []
    compiled = re.compile(pattern) if pattern else None
    seen: set[str] = set()
    links: List[str] = []
    link_re = re.compile(
        r"\[[^\]]{0,300}\]\((https?://[^)\s]+)\)"
        r"|<loc[^>]*>\s*(https?://[^<\s]+)"
        r"|(https?://[^\s<>\]\)\"']+)",
        flags=re.I,
    )
    for match in link_re.finditer(text):
        url = html.unescape(next(group for group in match.groups() if group)).strip()
        url = url.rstrip(".,;")
        if not url or url in seen:
            continue
        if compiled and not compiled.search(url):
            continue
        seen.add(url)
        links.append(url)
        if len(links) >= limit:
            return links
    return links


def _extract_markdown_link_blocks(
    text: str,
    *,
    url_pattern: Optional[str] = None,
    max_lines_after: int = 8,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    compiled = re.compile(url_pattern) if url_pattern else None
    lines = text.splitlines()
    link_re = re.compile(r"^\s*(?:[-*]\s*)?\[([^\]]{1,300})\]\((https?://[^)\s]+)\)\s*$")
    cards: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = link_re.match(line.strip())
        if not match:
            continue
        title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
        url = html.unescape(match.group(2)).strip()
        if compiled and not compiled.search(url):
            continue

        following: List[str] = []
        for next_line in lines[index + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if link_re.match(stripped) and following:
                break
            following.append(stripped)
            if len(following) >= max_lines_after:
                break
        cards.append({"title": title, "url": url, "lines": following})
        if len(cards) >= limit:
            break
    return cards


__all__ = [
    name
    for name, value in list(globals().items())
    if name.startswith("_") and not name.startswith("__") and callable(value)
]
