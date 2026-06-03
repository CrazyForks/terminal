"""Browser-script helpers.

Rust owns the CDP websocket and session state. This file owns the
LLM-readable browser interaction helpers. Keep these helpers close to
browser-harness semantics so the model sees one coherent browser API.
"""

import base64
import concurrent.futures
import csv
import fnmatch
import gzip
import html
import io
import ipaddress
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time as _time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode, urljoin, urlparse


INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")
__last_domain_skills = []


def _send_meta(meta, **params):
    return _bridge({"kind": "meta", "meta": meta, **params})


def cdp(method, session_id=None, **params):
    """Raw CDP. Example: cdp("Page.navigate", url="https://example.com")."""
    if method == "Page.navigate" and "url" in params:
        _ensure_navigation_allowed(params.get("url"))
    return _bridge({"kind": "cdp", "method": method, "session_id": session_id, "params": params})


def _env_bool(name):
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _env_ms(name, default_ms):
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default_ms
    try:
        return max(0, int(float(value)))
    except Exception:
        return default_ms


def _browser_minimum_wait_page_load_seconds():
    return _env_ms("BU_BROWSER_MINIMUM_WAIT_PAGE_LOAD_MS", 0) / 1000


def _browser_network_idle_ms():
    return _env_ms("BU_BROWSER_NETWORK_IDLE_PAGE_LOAD_MS", 500)


def _browser_wait_between_actions_seconds():
    return _env_ms("BU_BROWSER_WAIT_BETWEEN_ACTIONS_MS", 0) / 1000


def _browser_block_ip_addresses_enabled():
    return _env_bool("BU_BROWSER_BLOCK_IP_ADDRESSES") is True


def _env_string_list(name):
    raw = os.environ.get(name)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _ip_literal_host(url):
    try:
        value = str(url or "").strip()
        parsed = urlparse(value if "://" in value else f"https://{value}")
        host = parsed.hostname
        if not host:
            return None
        ipaddress.ip_address(host.split("%", 1)[0])
        return host
    except Exception:
        return None


def _ensure_ip_navigation_allowed(url):
    if _browser_block_ip_addresses_enabled():
        host = _ip_literal_host(url)
        if host:
            raise RuntimeError(f"BrowserProfile.block_ip_addresses blocked IP address host: {host}")
    return url


def _is_root_domain(domain):
    return "*" not in domain and "://" not in domain and domain.count(".") == 1


def _domain_pattern_matches(url, host, scheme, pattern):
    pattern = str(pattern or "").strip()
    if not pattern:
        return False
    full_url_pattern = f"{scheme}://{host}"
    if "*" in pattern:
        if pattern.startswith("*."):
            domain_part = pattern[2:].lower()
            host_lower = host.lower()
            return scheme in ("http", "https") and (host_lower == domain_part or host_lower.endswith("." + domain_part))
        if pattern.endswith("/*") and url.startswith(pattern[:-1]):
            return True
        target = full_url_pattern if "://" in pattern else host
        return fnmatch.fnmatch(target, pattern)
    if "://" in pattern:
        return url.startswith(pattern)
    host_lower = host.lower()
    pattern_lower = pattern.lower()
    if host_lower == pattern_lower:
        return True
    return _is_root_domain(pattern) and host_lower == f"www.{pattern_lower}"


def _browser_domain_constraints_allow_url(url):
    allowed = _env_string_list("BU_BROWSER_ALLOWED_DOMAINS")
    prohibited = _env_string_list("BU_BROWSER_PROHIBITED_DOMAINS")
    if not allowed and not prohibited:
        return True
    value = str(url or "").strip()
    if value in ("about:blank", "chrome://new-tab-page/", "chrome://new-tab-page", "chrome://newtab/"):
        return True
    try:
        parsed = urlparse(value if "://" in value else f"https://{value}")
    except Exception:
        return False
    if parsed.scheme in ("data", "blob"):
        return True
    host = parsed.hostname
    if not host:
        return False
    if allowed:
        return any(_domain_pattern_matches(value, host, parsed.scheme, pattern) for pattern in allowed)
    return not any(_domain_pattern_matches(value, host, parsed.scheme, pattern) for pattern in prohibited)


def _ensure_navigation_allowed(url):
    _ensure_ip_navigation_allowed(url)
    if not _browser_domain_constraints_allow_url(url):
        raise RuntimeError(f"BrowserProfile domain constraints blocked URL: {url}")
    return url


def _after_browser_action_wait():
    seconds = _browser_wait_between_actions_seconds()
    if seconds > 0:
        _time.sleep(seconds)


def _configured_viewport_params(default=True):
    if _env_bool("BU_BROWSER_NO_VIEWPORT") is True:
        return None
    raw = os.environ.get("BU_BROWSER_VIEWPORT")
    if raw:
        try:
            value = json.loads(raw)
            width = int(value["width"])
            height = int(value["height"])
            if width <= 0 or height <= 0:
                raise ValueError("invalid viewport size")
            params = {
                "width": width,
                "height": height,
                "deviceScaleFactor": float(value.get("deviceScaleFactor", 1)),
                "mobile": False,
            }
            if "screenWidth" in value:
                params["screenWidth"] = int(value["screenWidth"])
            if "screenHeight" in value:
                params["screenHeight"] = int(value["screenHeight"])
            return params
        except Exception:
            pass
    if default:
        return {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False}
    return None


def cdp_batch(calls):
    out = []
    for call in calls:
        if isinstance(call, dict):
            call = dict(call)
            method = call.pop("method")
            session_id = call.pop("session_id", None)
            out.append(cdp(method, session_id=session_id, **call))
        else:
            method, params = call
            out.append(cdp(method, **params))
    return out


def drain_events():
    return _send_meta("drain_events").get("events", [])


def _js_snippet(expression, limit=160):
    snippet = expression.strip().replace("\n", "\\n")
    return snippet[: limit - 3] + "..." if len(snippet) > limit else snippet


def _js_exception_description(result, details):
    desc = result.get("description")
    exc = details.get("exception") if details else None
    if not desc and isinstance(exc, dict):
        desc = exc.get("description")
        if desc is None and "value" in exc:
            desc = str(exc["value"])
        if desc is None:
            desc = exc.get("className")
    if not desc and details:
        desc = details.get("text")
    return desc or "JavaScript evaluation failed"


def _decode_unserializable_js_value(value):
    if value == "NaN":
        return math.nan
    if value == "Infinity":
        return math.inf
    if value == "-Infinity":
        return -math.inf
    if value == "-0":
        return -0.0
    if value.endswith("n"):
        return int(value[:-1])
    return value


def _runtime_value(response, expression):
    result = response.get("result", {})
    details = response.get("exceptionDetails")
    if details or result.get("subtype") == "error":
        desc = _js_exception_description(result, details)
        if details:
            line = details.get("lineNumber")
            col = details.get("columnNumber")
            loc = f" at line {line}, column {col}" if line is not None and col is not None else ""
        else:
            loc = ""
        raise RuntimeError(f"JavaScript evaluation failed{loc}: {desc}; expression: {_js_snippet(expression)}")
    if "value" in result:
        return result["value"]
    if "unserializableValue" in result:
        return _decode_unserializable_js_value(result["unserializableValue"])
    return None


def _runtime_evaluate(expression, session_id=None, await_promise=False, return_by_value=True):
    try:
        response = cdp(
            "Runtime.evaluate",
            session_id=session_id,
            expression=expression,
            returnByValue=return_by_value,
            awaitPromise=await_promise,
        )
    except TimeoutError as exc:
        raise RuntimeError(f"Runtime.evaluate timed out; expression: {_js_snippet(expression)}") from exc
    return _runtime_value(response, expression)


def _has_return_statement(expression):
    i = 0
    n = len(expression)
    state = "code"
    quote = ""
    brace_stack = []
    pending_function_body = False
    pending_arrow_body = False

    def is_ident(ch):
        return ch == "_" or ch == "$" or ch.isalnum()

    def is_keyword_at(keyword, pos):
        if not expression.startswith(keyword, pos):
            return False
        before = expression[pos - 1] if pos > 0 else ""
        after_pos = pos + len(keyword)
        after = expression[after_pos] if after_pos < n else ""
        return not is_ident(before) and not is_ident(after)

    def next_nonspace(pos):
        while pos < n and expression[pos].isspace():
            pos += 1
        return expression[pos] if pos < n else ""

    while i < n:
        ch = expression[i]
        nxt = expression[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch in ("'", '"', "`"):
                state = "string"
                quote = ch
                i += 1
                continue
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
                continue
            if ch == "=" and nxt == ">":
                pending_arrow_body = True
                i += 2
                continue
            if pending_arrow_body and not ch.isspace() and ch != "{":
                pending_arrow_body = False
            if ch == "{":
                brace_stack.append(pending_function_body or pending_arrow_body)
                pending_function_body = False
                pending_arrow_body = False
                i += 1
                continue
            if ch == "}":
                if brace_stack:
                    brace_stack.pop()
                i += 1
                continue
            if is_keyword_at("function", i):
                pending_function_body = True
                i += len("function")
                continue
            if is_keyword_at("return", i):
                inside_nested_function = any(brace_stack)
                looks_like_property_key = next_nonspace(i + len("return")) == ":"
                if not inside_nested_function and not looks_like_property_key:
                    return True
            i += 1
            continue
        if state == "line_comment":
            if ch == "\n":
                state = "code"
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
            i += 1
            continue
        if state == "string":
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                state = "code"
                quote = ""
            i += 1
            continue
    return False


def js(expression, target_id=None, returnByValue=True):
    """Run JS in the attached tab, or in an iframe target via target_id.

    Expressions with top-level `return` are wrapped in an IIFE, so both
    `document.title` and `const x = 1; return x` are valid.
    """
    session_id = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"] if target_id else None
    if _has_return_statement(expression) and not expression.strip().startswith("("):
        expression = f"(function(){{{expression}}})()"
    return _runtime_evaluate(
        expression,
        session_id=session_id,
        await_promise=True,
        return_by_value=returnByValue,
    )


def browser_fetch(url, headers=None, method="GET", body=None, timeout=20.0, binary=False):
    """Fetch from the current page context, preserving browser credentials."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme in ("http", "https"):
        _ensure_navigation_allowed(url)
    expression = f"""
(async () => {{
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), {int(float(timeout) * 1000)});
  try {{
    const response = await fetch({json.dumps(url)}, {{
      method: {json.dumps(method)},
      headers: {json.dumps(headers or {})},
      body: {json.dumps(body) if body is not None else "undefined"},
      credentials: 'include',
      signal: controller.signal,
    }});
    const responseHeaders = Object.fromEntries(Array.from(response.headers.entries()));
    const out = {{
      ok: response.ok,
      status_code: response.status,
      status: response.status,
      status_text: response.statusText,
      url: response.url,
      headers: responseHeaders,
    }};
    if ({json.dumps(bool(binary))}) {{
      const bytes = new Uint8Array(await response.arrayBuffer());
      let raw = '';
      for (let i = 0; i < bytes.length; i += 0x8000) {{
        raw += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }}
      out.content_base64 = btoa(raw);
    }} else {{
      out.text = await response.text();
      if ((responseHeaders['content-type'] || '').toLowerCase().includes('json')) {{
        try {{ out.json = JSON.parse(out.text); }} catch (err) {{}}
      }}
    }}
    return out;
  }} finally {{
    clearTimeout(timeoutId);
  }}
}})()
"""
    return js(expression)


def browser_fetch_many(urls, headers=None, method="GET", body=None, timeout=20.0, binary=False, max_concurrent=8):
    """Fetch many URLs from the current page context with browser credentials."""
    urls = list(urls or [])
    fetch_items = []
    precomputed = {}
    for index, url in enumerate(urls):
        parsed = urlparse(str(url or ""))
        if parsed.scheme in ("http", "https"):
            try:
                _ensure_navigation_allowed(url)
            except Exception as exc:
                precomputed[index] = {"index": index, "url": url, "ok": False, "error": str(exc)}
                continue
        fetch_items.append({"index": index, "url": url})
    if not fetch_items:
        return [precomputed.get(index) for index in range(len(urls))]

    expression = f"""
(async () => {{
  const items = {json.dumps(fetch_items)};
  const maxConcurrent = Math.max(1, Math.min(Number({int(max_concurrent or 1)}) || 1, 16));
  const requestInit = {{
    method: {json.dumps(method)},
    headers: {json.dumps(headers or {})},
    body: {json.dumps(body) if body is not None else "undefined"},
    credentials: 'include',
  }};
  const timeoutMs = {int(float(timeout) * 1000)};
  const binary = {json.dumps(bool(binary))};
  const encodeBytes = (bytes) => {{
    let raw = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {{
      raw += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }}
    return btoa(raw);
  }};
  const fetchOne = async (item) => {{
    const index = item.index;
    const url = item.url;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {{
      const response = await fetch(url, {{...requestInit, signal: controller.signal}});
      const responseHeaders = Object.fromEntries(Array.from(response.headers.entries()));
      const out = {{
        index,
        url,
        final_url: response.url,
        ok: response.ok,
        status_code: response.status,
        status: response.status,
        status_text: response.statusText,
        headers: responseHeaders,
      }};
      if (binary) {{
        out.content_base64 = encodeBytes(new Uint8Array(await response.arrayBuffer()));
      }} else {{
        out.text = await response.text();
        if ((responseHeaders['content-type'] || '').toLowerCase().includes('json')) {{
          try {{ out.json = JSON.parse(out.text); }} catch (err) {{}}
        }}
      }}
      return out;
    }} catch (err) {{
      return {{index, url, ok: false, error: String(err && (err.message || err))}};
    }} finally {{
      clearTimeout(timeoutId);
    }}
  }};
  const results = new Array(items.length);
  let next = 0;
  const workers = Array.from({{length: Math.min(maxConcurrent, items.length)}}, async () => {{
    while (next < items.length) {{
      const slot = next++;
      results[slot] = await fetchOne(items[slot]);
    }}
  }});
  await Promise.all(workers);
  return results;
}})()
"""
    fetched = js(expression)
    results = [precomputed.get(index) for index in range(len(urls))]
    for record in fetched or []:
        if isinstance(record, dict):
            index = record.get("index")
            if isinstance(index, int) and 0 <= index < len(results):
                results[index] = record
    return results


def _truthy_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _domain_skill_roots():
    roots = []
    configured = os.environ.get("BH_DOMAIN_SKILLS_ROOT") or os.environ.get("BH_DOMAIN_SKILLS_DIR")
    if configured:
        roots.extend(pathlib.Path(part).expanduser() for part in configured.split(os.pathsep) if part.strip())
    for root in globals().get("DOMAIN_SKILL_ROOTS", []):
        roots.append(pathlib.Path(root).expanduser())
    try:
        roots.append(pathlib.Path(agent_workspace()) / "domain-skills")
    except Exception:
        pass

    seen = set()
    out = []
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_dir():
            out.append(resolved)
    return out


def _domain_from_url(value):
    value = str(value or "").strip()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or value.split("/", 1)[0]).strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _domain_skill_aliases(url_or_domain):
    host = _domain_from_url(url_or_domain)
    aliases = {host, host.replace(".", "-")}
    labels = [part for part in host.split(".") if part]
    if labels:
        aliases.add(labels[0])
    if len(labels) >= 2:
        aliases.add(labels[-2])
        aliases.add(f"{labels[-2]}-{labels[-1]}")
    if len(labels) >= 3:
        aliases.add(f"{labels[-2]}-{labels[0]}")
        aliases.add(f"{labels[0]}-{labels[-2]}")
    return {alias.lower().replace("_", "-") for alias in aliases if alias}


def _domain_skills_enabled():
    if os.environ.get("BH_DOMAIN_SKILLS") is not None:
        return _truthy_env("BH_DOMAIN_SKILLS")
    return bool(_domain_skill_roots())


def domain_skills_for_url(url_or_domain, include_content=False, max_files=10, max_bytes=120000):
    """Return matching browser-harness domain-skill files for a URL/domain.

    Set include_content=True when the task is site-specific and the model needs
    the playbook before inventing selectors, private API routes, or flows.
    """
    aliases = _domain_skill_aliases(url_or_domain)
    matches = []
    remaining = int(max_bytes)
    for root in _domain_skill_roots():
        try:
            entries = sorted(root.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            continue
        for site_dir in entries:
            if not site_dir.is_dir():
                continue
            site_key = site_dir.name.lower().replace("_", "-")
            if site_key not in aliases:
                continue
            files = []
            for path in sorted(site_dir.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in (".md", ".py"):
                    continue
                rel = path.relative_to(site_dir).as_posix()
                item = {"name": rel, "path": str(path)}
                if include_content and remaining > 0:
                    try:
                        content = path.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        content = f"[failed to read domain skill: {exc}]"
                    encoded = content[:remaining]
                    item["content"] = encoded
                    item["truncated"] = len(encoded) < len(content)
                    remaining -= len(encoded)
                files.append(item)
                if len(files) >= max_files:
                    break
            if files:
                matches.append({"site": site_dir.name, "root": str(root), "files": files})
    return matches


def last_domain_skills(include_content=False):
    if not __last_domain_skills:
        return []
    if include_content:
        url = __last_domain_skills[0].get("url") if isinstance(__last_domain_skills[0], dict) else None
        if url:
            return domain_skills_for_url(url, include_content=True)
    return __last_domain_skills


def goto_url(url):
    global __last_domain_skills
    url = _ensure_navigation_allowed(url)
    result = cdp("Page.navigate", url=url)
    __last_domain_skills = []
    if _domain_skills_enabled():
        skills = domain_skills_for_url(url, include_content=False)
        if skills:
            __last_domain_skills = [{"url": url, **skill} for skill in skills]
            result = {**result, "domain_skills": __last_domain_skills}
    wait_for_load(timeout=15)
    minimum_wait = _browser_minimum_wait_page_load_seconds()
    if minimum_wait > 0:
        _time.sleep(minimum_wait)
    return result


def page_info():
    """Return url, title, viewport, scroll position, page size, and target info."""
    dialog = _send_meta("pending_dialog").get("dialog")
    if dialog:
        return {"dialog": dialog}
    expression = (
        "(()=>{"
        "const root=document.documentElement||document.body||{};"
        "return JSON.stringify({url:location.href,title:document.title||'',readyState:document.readyState||'',"
        "w:innerWidth,h:innerHeight,sx:scrollX||0,sy:scrollY||0,"
        "pw:root.scrollWidth||innerWidth,ph:root.scrollHeight||innerHeight});"
        "})()"
    )
    info = json.loads(_runtime_evaluate(expression))
    info["target"] = current_tab()
    return info


def navigation_snapshot(keywords=None, limit=80):
    """Return visible navigation links and menu-like controls with relevance scores.

    Use this before deciding a site lacks a listing/document section. It surfaces
    anchors, nav/menu links, and collapsed-menu buttons so the next action can be
    based on the live DOM rather than screenshots or guesswork.
    """
    if keywords is None:
        keywords = [
            "properties",
            "property",
            "rentals",
            "vacation rentals",
            "listings",
            "browse",
            "available",
            "investors",
            "investor",
            "reports",
            "financial",
            "documents",
            "filings",
            "search",
            "results",
        ]
    expression = f"""
(() => {{
  const keywords = {json.dumps([str(k).lower() for k in (keywords or [])])};
  const limit = {int(limit)};
  const clean = (text, max = 260) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const visible = (el) => {{
    if (!el || !(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
      && rect.width >= 4 && rect.height >= 4;
  }};
  const nearestArea = (el) => {{
    const area = el.closest('nav,header,footer,main,aside,[role="navigation"],[role="menu"],[role="menubar"],[aria-label]');
    if (!area) return '';
    const tag = (area.tagName || '').toLowerCase();
    const role = area.getAttribute('role') || '';
    const label = area.getAttribute('aria-label') || area.id || '';
    return clean([tag, role, label].filter(Boolean).join(' '), 120);
  }};
  const textFor = (el) => clean([
    el.innerText || el.textContent || el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    el.getAttribute('alt') || '',
    ...Array.from(el.querySelectorAll('img[alt]')).map(img => img.getAttribute('alt') || ''),
  ].filter(Boolean).join(' '), 260);
  const hrefFor = (el) => {{
    if (el.matches('a[href],area[href]')) return el.href;
    const nested = el.querySelector('a[href],area[href]');
    return nested ? nested.href : '';
  }};
  const stableAttrs = (el) => {{
    const attrs = {{}};
    for (const name of ['id', 'role', 'aria-label', 'aria-expanded', 'aria-controls', 'data-testid', 'data-test', 'data-cy', 'data-component', 'title']) {{
      const value = clean(el.getAttribute(name), 180);
      if (value) attrs[name] = value;
    }}
    return attrs;
  }};
  const selectorFor = (el) => {{
    const tag = (el.tagName || '').toLowerCase();
    if (!tag) return '';
    for (const attr of ['data-testid', 'data-test', 'data-cy', 'aria-controls', 'aria-label']) {{
      const value = el.getAttribute(attr);
      if (value && value.length <= 80) return `${{tag}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    if (el.id) return `${{tag}}#${{CSS.escape(el.id)}}`;
    const cls = Array.from(el.classList || []).find(c => c && c.length <= 80);
    return cls ? `${{tag}}.${{CSS.escape(cls)}}` : tag;
  }};
  const keywordScore = (text, href, area, attrs) => {{
    const haystack = [text, href, area, Object.values(attrs).join(' ')].join(' ').toLowerCase();
    let score = 0;
    const matches = [];
    for (const keyword of keywords) {{
      if (keyword && haystack.includes(keyword)) {{
        matches.push(keyword);
        score += keyword.includes(' ') ? 5 : 3;
      }}
    }}
    if (/\\b(menu|hamburger|toggle|open|more)\\b/i.test(haystack) && !href) score += 2;
    if (/\\b(pdf|docx?|xlsx?|download|filing|report|annual|quarterly)\\b/i.test(haystack)) score += 2;
    if (/\\b(properties|property|rentals|listings|investors?|reports?)\\b/i.test(haystack)) score += 4;
    return {{score, matches: Array.from(new Set(matches)).slice(0, 8)}};
  }};
  const nodes = Array.from(document.querySelectorAll(
    'a[href],area[href],nav a[href],header a[href],footer a[href],[role="navigation"] a[href],button,[role="button"],[aria-expanded],[aria-controls],[role="menuitem"],[role="link"]'
  ));
  const seen = new Set();
  const records = [];
  for (const el of nodes) {{
    if (!visible(el)) continue;
    const text = textFor(el);
    const href = hrefFor(el);
    const attrs = stableAttrs(el);
    if (!text && !href && !Object.keys(attrs).length) continue;
    const key = `${{href}}|${{text}}|${{attrs.id || ''}}|${{attrs['aria-controls'] || ''}}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const area = nearestArea(el);
    const relevance = keywordScore(text, href, area, attrs);
    records.push({{
      text,
      href,
      tag: (el.tagName || '').toLowerCase(),
      area,
      selector: selectorFor(el),
      attributes: attrs,
      relevance_score: relevance.score,
      keyword_matches: relevance.matches,
    }});
  }}
  records.sort((a, b) => b.relevance_score - a.relevance_score || Number(Boolean(b.href)) - Number(Boolean(a.href)) || a.text.localeCompare(b.text));
  return {{
    url: location.href,
    title: document.title || '',
    keywords,
    recommended: records.filter(r => r.relevance_score > 0).slice(0, Math.min(12, limit)),
    links: records.slice(0, limit),
  }};
}})()
"""
    return js(expression)


def sitemap_urls_snapshot(url_or_domain=None, keywords=None, limit=80, max_sitemaps=8, timeout=10.0):
    """Discover public routes from robots.txt and XML sitemaps.

    Use this before deciding a site lacks listings, products, documents,
    investor pages, search/results pages, or other public route families.
    """
    source = str(url_or_domain or "")
    if not source:
        try:
            info = page_info()
            source = info.get("url") or ""
        except Exception:
            source = ""
    if not source:
        return {"origin": "", "count": 0, "recommended": [], "urls": [], "diagnosis": "no current URL or domain"}
    parsed = urlparse(source if "://" in source else f"https://{source}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}".rstrip("/")
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    terms = [str(term).lower() for term in raw_keywords if str(term).strip()]
    if not terms:
        terms = [
            "properties",
            "property",
            "rentals",
            "vacation-rentals",
            "listing",
            "listings",
            "stays",
            "homes",
            "apartments",
            "suites",
            "accommodations",
            "availability",
            "book",
            "booking",
            "products",
            "catalog",
            "tickets",
            "investor",
            "documents",
            "reports",
            "search",
            "results",
        ]

    def absolute_url(value):
        text = str(value or "").strip()
        if not text:
            return ""
        return urljoin(origin + "/", text)

    def url_score(value):
        lower = str(value or "").lower()
        score = sum(10 for term in terms if term and term in lower)
        if re.search(r"/(?:property|properties|rentals?|vacation-rentals?|stays?|homes?|apartments?|suites?|accommodations?|availability|book(?:ing)?|listings?|products?|catalog|tickets?|investors?|documents?|reports?|search|results?)(?:[/?#-]|$)", lower):
            score += 12
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?)($|[?#])", lower):
            score -= 20
        return score

    sitemap_candidates = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
    robots_url = f"{origin}/robots.txt"
    try:
        robots = str(http_get(robots_url, timeout=timeout))
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_candidates.append(absolute_url(line.split(":", 1)[1].strip()))
    except Exception:
        pass

    seen_sitemaps = set()
    seen_urls = {}

    def parse_sitemap(sitemap_url, depth=0):
        if not sitemap_url or sitemap_url in seen_sitemaps or len(seen_sitemaps) >= int(max_sitemaps):
            return
        seen_sitemaps.add(sitemap_url)
        try:
            text = str(http_get(sitemap_url, timeout=timeout))
        except Exception:
            return
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except Exception:
            for match in re.findall(r"https?://[^\s<>\"]+", text):
                score = url_score(match)
                seen_urls.setdefault(match, {"url": match, "score": score, "source": sitemap_url})
            return
        for loc in root.findall(".//{*}sitemap/{*}loc"):
            child = absolute_url(loc.text)
            if depth < 1:
                parse_sitemap(child, depth + 1)
        for url_node in root.findall(".//{*}url"):
            loc = url_node.find("{*}loc")
            route = absolute_url(loc.text if loc is not None else "")
            if not route:
                continue
            lastmod = url_node.find("{*}lastmod")
            score = url_score(route)
            seen_urls.setdefault(
                route,
                {
                    "url": route,
                    "score": score,
                    "source": sitemap_url,
                    "lastmod": "" if lastmod is None or lastmod.text is None else lastmod.text.strip(),
                },
            )

    for sitemap_url in sitemap_candidates:
        parse_sitemap(sitemap_url)

    routes = sorted(seen_urls.values(), key=lambda item: (-item.get("score", 0), item.get("url", "")))[: int(limit)]
    recommended = [item for item in routes if item.get("score", 0) > 0][: min(12, int(limit))]
    return {
        "origin": origin,
        "keywords": terms,
        "sitemaps_checked": sorted(seen_sitemaps),
        "count": len(routes),
        "recommended": recommended,
        "urls": routes,
    }


def route_candidates_snapshot(url_or_domain=None, keywords=None, limit=80, max_scripts=12, timeout=10.0):
    """Discover likely public routes from visible links, scripts, and route manifests.

    Use after navigation/sitemap checks when an SPA may hide listings, booking,
    products, documents, or search pages in JavaScript route strings.
    """
    source = str(url_or_domain or "")
    if not source:
        try:
            info = page_info()
            source = info.get("url") or ""
        except Exception:
            source = ""
    if not source:
        return {"origin": "", "count": 0, "recommended": [], "routes": [], "diagnosis": "no current URL or domain"}
    parsed = urlparse(source if "://" in source else f"https://{source}")
    host = parsed.netloc or parsed.path
    origin = f"{parsed.scheme or 'https'}://{host}".rstrip("/")
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    terms = [str(term).lower() for term in raw_keywords if str(term).strip()]
    if not terms:
        terms = [
            "properties",
            "property",
            "rentals",
            "vacation-rentals",
            "listings",
            "stays",
            "homes",
            "apartments",
            "suites",
            "accommodations",
            "booking",
            "book",
            "availability",
            "search",
            "results",
            "products",
            "catalog",
            "documents",
            "reports",
            "investor",
        ]
    limit_int = max(1, min(int(limit or 80), 300))
    max_scripts_int = max(0, min(int(max_scripts or 12), 40))

    def absolute_url(value):
        text = str(value or "").strip()
        if not text:
            return ""
        return urljoin(origin + "/", text)

    def score_url(value, evidence=""):
        lower = f"{value} {evidence}".lower()
        score = sum(10 for term in terms if term and term in lower)
        if re.search(r"/(?:propert(?:y|ies)|rentals?|vacation-rentals?|stays?|homes?|apartments?|suites?|accommodations?|listings?|book(?:ing)?|availability|search|results?|products?|catalog|documents?|reports?|investors?)(?:[/?#._-]|$)", lower):
            score += 14
        if re.search(r"\b(book now|reserve|check[- ]?in|guest|available homes?|short[- ]term|stays?)\b", lower):
            score += 8
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|ico|woff2?|map)($|[?#])", lower):
            score -= 30
        if re.search(r"\.(?:js)($|[?#])", lower):
            score -= 8
        return score

    route_re = re.compile(
        r"""(?P<route>(?:https?://[^\s"'<>\\)]+|/(?!/)[A-Za-z0-9][A-Za-z0-9_./%#?=&:-]{1,220}))""",
        re.I,
    )
    interesting_re = re.compile(
        r"(propert(?:y|ies)|rentals?|vacation[-_ ]?rentals?|stays?|homes?|apartments?|suites?|accommodations?|listings?|book(?:ing)?|availability|search|results?|products?|catalog|documents?|reports?|investors?)",
        re.I,
    )
    seen = {}

    def add_candidate(value, source_label, context=""):
        text = str(value or "").strip()
        if not text or not interesting_re.search(f"{text} {context}"):
            return
        url = absolute_url(text)
        parsed_url = urlparse(url)
        if parsed_url.netloc and parsed_url.netloc != urlparse(origin).netloc:
            return
        score = score_url(url, context)
        if score <= 0:
            return
        key = url.split("#", 1)[0]
        current = seen.get(key)
        item = {
            "url": url,
            "score": score,
            "source": source_label,
            "keyword_matches": [term for term in terms if term and term in f"{url} {context}".lower()][:10],
            "context": re.sub(r"\s+", " ", str(context or "")).strip()[:260],
        }
        if current is None or score > current.get("score", 0):
            seen[key] = item

    def scan_text(text, source_label):
        body = str(text or "")
        for match in route_re.finditer(body[:1_500_000]):
            route = match.group("route")
            start = max(0, match.start() - 120)
            end = min(len(body), match.end() + 120)
            add_candidate(route, source_label, body[start:end])

    try:
        dom = js(
            """
(() => {
  const clean = (s, n = 1200) => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
  const attrs = [];
  for (const el of [...document.querySelectorAll('a[href],link[href],script[src],form[action]')]) {
    attrs.push({
      tag: (el.tagName || '').toLowerCase(),
      href: el.href || el.src || el.action || '',
      rel: el.getAttribute('rel') || '',
      type: el.getAttribute('type') || '',
      text: clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || ''),
    });
  }
  const inlineScripts = [...document.querySelectorAll('script:not([src])')]
    .map((el, index) => ({source: `inline-script-${index + 1}`, text: clean(el.textContent || '', 20000)}))
    .filter(item => item.text);
  return {url: location.href, title: document.title || '', attrs, inlineScripts};
})()
"""
        ) or {}
    except Exception as exc:
        dom = {"error": str(exc), "attrs": [], "inlineScripts": []}

    script_urls = []
    for attr in dom.get("attrs", []) if isinstance(dom, dict) else []:
        if not isinstance(attr, dict):
            continue
        href = attr.get("href") or ""
        context = " ".join(str(attr.get(key) or "") for key in ["tag", "rel", "type", "text"])
        add_candidate(href, "dom-attribute", context)
        if attr.get("tag") == "script" and href:
            script_urls.append(absolute_url(href))
    for script in dom.get("inlineScripts", []) if isinstance(dom, dict) else []:
        if isinstance(script, dict):
            scan_text(script.get("text") or "", script.get("source") or "inline-script")

    fetched = []
    for script_url in script_urls[:max_scripts_int]:
        parsed_script = urlparse(script_url)
        if parsed_script.netloc and parsed_script.netloc != urlparse(origin).netloc:
            continue
        try:
            text = str(http_get(script_url, timeout=timeout))
        except Exception:
            continue
        fetched.append(script_url)
        scan_text(text, script_url)

    routes = sorted(seen.values(), key=lambda item: (-item.get("score", 0), item.get("url", "")))[:limit_int]
    recommended = [item for item in routes if item.get("score", 0) >= 10][: min(12, limit_int)]
    return {
        "origin": origin,
        "url": dom.get("url") if isinstance(dom, dict) else source,
        "title": dom.get("title", "") if isinstance(dom, dict) else "",
        "keywords": terms,
        "scripts_fetched": fetched,
        "count": len(routes),
        "recommended": recommended,
        "routes": routes,
    }


def network_resources_snapshot(limit=80, keywords=None):
    """Return page resource/API/form/download URL candidates.

    Use this after navigation or a search/filter action to discover XHR/API,
    document/download, pagination, and form endpoints before clicking through
    pages one by one.
    """
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    keyword_list = [str(k).strip().lower() for k in raw_keywords if str(k).strip()]
    expression = f"""
(() => {{
  // __NETWORK_RESOURCES_SNAPSHOT__
  const limit={int(limit)};
  const keywords={json.dumps(keyword_list)};
  const clean=(t,m=500)=>(t||'').replace(/\\s+/g,' ').trim().slice(0,m);
  const abs=u=>{{try{{return new URL(u, location.href).href}}catch(e){{return ''}}}};
  const extRe=/\\.(?:json|csv|xml|pdf|docx?|xlsx?|zip|rss|atom)(?:$|[?#])/i;
  const apiRe=/(?:\\/api\\/|graphql|search|query|results?|list|catalog|products?|locations?|documents?|download|export|ajax|rest|svc|feed|wp-json|json)/i;
  const urlTokenRe=/(?:https?:\\/\\/[^\\s"'<>\\\\)]+|\\/[A-Za-z0-9_./~:@!$&()*+,;=%?-]*(?:api|graphql|search|query|results?|list|catalog|products?|locations?|documents?|download|export|ajax|rest|svc|feed|wp-json|json|\\.pdf|\\.csv|\\.xml|\\.xlsx?|\\.docx?|\\.zip)[A-Za-z0-9_./~:@!$&()*+,;=%?-]*)/ig;
  const uniq=new Map();
  const add=(source,type,url,extra={{}})=>{{
    const href=abs(url); if(!href) return;
    const text=clean([href, extra.text||'', extra.name||'', extra.rel||'', extra.type||'', extra.initiator_type||''].join(' '),1200);
    let score=0;
    if(apiRe.test(text)) score+=18;
    if(extRe.test(href)) score+=14;
    if(/download|export|document|file|attachment|filing|pdf|csv|xlsx|zip/i.test(text)) score+=10;
    if(/next|page|offset|cursor|limit|per_page|start=|from=/i.test(href)) score+=6;
    for(const kw of keywords) if(kw && text.toLowerCase().includes(kw)) score+=8;
    if(source==='performance') score+=3;
    if(source==='form') score+=5;
    if(source==='anchor') score+=2;
    const key=href+'|'+type+'|'+source;
    const item={{source,type,url:href,score,text:clean(extra.text||'',240),...extra}};
    if(!uniq.has(key) || score>uniq.get(key).score) uniq.set(key, item);
  }};
  for(const e of performance.getEntriesByType('resource')) {{
    add('performance', e.initiatorType||'resource', e.name, {{
      initiator_type:e.initiatorType||'resource',
      transfer_size:Math.round(e.transferSize||0),
      encoded_body_size:Math.round(e.encodedBodySize||0),
      duration_ms:Math.round(e.duration||0),
    }});
  }}
  for(const a of document.querySelectorAll('a[href]')) add('anchor','link',a.href,{{text:clean([a.innerText||a.textContent||'',a.getAttribute('aria-label')||'',a.getAttribute('title')||'',a.getAttribute('download')||'',a.rel||''].filter(Boolean).join(' '),320),rel:a.rel||'',download:a.getAttribute('download')||''}});
  for(const f of document.querySelectorAll('form')) add('form','form',f.getAttribute('action')||location.href,{{method:(f.getAttribute('method')||'GET').toUpperCase(),text:clean([f.getAttribute('aria-label')||'',f.getAttribute('name')||'',f.id||'',f.innerText||''].filter(Boolean).join(' '),320)}});
  for(const link of document.querySelectorAll('link[href][rel]')) add('head','link',link.href,{{rel:link.rel||'',type:link.type||'',text:clean(link.getAttribute('title')||'',160)}});
  for(const script of document.querySelectorAll('script[src]')) add('script','script',script.src,{{type:script.type||''}});
  const scanText=(source,type,text,context='')=>{{
    for(const match of String(text||'').matchAll(urlTokenRe)) {{
      const token=(match[0]||'').replace(/[\\]}}),.;]+$/,'');
      add(source,type,token,{{text:clean(context,180)}});
    }}
  }};
  for(const script of Array.from(document.querySelectorAll('script:not([src])')).slice(0,30)) scanText('inline-script','inline-url',script.textContent||'',script.id||script.type||'');
  for(const el of Array.from(document.querySelectorAll('[data-api],[data-url],[data-href],[data-endpoint],[data-src],[data-download],[data-feed],[data-query]')).slice(0,200)) {{
    for(const attr of el.attributes) if(/^data-/i.test(attr.name)) scanText('data-attribute','data-url',attr.value,attr.name);
  }}
  const items=Array.from(uniq.values()).sort((a,b)=>b.score-a.score||a.url.localeCompare(b.url)).slice(0,limit);
  const by_type={{}};
  for(const item of items) by_type[item.type]=(by_type[item.type]||0)+1;
  return {{url:location.href,title:document.title||'',keywords,count:items.length,by_type,resources:items}};
}})()
"""
    return js(expression)


def json_api_records(urls, records_path=None, limit=200, max_urls=8, use_browser_fetch=False, timeout=20.0):
    """Fetch JSON API URL(s) and return candidate record arrays.

    Use this after `network_resources_snapshot(...)` discovers API/XHR/JSON
    endpoints so agents do not hand-write recursive JSON traversal.
    """
    if isinstance(urls, str):
        url_list = [urls]
    else:
        url_list = [str(url) for url in (urls or []) if str(url).strip()]
    try:
        max_urls_int = max(1, min(int(max_urls or 8), 25))
    except Exception:
        max_urls_int = 8
    url_list = url_list[:max_urls_int]
    try:
        limit_int = max(1, min(int(limit or 200), 1000))
    except Exception:
        limit_int = 200

    def clean(value, max_chars=1200):
        return re.sub(r"\s+", " ", str(value or "")).strip()[:max_chars]

    def path_get(data, path):
        current = data
        if not path:
            return current
        for part in str(path).split("."):
            if part == "":
                continue
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except Exception:
                    return None
            else:
                return None
        return current

    def flatten_record(record):
        if not isinstance(record, dict):
            return {"value": record}
        out = {}
        for key, value in record.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[str(key)] = value
            elif isinstance(value, list):
                out[str(key)] = value[:20]
            elif isinstance(value, dict):
                scalar = {str(k): v for k, v in value.items() if isinstance(v, (str, int, float, bool)) or v is None}
                out[str(key)] = scalar if scalar else clean(json.dumps(value, ensure_ascii=False), 600)
            else:
                out[str(key)] = clean(value, 600)
        return out

    def array_fields(records):
        fields = []
        seen = set()
        for record in records[:20]:
            if isinstance(record, dict):
                keys = record.keys()
            else:
                keys = ["value"]
            for key in keys:
                key = str(key)
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
        return fields[:80]

    def find_arrays(data, path="$", depth=0):
        if depth > 7:
            return []
        found = []
        if isinstance(data, list):
            fields = array_fields(data)
            dict_count = sum(1 for item in data[:50] if isinstance(item, dict))
            score = len(data) * 4 + dict_count * 3 + len(fields)
            if fields:
                score += 10
            found.append(
                {
                    "path": path,
                    "count": len(data),
                    "fields": fields,
                    "score": score,
                    "records": [flatten_record(item) for item in data[:limit_int]],
                }
            )
            for index, item in enumerate(data[:10]):
                if isinstance(item, (dict, list)):
                    found.extend(find_arrays(item, f"{path}.{index}", depth + 1))
        elif isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    found.extend(find_arrays(value, f"{path}.{key}", depth + 1))
        return found

    def parse_payload(payload):
        if isinstance(payload, dict) and "json" in payload and payload.get("json") is not None:
            return payload.get("json")
        if hasattr(payload, "json"):
            return payload.json()
        if isinstance(payload, (dict, list)):
            return payload
        return json.loads(str(payload))

    sources = []
    aggregate = []
    seen_records = set()
    for url in url_list:
        source = {"url": url, "ok": False, "selected_path": None, "record_count": 0, "records": [], "candidate_arrays": []}
        try:
            payload = browser_fetch(url, timeout=timeout) if use_browser_fetch else http_get(url, timeout=timeout)
            data = parse_payload(payload)
            arrays = []
            if records_path:
                selected = path_get(data, records_path)
                if isinstance(selected, list):
                    arrays.append(
                        {
                            "path": str(records_path),
                            "count": len(selected),
                            "fields": array_fields(selected),
                            "score": len(selected) * 4 + 100,
                            "records": [flatten_record(item) for item in selected[:limit_int]],
                        }
                    )
            arrays.extend(find_arrays(data))
            dedup = {}
            for array in arrays:
                key = array.get("path")
                if key not in dedup or array.get("score", 0) > dedup[key].get("score", 0):
                    dedup[key] = array
            candidates = sorted(dedup.values(), key=lambda item: (-item.get("score", 0), item.get("path", "")))
            chosen = candidates[0] if candidates else None
            source.update(
                {
                    "ok": bool(chosen),
                    "selected_path": chosen.get("path") if chosen else None,
                    "record_count": chosen.get("count", 0) if chosen else 0,
                    "fields": chosen.get("fields", []) if chosen else [],
                    "records": chosen.get("records", []) if chosen else [],
                    "candidate_arrays": [
                        {k: v for k, v in candidate.items() if k != "records"} for candidate in candidates[:8]
                    ],
                }
            )
            for record in source["records"]:
                key = json.dumps(record, sort_keys=True, default=str)[:1000]
                if key in seen_records:
                    continue
                seen_records.add(key)
                aggregate.append({"source_url": url, "record": record})
                if len(aggregate) >= limit_int:
                    break
        except Exception as exc:
            source["error"] = str(exc)[:500]
        sources.append(source)
    return {
        "count": len(aggregate),
        "records": aggregate,
        "source_count": len(sources),
        "sources": sources,
        "records_path": records_path,
        "used_browser_fetch": bool(use_browser_fetch),
    }


def tabular_data_records(source, delimiter=None, limit=500, use_browser_fetch=False, table_index=0, timeout=20.0):
    """Fetch/read CSV, TSV, or simple HTML table data and return normalized records."""
    try:
        limit_int = max(1, min(int(limit or 500), 5000))
    except Exception:
        limit_int = 500

    def clean(value, max_chars=2000):
        return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()[:max_chars]

    def unique_headers(headers):
        out = []
        seen = {}
        for index, header in enumerate(headers):
            base = clean(header, 120) or f"column_{index + 1}"
            count = seen.get(base, 0) + 1
            seen[base] = count
            out.append(base if count == 1 else f"{base}_{count}")
        return out

    def response_text(payload):
        if isinstance(payload, dict):
            if payload.get("text") is not None:
                return str(payload.get("text") or "")
            if payload.get("json") is not None:
                return json.dumps(payload.get("json"), ensure_ascii=False)
        if hasattr(payload, "text"):
            return str(payload.text)
        return str(payload)

    def read_source(value):
        text = str(value or "")
        if not text:
            return "", "empty", ""
        if len(text) < 1000 and "\n" not in text and "\r" not in text:
            try:
                path = pathlib.Path(text).expanduser()
                if path.exists():
                    return path.read_text(encoding="utf-8", errors="replace"), "file", str(path)
            except OSError:
                pass
        if re.match(r"^https?://", text) or text.startswith("/"):
            payload = browser_fetch(text, timeout=timeout) if use_browser_fetch else http_get(text, timeout=timeout)
            return response_text(payload), "browser_fetch" if use_browser_fetch else "http_get", text
        return text, "literal", ""

    def parse_csv_text(text):
        sample = text[:4096]
        delimiter_char = None
        dialect = None
        if delimiter:
            delimiter_char = str(delimiter)[0]
        else:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            except Exception:
                dialect = csv.excel_tab if "\t" in sample and "," not in sample else csv.excel
        rows = (
            list(csv.reader(io.StringIO(text), delimiter=delimiter_char))
            if delimiter_char
            else list(csv.reader(io.StringIO(text), dialect))
        )
        rows = [[clean(cell) for cell in row] for row in rows if any(clean(cell) for cell in row)]
        if not rows:
            return [], [], dialect.delimiter if dialect else delimiter_char, "empty_csv"
        headers = unique_headers(rows[0])
        records = []
        for row in rows[1 : limit_int + 1]:
            padded = row + [""] * max(0, len(headers) - len(row))
            record = {headers[index]: padded[index] if index < len(padded) else "" for index in range(len(headers))}
            if any(str(value).strip() for value in record.values()):
                records.append(record)
        return headers, records, delimiter_char or dialect.delimiter, ""

    def strip_tags(value):
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        return clean(value)

    def parse_html_tables(text):
        tables = re.findall(r"<table\b[^>]*>.*?</table>", text, flags=re.I | re.S)
        if not tables:
            return [], [], "no_html_table"
        try:
            table_i = max(0, min(int(table_index or 0), len(tables) - 1))
        except Exception:
            table_i = 0
        table = tables[table_i]
        row_blocks = re.findall(r"<tr\b[^>]*>.*?</tr>", table, flags=re.I | re.S)
        parsed_rows = []
        header_row_index = 0
        for row_index, row_html in enumerate(row_blocks):
            cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
            if cells:
                parsed_rows.append([strip_tags(cell) for cell in cells])
                if re.search(r"<th\b", row_html, flags=re.I):
                    header_row_index = row_index
        if not parsed_rows:
            return [], [], "empty_html_table"
        headers = unique_headers(parsed_rows[header_row_index])
        data_rows = parsed_rows[header_row_index + 1 :] if header_row_index == 0 else parsed_rows[:header_row_index] + parsed_rows[header_row_index + 1 :]
        records = []
        for row in data_rows[:limit_int]:
            padded = row + [""] * max(0, len(headers) - len(row))
            record = {headers[index]: padded[index] if index < len(padded) else "" for index in range(len(headers))}
            if any(str(value).strip() for value in record.values()):
                records.append(record)
        return headers, records, f"html_table_{table_i}", ""

    text, source_kind, resolved_source = read_source(source)
    lower = str(source or "").lower()
    if "<table" in text[:20000].lower() or lower.endswith((".html", ".htm")):
        fields, records, parsed_as, diagnosis = parse_html_tables(text)
        kind = "html_table"
    else:
        fields, records, parsed_as, diagnosis = parse_csv_text(text)
        kind = "csv" if parsed_as != "\t" else "tsv"
    return {
        "source": source,
        "resolved_source": resolved_source,
        "source_kind": source_kind,
        "kind": kind,
        "parsed_as": parsed_as,
        "count": len(records),
        "fields": fields,
        "records": records,
        "diagnosis": diagnosis,
        "used_browser_fetch": bool(use_browser_fetch),
    }


def shopify_products_api(url_or_domain=None, limit=250, page_limit=20, timeout=12.0):
    """Fetch public Shopify storefront products from /products.json and normalize catalog records."""
    source = str(url_or_domain or "").strip()
    if not source:
        try:
            source = (page_info() or {}).get("url") or ""
        except Exception:
            source = ""
    if not source:
        return {"origin": "", "count": 0, "products": [], "attempted_urls": [], "diagnosis": "no current URL or domain"}
    parsed = urlparse(source if "://" in source else f"https://{source}")
    host = parsed.netloc or parsed.path
    origin = f"{parsed.scheme or 'https'}://{host}".rstrip("/")
    try:
        limit_int = max(1, min(int(limit or 250), 1000))
    except Exception:
        limit_int = 250
    try:
        page_limit_int = max(1, min(int(page_limit or 20), 100))
    except Exception:
        page_limit_int = 20

    def clean_text(value, max_chars=4000):
        text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
        return re.sub(r"\s+", " ", text).strip()[:max_chars]

    def product_url(product):
        handle = str(product.get("handle") or "").strip()
        url = product.get("url") or product.get("product_url") or ""
        if url:
            return urljoin(origin + "/", str(url))
        return f"{origin}/products/{handle}" if handle else origin

    def normalize_product(product):
        variants = product.get("variants") if isinstance(product.get("variants"), list) else []
        images = product.get("images") if isinstance(product.get("images"), list) else []
        normalized_variants = []
        prices = []
        for variant in variants[:50]:
            if not isinstance(variant, dict):
                continue
            price = variant.get("price")
            if price not in (None, ""):
                prices.append(str(price))
            normalized_variants.append(
                {
                    "id": variant.get("id"),
                    "title": clean_text(variant.get("title"), 300),
                    "sku": clean_text(variant.get("sku"), 200),
                    "available": variant.get("available"),
                    "price": price,
                    "compare_at_price": variant.get("compare_at_price"),
                    "option1": variant.get("option1"),
                    "option2": variant.get("option2"),
                    "option3": variant.get("option3"),
                }
            )
        normalized_images = []
        for image in images[:30]:
            if isinstance(image, dict):
                src = image.get("src") or image.get("url")
                alt = image.get("alt")
            else:
                src = image
                alt = ""
            if src:
                normalized_images.append({"src": urljoin(origin + "/", str(src)), "alt": clean_text(alt, 200)})
        tags = product.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        return {
            "id": product.get("id"),
            "title": clean_text(product.get("title"), 500),
            "handle": clean_text(product.get("handle"), 240),
            "url": product_url(product),
            "vendor": clean_text(product.get("vendor"), 300),
            "product_type": clean_text(product.get("product_type"), 300),
            "tags": tags[:50] if isinstance(tags, list) else [],
            "published_at": product.get("published_at"),
            "created_at": product.get("created_at"),
            "updated_at": product.get("updated_at"),
            "body": clean_text(product.get("body_html") or product.get("body") or product.get("description"), 4000),
            "prices": sorted(set(prices)),
            "variants": normalized_variants,
            "images": normalized_images,
        }

    attempted = []
    products = []
    seen = set()
    diagnosis = ""
    for page in range(1, page_limit_int + 1):
        if len(products) >= limit_int:
            break
        url = f"{origin}/products.json?limit=250&page={page}"
        attempted.append(url)
        try:
            payload = http_get(url, headers={"Accept": "application/json"}, timeout=timeout)
            data = payload.json() if hasattr(payload, "json") else json.loads(str(payload))
        except Exception as exc:
            diagnosis = f"stopped after {len(products)} products: {exc}"
            break
        page_products = data.get("products") if isinstance(data, dict) else None
        if not page_products:
            break
        for product in page_products:
            if not isinstance(product, dict):
                continue
            key = product.get("id") or product.get("handle") or product.get("title")
            if key in seen:
                continue
            seen.add(key)
            products.append(normalize_product(product))
            if len(products) >= limit_int:
                break
        if len(page_products) < 250:
            break
    return {
        "origin": origin,
        "count": len(products),
        "products": products,
        "attempted_urls": attempted,
        "diagnosis": diagnosis,
    }


def product_records_snapshot(limit=80, keywords=None):
    """Return normalized product/listing records from visible cards and product metadata.

    Use this on non-Shopify product grids, vendor catalogs, and product-list pages
    before manually opening each card. It combines JSON-LD/meta product signals
    with visible product-like cards.
    """
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    keyword_list = [str(k).strip().lower() for k in raw_keywords if str(k).strip()]
    expression = f"""
(() => {{
  // __PRODUCT_RECORDS_SNAPSHOT__
  const limit = {int(limit)};
  const keywords = {json.dumps(keyword_list)};
  const clean = (text, max = 1600) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const abs = value => {{ try {{ return value ? new URL(value, location.href).href : ''; }} catch (e) {{ return value || ''; }} }};
  const visible = el => {{
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width >= 40 && r.height >= 24 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  }};
  const lower = value => clean(value, 2000).toLowerCase();
  const scoreText = text => keywords.reduce((score, kw) => score + (kw && lower(text).includes(kw) ? 12 : 0), 0);
  const priceRe = /(?:[$€£¥]\\s?\\d[\\d.,]*|\\d[\\d.,]*\\s?(?:€|eur|usd|gbp|aud|cad|dkk|sek|nok|kr))/ig;
  const specRe = /\\b(?:wi-?fi\\s?\\d+|wifi|poe\\+?\\+?|gbe|gbps|mbps|ghz|mhz|ports?|switch|gateway|router|access point|camera|sensor|cloud|rack|wan|lan|tb|gb|4k|hd|mesh|vpn|firewall)\\b[^|\\n\\r;,.]*/ig;
  const productUrlRe = /\\/(?:products?|product|store|shop|catalog|category|collections?|sku|item)\\//i;
  const uniq = values => Array.from(new Set((values || []).map(v => clean(v, 300)).filter(Boolean)));
  const imageFrom = el => {{
    if (!el) return '';
    const img = el.querySelector('img[src],img[srcset],img[data-src],picture source[srcset]');
    if (!img) return '';
    return abs(img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || (img.getAttribute('srcset') || '').split(',')[0].trim().split(/\\s+/)[0]);
  }};
  const linkText = a => clean([a.innerText || a.textContent || '', a.getAttribute('aria-label') || '', a.getAttribute('title') || ''].join(' '), 260);
  const add = (records, seen, raw) => {{
    const title = clean(raw.title || raw.name || '', 300);
    const url = abs(raw.url || raw.href || '');
    const description = clean(raw.description || raw.text || '', 1200);
    const key = url || lower(title) || lower(description).slice(0, 120);
    if (!key || seen.has(key)) return;
    seen.add(key);
    const text = clean([title, description, (raw.specs || []).join(' '), (raw.price_texts || []).join(' ')].join(' '), 2200);
    let score = 0;
    if (title) score += 30;
    if (url) score += 15;
    if (productUrlRe.test(url)) score += 20;
    if ((raw.price_texts || []).length) score += 8;
    if ((raw.specs || []).length) score += 8;
    if (raw.image_url) score += 4;
    score += scoreText(text);
    records.push({{
      source: raw.source || 'visible_dom',
      title,
      url,
      description,
      price_texts: uniq(raw.price_texts || []),
      specs: uniq(raw.specs || []),
      image_url: abs(raw.image_url || ''),
      labels: uniq(raw.labels || []),
      text,
      score,
    }});
  }};
  const records = [], seen = new Set();
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {{
    let parsed;
    try {{ parsed = JSON.parse(script.textContent || 'null'); }} catch (e) {{ continue; }}
    const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
    while (queue.length) {{
      const item = queue.shift();
      if (!item || typeof item !== 'object') continue;
      if (Array.isArray(item)) {{ queue.push(...item); continue; }}
      for (const key of ['@graph', 'itemListElement', 'hasVariant', 'offers']) {{
        const value = item[key];
        if (Array.isArray(value)) queue.push(...value);
        else if (value && typeof value === 'object') queue.push(value);
      }}
      const type = Array.isArray(item['@type']) ? item['@type'].join(' ') : (item['@type'] || '');
      const nested = item.item && typeof item.item === 'object' ? item.item : item;
      if (!/Product|Offer|ListItem/i.test(type) && !nested.name) continue;
      const offer = nested.offers && typeof nested.offers === 'object' ? nested.offers : item.offers || {{}};
      add(records, seen, {{
        source: 'json_ld',
        title: nested.name || item.name,
        url: nested.url || item.url,
        description: nested.description || item.description,
        image_url: Array.isArray(nested.image) ? nested.image[0] : nested.image,
        price_texts: [offer.price ? `${{offer.price}} ${{offer.priceCurrency || ''}}` : '', offer.lowPrice ? `${{offer.lowPrice}} ${{offer.priceCurrency || ''}}` : ''],
        specs: [nested.sku, nested.model, nested.category, nested.brand && (nested.brand.name || nested.brand)],
        labels: [type],
      }});
    }}
  }}
  const metaTitle = document.querySelector('meta[property="og:title"],meta[name="twitter:title"]')?.content || '';
  const metaDescription = document.querySelector('meta[property="og:description"],meta[name="description"],meta[name="twitter:description"]')?.content || '';
  const metaImage = document.querySelector('meta[property="og:image"],meta[name="twitter:image"]')?.content || '';
  if (metaTitle && /product|store|shop|catalog|ubiquiti|unifi|ui /i.test([metaTitle, location.href].join(' '))) {{
    add(records, seen, {{source: 'meta', title: metaTitle, url: location.href, description: metaDescription, image_url: metaImage}});
  }}
  const cardSelector = [
    'article', 'li', '[role="listitem"]', '[itemscope]', '[itemtype*="Product" i]',
    '[class*="product" i]', '[class*="card" i]', '[class*="tile" i]', '[data-testid*="product" i]',
    'a[href*="/product"]', 'a[href*="/products"]'
  ].join(',');
  for (const el of document.querySelectorAll(cardSelector)) {{
    if (!visible(el)) continue;
    const text = clean([el.innerText || el.textContent || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || ''].join(' '), 1800);
    const linkNodes = (el.matches('a[href]') ? [el] : []).concat(Array.from(el.querySelectorAll('a[href]')));
    const links = linkNodes.map(a => ({{href: a.href, text: linkText(a)}})).filter(link => link.href);
    const productLink = links.find(link => productUrlRe.test(link.href)) || links[0] || {{}};
    const headings = Array.from(el.querySelectorAll('h1,h2,h3,h4,[role="heading"],[itemprop="name"]')).map(h => clean(h.textContent, 220)).filter(Boolean);
    const labels = uniq([el.getAttribute('aria-label'), el.getAttribute('title'), ...Array.from(el.querySelectorAll('img[alt]')).map(img => img.getAttribute('alt'))]);
    const title = headings[0] || productLink.text || labels[0] || text.split(/[\\n|•]/).map(part => clean(part, 220)).find(Boolean) || '';
    const prices = uniq(text.match(priceRe) || []);
    const specs = uniq(text.match(specRe) || []);
    if (!title || (text.length < 8 && !productLink.href)) continue;
    if (!productUrlRe.test(productLink.href || '') && !/product|unifi|ubiquiti|sku|model|gateway|switch|camera|access point/i.test(text + ' ' + labels.join(' '))) continue;
    add(records, seen, {{
      source: 'visible_dom',
      title,
      url: productLink.href || '',
      description: text,
      price_texts: prices,
      specs,
      image_url: imageFrom(el),
      labels,
    }});
  }}
  records.sort((a, b) => b.score - a.score || (b.url ? 1 : 0) - (a.url ? 1 : 0));
  return {{url: location.href, title: document.title || '', keywords, count: records.length, products: records.slice(0, limit)}};
}})()
"""
    data = js(expression) or {}
    products = data.get("products") if isinstance(data, dict) else []
    detail_action_count = 0
    seen_urls = set()
    for product in products or []:
        if not isinstance(product, dict):
            continue
        url = product.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        detail_action_count += 1
    return {
        "url": data.get("url") if isinstance(data, dict) else "",
        "title": data.get("title") if isinstance(data, dict) else "",
        "keywords": keyword_list,
        "count": len(products or []),
        "products": products or [],
        "detail_action_count": detail_action_count,
    }


def pagination_controls_snapshot(limit=20):
    """Return likely visible pagination/load-more controls with ranking evidence."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 20
    limit_int = max(1, min(limit_int, 100))
    expression = f"""
(() => {{
  // __PAGINATION_CONTROLS_SNAPSHOT__
  const limit = {limit_int};
  const clean = (text, max = 260) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const css = value => CSS.escape(String(value || ''));
  const selectorFor = el => {{
    if (!el) return '';
    if (el.id) return '#' + css(el.id);
    for (const attr of ['aria-label', 'title', 'rel', 'data-testid', 'data-test', 'name']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{css(value)}}"]`;
    }}
    const role = el.getAttribute('role');
    if (role) return `${{el.tagName.toLowerCase()}}[role="${{css(role)}}"]`;
    return el.tagName.toLowerCase();
  }};
  const visible = el => {{
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' &&
      r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth;
  }};
  const textFor = el => clean([
    el.innerText || el.textContent || '',
    el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    el.getAttribute('rel') || '',
    el.id || '',
  ].join(' '));
  const nodes = Array.from(document.querySelectorAll([
    'a[href]', 'button', 'input[type="button"]', 'input[type="submit"]',
    '[role="button"]', '[aria-label]', '[title]'
  ].join(',')));
  const controls = [];
  for (const el of nodes) {{
    if (el.disabled || !visible(el)) continue;
    const label = textFor(el);
    const hay = clean([
      label,
      el.className || '',
      el.getAttribute('rel') || '',
      el.getAttribute('aria-current') || '',
      el.closest('nav,[role="navigation"],[class*="pagination" i],[id*="pagination" i],[class*="pager" i],[id*="pager" i]') ? 'pagination' : '',
    ].join(' '), 1000).toLowerCase();
    let score = 0;
    if (/\\b(next|more|load more|show more|more results|older|forward|continue)\\b|›|»|>/.test(hay)) score += 90;
    if (/\\b(page|pages|pagination|pager|results?|records?|items?)\\b/.test(hay)) score += 30;
    if (el.getAttribute('rel') === 'next') score += 120;
    if (/\\b(prev|previous|back|newer)\\b/.test(hay)) score -= 60;
    if (el.getAttribute('aria-disabled') === 'true' || el.getAttribute('disabled') !== null) score -= 120;
    if (score <= 0) continue;
    const r = el.getBoundingClientRect();
    controls.push({{
      selector: selectorFor(el),
      tag: el.tagName.toLowerCase(),
      text: label,
      href: el.href || '',
      rel: el.getAttribute('rel') || '',
      aria_current: el.getAttribute('aria-current'),
      aria_disabled: el.getAttribute('aria-disabled'),
      score,
      rect: {{
        x: Math.round(r.x), y: Math.round(r.y),
        width: Math.round(r.width), height: Math.round(r.height),
        in_viewport: true,
      }},
      center: {{x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}},
    }});
  }}
  const bodyText = clean(document.body && document.body.innerText || '', 1800);
  const pageHint = bodyText.match(/(?:page\\s+\\d+\\s+(?:of|\\/|from)\\s+\\d+|\\d+\\s*[-–]\\s*\\d+\\s+of\\s+\\d+|showing\\s+\\d+)/i);
  controls.sort((a, b) => b.score - a.score || a.rect.y - b.rect.y);
  return {{url: location.href, title: document.title || '', page_hint: pageHint ? pageHint[0] : '', count: controls.length, controls: controls.slice(0, limit)}};
}})()
"""
    data = js(expression) or {}
    controls = data.get("controls") if isinstance(data, dict) else data
    return {
        "url": data.get("url") if isinstance(data, dict) else "",
        "title": data.get("title") if isinstance(data, dict) else "",
        "count": len(controls or []),
        "page_hint": data.get("page_hint", "") if isinstance(data, dict) else "",
        "controls": controls or [],
    }


def click_pagination(label_or_text="next", timeout=2.0):
    """Click a visible pagination/load-more control matched by label or intent."""
    needle = str(label_or_text or "next").strip().lower()
    expression = f"""
(() => {{
  // __CLICK_PAGINATION__
  const needle = {json.dumps(needle)};
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const css = value => CSS.escape(String(value || ''));
  const selectorFor = el => {{
    if (el.id) return '#' + css(el.id);
    for (const attr of ['aria-label', 'title', 'rel', 'data-testid', 'data-test', 'name']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{css(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' &&
      r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth;
  }};
  const textFor = el => clean([
    el.innerText || el.textContent || '',
    el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    el.getAttribute('rel') || '',
    el.id || '',
  ].join(' '));
  let best = null, bestScore = -100000, bestText = '';
  for (const el of document.querySelectorAll('a[href],button,input[type="button"],input[type="submit"],[role="button"],[aria-label],[title]')) {{
    if (el.disabled || !visible(el) || el.getAttribute('aria-disabled') === 'true') continue;
    const label = textFor(el);
    const hay = clean([label, el.className || '', el.getAttribute('rel') || '', selectorFor(el)].join(' ')).toLowerCase();
    let score = 0;
    if (needle && (hay === needle || label.toLowerCase() === needle)) score += 160;
    if (needle && hay.includes(needle)) score += 95;
    if (needle === 'next' && (/\\bnext\\b|›|»|>/.test(hay) || el.getAttribute('rel') === 'next')) score += 130;
    if ((needle.includes('more') || needle === 'next') && /\\b(load more|show more|more results|older|forward|continue)\\b/.test(hay)) score += 110;
    if (/\\b(prev|previous|back|newer)\\b/.test(hay)) score -= 90;
    if (score > bestScore) {{ best = el; bestScore = score; bestText = label; }}
  }}
  if (!best || bestScore <= 0) return null;
  const r = best.getBoundingClientRect();
  return {{
    selector: selectorFor(best),
    matched_text: bestText,
    score: bestScore,
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    href: best.href || '',
    rel: best.getAttribute('rel') || '',
  }};
}})()
"""
    match = js(expression)
    if not match:
        controls = pagination_controls_snapshot(limit=20)
        raise RuntimeError(f"click_pagination: no likely pagination control matched {label_or_text!r}; controls={controls}")
    click_at_xy(match["x"], match["y"])
    if timeout:
        _time.sleep(min(max(float(timeout), 0.0), 3.0))
    return {
        "clicked": True,
        "selector": match.get("selector", ""),
        "matched_text": match.get("matched_text", ""),
        "href": match.get("href", ""),
        "rel": match.get("rel", ""),
        "score": match.get("score"),
        "x": match.get("x"),
        "y": match.get("y"),
    }


def _pagination_progress_state(count_selector=None):
    selector = str(count_selector or "").strip()
    expression = f"""
(() => {{
  // __PAGINATION_PROGRESS_STATE__
  const selector = {json.dumps(selector)};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  }};
  const countVisible = css => {{
    try {{ return Array.from(document.querySelectorAll(css)).filter(visible).length; }}
    catch (e) {{ return null; }}
  }};
  const selectorCounts = {{}};
  const fallbacks = ['article', 'li', 'tr', '[role="listitem"]', '[class*="card" i]', '[class*="item" i]', '[class*="result" i]', '[class*="product" i]', '[data-testid*="card" i]', '[data-testid*="item" i]', '[data-testid*="result" i]'];
  let itemCount = selector ? countVisible(selector) : null;
  for (const css of fallbacks) {{
    const count = countVisible(css);
    selectorCounts[css] = count;
    if (count !== null && (itemCount === null || count > itemCount)) itemCount = count;
  }}
  const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
  return {{
    count_selector: selector,
    item_count: itemCount || 0,
    text_length: text.length,
    text_sample: text.slice(0, 500),
    selector_counts: selectorCounts,
    url: location.href,
    title: document.title || '',
  }};
}})()
"""
    try:
        state = js(expression) or {}
    except Exception as exc:
        return {"count_selector": selector, "item_count": 0, "text_length": 0, "text_sample": "", "error": str(exc)}
    if isinstance(state, dict):
        return state
    return {"count_selector": selector, "item_count": 0, "text_length": 0, "text_sample": "", "raw": state}


def click_pagination_until_stable(label_or_text="load more", max_clicks=20, wait_seconds=0.8, idle_timeout=3.0, count_selector=None):
    """Click Next/Load More repeatedly until no matching control remains or progress stops."""
    needle = str(label_or_text or "load more").strip().lower()
    try:
        max_clicks_int = int(max_clicks)
    except Exception:
        max_clicks_int = 20
    max_clicks_int = max(0, min(max_clicks_int, 50))
    wait_s = max(0.0, min(float(wait_seconds or 0), 5.0))
    idle_s = max(0.0, min(float(idle_timeout or 0), 10.0))

    def matching_control(controls):
        best = None
        best_score = -10**9
        for control in controls or []:
            if not isinstance(control, dict):
                continue
            text = str(control.get("text") or "").strip().lower()
            hay = " ".join([
                text,
                str(control.get("rel") or "").lower(),
                str(control.get("selector") or "").lower(),
            ])
            disabled = control.get("aria_disabled") is True or str(control.get("aria_disabled") or "").lower() == "true"
            if disabled:
                continue
            score = int(control.get("score") or 0)
            if needle and (needle == text or needle in hay):
                score += 200
            if needle == "next" and ("next" in hay or control.get("rel") == "next" or "›" in hay or "»" in hay):
                score += 160
            if "more" in needle and ("load more" in hay or "show more" in hay or "more results" in hay):
                score += 160
            if ("load more" in hay or "show more" in hay or "more results" in hay or "older" in hay or "continue" in hay) and needle in ("next", "more", "load more"):
                score += 80
            if "prev" in hay or "previous" in hay or "back" in hay:
                score -= 120
            if score > best_score:
                best = control
                best_score = score
        return best if best is not None and best_score > 0 else None

    states = []
    clicked = []
    stopped_reason = "max_clicks"
    unchanged_runs = 0
    initial_state = _pagination_progress_state(count_selector=count_selector)
    states.append(initial_state)
    previous_signature = (initial_state.get("item_count"), initial_state.get("text_length"), initial_state.get("url"))

    for _ in range(max_clicks_int):
        snapshot = pagination_controls_snapshot(limit=20)
        control = matching_control(snapshot.get("controls", []))
        if not control:
            stopped_reason = "no_matching_control"
            break
        try:
            click_result = click_pagination(label_or_text, timeout=0)
        except Exception as exc:
            stopped_reason = "click_failed"
            clicked.append({"control": control, "error": str(exc)})
            break
        clicked.append({"control": control, "result": click_result})
        if wait_s:
            _time.sleep(wait_s)
        if idle_s:
            try:
                wait_for_network_idle(timeout=idle_s)
            except Exception:
                pass
        state = _pagination_progress_state(count_selector=count_selector)
        states.append(state)
        signature = (state.get("item_count"), state.get("text_length"), state.get("url"))
        if signature == previous_signature:
            unchanged_runs += 1
        else:
            unchanged_runs = 0
        previous_signature = signature
        if unchanged_runs >= 2:
            stopped_reason = "stable_after_click"
            break

    final_state = states[-1] if states else {}
    return {
        "clicks": len(clicked),
        "stopped_reason": stopped_reason,
        "states": states,
        "clicked": clicked,
        "final_controls": pagination_controls_snapshot(limit=20),
        "final_count": final_state.get("item_count", 0),
        "final_text_length": final_state.get("text_length", 0),
        "count_selector": str(count_selector or "").strip(),
        "label_or_text": label_or_text,
    }


def result_count_snapshot(limit=12):
    """Return parsed visible result/page-count snippets with evidence text."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 12
    limit_int = max(1, min(limit_int, 100))
    expression = f"""
(() => {{
  // __RESULT_COUNT_SNAPSHOT__
  const clean = (text, max = 500) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const num = value => {{
    const parsed = parseInt(String(value || '').replace(/,/g, ''), 10);
    return Number.isFinite(parsed) ? parsed : null;
  }};
  const out = [], seen = new Set();
  const add = (kind, evidence, score, fields = {{}}) => {{
    evidence = clean(evidence);
    const key = kind + '|' + evidence;
    if (!evidence || seen.has(key)) return;
    seen.add(key);
    out.push(Object.assign({{kind, evidence, score}}, fields));
  }};
  const texts = [clean(document.body && document.body.innerText || '', 8000)];
  for (const el of document.querySelectorAll('main,section,header,footer,nav,table,tbody,thead,tfoot,[role="status"],[aria-live],[class*="result" i],[id*="result" i],[class*="pagination" i],[id*="pagination" i],[class*="pager" i],[id*="pager" i]')) {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden') {{
      texts.push(clean(el.innerText || el.textContent || '', 1200));
    }}
  }}
  for (const text of texts) {{
    if (!text) continue;
    let match;
    const range = /\\b(?:matches|records|results?|entries|items?)\\s+(\\d[\\d,]*)\\s*(?:-|–|to|through)\\s*(\\d[\\d,]*)\\s+(?:of|from)\\s+(\\d[\\d,]*)\\b/ig;
    while ((match = range.exec(text))) add('range_total', match[0], 140, {{start: num(match[1]), end: num(match[2]), total: num(match[3])}});
    const showing = /\\b(?:showing|displaying|viewing)\\s+(\\d[\\d,]*)\\s*(?:-|–|to|through)\\s*(\\d[\\d,]*)\\s+(?:of|from)\\s+(\\d[\\d,]*)\\b/ig;
    while ((match = showing.exec(text))) add('showing_total', match[0], 130, {{start: num(match[1]), end: num(match[2]), total: num(match[3])}});
    const page = /\\bpage\\s+(\\d[\\d,]*)\\s*(?:of|\\/|from)\\s*(\\d[\\d,]*)\\b/ig;
    while ((match = page.exec(text))) add('page_total', match[0], 105, {{current_page: num(match[1]), total_pages: num(match[2])}});
    const compact = /\\b(\\d[\\d,]*)\\s+(?:of|\\/)\\s+(\\d[\\d,]*)\\b/ig;
    if (/\\b(page|pagination|pager|next|previous|results?|records?|entries)\\b/i.test(text)) {{
      while ((match = compact.exec(text))) add('compact_page_total', match[0], 70, {{current_page: num(match[1]), total_pages: num(match[2])}});
    }}
    const total = /\\b(\\d[\\d,]*)\\s+(exhibitors?|results?|records?|entries|items?|companies|matches)\\b/ig;
    while ((match = total.exec(text))) add('labeled_total', match[0], 80, {{total: num(match[1]), label: match[2].toLowerCase()}});
    const found = /\\b(?:found|total)\\s*:?\\s*(\\d[\\d,]*)\\s+(results?|records?|entries|items?|companies|matches)\\b/ig;
    while ((match = found.exec(text))) add('found_total', match[0], 90, {{total: num(match[1]), label: match[2].toLowerCase()}});
  }}
  out.sort((a, b) => b.score - a.score);
  return {{best: out[0] || null, candidates: out.slice(0, {limit_int})}};
}})()
"""
    data = js(expression) or {}
    candidates = data.get("candidates") if isinstance(data, dict) else []
    return {
        "count": len(candidates or []),
        "best": data.get("best") if isinstance(data, dict) else None,
        "candidates": candidates or [],
    }


def contact_details_snapshot(limit=50):
    """Return visible and structured contact details: emails, phones, links, addresses."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 50
    limit_int = max(1, min(limit_int, 200))
    expression = r"""
(() => {
  // __CONTACT_DETAILS_SNAPSHOT__
  const limit = __LIMIT__;
  const clean = (text, max = 500) => (text || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const normEmail = value => clean(String(value || '')
    .replace(/^mailto:/i, '')
    .replace(/\?.*$/, '')
    .replace(/\s*\[at\]\s*/ig, '@').replace(/\s*\(at\)\s*/ig, '@').replace(/\s+at\s+/ig, '@')
    .replace(/\s*\[dot\]\s*/ig, '.').replace(/\s*\(dot\)\s*/ig, '.').replace(/\s+dot\s+/ig, '.')
  ).toLowerCase();
  const normPhone = value => clean(String(value || '').replace(/^tel:/i, '').replace(/[^\d+().\-\s extx]/ig, ' ').replace(/\s+/g, ' '), 80);
  const selectorFor = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['aria-label', 'title', 'data-testid', 'data-test', 'href']) {
      const value = el.getAttribute(attr);
      if (value) return `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(value)}"]`;
    }
    return el.tagName.toLowerCase();
  };
  const emails = [], phones = [], contactLinks = [], socialLinks = [], addresses = [], sections = [], jsonldContacts = [];
  const seen = {emails: new Set(), phones: new Set(), links: new Set(), social: new Set(), addresses: new Set(), sections: new Set()};
  const addEmail = (email, source, context = '') => {
    email = normEmail(email);
    if (!email || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>.]+$/.test(email) || seen.emails.has(email)) return;
    seen.emails.add(email);
    emails.push({email, source, context: clean(context, 260)});
  };
  const addPhone = (phone, source, context = '') => {
    phone = normPhone(phone);
    const digits = (phone.match(/\d/g) || []).length;
    if (!phone || digits < 7 || seen.phones.has(phone)) return;
    seen.phones.add(phone);
    phones.push({phone, source, context: clean(context, 260)});
  };
  const addAddress = (address, source, context = '') => {
    address = clean(address, 260);
    if (!address || address.length < 8 || seen.addresses.has(address)) return;
    seen.addresses.add(address);
    addresses.push({address, source, context: clean(context, 220)});
  };
  const addLink = (arr, seenSet, link, source) => {
    const href = link.href || link.getAttribute('href') || '';
    const text = clean(link.innerText || link.getAttribute('aria-label') || link.getAttribute('title') || href, 220);
    const key = href + '|' + text;
    if (!href || seenSet.has(key)) return;
    seenSet.add(key);
    const r = link.getBoundingClientRect();
    arr.push({
      text, href, source, selector: selectorFor(link),
      rect: {
        x: Math.round(r.x), y: Math.round(r.y),
        width: Math.round(r.width), height: Math.round(r.height),
        in_viewport: r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth,
      },
    });
  };
  const text = clean(document.body && document.body.innerText || '', 50000);
  const emailRe = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig;
  let match;
  while ((match = emailRe.exec(text))) addEmail(match[0], 'visible_text');
  const obfuscatedRe = /[A-Z0-9._%+-]+\s*(?:\[at\]|\(at\)|\sat\s)\s*[A-Z0-9.-]+\s*(?:\[dot\]|\(dot\)|\sdot\s)\s*[A-Z]{2,}/ig;
  while ((match = obfuscatedRe.exec(text))) addEmail(match[0], 'visible_text_obfuscated');
  const phoneRe = /(?:\+?\d[\d().\-\s]{6,}\d)(?:\s*(?:ext|x)\s*\d{1,6})?/ig;
  while ((match = phoneRe.exec(text))) addPhone(match[0], 'visible_text');
  for (const link of document.querySelectorAll('a[href]')) {
    const href = link.getAttribute('href') || '', lower = href.toLowerCase();
    const label = clean(link.innerText || link.getAttribute('aria-label') || link.getAttribute('title') || href, 240);
    if (lower.startsWith('mailto:')) addEmail(href, 'mailto', label);
    if (lower.startsWith('tel:')) addPhone(href, 'tel', label);
    if (/contact|support|help|customer|service|about|team|staff|location|store|directory|provider/i.test(label + ' ' + href)) addLink(contactLinks, seen.links, link, 'contact_candidate');
    if (/linkedin|facebook|twitter|x\.com|instagram|youtube|github|tiktok|pinterest/i.test(href)) addLink(socialLinks, seen.social, link, 'social');
  }
  const flatten = value => Array.isArray(value) ? value.flatMap(flatten) : (value && typeof value === 'object' ? [value, ...Object.values(value).flatMap(flatten)] : []);
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      for (const item of flatten(JSON.parse(script.textContent || '{}'))) {
        if (!item || typeof item !== 'object') continue;
        const name = clean(item.name || item.legalName || (item.givenName && item.familyName && `${item.givenName} ${item.familyName}`) || '', 180);
        if (item.email) addEmail(item.email, 'json_ld', name);
        if (item.telephone) addPhone(item.telephone, 'json_ld', name);
        const address = item.address;
        if (address && typeof address === 'object') addAddress([address.streetAddress, address.addressLocality, address.addressRegion, address.postalCode, address.addressCountry].filter(Boolean).join(', '), 'json_ld', name);
        if (item.url || item.email || item.telephone || address) {
          jsonldContacts.push({type: item['@type'] || '', name, email: item.email || '', telephone: item.telephone || '', url: item.url || '', address: address || null});
        }
      }
    } catch (e) {}
  }
  for (const el of document.querySelectorAll('address,[itemtype*="PostalAddress"],[class*="contact" i],[id*="contact" i],[class*="location" i],[id*="location" i],[class*="address" i],[id*="address" i],footer')) {
    if (!visible(el)) continue;
    const sectionText = clean(el.innerText || el.textContent || '', 900);
    if (!sectionText) continue;
    if (/@|phone|tel|email|contact|support|address|location|hours|\d{3,}[\s\w,.-]+(?:st|street|ave|avenue|rd|road|blvd|drive|dr|lane|ln|way|suite|ste)\b/i.test(sectionText)) {
      const key = sectionText.slice(0, 260);
      if (!seen.sections.has(key)) {
        seen.sections.add(key);
        sections.push({text: sectionText, selector: selectorFor(el)});
      }
      if (/(?:st|street|ave|avenue|rd|road|blvd|drive|dr|lane|ln|way|suite|ste)\b/i.test(sectionText)) addAddress(sectionText, 'visible_section');
    }
  }
  return {
    emails: emails.slice(0, limit),
    phones: phones.slice(0, limit),
    contact_links: contactLinks.slice(0, limit),
    social_links: socialLinks.slice(0, limit),
    addresses: addresses.slice(0, limit),
    jsonld_contacts: jsonldContacts.slice(0, limit),
    sections: sections.slice(0, Math.min(limit, 20)),
    counts: {
      emails: emails.length,
      phones: phones.length,
      contact_links: contactLinks.length,
      social_links: socialLinks.length,
      addresses: addresses.length,
      jsonld_contacts: jsonldContacts.length,
      sections: sections.length,
    },
  };
})()
""".replace("__LIMIT__", str(limit_int))
    data = js(expression) or {}
    if not isinstance(data, dict):
        return {
            "emails": [],
            "phones": [],
            "contact_links": [],
            "social_links": [],
            "addresses": [],
            "jsonld_contacts": [],
            "sections": [],
            "counts": {},
            "raw": data,
        }
    keys = ["emails", "phones", "contact_links", "social_links", "addresses", "jsonld_contacts", "sections"]
    for key in keys:
        data.setdefault(key, [])
    data.setdefault("counts", {key: len(data.get(key) or []) for key in keys})
    return data


def location_records_snapshot(limit=200, keywords=None):
    """Return normalized visible/structured store, hospital, office, and directory location records."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 200
    limit_int = max(1, min(limit_int, 500))
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    keyword_list = [str(keyword).strip().lower() for keyword in raw_keywords if str(keyword).strip()]
    expression = f"""
(() => {{
  // __LOCATION_RECORDS_SNAPSHOT__
  const limit = {limit_int};
  const keywords = {json.dumps(keyword_list)};
  const clean = (value, max = 500) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const abs = value => {{ try {{ return value ? new URL(value, location.href).href : ''; }} catch (e) {{ return ''; }} }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  }};
  const stateRe = /\\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY|DC)\\b/;
  const phoneRe = /(?:\\+?1[\\s.-]?)?\\(?\\d{{3}}\\)?[\\s.-]?\\d{{3}}[\\s.-]?\\d{{4}}/;
  const zipRe = /\\b\\d{{5}}(?:-\\d{{4}})?\\b/;
  const records = [], seen = new Set();
  const scoreText = text => keywords.reduce((score, keyword) => score + (text.toLowerCase().includes(keyword) ? 25 : 0), 0);
  const normAddr = address => !address ? '' :
    typeof address === 'string' ? clean(address, 300) :
    typeof address === 'object' ? clean([address.streetAddress, address.addressLocality, address.addressRegion, address.postalCode, address.addressCountry].filter(Boolean).join(', '), 300) : '';
  const add = raw => {{
    const name = clean(raw.name, 240);
    const address = clean(raw.address, 300);
    const url = abs(raw.url || '');
    if (!name && !address) return;
    const key = (name + '|' + address + '|' + url).toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    const text = clean([name, address, raw.phone, raw.hours, raw.kind, raw.source].join(' '), 900);
    const stateMatch = address.match(stateRe) || text.match(stateRe);
    records.push({{
      source: clean(raw.source || '', 80),
      kind: clean(raw.kind || '', 80),
      name,
      address,
      city: clean(raw.city || '', 120),
      state: clean(raw.state || raw.region || (stateMatch ? stateMatch[1] : ''), 40),
      postal_code: clean(raw.postal_code || '', 40),
      country: clean(raw.country || '', 80),
      phone: clean(raw.phone || '', 80),
      url,
      hours: clean(raw.hours || '', 500),
      text,
      score: (name ? 30 : 0) + (address ? 50 : 0) + (url ? 10 : 0) + (raw.phone ? 10 : 0) + scoreText(text),
    }});
  }};
  const visit = value => {{
    if (!value) return;
    if (Array.isArray(value)) {{ for (const item of value) visit(item); return; }}
    if (typeof value !== 'object') return;
    const type = Array.isArray(value['@type']) ? value['@type'].join(' ') : String(value['@type'] || '');
    if (/LocalBusiness|Store|Hospital|VeterinaryCare|MedicalBusiness|Organization|Place|LodgingBusiness|Restaurant|AutoDealer/i.test(type) || value.address) {{
      const address = value.address || {{}};
      add({{
        source: 'json_ld',
        kind: type,
        name: value.name || value.legalName || '',
        address: normAddr(address),
        city: address.addressLocality || '',
        state: address.addressRegion || '',
        postal_code: address.postalCode || '',
        country: address.addressCountry || '',
        phone: value.telephone || value.phone || '',
        url: value.url || '',
      }});
    }}
    for (const child of Object.values(value)) if (child && typeof child === 'object') visit(child);
  }};
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {{
    try {{ visit(JSON.parse(script.textContent || '')); }} catch (e) {{}}
  }}
  const selectors = [
    'article', 'li', 'section',
    '[itemtype*="LocalBusiness"]', '[itemtype*="PostalAddress"]', '[itemtype*="Place"]',
    '[class*="store" i]', '[class*="location" i]', '[class*="hospital" i]', '[class*="clinic" i]', '[class*="directory" i]',
    '[data-testid*="store" i]', '[data-testid*="location" i]'
  ].join(',');
  for (const el of Array.from(document.querySelectorAll(selectors)).filter(visible).slice(0, limit * 6)) {{
    const text = clean(el.innerText || el.textContent || '', 900);
    if (text.length < 8) continue;
    const hay = (text + ' ' + (el.className || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('data-testid') || '')).toLowerCase();
    if (!/@|address|hours?|phone|tel|store|location|hospital|clinic|practice|\\d{{5}}|\\b[A-Z]{{2}}\\b/.test(text) && !/store|location|hospital|clinic|practice|directory/.test(hay)) continue;
    const link = el.querySelector('a[href]');
    const phone = (text.match(phoneRe) || [''])[0];
    const lines = text.split(/\\n+/).map(line => clean(line, 180)).filter(Boolean);
    let name = clean(el.querySelector('h1,h2,h3,h4,[itemprop="name"],[class*="name" i],[class*="title" i]')?.innerText || lines[0] || '', 180);
    let address = '';
    const addressEl = el.querySelector('address,[itemprop="address"],[class*="address" i]');
    if (addressEl) address = clean(addressEl.innerText || addressEl.textContent, 300);
    if (!address) {{
      const index = lines.findIndex(line => zipRe.test(line) || stateRe.test(line));
      if (index >= 0) address = clean(lines.slice(Math.max(0, index - 2), index + 1).join(', '), 300);
    }}
    if (!address && text.length < 300 && /\\d{{2,}}/.test(text) && stateRe.test(text)) address = text;
    const hours = clean(lines.filter(line => /\\b(mon|tue|wed|thu|fri|sat|sun|hours?|open|closed)\\b/i.test(line)).slice(0, 7).join('; '), 500);
    add({{source: 'visible_dom', kind: 'location_card', name, address, phone, url: link ? link.href : '', hours}});
  }}
  records.sort((a, b) => b.score - a.score);
  return {{url: location.href, title: document.title || '', count: records.length, records: records.slice(0, limit), keywords}};
}})()
"""
    try:
        data = js(expression) or {}
    except Exception as exc:
        return {"url": "", "title": "", "count": 0, "records": [], "keywords": keyword_list, "error": str(exc)}
    if not isinstance(data, dict):
        return {"url": "", "title": "", "count": 0, "records": [], "keywords": keyword_list, "raw": data}
    data.setdefault("records", [])
    data["count"] = len(data.get("records") or [])
    data.setdefault("keywords", keyword_list)
    return data


def form_controls_snapshot(limit=30):
    """Return compact rendered checkboxes, radios, and switches with labels and state."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 30
    limit_int = max(1, min(limit_int, 200))
    expression = f"""
(() => {{
  // __FORM_CONTROLS_SNAPSHOT__
  const clean = (text, max = 180) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const labelFor = el => {{
    const bits = [el.getAttribute('aria-label'), el.getAttribute('name'), el.id];
    if (el.id) for (const label of document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)) bits.push(label.innerText);
    const parent = el.closest('label,[aria-label],[data-label],.field,.form-group,li,div');
    if (parent) bits.push(parent.innerText, parent.getAttribute('aria-label'), parent.getAttribute('data-label'));
    return clean(bits.filter(Boolean).join(' '));
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  return Array.from(document.querySelectorAll('input[type="checkbox"],input[type="radio"],[role="checkbox"],[role="radio"],[role="switch"]'))
    .filter(el => !el.disabled && visible(el))
    .slice(0, {limit_int})
    .map((el, index) => {{
      const r = el.getBoundingClientRect();
      const role = el.getAttribute('role') || '';
      const checked = el.checked === true || el.getAttribute('aria-checked') === 'true';
      return {{
        index,
        selector: selectorFor(el),
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || role,
        label: labelFor(el),
        name: clean(el.getAttribute('name')),
        checked,
        aria_checked: el.getAttribute('aria-checked'),
        rect: {{
          x: Math.round(r.x), y: Math.round(r.y),
          width: Math.round(r.width), height: Math.round(r.height),
          in_viewport: r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth,
        }},
      }};
    }});
}})()
"""
    controls = js(expression) or []
    return {"count": len(controls), "controls": controls}


def toggle_form_control(label_or_text, checked=True, timeout=1.0):
    """Set a rendered checkbox/radio/switch by label, name, or selector using a real click."""
    needle = str(label_or_text or "").strip().lower()
    if not needle:
        raise RuntimeError("toggle_form_control requires a label_or_text")
    expression = f"""
(() => {{
  // __TOGGLE_FORM_CONTROL__
  const needle = {json.dumps(needle)};
  const want = {json.dumps(bool(checked))};
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return '';
  }};
  const textFor = el => {{
    const bits = [el.getAttribute('aria-label'), el.getAttribute('name'), el.id];
    if (el.id) for (const label of document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)) bits.push(label.innerText);
    const parent = el.closest('label,[aria-label],[data-label],.field,.form-group,li,div');
    if (parent) bits.push(parent.innerText, parent.getAttribute('aria-label'), parent.getAttribute('data-label'));
    return clean(bits.filter(Boolean).join(' ')).toLowerCase();
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  let best = null, bestScore = -1, bestText = '';
  for (const el of document.querySelectorAll('input[type="checkbox"],input[type="radio"],[role="checkbox"],[role="radio"],[role="switch"]')) {{
    if (el.disabled || !visible(el)) continue;
    const selector = selectorFor(el);
    const hay = textFor(el);
    let score = hay === needle ? 130 : hay.includes(needle) ? 80 : needle.includes(hay) && hay.length > 2 ? 35 : 0;
    if (selector && selector.toLowerCase() === needle) score = 150;
    const r = el.getBoundingClientRect();
    if (score > 0 && r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth) score += 5;
    if (score > bestScore) {{ best = el; bestScore = score; bestText = hay; }}
  }}
  if (!best || bestScore <= 0) return null;
  const r = best.getBoundingClientRect();
  const state = best.checked === true || best.getAttribute('aria-checked') === 'true';
  const type = (best.getAttribute('type') || best.getAttribute('role') || '').toLowerCase();
  return {{
    selector: selectorFor(best),
    score: bestScore,
    matched_text: bestText,
    type,
    state,
    want,
    needs_click: state !== want,
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    rect: {{x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}},
  }};
}})()
"""
    match = js(expression)
    if not match:
        controls = form_controls_snapshot(limit=20)
        raise RuntimeError(f"toggle_form_control: no rendered control matched {label_or_text!r}; controls={controls}")
    if match.get("type") == "radio" and not checked:
        raise RuntimeError(f"toggle_form_control: cannot unset a radio button directly: {label_or_text!r}")
    if match.get("needs_click"):
        click_at_xy(match["x"], match["y"])
        if timeout:
            _time.sleep(min(max(float(timeout), 0.0), 2.0))
    return {
        "changed": bool(match.get("needs_click")),
        "selector": match.get("selector", ""),
        "matched_text": match.get("matched_text", ""),
        "checked": bool(checked),
        "score": match.get("score"),
        "x": match.get("x"),
        "y": match.get("y"),
    }


def select_controls_snapshot(limit=20, option_limit=30):
    """Return rendered selects/comboboxes with labels, current values, and options."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 20
    try:
        option_limit_int = int(option_limit)
    except Exception:
        option_limit_int = 30
    limit_int = max(1, min(limit_int, 100))
    option_limit_int = max(1, min(option_limit_int, 200))
    expression = f"""
(() => {{
  // __SELECT_CONTROLS_SNAPSHOT__
  const clean = (text, max = 180) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'placeholder', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const labelFor = el => {{
    const bits = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id];
    if (el.id) for (const label of document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)) bits.push(label.innerText);
    const parent = el.closest('label,[aria-label],[data-label],.field,.form-group,li,div');
    if (parent) bits.push(parent.innerText, parent.getAttribute('aria-label'), parent.getAttribute('data-label'));
    return clean(bits.filter(Boolean).join(' '));
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  return Array.from(document.querySelectorAll('select,[role="combobox"],[aria-haspopup="listbox"],[aria-controls][aria-expanded]'))
    .filter(el => !el.disabled && visible(el))
    .slice(0, {limit_int})
    .map((el, index) => {{
      const r = el.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      const options = tag === 'select'
        ? Array.from(el.options).slice(0, {option_limit_int}).map(option => ({{text: clean(option.textContent), value: option.value, selected: option.selected}}))
        : [];
      return {{
        index,
        selector: selectorFor(el),
        tag,
        role: el.getAttribute('role') || '',
        label: labelFor(el),
        name: clean(el.getAttribute('name')),
        value: clean(el.value || el.textContent, 240),
        aria_expanded: el.getAttribute('aria-expanded'),
        options,
        rect: {{
          x: Math.round(r.x), y: Math.round(r.y),
          width: Math.round(r.width), height: Math.round(r.height),
          in_viewport: r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth,
        }},
      }};
    }});
}})()
"""
    controls = js(expression) or []
    return {"count": len(controls), "controls": controls}


def select_option(label_or_placeholder, option_text_or_value, timeout=1.0):
    """Select a native select/combobox option by field label and option text/value."""
    needle = str(label_or_placeholder or "").strip().lower()
    option = str(option_text_or_value or "").strip()
    if not needle or not option:
        raise RuntimeError("select_option requires both a field label and option text/value")
    expression = f"""
(() => {{
  // __SELECT_OPTION__
  const needle = {json.dumps(needle)};
  const wanted = {json.dumps(option.lower())};
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'placeholder', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return '';
  }};
  const textFor = el => {{
    const bits = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id];
    if (el.id) for (const label of document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)) bits.push(label.innerText);
    const parent = el.closest('label,[aria-label],[data-label],.field,.form-group,li,div');
    if (parent) bits.push(parent.innerText, parent.getAttribute('aria-label'), parent.getAttribute('data-label'));
    return clean(bits.filter(Boolean).join(' ')).toLowerCase();
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  let best = null, bestScore = -1, bestText = '';
  for (const el of document.querySelectorAll('select,[role="combobox"],[aria-haspopup="listbox"],[aria-controls][aria-expanded]')) {{
    if (el.disabled || !visible(el)) continue;
    const selector = selectorFor(el);
    const hay = textFor(el);
    let score = hay === needle ? 130 : hay.includes(needle) ? 80 : needle.includes(hay) && hay.length > 2 ? 35 : 0;
    if (selector && selector.toLowerCase() === needle) score = 150;
    if (score > bestScore) {{ best = el; bestScore = score; bestText = hay; }}
  }}
  if (!best || bestScore <= 0) return null;
  const r = best.getBoundingClientRect();
  const tag = best.tagName.toLowerCase();
  let optionIndex = -1, optionText = '', optionValue = '';
  if (tag === 'select') {{
    const options = Array.from(best.options);
    optionIndex = options.findIndex(o => o.value.toLowerCase() === wanted || clean(o.textContent).toLowerCase() === wanted || clean(o.textContent).toLowerCase().includes(wanted));
    if (optionIndex >= 0) {{
      optionText = clean(options[optionIndex].textContent);
      optionValue = options[optionIndex].value;
    }}
  }}
  return {{
    selector: selectorFor(best),
    score: bestScore,
    matched_text: bestText,
    tag,
    option_index: optionIndex,
    option_text: optionText,
    option_value: optionValue,
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
  }};
}})()
"""
    match = js(expression)
    if not match:
        controls = select_controls_snapshot(limit=20)
        raise RuntimeError(f"select_option: no rendered select/combobox matched {label_or_placeholder!r}; controls={controls}")
    selector = match.get("selector")
    if match.get("tag") == "select":
        if match.get("option_index", -1) < 0:
            controls = select_controls_snapshot(limit=20)
            raise RuntimeError(f"select_option: no option matched {option_text_or_value!r}; controls={controls}")
        if not _focus_selector_like_user(selector, timeout=timeout):
            raise RuntimeError(f"select_option: element not found: {selector!r}")
        press_key("Home")
        for _ in range(int(match["option_index"])):
            press_key("ArrowDown")
        press_key("Enter")
    else:
        click_at_xy(match["x"], match["y"])
        if timeout:
            _time.sleep(min(max(float(timeout), 0.0), 1.0))
        type_text(option)
        press_key("Enter")
    return {
        "selected": True,
        "selector": selector,
        "matched_text": match.get("matched_text", ""),
        "option_text": match.get("option_text", option),
        "option_value": match.get("option_value", ""),
        "score": match.get("score"),
    }


def investor_documents_snapshot(limit=80, keywords=None, latest_only=False):
    """Return classified investor/report/earnings document candidates from visible links."""
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    keyword_list = [str(k).lower() for k in raw_keywords if str(k).strip()]
    expression = f"""
(() => {{
 // __INVESTOR_DOCUMENTS_SNAPSHOT__
 const limit={int(limit)}, keywords={json.dumps(keyword_list)}, latestOnly={json.dumps(bool(latest_only))};
 const clean=(s,n=700)=>String(s||'').replace(/\\s+/g,' ').trim().slice(0,n);
 const abs=u=>{{try{{return new URL(u,location.href).href}}catch(e){{return ''}}}};
 const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}};
 const month={{jan:'01',january:'01',feb:'02',february:'02',mar:'03',march:'03',apr:'04',april:'04',may:'05',jun:'06',june:'06',jul:'07',july:'07',aug:'08',august:'08',sep:'09',sept:'09',september:'09',oct:'10',october:'10',nov:'11',november:'11',dec:'12',december:'12'}};
 const classify=t=>{{const s=t.toLowerCase();if(/transcript|call transcript/.test(s))return 'earnings_call_transcript';if(/supplement|data book|statistical|trends report|financial data/.test(s))return 'financial_supplement';if(/presentation|slides|deck|roadshow|capital markets/.test(s))return /earnings|results|quarter|q[1-4]/.test(s)?'investor_presentation':'corporate_presentation';if(/earnings release|press release|financial results|quarterly results|annual results|full year results|results release/.test(s))return 'earnings_release';if(/annual report|quarterly report|interim report|integrated report|financial statements|10-k|10-q|20-f|md&a|investor report/.test(s))return 'investor_report';if(/\\.xlsx?|spreadsheet|data tables|questionnaire|coding frame|annex/.test(s))return 'data_table_or_annex';return 'other'}};
 const parseDate=t=>{{let m=t.match(/\\b(20\\d{{2}}|19\\d{{2}})[-_ /.](\\d{{1,2}})[-_ /.](\\d{{1,2}})\\b/);if(m)return `${{m[1]}}-${{m[2].padStart(2,'0')}}-${{m[3].padStart(2,'0')}}`;m=t.match(/\\b(\\d{{1,2}})[-_ /.](\\d{{1,2}})[-_ /.](20\\d{{2}}|19\\d{{2}})\\b/);if(m)return `${{m[3]}}-${{m[1].padStart(2,'0')}}-${{m[2].padStart(2,'0')}}`;m=t.match(/\\b(january|february|march|april|may|june|july|august|september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\\s+(\\d{{1,2}}),?\\s+(20\\d{{2}}|19\\d{{2}})\\b/i);if(m)return `${{m[3]}}-${{month[m[1].toLowerCase()]}}-${{m[2].padStart(2,'0')}}`;m=t.match(/\\b(20\\d{{2}}|19\\d{{2}})\\b/);return m?m[1]+'-01-01':null}};
 const periodTokens=t=>Array.from(new Set((t.match(/\\b(?:q[1-4]|fy\\d{{2,4}}|f\\dq\\d{{2}}|20\\d{{2}}|19\\d{{2}}|first quarter|second quarter|third quarter|fourth quarter|full year|annual)\\b/ig)||[]).map(x=>x.toLowerCase())));
 const docs=[],seen=new Set(),docRe=/(\\.pdf($|[?#])|\\.docx?($|[?#])|\\.xlsx?($|[?#])|download|document|report|results?|earnings|presentation|supplement|transcript|filing|financial|investor|annual|quarter|data table|questionnaire|annex)/i;
 for(const a of [...document.querySelectorAll('a[href]')].filter(visible).slice(0,limit*12)){{
   const href=abs(a.href); if(!href)continue;
   const row=a.closest('tr,li,article,section,div');
   const text=clean([a.innerText||a.textContent||'',a.getAttribute('aria-label')||'',a.getAttribute('title')||'',a.getAttribute('download')||'',row?(row.innerText||row.textContent||''):''].filter(Boolean).join(' '),1200);
   const hay=(href+' '+text).toLowerCase();
   if(!docRe.test(hay) && !keywords.some(k=>hay.includes(k)))continue;
   const key=href+'|'+text.slice(0,80); if(seen.has(key))continue; seen.add(key);
   const type=classify(hay), date=parseDate(href+' '+text), tokens=periodTokens(href+' '+text);
   const extension=(href.match(/\\.([a-z0-9]{{2,5}})(?:[?#]|$)/i)||[])[1]||'';
   const keyword_matches=keywords.filter(k=>hay.includes(k));
   let score=keyword_matches.length*25 + (/\\.pdf(?:[?#]|$)/i.test(href)?30:0) + (/\\.xlsx?(?:[?#]|$)/i.test(href)?25:0) + (type==='other'?0:35) + (date?10:0);
   if(/investor|financial|earnings|results|reports?|presentation|supplement/.test(hay))score+=20;
   docs.push({{title:clean(a.innerText||a.textContent||a.getAttribute('title')||a.getAttribute('aria-label')||'',300),url:href,type,published_on:date,extension,period_tokens:tokens,keyword_matches,context:text,score}});
 }}
 docs.sort((a,b)=>(b.published_on||'').localeCompare(a.published_on||'')||b.score-a.score);
 const out=latestOnly&&docs.length?docs.filter(d=>d.published_on===docs[0].published_on):docs;
 return {{url:location.href,title:document.title||'',count:out.length,documents:out.slice(0,limit),keywords,latest_only:latestOnly}};
}})()
"""
    try:
        data = js(expression) or {}
    except Exception as exc:
        return {"url": "", "title": "", "count": 0, "documents": [], "keywords": keyword_list, "latest_only": bool(latest_only), "error": str(exc)}
    if not isinstance(data, dict):
        return {"url": "", "title": "", "count": 0, "documents": [], "keywords": keyword_list, "latest_only": bool(latest_only), "raw": data}
    data.setdefault("documents", [])
    documents = data.get("documents") or []
    data["count"] = len(documents)
    data.setdefault("keywords", keyword_list)
    data.setdefault("latest_only", bool(latest_only))
    data["document_action_count"] = sum(1 for document in documents if isinstance(document, dict) and document.get("url"))
    return data


def document_links_snapshot(limit=100, keywords=None):
    """Return generic filing/document links with row and context metadata.

    Use this on FERC, docket, regulatory, government, report, and search-result
    pages where visible links point to PDFs, spreadsheets, filings, exhibits, or
    document detail pages.
    """
    raw_keywords = [keywords] if isinstance(keywords, str) else (keywords or [])
    keyword_list = [str(k).strip().lower() for k in raw_keywords if str(k).strip()]
    expression = f"""
(() => {{
  // __DOCUMENT_LINKS_SNAPSHOT__
  const limit = {int(limit)};
  const keywords = {json.dumps(keyword_list)};
  const clean = (text, max = 1200) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const visible = el => {{
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width >= 2 && r.height >= 2 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  }};
  const abs = value => {{ try {{ return value ? new URL(value, location.href).href : ''; }} catch (e) {{ return value || ''; }} }};
  const docRe = /(\\.pdf($|[?#])|\\.docx?($|[?#])|\\.xlsx?($|[?#])|\\.csv($|[?#])|download|document|filing|attachment|exhibit|transmittal|order|notice|report|spreadsheet|submission|supplement|tariff|form)/i;
  const dateRe = /\\b(?:20\\d{{2}}|19\\d{{2}})[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\\d|3[01])\\b|\\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\\s+\\d{{1,2}},?\\s+(?:20\\d{{2}}|19\\d{{2}})\\b/i;
  const docketRe = /\\b(?:[A-Z]{{1,4}}\\d{{2,4}}[-–]\\d{{1,5}}|[A-Z]{{1,4}}[-–]\\d{{2,4}}[-–]\\d{{1,5}}|Docket\\s+No\\.?\\s*[A-Z0-9-]+)\\b/ig;
  const accessionRe = /\\b(?:accession|acc(?:ession)?\\s*no\\.?|document\\s*id|elibrary\\s*no\\.?)\\s*[:#]?\\s*([0-9]{{8,}}|[A-Z0-9-]{{8,}})\\b/i;
  const extOf = url => ((url || '').split(/[?#]/)[0].match(/\\.([a-z0-9]{{2,6}})$/i) || [,''])[1].toLowerCase();
  const classify = text => {{
    const s = text.toLowerCase();
    if (/transmittal/.test(s)) return 'transmittal';
    if (/exhibit/.test(s)) return 'exhibit';
    if (/tariff/.test(s)) return 'tariff';
    if (/order/.test(s)) return 'order';
    if (/notice/.test(s)) return 'notice';
    if (/spreadsheet|xlsx?|csv|data/.test(s)) return 'data_table';
    if (/report/.test(s)) return 'report';
    if (/filing|submission|attachment|document/.test(s)) return 'filing_document';
    return 'document';
  }};
  const nearestContext = a => {{
    const row = a.closest('tr,[role="row"],article,li,section,.row,.result,.document,.filing,[data-testid],[data-test]') || a.parentElement || a;
    return {{node: row, text: clean(row.innerText || row.textContent || '', 1800)}};
  }};
  const out = [], seen = new Set();
  for (const a of Array.from(document.querySelectorAll('a[href]')).filter(visible).slice(0, limit * 20)) {{
    const href = abs(a.href || a.getAttribute('href') || '');
    const linkText = clean([a.innerText || a.textContent || '', a.getAttribute('aria-label') || '', a.getAttribute('title') || '', a.getAttribute('download') || ''].join(' '), 500);
    const ctx = nearestContext(a);
    const hay = [href, linkText, ctx.text].join(' ');
    if (!href || !docRe.test(hay)) continue;
    if (keywords.length && !keywords.some(kw => hay.toLowerCase().includes(kw))) {{
      if (!docRe.test(linkText + ' ' + href)) continue;
    }}
    const key = href + '|' + linkText;
    if (seen.has(key)) continue;
    seen.add(key);
    const date = (hay.match(dateRe) || [''])[0];
    const docket_tokens = Array.from(new Set((hay.match(docketRe) || []).map(x => clean(x, 80)))).slice(0, 6);
    const accession = ((hay.match(accessionRe) || [])[1] || '');
    const extension = extOf(href);
    let score = 20;
    if (/\\.pdf($|[?#])/i.test(href)) score += 18;
    if (/\\.xlsx?($|[?#])|\\.csv($|[?#])/i.test(href)) score += 12;
    if (/filing|docket|ferc|accession|elibrary|transmittal|exhibit/i.test(hay)) score += 18;
    if (date) score += 5;
    if (docket_tokens.length) score += 8;
    out.push({{
      title: linkText || clean(ctx.text, 160),
      url: href,
      type: classify(hay),
      extension,
      published_on: date,
      docket_tokens,
      accession,
      context: ctx.text,
      source: 'visible_dom',
      score,
    }});
  }}
  out.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title));
  return {{url: location.href, title: document.title || '', keywords, count: out.length, documents: out.slice(0, limit)}};
}})()
"""
    data = js(expression) or {}
    documents = data.get("documents") if isinstance(data, dict) else []
    document_action_count = sum(1 for document in documents or [] if isinstance(document, dict) and document.get("url"))
    return {
        "url": data.get("url") if isinstance(data, dict) else "",
        "title": data.get("title") if isinstance(data, dict) else "",
        "keywords": keyword_list,
        "count": len(documents or []),
        "documents": documents or [],
        "document_action_count": document_action_count,
    }


def embedded_data_snapshot(limit=80, max_sources=12):
    """Extract compact records from page-embedded structured/hydration data.

    Use this on ecommerce, directory, listing, investor/document, and SPA pages
    before doing brittle visual scraping. It scans JSON-LD, Next.js/Nuxt payloads,
    and product/document meta tags, then returns normalized product/listing-like
    records plus compact source metadata.
    """
    expression = f"""
(() => {{
  const limit = {int(limit)};
  const maxSources = {int(max_sources)};
  const clean = (value, max = 500) => String(value == null ? '' : value).replace(/\\s+/g, ' ').trim().slice(0, max);
  const absolute = (value) => {{
    const text = clean(value, 1200);
    if (!text) return '';
    try {{ return new URL(text, location.href).href; }} catch (err) {{ return text; }}
  }};
  const firstString = (value) => {{
    if (typeof value === 'string' || typeof value === 'number') return clean(value, 500);
    if (Array.isArray(value)) {{
      for (const item of value) {{
        const found = firstString(item);
        if (found) return found;
      }}
      return '';
    }}
    if (value && typeof value === 'object') {{
      for (const key of ['name', 'title', 'text', 'url', '@id', 'src', 'content']) {{
        const found = firstString(value[key]);
        if (found) return found;
      }}
    }}
    return '';
  }};
  const typeName = (obj) => clean(obj && (obj['@type'] || obj.type || obj.__typename || obj.kind), 120);
  const scalarKeys = (obj) => Object.entries(obj || {{}})
    .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
    .slice(0, 12)
    .reduce((out, [key, value]) => {{ out[key] = clean(value, 240); return out; }}, {{}});
  const priceFrom = (obj) => {{
    if (!obj || typeof obj !== 'object') return '';
    for (const key of ['price', 'priceText', 'currentPrice', 'salePrice', 'regularPrice', 'amount', 'value']) {{
      const value = obj[key];
      if (typeof value === 'string' || typeof value === 'number') return clean(value, 120);
      if (value && typeof value === 'object') {{
        const nested = priceFrom(value);
        if (nested) return nested;
      }}
    }}
    if (obj.offers) return priceFrom(Array.isArray(obj.offers) ? obj.offers[0] : obj.offers);
    return '';
  }};
  const imageFrom = (obj) => {{
    if (!obj || typeof obj !== 'object') return '';
    for (const key of ['image', 'images', 'thumbnail', 'thumbnailUrl', 'mainImage', 'media']) {{
      const value = obj[key];
      if (typeof value === 'string') return absolute(value);
      if (Array.isArray(value)) {{
        for (const item of value) {{
          const found = imageFrom({{image: item}});
          if (found) return found;
        }}
      }} else if (value && typeof value === 'object') {{
        const found = firstString(value.url || value.src || value.contentUrl || value);
        if (found) return absolute(found);
      }}
    }}
    return '';
  }};
  const urlFrom = (obj) => {{
    if (!obj || typeof obj !== 'object') return '';
    for (const key of ['url', 'href', 'link', 'permalink', 'canonicalUrl', '@id']) {{
      if (typeof obj[key] === 'string' || typeof obj[key] === 'number') return absolute(obj[key]);
    }}
    if (typeof obj.handle === 'string' && /^\\/?[a-z0-9][a-z0-9-_/]*$/i.test(obj.handle)) return absolute(obj.handle);
    return '';
  }};
  const recordFrom = (obj, source, path) => {{
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
    const name = firstString(obj.name || obj.title || obj.headline || obj.productName || obj.displayName || obj.companyName);
    const url = urlFrom(obj);
    const image = imageFrom(obj);
    const price = priceFrom(obj);
    const documentUrl = firstString(obj.fileUrl || obj.documentUrl || obj.downloadUrl || obj.pdfUrl || obj.href);
    const kind = typeName(obj);
    const hasUsefulKeys = Object.keys(obj).some(key => /^(?:name|title|headline|url|href|price|offers|image|images|brand|sku|description|date|published|file|document|pdf|website)$/i.test(key));
    if (!name && !url && !image && !price && !documentUrl) return null;
    if (!hasUsefulKeys && !kind) return null;
    const record = {{
      source,
      path,
      type: kind,
      name,
      url: url || absolute(documentUrl),
      image,
      price,
      brand: firstString(obj.brand || obj.manufacturer || obj.vendor),
      description: firstString(obj.description || obj.summary || obj.subtitle),
      date: firstString(obj.datePublished || obj.dateModified || obj.publishedAt || obj.published_on || obj.date),
      raw_keys: Object.keys(obj).slice(0, 30),
      fields: scalarKeys(obj),
    }};
    return Object.fromEntries(Object.entries(record).filter(([, value]) => Array.isArray(value) ? value.length : Boolean(value)));
  }};
  const walk = (value, source, path, records, seen, depth = 0) => {{
    if (records.length >= limit || depth > 8 || value == null) return;
    if (typeof value !== 'object') return;
    if (seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {{
      for (let i = 0; i < Math.min(value.length, 400) && records.length < limit; i++) {{
        walk(value[i], source, `${{path}}[${{i}}]`, records, seen, depth + 1);
      }}
      return;
    }}
    const record = recordFrom(value, source, path);
    if (record) records.push(record);
    for (const [key, child] of Object.entries(value).slice(0, 80)) {{
      if (records.length >= limit) break;
      if (child && typeof child === 'object') walk(child, source, path ? `${{path}}.${{key}}` : key, records, seen, depth + 1);
    }}
  }};
  const parseJson = (text) => {{
    try {{ return JSON.parse(text); }} catch (err) {{ return null; }}
  }};
  const sources = [];
  const records = [];
  const addSource = (source) => {{
    if (sources.length < maxSources) sources.push(source);
  }};
  const consume = (label, text, path) => {{
    const trimmed = String(text == null ? '' : text).trim().slice(0, 2000000);
    if (!trimmed) return;
    const parsed = parseJson(trimmed);
    if (!parsed) return;
    const before = records.length;
    walk(parsed, label, path || '', records, new WeakSet());
    addSource({{label, path: path || '', size: trimmed.length, record_count: records.length - before}});
  }};
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {{
    consume('json-ld', script.textContent || '', script.id || script.getAttribute('data-rh') || '');
  }}
  const next = document.getElementById('__NEXT_DATA__');
  if (next) consume('__NEXT_DATA__', next.textContent || '', '#__NEXT_DATA__');
  const nuxt = document.getElementById('__NUXT_DATA__');
  if (nuxt) consume('__NUXT_DATA__', nuxt.textContent || '', '#__NUXT_DATA__');
  for (const script of Array.from(document.querySelectorAll('script[type="application/json"],script[type="application/graphql-response+json"]')).slice(0, 20)) {{
    if (script.id === '__NEXT_DATA__' || script.id === '__NUXT_DATA__') continue;
    consume(script.id ? `json-script#${{script.id}}` : 'json-script', script.textContent || '', script.id || '');
  }}
  const metaRecord = {{}};
  for (const meta of document.querySelectorAll('meta[property],meta[name],link[rel]')) {{
    const key = clean(meta.getAttribute('property') || meta.getAttribute('name') || meta.getAttribute('rel'), 120);
    const value = clean(meta.getAttribute('content') || meta.getAttribute('href'), 1000);
    if (!key || !value) continue;
    if (/^(?:og:|product:|twitter:|article:|citation_|dc\\.|schema:|canonical$)/i.test(key)) metaRecord[key] = value;
  }}
  if (Object.keys(metaRecord).length) {{
    const obj = {{
      name: metaRecord['og:title'] || metaRecord['twitter:title'] || metaRecord.title,
      url: metaRecord['og:url'] || metaRecord.canonical,
      image: metaRecord['og:image'] || metaRecord['twitter:image'],
      price: metaRecord['product:price:amount'] || metaRecord['product:sale_price:amount'],
      description: metaRecord['og:description'] || metaRecord.description || metaRecord['twitter:description'],
      datePublished: metaRecord['article:published_time'] || metaRecord['citation_publication_date'],
      meta: metaRecord,
    }};
    const record = recordFrom(obj, 'meta', 'head');
    if (record) records.push(record);
    addSource({{label: 'meta', path: 'head', size: JSON.stringify(metaRecord).length, record_count: record ? 1 : 0}});
  }}
  const deduped = [];
  const seenKeys = new Set();
  for (const record of records) {{
    const key = [record.url || '', record.name || '', record.price || '', record.image || ''].join('|').toLowerCase();
    if (seenKeys.has(key)) continue;
    seenKeys.add(key);
    deduped.push(record);
    if (deduped.length >= limit) break;
  }}
  return {{
    url: location.href,
    title: document.title || '',
    source_count: sources.length,
    sources,
    record_count: deduped.length,
    records: deduped,
  }};
}})()
"""
    return js(expression)


def repeated_items_snapshot(min_count=3, limit=8, include_prices=True):
    """Find repeated visible cards/list items and suggest an extraction selector."""
    expression = f"""
(() => {{
  const minCount = {int(min_count)};
  const limit = {int(limit)};
  const includePrices = {json.dumps(bool(include_prices))};
  const priceRe = /(?:[$€£¥]\\s?\\d|\\d[\\d.,]*\\s?(?:€|eur|usd|gbp|kr|dkk|sek|nok)|\\d+\\s?(?:Mbit|Mbps|GB|TB))/i;
  const visible = (el) => {{
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width >= 40 && r.height >= 20 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  }};
  const clean = (text, max = 240) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const recordText = (el, max = 500) => {{
    const parts = [el.innerText || el.textContent || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || ''];
    for (const img of el.querySelectorAll('img[alt]')) parts.push(img.getAttribute('alt') || '');
    return clean(parts.filter(Boolean).join(' '), max);
  }};
  const selectorCandidates = (el) => {{
    if (!el || !el.tagName) return [];
    const tag = el.tagName.toLowerCase();
    const out = [];
    const utilityClassRe = /^(?:ng-|css-|sc-|_[a-z0-9]|flex|grid|block|inline|hidden|relative|absolute|fixed|sticky|static|container|row|col|clearfix|sr-only|w-|h-|min-w-|min-h-|max-w-|max-h-|p[trblxy]?[-_]|m[trblxy]?[-_]|gap[-_]|space-[xy]-|text[-_]|font[-_]|leading[-_]|tracking[-_]|bg[-_]|border(?:[-_]|$)|rounded(?:[-_]|$)|shadow(?:[-_]|$)|opacity[-_]|overflow[-_]|z[-_]|items[-_]|justify[-_]|content[-_]|self[-_]|place[-_])/i;
    const classes = [...el.classList].filter(c => c && !utilityClassRe.test(c)).slice(0, 2);
    for (const attr of ['data-testid', 'data-test', 'data-cy', 'data-component']) {{
      const value = el.getAttribute(attr);
      if (value && value.length <= 80) out.push(`${{tag}}[${{attr}}="${{CSS.escape(value)}}"]`);
    }}
    const role = el.getAttribute('role');
    if (role && /^(?:listitem|article|row|gridcell|option|menuitem|button|link)$/i.test(role)) {{
      out.push(`${{tag}}[role="${{CSS.escape(role)}}"]`);
    }}
    const itemtype = el.getAttribute('itemtype');
    if (itemtype && itemtype.length <= 160) out.push(`${{tag}}[itemtype="${{CSS.escape(itemtype)}}"]`);
    if (classes.length) {{
      out.push(`${{tag}}.${{classes.map(c => CSS.escape(c)).join('.')}}`);
      for (const cls of classes) out.push(`${{tag}}.${{CSS.escape(cls)}}`);
    }}
    if (el.id) out.push(`${{tag}}#${{CSS.escape(el.id)}}`);
    if (!out.length) out.push(tag);
    return [...new Set(out)];
  }};
  const scoreGroup = (items, selector) => {{
    const samples = items.slice(0, 5).map(el => recordText(el));
    const nonempty = samples.filter(Boolean);
    const priceSignals = samples.filter(text => priceRe.test(text)).length;
    const avgLen = nonempty.reduce((sum, text) => sum + text.length, 0) / Math.max(1, nonempty.length);
    const links = new Set();
    const detailLinks = [];
    let imageCount = 0;
    for (const el of items.slice(0, 10)) {{
      const itemLinks = (el.matches('a[href]') ? [el] : []).concat(Array.from(el.querySelectorAll('a[href]')));
      for (const a of itemLinks) {{
        if (!a.href) continue;
        links.add(a.href);
        if (!detailLinks.some(link => link.href === a.href)) {{
          detailLinks.push({{ text: clean([a.innerText || a.textContent || '', a.getAttribute('aria-label') || '', a.getAttribute('title') || ''].filter(Boolean).join(' '), 200), href: a.href }});
        }}
      }}
      imageCount += el.querySelectorAll('img[src],img[srcset],picture source[srcset]').length;
    }}
    let score = items.length * 2 + Math.min(avgLen / 30, 8) + Math.min(links.size, 8) + Math.min(imageCount, 6);
    if (priceSignals) score += priceSignals * 6;
    if (/^(li|div|article|section|tr)($|[.#])/.test(selector)) score += 1;
    return {{
      selector,
      count: items.length,
      price_signal_count: priceSignals,
      link_count: links.size,
      detail_link_count: links.size,
      detail_links: detailLinks.slice(0, 8),
      image_count: imageCount,
      score,
      samples,
    }};
  }};
  const groups = new Map();
  for (const el of document.querySelectorAll('article, section, li, tr, [role="listitem"], [role="article"], [role="row"], [role="gridcell"], [role="option"], [itemscope], [itemtype], [data-testid], [data-test], [data-cy], [data-component], a[href], button, [role="button"], [class]')) {{
    if (!visible(el)) continue;
    const text = recordText(el, 500);
    if (text.length < 12) continue;
    for (const selector of selectorCandidates(el)) {{
      if (!groups.has(selector)) groups.set(selector, []);
      groups.get(selector).push(el);
    }}
  }}
  const candidates = [];
  for (const [selector, items] of groups) {{
    if (items.length < minCount) continue;
    const scored = scoreGroup(items, selector);
    if (includePrices && scored.price_signal_count === 0 && scored.link_count === 0 && scored.score < 14) continue;
    candidates.push(scored);
  }}
  candidates.sort((a, b) => b.score - a.score || b.price_signal_count - a.price_signal_count || b.count - a.count);
  const recommended = candidates[0] || null;
  return {{
    recommended_action: recommended ? 'extract_repeated_items' : null,
    recommended_selector: recommended ? recommended.selector : null,
    next_extract_hint: recommended ? `extract_repeated_items(selector=${{JSON.stringify(recommended.selector)}})` : null,
    candidates: candidates.slice(0, limit),
  }};
}})()
"""
    return js(expression)


def extract_repeated_items(selector, limit=50, include_html=False):
    """Extract compact records from repeated page elements matching selector."""
    expression = f"""
(() => {{
  const selector = {json.dumps(selector)};
  const limit = {int(limit)};
  const includeHtml = {json.dumps(bool(include_html))};
  const clean = (text, max = 2000) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const imageAlts = (el) => Array.from(el.querySelectorAll('img[alt]')).map(img => clean(img.getAttribute('alt'), 200)).filter(Boolean).slice(0, 8);
  const imageRecords = (el) => {{
    const imgRecords = Array.from(el.querySelectorAll('img[src],img[srcset],img[data-src],img[data-srcset]')).map(img => {{
      const rect = img.getBoundingClientRect();
      return {{
        alt: clean(img.getAttribute('alt'), 200),
        src: img.currentSrc || img.src || img.getAttribute('src') || '',
        srcset: img.getAttribute('srcset') || '',
        data_src: img.getAttribute('data-src') || '',
        data_srcset: img.getAttribute('data-srcset') || '',
        width: Math.round(rect.width || img.naturalWidth || 0),
        height: Math.round(rect.height || img.naturalHeight || 0),
      }};
    }});
    const sourceRecords = Array.from(el.querySelectorAll('picture source[srcset],source[srcset]')).map(source => ({{
      alt: '',
      src: '',
      srcset: source.getAttribute('srcset') || '',
      data_src: '',
      data_srcset: source.getAttribute('data-srcset') || '',
      media: source.getAttribute('media') || '',
      type: source.getAttribute('type') || '',
      width: 0,
      height: 0,
    }}));
    return imgRecords.concat(sourceRecords).filter(img => img.src || img.srcset || img.data_src || img.data_srcset || img.alt).slice(0, 8);
  }};
  const actionText = (el) => clean([
    el.innerText || el.textContent || el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    ...imageAlts(el),
  ].filter(Boolean).join(' '), 200);
  const recordText = (el) => clean([
    el.innerText || el.textContent || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    ...imageAlts(el),
  ].filter(Boolean).join(' '));
  const stableAttributes = (el) => {{
    const attrs = {{}};
    for (const name of ['id', 'role', 'data-testid', 'data-test', 'data-cy', 'data-component', 'itemtype', 'itemprop', 'aria-label', 'title']) {{
      const value = clean(el.getAttribute(name), 240);
      if (value) attrs[name] = value;
    }}
    if (el.matches('a[href]')) attrs.href = el.href;
    return attrs;
  }};
  const directCellNodes = (el) => {{
    const tag = (el.tagName || '').toUpperCase();
    if (tag === 'TR') {{
      return Array.from(el.children).filter(child => ['TH', 'TD'].includes((child.tagName || '').toUpperCase()));
    }}
    if (el.getAttribute('role') === 'row') {{
      return Array.from(el.children).filter(child => ['cell', 'gridcell', 'columnheader', 'rowheader'].includes(child.getAttribute('role') || ''));
    }}
    return [];
  }};
  const tableHeadersForRow = (row) => {{
    if ((row.tagName || '').toUpperCase() !== 'TR') return [];
    const table = row.closest('table');
    if (!table) return [];
    const headerRow = table.querySelector('thead tr') || Array.from(table.querySelectorAll('tr')).find(tr => tr.querySelector('th'));
    if (!headerRow || headerRow === row) return [];
    const out = [];
    for (const cell of Array.from(headerRow.children).filter(cell => ['TH', 'TD'].includes((cell.tagName || '').toUpperCase()))) {{
      const span = Math.max(1, parseInt(cell.getAttribute('colspan') || '1', 10) || 1);
      for (let i = 0; i < span; i++) out.push(clean(cell.textContent, 200));
    }}
    return out;
  }};
  const ariaHeadersForRow = (row) => {{
    const container = row.closest('[role="table"],[role="grid"],[role="treegrid"]');
    if (!container) return [];
    return Array.from(container.querySelectorAll('[role="columnheader"]')).map(cell => clean(cell.textContent, 200));
  }};
  const idRefTexts = (value) => (value || '').split(/\\s+/).map(id => {{
    const node = id ? document.getElementById(id) : null;
    return node ? clean(node.textContent, 200) : '';
  }}).filter(Boolean);
  const rowHeaderTexts = (cell, row) => {{
    if (row.getAttribute('role') === 'row') {{
      return Array.from(row.children)
        .filter(other => other !== cell && other.getAttribute('role') === 'rowheader')
        .map(other => clean(other.textContent, 200))
        .filter(Boolean);
    }}
    return Array.from(row.children)
      .filter(other => other !== cell && (other.tagName || '').toUpperCase() === 'TH')
      .filter(other => ['row', 'rowgroup'].includes(other.getAttribute('scope') || '') || !other.getAttribute('scope'))
      .map(other => clean(other.textContent, 200))
      .filter(Boolean);
  }};
  const headerLabelsForCell = (cell, row, cellIndex, headers, ariaHeaders) => Array.from(new Set([
    ...rowHeaderTexts(cell, row),
    ...idRefTexts(cell.getAttribute('headers')),
    ...idRefTexts(cell.getAttribute('aria-labelledby')),
    headers[cellIndex] || '',
    ariaHeaders[cellIndex] || '',
    clean(cell.getAttribute('data-label'), 200),
    clean(cell.getAttribute('aria-label'), 200),
  ].filter(Boolean))).slice(0, 6);
  const cellRecords = (el) => {{
    const cells = directCellNodes(el);
    if (!cells.length) return [];
    const headers = tableHeadersForRow(el);
    const ariaHeaders = headers.length ? [] : ariaHeadersForRow(el);
    return cells.map((cell, cellIndex) => {{
      const headerLabels = headerLabelsForCell(cell, el, cellIndex, headers, ariaHeaders);
      const cellLinks = Array.from(cell.querySelectorAll('a[href]')).slice(0, 4).map(a => ({{
        text: actionText(a),
        aria_label: clean(a.getAttribute('aria-label'), 160),
        title: clean(a.getAttribute('title'), 160),
        href: a.href,
      }}));
      return {{
        index: cellIndex,
        header: headerLabels.join(' / '),
        headers: headerLabels,
        text: recordText(cell),
        links: cellLinks,
      }};
    }}).filter(cell => cell.text || cell.header || cell.links.length).slice(0, 20);
  }};
  const priceRe = /(?:[$€£¥]\\s?\\d|\\d[\\d.,]*\\s?(?:€|eur|usd|gbp|kr|dkk|sek|nok)|\\d+\\s?(?:Mbit|Mbps|GB|TB))/ig;
  return Array.from(document.querySelectorAll(selector)).slice(0, limit).map((el, index) => {{
    const text = recordText(el);
    const headings = Array.from(el.querySelectorAll('h1,h2,h3,h4,[role="heading"]')).map(h => clean(h.textContent, 200)).filter(Boolean);
    const labels = Array.from(new Set([clean(el.getAttribute('aria-label'), 200), clean(el.getAttribute('title'), 200), ...imageAlts(el)].filter(Boolean))).slice(0, 12);
    const linkNodes = (el.matches('a[href]') ? [el] : []).concat(Array.from(el.querySelectorAll('a[href]')));
    const buttonNodes = (el.matches('button,[role="button"],input[type="button"],input[type="submit"]') ? [el] : []).concat(Array.from(el.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')));
    const seenLinks = new Set();
    const links = linkNodes.filter(a => {{
      if (seenLinks.has(a.href)) return false;
      seenLinks.add(a.href);
      return true;
    }}).slice(0, 8).map(a => ({{ text: actionText(a), aria_label: clean(a.getAttribute('aria-label'), 160), title: clean(a.getAttribute('title'), 160), href: a.href }}));
    const buttons = buttonNodes.slice(0, 8).map(b => actionText(b)).filter(Boolean);
    const prices = Array.from(new Set(text.match(priceRe) || [])).slice(0, 12);
    const images = imageRecords(el);
    const cells = cellRecords(el);
    const attributes = stableAttributes(el);
    const record = {{ index, text, attributes, headings, labels, prices, links, buttons, images }};
    if (cells.length) record.cells = cells;
    if (includeHtml) record.html = clean(el.outerHTML || '', 4000);
    return record;
  }});
}})()
"""
    records = js(expression)
    return {"selector": selector, "count": len(records or []), "records": records or []}


def pricing_cards_snapshot(limit=50):
    """Return visible pricing/product/package cards with extracted commercial signals."""
    expression = f"""
(() => {{
  // __PRICING_CARDS_SNAPSHOT__
  const limit={int(limit)},clean=(t,m=1200)=>(t||'').replace(/\\s+/g,' ').trim().slice(0,m);
  const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>=60&&r.height>=24&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&r.bottom>=0&&r.top<=innerHeight*1.5}};
  const priceRe=/(?:[$€£¥]\\s?\\d[\\d.,]*(?:\\s?(?:\\/|per)\\s?(?:mo|month|mth|yr|year|day|night|kk|mdr|måned|maaned))?|\\d[\\d.,]*\\s?(?:€|eur|usd|gbp|dkk|sek|nok|kr|ron|aud|cad|nzd|jpy)(?:\\s?(?:\\/|per)\\s?(?:mo|month|mth|yr|year|day|night|kk|mdr|måned|maaned))?)/ig;
  const speedRe=/\\b(?:\\d+(?:[.,]\\d+)?\\s?(?:gb|mb|tb|gbit|mbit|gbps|mbps)|unlimited|ubegrænset|ubegrenset|fri data|5g|4g|fiber|fibre|coax|cable|broadband|mobile)\\b/ig;
  const dataRe=/\\b(?:(?:\\d+(?:[.,]\\d+)?\\s?(?:gb|mb|tb)\\s*(?:data|datapakke|internet)?|unlimited\\s+data|unlimited|ubegrænset\\s+data|ubegrænset|ubegrenset|fri\\s+data))\\b/ig;
  const networkRe=/\\b(?:5g|4g|lte|fiber|fibre|fibernet|coax|cable|dsl|broadband|mobile broadband|mobilt bredbånd|internet)\\b/ig;
  const contractRe=/\\b(?:no\\s+(?:binding|commitment|contract)|without commitment|sans engagement|ingen binding|binding|commitment|contract|\\d+\\s?(?:month|months|måneder|maaneder|år|year|years))\\b/ig;
  const offerRe=/\\b(?:new customer|new customers|student|standard|senior|family|promo|promotion|discount|offer|early bird|sale|para nuevos clientes|nuevos clientes|ny kunde|nye kunder)\\b/ig;
  const planLabelRe=/\\b(?:plan|package|pakke|subscription|abonnement|tariff|ticket|bundle|deal|membership|product)\\b[:\\s-]*([A-ZÆØÅa-zæøå0-9][A-ZÆØÅa-zæøå0-9 .,+/&-]{{2,80}})/i;
  const textOf=e=>clean([e.innerText||e.textContent||'',e.getAttribute('aria-label')||'',e.getAttribute('title')||''].filter(Boolean).join(' '));
  const uniq=a=>Array.from(new Set(a.map(x=>clean(x,160)).filter(Boolean))).slice(0,12);
  const links=e=>Array.from(e.querySelectorAll('a[href]')).slice(0,6).map(a=>({{text:textOf(a).slice(0,180),href:a.href}}));
  const images=e=>Array.from(e.querySelectorAll('img[src],img[srcset],img[data-src],picture source[srcset]')).slice(0,6).map(img=>({{alt:clean(img.getAttribute('alt'),180),src:img.currentSrc||img.src||img.getAttribute('src')||'',srcset:img.getAttribute('srcset')||'',data_src:img.getAttribute('data-src')||''}})).filter(i=>i.alt||i.src||i.srcset||i.data_src);
  const headings=e=>Array.from(e.querySelectorAll('h1,h2,h3,h4,[role=heading]')).map(h=>clean(h.textContent,160)).filter(Boolean).slice(0,6);
  const labelled=e=>Array.from(e.querySelectorAll('[aria-label],[title],[alt],[itemprop],[data-testid],[data-test]')).flatMap(n=>['aria-label','title','alt','itemprop','data-testid','data-test'].map(a=>clean(n.getAttribute(a),120))).filter(Boolean).slice(0,24);
  const currencyOf=raw=>{{const s=(raw||'').toLowerCase();if(/\\baud\\b/.test(s))return 'AUD';if(/\\bcad\\b/.test(s))return 'CAD';if(/\\bnzd\\b/.test(s))return 'NZD';if(/\\busd\\b/.test(s)||/[$]/.test(raw))return 'USD';if(/€|eur/.test(s))return 'EUR';if(/£|gbp/.test(s))return 'GBP';if(/¥|jpy/.test(s))return 'JPY';if(/\\bdkk\\b/.test(s))return 'DKK';if(/\\bsek\\b/.test(s))return 'SEK';if(/\\bnok\\b/.test(s))return 'NOK';if(/\\bkr\\b/.test(s))return 'KR';if(/\\bron\\b/.test(s))return 'RON';return null}};
  const amountOf=raw=>{{const m=(raw||'').match(/\\d[\\d.,]*/);if(!m)return null;let n=m[0];if(n.includes(',')&&n.includes('.'))n=n.lastIndexOf(',')>n.lastIndexOf('.')?n.replace(/\\./g,'').replace(',','.'):n.replace(/,/g,'');else if(n.includes(','))n=n.replace(',','.');const v=Number(n.replace(/[^\\d.]/g,''));return Number.isFinite(v)?v:null}};
  const billingOf=raw=>{{const s=(raw||'').toLowerCase();if(/\\/\\s*(mo|month|mth)|per\\s+(mo|month|mth)|\\/kk|mdr|måned|maaned/.test(s))return 'monthly';if(/\\/\\s*(yr|year)|per\\s+year|årlig|aarlig/.test(s))return 'yearly';if(/\\/\\s*day|per\\s+day/.test(s))return 'daily';if(/\\/\\s*night|per\\s+night/.test(s))return 'nightly';return null}};
  const priceAmounts=prices=>prices.map(raw=>({{raw,amount:amountOf(raw),currency:currencyOf(raw),billing_period:billingOf(raw)}}));
  const packageName=(hs,text)=>clean((hs&&hs[0])||((text.match(planLabelRe)||[])[1])||'',160);
  const tokenTest=(re,v)=>{{re.lastIndex=0;return re.test(v)}};
  const providerCandidates=(hs,labels,imgs,text)=>uniq([...hs,...labels,...imgs.map(i=>i.alt||''),...text.split(/[|•\\n]/).slice(0,3)].map(v=>clean(v,80)).filter(v=>v&&!tokenTest(priceRe,v)&&!tokenTest(speedRe,v)&&!tokenTest(contractRe,v))).slice(0,5);
  const sel=e=>{{if(e.id)return '#'+CSS.escape(e.id);for(const a of ['data-testid','data-test','data-cy','data-component','itemtype']){{const v=e.getAttribute(a);if(v)return `${{e.tagName.toLowerCase()}}[${{a}}="${{CSS.escape(v)}}"]`}}const cls=[...e.classList].filter(c=>!/^(css-|sc-|ng-|flex|grid|block|row|col|p[trblxy]?-|m[trblxy]?-|text-|bg-|border|rounded|shadow|w-|h-)/i.test(c)).slice(0,2);return e.tagName.toLowerCase()+(cls.length?'.'+cls.map(c=>CSS.escape(c)).join('.'):'')}};
  const nodes=[...document.querySelectorAll('article,section,li,tr,[role=listitem],[role=row],[role=article],[itemscope],[itemtype],[class*=card],[class*=tile],[class*=plan],[class*=package],[class*=offer],[class*=product],[class*=price],[data-testid],[data-test]')].filter(visible);
  const seen=new Set(),out=[];
  for(const e of nodes){{const text=textOf(e);if(text.length<8)continue;const prices=uniq(text.match(priceRe)||[]),speeds=uniq(text.match(speedRe)||[]),data_allowances=uniq(text.match(dataRe)||[]),network_types=uniq(text.match(networkRe)||[]),contracts=uniq(text.match(contractRe)||[]),offers=uniq(text.match(offerRe)||[]);const hs=headings(e),ls=links(e),imgs=images(e),labels=labelled(e),package_name=packageName(hs,text),providers=providerCandidates(hs,labels,imgs,text),price_amounts=priceAmounts(prices);let score=prices.length*18+speeds.length*8+data_allowances.length*6+network_types.length*5+contracts.length*6+offers.length*5+hs.length*4+labels.length+ls.length*2+imgs.length*2;if(/price|plan|package|offer|product|card|tile|subscription|tariff|ticket/i.test(`${{e.className||''}} ${{e.id||''}} ${{e.getAttribute('data-testid')||''}} ${{e.getAttribute('data-test')||''}}`))score+=10;if(!prices.length&&score<18)continue;const r=e.getBoundingClientRect(),key=`${{prices.join('|')}}|${{hs.join('|')}}|${{text.slice(0,120)}}`;if(seen.has(key))continue;seen.add(key);out.push({{selector:sel(e),tag:e.tagName.toLowerCase(),score,text:clean(text,1800),headings:hs,package_name,provider_candidates:providers,prices,price_amounts,speeds,data_allowances,network_types,contracts,contract_terms:contracts,offer_types:offers,offer_labels:offers,labels,links:ls,images:imgs,rect:{{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),in_viewport:r.bottom>=0&&r.top<=innerHeight&&r.right>=0&&r.left<=innerWidth}}}})}}
  out.sort((a,b)=>b.score-a.score);
  return {{count:out.length,cards:out.slice(0,limit)}};
}})()
"""
    data = js(expression) or {}
    cards = data.get("cards") if isinstance(data, dict) else []
    detail_actions = []
    seen_hrefs = set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        card_text = card.get("text") or " ".join(card.get("headings") or [])
        for link in card.get("links") or []:
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            detail_actions.append({"text": card_text or link.get("text"), "href": href})
            break
    return {
        "count": len(cards or []),
        "cards": cards or [],
        "detail_action_count": len(detail_actions),
        "detail_actions": detail_actions,
    }


def rows_snapshot(limit=8):
    """Find row-like table/grid/list records and suggest row-scoped extraction.

    Use this on search-result grids, docket tables, comparison tables, and SPA
    row lists where links/buttons must be associated with the correct row.
    """
    expression = f"""
(() => {{
 const clean=(t,m=260)=>(t||'').replace(/\\s+/g,' ').trim().slice(0,m),vis=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>60&&r.height>12&&s.display!=='none'&&s.visibility!=='hidden'}},fileRe=/(pdf|docx?|xlsx?|zip|download|file|filing|attachment|exhibit|transmittal)/i;
 const choices=['tbody tr','tr','[role="row"]','[data-rowindex]','[aria-rowindex]','[class*="row"]','[class*="record"]','[class*="result"]','li'];
 const candidates=choices.map(selector=>{{const rows=[...document.querySelectorAll(selector)].filter(r=>vis(r)&&clean(r.innerText||r.textContent,800).length>12).slice(0,80);let action_count=0,file_action_count=0,samples=[];for(const r of rows.slice(0,8)){{const links=[...r.querySelectorAll('a[href]')],buttons=[...r.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')];action_count+=links.length+buttons.length;file_action_count+=links.filter(a=>fileRe.test(`${{a.href}} ${{a.textContent}} ${{a.getAttribute('aria-label')||''}}`)).length;samples.push(clean(r.innerText||r.textContent))}}return {{selector,count:rows.length,action_count,file_action_count,score:rows.length*3+action_count*2+file_action_count*5,samples:samples.filter(Boolean).slice(0,4)}}}}).filter(c=>c.count&&c.action_count).sort((a,b)=>b.score-a.score||b.file_action_count-a.file_action_count);
 const r=candidates[0]||null;return {{recommended_action:r?'extract_grid_rows':null,recommended_selector:r?r.selector:null,next_extract_hint:r?`extract_grid_rows(selector=${{JSON.stringify(r.selector)}})`:null,candidates:candidates.slice(0,{int(limit)})}};
}})()
"""
    return js(expression)


def extract_grid_rows(selector=None, limit=50, include_html=False):
    """Extract row-scoped cells, links, buttons, and file/document actions."""
    if not selector:
        snapshot = rows_snapshot(limit=1)
        selector = snapshot.get("recommended_selector")
        if not selector:
            return {"selector": None, "count": 0, "records": [], "diagnosis": "no row selector found"}
    expression = f"""
(() => {{
 const selector={json.dumps(selector)},limit={int(limit)},includeHtml={json.dumps(bool(include_html))},clean=(t,m=1400)=>(t||'').replace(/\\s+/g,' ').trim().slice(0,m),fileRe=/(\\.pdf($|[?#])|\\.docx?($|[?#])|\\.xlsx?($|[?#])|\\.zip($|[?#])|download|file|filing|attachment|exhibit|transmittal)/i;
 const rect=e=>{{const r=e.getBoundingClientRect();return {{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),center_x:Math.round(r.x+r.width/2),center_y:Math.round(r.y+r.height/2)}}}},txt=e=>clean([e.innerText||e.textContent||e.value||'',e.getAttribute('aria-label')||'',e.getAttribute('title')||''].filter(Boolean).join(' '),200);
 const link=a=>{{const text=txt(a),href=a.href||a.getAttribute('href')||'',file_like=fileRe.test(`${{href}} ${{text}}`);return {{text,href,file_like,rect:rect(a)}}}},button=b=>({{text:txt(b),role:clean(b.getAttribute('role'),80)||(b.tagName||'').toLowerCase(),rect:rect(b)}});
 return [...document.querySelectorAll(selector)].slice(0,limit).map((row,index)=>{{const heads=[...(row.closest('table')?.querySelectorAll('thead th,thead td')||[])].map(h=>clean(h.textContent,120)),raw=[...row.querySelectorAll(':scope>td,:scope>th,:scope>[role="cell"],:scope>[role="gridcell"],:scope>[data-label]')];const kids=raw.length?raw:[...row.children].slice(0,20),cells=kids.map((c,i)=>{{const header=clean(c.getAttribute('data-label')||c.getAttribute('aria-label')||heads[i]||'',160);return {{index:i,header,headers:header?[header]:[],text:clean(c.innerText||c.textContent,900),links:[...c.querySelectorAll('a[href]')].slice(0,6).map(link),buttons:[...c.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')].slice(0,6).map(button)}}}}).filter(c=>c.text||c.header||c.links.length||c.buttons.length),links=[...row.querySelectorAll('a[href]')].slice(0,16).map(link),buttons=[...row.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')].slice(0,16).map(button),file_actions=links.filter(l=>l.file_like),description_fields=cells.filter(c=>/(description|title|subject|name|document|file|requirement|summary)/i.test(c.header)).map(c=>({{header:c.header,text:c.text}})).slice(0,8),rec={{index,text:clean(row.innerText||row.textContent,1800),rect:rect(row),cells,description_fields,links,buttons,file_actions}};if(includeHtml)rec.html=clean(row.outerHTML,4000);return rec}});
}})()
"""
    records = js(expression)
    return {"selector": selector, "count": len(records or []), "records": records or []}


def extract_paginated_grid_rows(
    selector=None,
    next_label="next",
    max_pages=10,
    per_page_limit=100,
    include_html=False,
    stop_on_duplicate_page=True,
):
    """Extract row-scoped records across paginated table/grid/list pages."""
    if not selector:
        snapshot = rows_snapshot(limit=1)
        selector = snapshot.get("recommended_selector")
        if not selector:
            return {
                "selector": None,
                "count": 0,
                "pages_visited": 0,
                "pages": [],
                "records": [],
                "detail_action_count": 0,
                "detail_actions": [],
                "diagnosis": "no row selector found",
            }
    try:
        max_pages_int = max(1, min(int(max_pages or 10), 50))
    except Exception:
        max_pages_int = 10
    try:
        per_page_limit_int = max(1, min(int(per_page_limit or 100), 500))
    except Exception:
        per_page_limit_int = 100

    records = []
    pages = []
    seen_rows = set()
    diagnosis = ""
    for page_index in range(max_pages_int):
        page = extract_grid_rows(selector=selector, limit=per_page_limit_int, include_html=include_html)
        page_records = page.get("records") if isinstance(page, dict) else []
        added = 0
        duplicate = 0
        for record in page_records or []:
            if not isinstance(record, dict):
                continue
            row_links = tuple(
                sorted(
                    link.get("href")
                    for link in (record.get("file_actions") or record.get("links") or [])
                    if isinstance(link, dict) and link.get("href")
                )
            )
            key = (record.get("text") or "", row_links)
            if key in seen_rows:
                duplicate += 1
                continue
            seen_rows.add(key)
            record = dict(record)
            record["page_index"] = page_index
            records.append(record)
            added += 1
        pages.append(
            {
                "page_index": page_index,
                "raw_count": len(page_records or []),
                "added_count": added,
                "duplicate_count": duplicate,
            }
        )
        if page_index >= max_pages_int - 1:
            break
        if stop_on_duplicate_page and page_records and added == 0:
            diagnosis = "stopped after duplicate page"
            break
        try:
            click = click_pagination(next_label, timeout=0)
        except Exception as exc:
            diagnosis = f"stopped after page {page_index + 1}: {exc}"
            break
        if not isinstance(click, dict) or not click.get("clicked"):
            diagnosis = f"stopped after page {page_index + 1}: no pagination click"
            break
        try:
            wait_for_network_idle(timeout=2.0)
        except Exception:
            pass

    detail_actions = []
    seen_hrefs = set()
    for record in records:
        row_text = record.get("text") or ""
        for link in (record.get("file_actions") or record.get("links") or []):
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            detail_actions.append(
                {
                    "page_index": record.get("page_index"),
                    "row_index": record.get("index"),
                    "row_text": row_text[:240],
                    "text": link.get("text") or "",
                    "href": href,
                    "file_like": bool(link.get("file_like")),
                }
            )

    return {
        "selector": selector,
        "count": len(records),
        "pages_visited": len(pages),
        "pages": pages,
        "records": records,
        "detail_action_count": len(detail_actions),
        "detail_actions": detail_actions[:20],
        "diagnosis": diagnosis,
    }


extract_rows = extract_grid_rows
grid_rows_snapshot = rows_snapshot


def current_tab():
    page = _send_meta("current_tab")
    target_id = page.get("targetId") or page.get("target_id")
    session_id = page.get("sessionId") or page.get("session_id")
    return {
        "targetId": target_id,
        "target_id": target_id,
        "sessionId": session_id,
        "session_id": session_id,
        "url": page.get("url", ""),
        "title": page.get("title", ""),
    }


def list_tabs(include_chrome=True):
    out = []
    for target in cdp("Target.getTargets").get("targetInfos", []):
        if target.get("type") != "page":
            continue
        url = target.get("url", "")
        if not include_chrome and url.startswith(INTERNAL):
            continue
        target_id = target.get("targetId")
        out.append(
            {
                "targetId": target_id,
                "target_id": target_id,
                "title": target.get("title", ""),
                "url": url,
            }
        )
    return out


def _mark_tab():
    # Kept as a no-op compatibility hook. Browser-harness marks tab titles for
    # visibility, but here Rust tracks the current target explicitly.
    return None


def switch_tab(target):
    """Switch to a tab by raw target id or a tab dict returned by current_tab/list_tabs."""
    target_id = target.get("targetId") or target.get("target_id") if isinstance(target, dict) else target
    if not target_id:
        raise RuntimeError("switch_tab requires target_id")
    cdp("Target.activateTarget", targetId=target_id)
    session_id = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    _send_meta("set_session", target_id=target_id, session_id=session_id)
    _mark_tab()
    return session_id


def new_tab(url="about:blank"):
    # Match browser-harness: create blank first, attach, then navigate. Passing
    # the final URL to createTarget can race with attach/load polling.
    target_id = cdp("Target.createTarget", url="about:blank")["targetId"]
    switch_tab(target_id)
    if url != "about:blank":
        goto_url(url)
    return target_id


def ensure_real_tab():
    tabs = list_tabs(include_chrome=False)
    if not tabs:
        return None
    try:
        current = current_tab()
        if current["url"] and not current["url"].startswith(INTERNAL):
            return current
    except Exception:
        pass
    switch_tab(tabs[0])
    return tabs[0]


def iframe_target(url_substr):
    for target in cdp("Target.getTargets").get("targetInfos", []):
        if target.get("type") == "iframe" and url_substr in target.get("url", ""):
            return target.get("targetId")
    return None


def wait(seconds=1.0):
    _time.sleep(seconds)


def _timeout_seconds(timeout):
    timeout = float(timeout)
    if timeout > 1000:
        timeout = timeout / 1000
    return min(timeout, 60.0)


def wait_for_load(timeout=15.0):
    timeout = _timeout_seconds(timeout)
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            if js("document.readyState") == "complete":
                return True
        except Exception:
            pass
        _time.sleep(0.3)
    return False


def wait_for_element(selector, timeout=10.0, visible=False):
    timeout = _timeout_seconds(timeout)
    if visible:
        check = (
            f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
            "if(!e)return false;"
            "if(typeof e.checkVisibility==='function')"
            "return e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});"
            "const s=getComputedStyle(e);"
            "return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'}})()"
        )
    else:
        check = f"!!document.querySelector({json.dumps(selector)})"
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if js(check):
            return True
        _time.sleep(0.3)
    return False


def wait_for_network_idle(timeout=10.0, idle_ms=None):
    timeout = _timeout_seconds(timeout)
    if idle_ms is None:
        idle_ms = _browser_network_idle_ms()
    deadline = _time.time() + timeout
    last_activity = _time.time()
    inflight = set()
    active_session = _send_meta("session").get("session_id")
    while _time.time() < deadline:
        for event in drain_events():
            if event.get("session_id") != active_session:
                continue
            method = event.get("method", "")
            params = event.get("params", {})
            if method == "Network.requestWillBeSent":
                inflight.add(params.get("requestId"))
                last_activity = _time.time()
            elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                inflight.discard(params.get("requestId"))
                last_activity = _time.time()
            elif method.startswith("Network."):
                last_activity = _time.time()
        if not inflight and (_time.time() - last_activity) * 1000 >= idle_ms:
            return True
        _time.sleep(0.1)
    return False


def _write_b64_artifact(label, data_b64, suffix=".png", mime_type="image/png"):
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(label or "screenshot")).strip("_") or "screenshot"
    path = ARTIFACT_DIR / f"{int(_time.time() * 1000)}_{safe}{suffix}"
    path.write_bytes(base64.b64decode(data_b64))
    meta = {"path": str(path), "mime_type": mime_type, "detail": "auto", "label": label, "source": "screenshot"}
    __images.append(meta)
    __artifacts.append({"path": str(path), "kind": "image", "mime_type": mime_type})
    return str(path)


def capture_screenshot(label="screenshot", full=False, attach=True, max_dim=None, **kwargs):
    """Save a PNG of the current viewport and return its local artifact path."""
    try:
        target_id = (current_tab() or {}).get("targetId")
        if target_id:
            cdp("Target.activateTarget", session_id=None, targetId=target_id)
        cdp("Page.bringToFront")
        version = cdp("Browser.getVersion", session_id=None)
        if "Headless" in (version.get("userAgent") or ""):
            viewport = _configured_viewport_params()
            if viewport:
                cdp("Emulation.setDeviceMetricsOverride", **viewport)
                _time.sleep(0.2)
    except Exception:
        pass
    params = {"format": kwargs.pop("format", "png")}
    if full:
        params["captureBeyondViewport"] = True
    params.update(kwargs)
    result = cdp("Page.captureScreenshot", **params)
    if not attach:
        return result
    path = _write_b64_artifact(label, result["data"], ".png", "image/png")
    if max_dim:
        try:
            from PIL import Image

            img = Image.open(path)
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim))
                img.save(path)
        except Exception:
            pass
    return path


def note(caption):
    """Mark the current moment as important for the recording, with a short
    human-readable caption (e.g. note("Delta $209 - cheapest fare details")).
    Cheap: it just timestamps a caption; the 2fps session capture already has the
    frame. Call it at each meaningful step so the end-of-run highlight GIF can be
    captioned. Returns the recorded note."""
    record = {"ts_ms": int(_time.time() * 1000), "caption": str(caption)}
    try:
        notes_path = ARTIFACT_DIR / ".capture.notes.ndjson"
        with notes_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
    except Exception:
        pass
    return record


def screenshot(label="screenshot", full=False):
    return capture_screenshot(label=label, full=full, attach=True)


def screenshot_clip(label, x, y, width, height):
    return capture_screenshot(label=label, clip={"x": x, "y": y, "width": width, "height": height, "scale": 1}, attach=True)


def click_at_xy(x, y, button="left", clicks=1):
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=clicks)
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=clicks)
    _after_browser_action_wait()
    return True


def type_text(text):
    cdp("Input.insertText", text=text)
    _after_browser_action_wait()
    return True


_KEYS = {
    "Enter": (13, "Enter", "\r"),
    "Tab": (9, "Tab", "\t"),
    "Backspace": (8, "Backspace", ""),
    "Escape": (27, "Escape", ""),
    "Delete": (46, "Delete", ""),
    " ": (32, "Space", " "),
    "ArrowLeft": (37, "ArrowLeft", ""),
    "ArrowUp": (38, "ArrowUp", ""),
    "ArrowRight": (39, "ArrowRight", ""),
    "ArrowDown": (40, "ArrowDown", ""),
    "Home": (36, "Home", ""),
    "End": (35, "End", ""),
    "PageUp": (33, "PageUp", ""),
    "PageDown": (34, "PageDown", ""),
}

_PRINTABLE_KEY_CODES = {
    "-": (189, "Minus"),
    "=": (187, "Equal"),
    "[": (219, "BracketLeft"),
    "]": (221, "BracketRight"),
    "\\": (220, "Backslash"),
    ";": (186, "Semicolon"),
    "'": (222, "Quote"),
    ",": (188, "Comma"),
    ".": (190, "Period"),
    "/": (191, "Slash"),
    "`": (192, "Backquote"),
}

_MODIFIER_BITS = {
    "alt": 1,
    "option": 1,
    "ctrl": 2,
    "control": 2,
    "cmd": 4,
    "command": 4,
    "meta": 4,
    "shift": 8,
}


def _printable_key_metadata(key):
    if len(key) != 1:
        return None
    if key.isalpha():
        upper = key.upper()
        return ord(upper), f"Key{upper}", key
    if key.isdigit():
        return ord(key), f"Digit{key}", key
    if key in _PRINTABLE_KEY_CODES:
        vk, code = _PRINTABLE_KEY_CODES[key]
        return vk, code, key
    return ord(key), key, key


def _parse_key_chord(key, modifiers):
    if not isinstance(key, str) or "+" not in key:
        return key, modifiers
    parts = [part.strip() for part in key.split("+") if part.strip()]
    if len(parts) < 2:
        return key, modifiers
    parsed_modifiers = modifiers
    for part in parts[:-1]:
        bit = _MODIFIER_BITS.get(part.lower())
        if bit is None:
            return key, modifiers
        parsed_modifiers |= bit
    parsed_key = parts[-1]
    if parsed_key.lower() == "space":
        parsed_key = " "
    return parsed_key, parsed_modifiers


def press_key(key, modifiers=0):
    """Modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift. Chords like "Meta+A" also work."""
    key, modifiers = _parse_key_chord(key, modifiers)
    vk, code, text = _KEYS.get(key) or _printable_key_metadata(key) or (0, key, "")
    base = {
        "key": key,
        "code": code,
        "modifiers": modifiers,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
    }
    event_type = "rawKeyDown" if modifiers else "keyDown"
    cdp("Input.dispatchKeyEvent", type=event_type, **base, **({"text": text} if text and not modifiers else {}))
    cdp("Input.dispatchKeyEvent", type="keyUp", **base)
    _after_browser_action_wait()
    return True


def scroll(x=0, y=0, dy=600, dx=0):
    cdp("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)
    _after_browser_action_wait()
    return True


def _query_selector_node_id(selector):
    doc = cdp("DOM.getDocument", depth=0)
    root = (doc or {}).get("root") or {}
    root_id = root.get("nodeId")
    if not root_id:
        return None
    result = cdp("DOM.querySelector", nodeId=root_id, selector=selector)
    node_id = (result or {}).get("nodeId")
    return node_id or None


def _wait_for_selector_node_id(selector, timeout=0.0):
    deadline = _time.monotonic() + _timeout_seconds(timeout)
    while True:
        node_id = _query_selector_node_id(selector)
        if node_id:
            return node_id
        if timeout <= 0 or _time.monotonic() >= deadline:
            return None
        _time.sleep(0.1)


def _quad_center(quad):
    if not quad or len(quad) < 8:
        return None
    xs = quad[0::2]
    ys = quad[1::2]
    if max(xs) <= min(xs) or max(ys) <= min(ys):
        return None
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def _node_center(node_id):
    try:
        model = (cdp("DOM.getBoxModel", nodeId=node_id) or {}).get("model") or {}
    except Exception:
        return None
    return _quad_center(model.get("border")) or _quad_center(model.get("content"))


def _focus_selector_like_user(selector, timeout=0.0):
    node_id = _wait_for_selector_node_id(selector, timeout=timeout)
    if not node_id:
        return False
    try:
        cdp("DOM.scrollIntoViewIfNeeded", nodeId=node_id)
    except Exception:
        pass
    center = _node_center(node_id)
    if center:
        click_at_xy(center[0], center[1])
        return True
    try:
        cdp("DOM.focus", nodeId=node_id)
        return True
    except Exception:
        return False


def fill_input(selector, text, clear=True, clear_first=None, timeout=0.0):
    """Fill an input by focusing it through CDP, then using browser input events."""
    if clear_first is not None:
        clear = clear_first
    if not _focus_selector_like_user(selector, timeout=timeout):
        raise RuntimeError(f"fill_input: element not found: {selector!r}")
    if clear:
        mods = 4 if sys.platform == "darwin" else 2
        select_all = {
            "key": "a",
            "code": "KeyA",
            "modifiers": mods,
            "windowsVirtualKeyCode": 65,
            "nativeVirtualKeyCode": 65,
        }
        cdp("Input.dispatchKeyEvent", type="rawKeyDown", **select_all)
        cdp("Input.dispatchKeyEvent", type="keyUp", **select_all)
        press_key("Backspace")
    if text:
        type_text(str(text))
    return True


def form_fields_snapshot(limit=30):
    """Return compact rendered form fields with labels, placeholders, and selectors."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 30
    limit_int = max(1, min(limit_int, 200))
    expression = f"""
(() => {{
  // __FORM_FIELDS_SNAPSHOT__
  const clean = (text, max = 220) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'placeholder', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const labelFor = el => {{
    const bits = [el.getAttribute('aria-label')];
    if (el.id) for (const label of document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)) bits.push(label.innerText);
    const parentLabel = el.closest('label');
    if (parentLabel) bits.push(parentLabel.innerText);
    const wrapper = el.closest('[aria-label],[data-label],.field,.form-group,li,div');
    if (wrapper) bits.push(wrapper.getAttribute('aria-label') || wrapper.getAttribute('data-label') || '');
    return clean(bits.filter(Boolean).join(' '));
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  return Array.from(document.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"],[role="combobox"],[role="textbox"]'))
    .filter(el => !el.disabled && visible(el))
    .slice(0, {limit_int})
    .map((el, index) => {{
      const r = el.getBoundingClientRect();
      const type = (el.getAttribute('type') || el.tagName).toLowerCase();
      return {{
        index,
        selector: selectorFor(el),
        tag: el.tagName.toLowerCase(),
        type,
        label: labelFor(el),
        placeholder: clean(el.getAttribute('placeholder')),
        name: clean(el.getAttribute('name')),
        value: clean(el.value || el.textContent, 300),
        required: !!el.required,
        autocomplete: clean(el.getAttribute('autocomplete')),
        aria_expanded: el.getAttribute('aria-expanded'),
        rect: {{
          x: Math.round(r.x), y: Math.round(r.y),
          width: Math.round(r.width), height: Math.round(r.height),
          in_viewport: r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth,
        }},
      }};
    }});
}})()
"""
    fields = js(expression) or []
    return {"count": len(fields), "fields": fields}


def fill_form_field(label_or_placeholder, value, clear=True, timeout=3.0):
    """Fill a rendered text field by label, placeholder, name, selector, or nearby text."""
    needle = str(label_or_placeholder or "").strip().lower()
    if not needle:
        raise RuntimeError("fill_form_field requires a label_or_placeholder")
    expression = f"""
(() => {{
  // __FILL_FORM_FIELD__
  const needle = {json.dumps(needle)};
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'placeholder', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return '';
  }};
  const textFor = el => {{
    const bits = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id];
    if (el.id) for (const label of document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)) bits.push(label.innerText);
    const parent = el.closest('label,[aria-label],[data-label],.field,.form-group,li,div');
    if (parent) bits.push(parent.innerText, parent.getAttribute('aria-label'), parent.getAttribute('data-label'));
    return clean(bits.filter(Boolean).join(' ')).toLowerCase();
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  let best = null, bestScore = -1;
  for (const el of document.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"],[role="combobox"],[role="textbox"]')) {{
    if (el.disabled || !visible(el)) continue;
    const selector = selectorFor(el);
    const hay = textFor(el);
    let score = hay === needle ? 100 : hay.includes(needle) ? 50 : needle.includes(hay) && hay.length > 2 ? 20 : 0;
    if (selector && selector.toLowerCase() === needle) score = 120;
    if (score > bestScore) {{ best = el; bestScore = score; }}
  }}
  return best && bestScore > 0 ? {{
    selector: selectorFor(best),
    score: bestScore,
    matched_text: textFor(best),
    tag: best.tagName.toLowerCase(),
    type: (best.getAttribute('type') || best.tagName).toLowerCase(),
  }} : null;
}})()
"""
    match = js(expression)
    if not match or not match.get("selector"):
        fields = form_fields_snapshot(limit=20)
        raise RuntimeError(f"fill_form_field: no rendered field matched {label_or_placeholder!r}; fields={fields}")
    fill_input(match["selector"], value, clear=clear, timeout=timeout)
    return {"filled": True, "selector": match["selector"], "matched_text": match.get("matched_text", ""), "score": match.get("score")}


def autocomplete_suggestions_snapshot(query=None, limit=20):
    """Return likely visible autocomplete/typeahead suggestions with text and rects."""
    needle = str(query or "").strip().lower()
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 20
    limit_int = max(1, min(limit_int, 100))
    expression = f"""
(() => {{
  // __AUTOCOMPLETE_SUGGESTIONS__
  const needle = {json.dumps(needle)};
  const clean = (text, max = 220) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['aria-label', 'title', 'data-testid', 'data-test', 'data-value', 'value']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' &&
      r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth;
  }};
  const active = document.activeElement;
  const controlled = new Set(((active && active.getAttribute('aria-controls')) || '').split(/\\s+/).filter(Boolean));
  const out = [], seen = new Set();
  const nodes = Array.from(document.querySelectorAll([
    '[role="option"]', '[role="menuitem"]', '[role="treeitem"]', '[role="listitem"]',
    'li', 'option', '[data-value]', '[data-testid]', '[data-test]',
    '.suggestion', '.suggestions', '.autocomplete', '.typeahead', '.pac-item'
  ].join(',')));
  for (const el of nodes) {{
    if (!visible(el)) continue;
    const text = clean([
      el.innerText || el.textContent || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('data-value') || '',
      el.value || '',
    ].filter(Boolean).join(' '));
    if (!text) continue;
    const hay = [
      text,
      el.id || '',
      String(el.className || ''),
      el.getAttribute('role') || '',
      el.getAttribute('data-testid') || '',
      el.getAttribute('data-test') || '',
    ].join(' ').toLowerCase();
    let score = 0;
    if (needle && hay.includes(needle)) score += 120;
    if (needle && needle.includes(text.toLowerCase()) && text.length > 2) score += 35;
    if (/option|menuitem|treeitem|listitem/.test(el.getAttribute('role') || '')) score += 45;
    if (/suggest|autocomplete|typeahead|pac-item|result|dropdown|menu|listbox/.test(hay)) score += 40;
    for (let node = el; node && node !== document.body; node = node.parentElement) {{
      if (controlled.has(node.id)) score += 70;
      const parentHay = [node.id || '', String(node.className || ''), node.getAttribute('role') || ''].join(' ').toLowerCase();
      if (/suggest|autocomplete|typeahead|pac-container|results?|dropdown|listbox|menu/.test(parentHay)) score += 25;
      if (node.getAttribute('role') === 'listbox' || node.getAttribute('role') === 'menu') score += 25;
    }}
    if (!needle && score < 40) continue;
    if (needle && score < 80) continue;
    const r = el.getBoundingClientRect();
    const key = `${{Math.round(r.left)}}:${{Math.round(r.top)}}:${{text}}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({{
      selector: selectorFor(el),
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      text,
      score,
      rect: {{
        x: Math.round(r.x), y: Math.round(r.y),
        width: Math.round(r.width), height: Math.round(r.height),
        in_viewport: true,
      }},
      center: {{x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}},
    }});
  }}
  return out.sort((a, b) => b.score - a.score).slice(0, {limit_int});
}})()
"""
    suggestions = js(expression) or []
    return {"count": len(suggestions), "query": query or "", "suggestions": suggestions}


def select_autocomplete(label_or_placeholder, query, match_text=None, timeout=3.0):
    """Fill an autocomplete field and click the best visible suggestion."""
    filled = fill_form_field(label_or_placeholder, query, clear=True, timeout=timeout)
    needle = str(match_text or query or "").strip().lower()
    deadline = _time.monotonic() + _timeout_seconds(timeout)
    snapshot = {"suggestions": []}
    while True:
        snapshot = autocomplete_suggestions_snapshot(needle, limit=20)
        if snapshot["suggestions"]:
            break
        if _time.monotonic() >= deadline:
            raise RuntimeError(
                f"select_autocomplete: no visible suggestion matched {needle!r}; suggestions={autocomplete_suggestions_snapshot(limit=20)}"
            )
        _time.sleep(0.2)
    best = snapshot["suggestions"][0]
    click_at_xy(best["center"]["x"], best["center"]["y"])
    return {
        "selected": True,
        "field_selector": filled.get("selector", ""),
        "selector": best.get("selector", ""),
        "matched_text": best.get("text", ""),
        "score": best.get("score"),
        "x": best["center"]["x"],
        "y": best["center"]["y"],
    }


def action_controls_snapshot(limit=30):
    """Return compact rendered buttons, links, and action controls with text/selectors/rects."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 30
    limit_int = max(1, min(limit_int, 200))
    expression = f"""
(() => {{
  // __ACTION_CONTROLS_SNAPSHOT__
  const clean = (text, max = 180) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'value', 'title', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const textFor = el => clean([
    el.innerText || '',
    el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    el.getAttribute('name') || '',
    el.id || '',
  ].filter(Boolean).join(' '));
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  return Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],input[type="reset"],a[href],[role="button"],[role="link"],[onclick]'))
    .filter(el => !el.disabled && visible(el))
    .slice(0, {limit_int})
    .map((el, index) => {{
      const r = el.getBoundingClientRect();
      return {{
        index,
        selector: selectorFor(el),
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        text: textFor(el),
        href: el.href || '',
        name: clean(el.getAttribute('name')),
        aria_label: clean(el.getAttribute('aria-label')),
        rect: {{
          x: Math.round(r.x), y: Math.round(r.y),
          width: Math.round(r.width), height: Math.round(r.height),
          in_viewport: r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth,
        }},
      }};
    }});
}})()
"""
    actions = js(expression) or []
    return {"count": len(actions), "actions": actions}


def click_button(label_or_text, timeout=3.0):
    """Click a rendered button/link/action by visible text, aria-label, name, or selector."""
    needle = str(label_or_text or "").strip().lower()
    if not needle:
        raise RuntimeError("click_button requires a label_or_text")
    expression = f"""
(() => {{
  // __CLICK_BUTTON__
  const needle = {json.dumps(needle)};
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['name', 'aria-label', 'value', 'title', 'data-testid', 'data-test']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return '';
  }};
  const textFor = el => clean([
    el.innerText || '',
    el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || '',
    el.getAttribute('name') || '',
    el.id || '',
  ].filter(Boolean).join(' ')).toLowerCase();
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }};
  let best = null, bestScore = -1, bestText = '';
  for (const el of document.querySelectorAll('button,input[type="button"],input[type="submit"],input[type="reset"],a[href],[role="button"],[role="link"],[onclick]')) {{
    if (el.disabled || !visible(el)) continue;
    const selector = selectorFor(el);
    const hay = textFor(el);
    let score = hay === needle ? 140 : hay.startsWith(needle) ? 100 : hay.includes(needle) ? 70 : needle.includes(hay) && hay.length > 2 ? 30 : 0;
    if (selector && selector.toLowerCase() === needle) score = 160;
    const r = el.getBoundingClientRect();
    if (score > 0 && r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth) score += 5;
    if (score > bestScore) {{ best = el; bestScore = score; bestText = hay; }}
  }}
  if (!best || bestScore <= 0) return null;
  const r = best.getBoundingClientRect();
  return {{
    selector: selectorFor(best),
    score: bestScore,
    matched_text: bestText,
    tag: best.tagName.toLowerCase(),
    type: best.getAttribute('type') || '',
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    rect: {{x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}},
  }};
}})()
"""
    match = js(expression)
    if not match:
        actions = action_controls_snapshot(limit=20)
        raise RuntimeError(f"click_button: no rendered action matched {label_or_text!r}; actions={actions}")
    click_at_xy(match["x"], match["y"])
    if timeout:
        _time.sleep(min(max(float(timeout), 0.0), 3.0))
    return {
        "clicked": True,
        "selector": match.get("selector", ""),
        "matched_text": match.get("matched_text", ""),
        "score": match.get("score"),
        "x": match.get("x"),
        "y": match.get("y"),
    }


def overlay_actions_snapshot(limit=20):
    """Return likely cookie/privacy/modal overlay actions with text and rects."""
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 20
    limit_int = max(1, min(limit_int, 100))
    expression = f"""
(() => {{
  // __OVERLAY_ACTIONS_SNAPSHOT__
  const clean = (text, max = 220) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['aria-label', 'title', 'data-testid', 'data-test', 'name']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return el.tagName.toLowerCase();
  }};
  const textFor = el => clean([el.innerText || '', el.value || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || '', el.id || ''].filter(Boolean).join(' '));
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' &&
      r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth;
  }};
  const overlayFor = el => {{
    for (let node = el; node && node !== document.body; node = node.parentElement) {{
      const r = node.getBoundingClientRect(), s = getComputedStyle(node);
      const hay = clean(`${{node.id}} ${{node.className}} ${{node.getAttribute('role') || ''}} ${{node.getAttribute('aria-label') || ''}} ${{node.innerText || ''}}`, 900).toLowerCase();
      if (s.position === 'fixed' || s.position === 'sticky' || node.getAttribute('role') === 'dialog' || /(cookie|consent|privacy|gdpr|modal|dialog|popup|banner)/i.test(hay)) {{
        return {{
          text: clean(node.innerText || '', 500),
          role: node.getAttribute('role') || '',
          z: Number(s.zIndex) || 0,
          rect: {{x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}},
        }};
      }}
    }}
    return null;
  }};
  const out = [];
  const nodes = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a[href],[role="button"],[aria-label],[title]')).filter(visible);
  for (const el of nodes) {{
    const overlay = overlayFor(el);
    const text = textFor(el);
    const hay = `${{text}} ${{overlay ? overlay.text : ''}}`.toLowerCase();
    let score = 0;
    if (overlay) score += 40;
    if (/accept all|accept cookies|allow all|i agree|agree|ok|got it|continue|reject all|decline|necessary only|close|dismiss|×|x/i.test(text)) score += 80;
    if (/cookie|consent|privacy|gdpr|modal|dialog|popup|banner/i.test(hay)) score += 40;
    if (!score) continue;
    const r = el.getBoundingClientRect();
    out.push({{
      selector: selectorFor(el),
      tag: el.tagName.toLowerCase(),
      text,
      score,
      overlay,
      rect: {{
        x: Math.round(r.x), y: Math.round(r.y),
        width: Math.round(r.width), height: Math.round(r.height),
        in_viewport: true,
      }},
      center: {{x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}},
    }});
  }}
  return out.sort((a, b) => b.score - a.score).slice(0, {limit_int});
}})()
"""
    actions = js(expression) or []
    return {"count": len(actions), "actions": actions}


def dismiss_overlay(prefer="accept", timeout=1.0):
    """Click a likely cookie/privacy/modal action by preference using real mouse events."""
    pref = str(prefer or "accept").strip().lower()
    expression = f"""
(() => {{
  // __DISMISS_OVERLAY__
  const pref = {json.dumps(pref)};
  const clean = (text, max = 220) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, max);
  const selectorFor = el => {{
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['aria-label', 'title', 'data-testid', 'data-test', 'name']) {{
      const value = el.getAttribute(attr);
      if (value) return `${{el.tagName.toLowerCase()}}[${{attr}}="${{CSS.escape(value)}}"]`;
    }}
    return '';
  }};
  const textFor = el => clean([el.innerText || '', el.value || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || '', el.id || ''].filter(Boolean).join(' '));
  const visible = el => {{
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' &&
      r.bottom >= 0 && r.top <= innerHeight && r.right >= 0 && r.left <= innerWidth;
  }};
  const hasOverlayAncestor = el => {{
    for (let node = el; node && node !== document.body; node = node.parentElement) {{
      const s = getComputedStyle(node);
      const hay = clean(`${{node.id}} ${{node.className}} ${{node.getAttribute('role') || ''}} ${{node.getAttribute('aria-label') || ''}} ${{node.innerText || ''}}`, 900).toLowerCase();
      if (s.position === 'fixed' || s.position === 'sticky' || node.getAttribute('role') === 'dialog' || /(cookie|consent|privacy|gdpr|modal|dialog|popup|banner)/i.test(hay)) return true;
    }}
    return false;
  }};
  const prefRe = pref.startsWith('reject') || pref.startsWith('decline')
    ? /reject all|reject|decline|necessary only|essential only/i
    : pref.startsWith('close') || pref.startsWith('dismiss')
      ? /close|dismiss|×|^x$/i
      : /accept all|accept cookies|allow all|i agree|agree|ok|got it|continue/i;
  let best = null, bestScore = -1, bestText = '';
  for (const el of document.querySelectorAll('button,input[type="button"],input[type="submit"],a[href],[role="button"],[aria-label],[title]')) {{
    if (!visible(el)) continue;
    const text = textFor(el), hay = text.toLowerCase();
    let score = 0;
    if (hasOverlayAncestor(el)) score += 40;
    if (prefRe.test(text)) score += 120;
    if (/cookie|consent|privacy|gdpr/.test(hay)) score += 20;
    if (/modal|dialog|popup|banner/.test(hay)) score += 10;
    if (score > bestScore) {{ best = el; bestScore = score; bestText = text; }}
  }}
  if (!best || bestScore <= 0) return null;
  const r = best.getBoundingClientRect();
  return {{
    selector: selectorFor(best),
    matched_text: bestText,
    score: bestScore,
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
  }};
}})()
"""
    match = js(expression)
    if not match:
        actions = overlay_actions_snapshot(limit=20)
        raise RuntimeError(f"dismiss_overlay: no likely overlay action matched prefer={prefer!r}; actions={actions}")
    click_at_xy(match["x"], match["y"])
    if timeout:
        _time.sleep(min(max(float(timeout), 0.0), 2.0))
    return {
        "clicked": True,
        "selector": match.get("selector", ""),
        "matched_text": match.get("matched_text", ""),
        "score": match.get("score"),
        "x": match.get("x"),
        "y": match.get("y"),
    }


def upload_file(selector, path):
    doc = cdp("DOM.getDocument", depth=-1)
    node_id = cdp("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector=selector)["nodeId"]
    if not node_id:
        raise RuntimeError(f"no element for {selector}")
    files = [path] if isinstance(path, str) else list(path)
    cdp("DOM.setFileInputFiles", files=files, nodeId=node_id)


class _HttpGetText(str):
    def __new__(cls, value, status_code=None, headers=None, url=None):
        obj = str.__new__(cls, value)
        obj.status_code = status_code
        obj.status = status_code
        obj.headers = headers or {}
        obj.url = url
        return obj

    @property
    def text(self):
        return str(self)

    @property
    def content(self):
        return str(self).encode("utf-8")

    def json(self):
        return json.loads(str(self))


class _HttpGetBytes(bytes):
    def __new__(cls, value, status_code=None, headers=None, url=None):
        obj = bytes.__new__(cls, value)
        obj.status_code = status_code
        obj.status = status_code
        obj.headers = headers or {}
        obj.url = url
        return obj

    @property
    def content(self):
        return bytes(self)

    @property
    def text(self):
        return bytes(self).decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


def http_get(url, headers=None, timeout=20.0, binary=None):
    """Pure HTTP fetch for static pages and APIs.

    When BROWSER_USE_API_KEY is set and fetch_use is installed, route through
    fetch-use like browser-harness. Otherwise fall back to local urllib with a
    browser-like UA and gzip handling. Pass binary=True for bytes.
    """
    _ensure_navigation_allowed(url)
    if os.environ.get("BROWSER_USE_API_KEY"):
        try:
            from fetch_use import fetch_sync

            response = fetch_sync(url, headers=headers, timeout_ms=int(float(timeout) * 1000))
            status_code = getattr(response, "status_code", getattr(response, "status", None))
            response_headers = dict(getattr(response, "headers", {}) or {})
            response_url = getattr(response, "url", url)
            if binary is True:
                data = getattr(response, "content", None)
                if data is None:
                    data = getattr(response, "body", None)
                if data is None:
                    data = getattr(response, "text", "").encode("utf-8", errors="replace")
                elif isinstance(data, str):
                    data = data.encode("utf-8", errors="replace")
                else:
                    data = bytes(data)
                return _HttpGetBytes(data, status_code, response_headers, response_url)
            return _HttpGetText(
                response.text,
                status_code,
                response_headers,
                response_url,
            )
        except ImportError:
            pass
    request_headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
    if headers:
        request_headers.update(headers)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=timeout) as response:
            data = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            content_type = response.headers.get("Content-Type", "")
            response_headers = dict(response.headers.items())
            status_code = getattr(response, "status", None) or response.getcode()
            if binary is True:
                return _HttpGetBytes(data, status_code, response_headers, response.geturl())
            if binary is False or "text" in content_type or "json" in content_type or "html" in content_type:
                charset = response.headers.get_content_charset() or "utf-8"
                return _HttpGetText(data.decode(charset, errors="replace"), status_code, response_headers, response.geturl())
            return _HttpGetBytes(data, status_code, response_headers, response.geturl())
    except urllib.error.HTTPError as exc:
        guidance = (
            "http_get received HTTP "
            f"{exc.code} for {url}. If this is bot/login protection, retry from the browser with js(fetch(...)), "
            "pass site-specific headers/cookies, or configure the Browser Use fetch proxy with BROWSER_USE_API_KEY."
        )
        raise RuntimeError(guidance) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"http_get failed for {url}: {exc}. Try a shorter timeout, browser js(fetch(...)), or a configured proxy if the site blocks direct HTTP."
        ) from exc


def http_get_many(urls, headers=None, timeout=20.0, binary=False, max_workers=8):
    """Fetch many static pages/APIs concurrently and return compact records.

    Results preserve input order. Each record is serializable and has either
    ok=True with text/content_base64 plus response metadata, or ok=False with an
    error string so one blocked URL does not discard the whole batch.
    """
    urls = list(urls or [])
    worker_count = max(1, min(int(max_workers or 1), max(1, len(urls)), 16))

    def fetch_one(index_url):
        index, url = index_url
        try:
            body = http_get(url, headers=headers, timeout=timeout, binary=binary)
            record = {
                "index": index,
                "url": url,
                "final_url": getattr(body, "url", url),
                "status_code": getattr(body, "status_code", None),
                "headers": dict(getattr(body, "headers", {}) or {}),
                "ok": True,
            }
            if isinstance(body, (bytes, bytearray)):
                record["content_base64"] = base64.b64encode(bytes(body)).decode("ascii")
            else:
                record["text"] = str(body)
            return record
        except Exception as exc:
            return {"index": index, "url": url, "ok": False, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(fetch_one, enumerate(urls)))


def _document_source_bytes(source, headers=None, timeout=30.0, binary=None):
    source_text = str(source or "")
    parsed = urlparse(source_text)
    if parsed.scheme in ("http", "https"):
        body = http_get(source_text, headers=headers, timeout=timeout, binary=True if binary is None else binary)
        data = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8", errors="replace")
        return bytes(data), getattr(body, "url", source_text), dict(getattr(body, "headers", {}) or {})
    path = pathlib.Path(source_text).expanduser()
    data = path.read_bytes()
    return data, str(path), {}


def _guess_document_kind(source, headers, data):
    content_type = ""
    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            content_type = str(value).lower()
            break
    lower = str(source or "").split("?", 1)[0].lower()
    head = bytes(data[:256])
    if "pdf" in content_type or lower.endswith(".pdf") or head.startswith(b"%PDF"):
        return "pdf"
    if "wordprocessingml" in content_type or lower.endswith(".docx") or head.startswith(b"PK"):
        return "docx"
    if "html" in content_type or lower.endswith((".html", ".htm")) or b"<html" in head.lower():
        return "html"
    return "text"


def _decode_text_bytes(data):
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_html_text(text):
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _extract_docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name for name in archive.namelist() if name.startswith("word/") and name.endswith(".xml")]
        chunks = []
        for name in sorted(names):
            if not (name == "word/document.xml" or name.startswith("word/header") or name.startswith("word/footer")):
                continue
            xml = archive.read(name).decode("utf-8", errors="replace")
            xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
            xml = re.sub(r"</w:p>", "\n", xml)
            texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.S)
            if texts:
                chunks.append("".join(html.unescape(part) for part in texts))
        return "\n".join(chunks)


def _extract_pdf_text(data):
    errors = []
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name)
            reader = module.PdfReader(io.BytesIO(data))
            pages = []
            for page in getattr(reader, "pages", [])[:80]:
                pages.append(page.extract_text() or "")
            text = "\n".join(pages).strip()
            if text:
                return text, module_name
        except Exception as exc:
            errors.append(f"{module_name}: {exc}")
    if shutil.which("pdftotext"):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            proc = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", tmp_path, "-"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.decode("utf-8", errors="replace"), "pdftotext"
            errors.append(f"pdftotext: {proc.stderr.decode('utf-8', errors='replace')[:200]}")
        except Exception as exc:
            errors.append(f"pdftotext: {exc}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    strings = re.findall(rb"[\x09\x0a\x0d\x20-\x7e]{8,}", data)
    text = "\n".join(chunk.decode("latin-1", errors="replace") for chunk in strings)
    if text.strip():
        return text, "pdf-byte-strings"
    raise RuntimeError("could not extract PDF text; install pypdf/PyPDF2 or pdftotext. " + "; ".join(errors[:3]))


def arxiv_query(search_query="cat:cs.AI", start=0, max_results=20, sort_by="submittedDate", sort_order="descending", timeout=20.0):
    """Query arXiv's Atom API and return normalized paper metadata.

    Useful for arXiv recent-list and paper-search tasks: title, authors,
    abstract, abs/pdf URLs, first-version submission time when exposed by arXiv,
    categories, DOI, journal ref, and any author affiliations present in the API.
    """
    params = {
        "search_query": str(search_query),
        "start": int(start),
        "max_results": int(max_results),
        "sortBy": str(sort_by),
        "sortOrder": str(sort_order),
    }
    url = "https://export.arxiv.org/api/query?" + urlencode(params)
    feed_text = str(http_get(url, timeout=timeout))
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(feed_text.encode("utf-8"))

    def child_text(node, path, default=""):
        found = node.find(path, ns)
        return default if found is None or found.text is None else re.sub(r"\s+", " ", found.text).strip()

    def link_href(entry, rel=None, title=None, type_=None):
        for link in entry.findall("atom:link", ns):
            if rel and link.get("rel") != rel:
                continue
            if title and link.get("title") != title:
                continue
            if type_ and link.get("type") != type_:
                continue
            href = link.get("href")
            if href:
                return href
        return ""

    entries = []
    for entry in root.findall("atom:entry", ns):
        id_url = child_text(entry, "atom:id")
        arxiv_id = id_url.rstrip("/").rsplit("/", 1)[-1] if id_url else ""
        abs_url = link_href(entry, rel="alternate") or id_url
        pdf_url = link_href(entry, title="pdf") or link_href(entry, type_="application/pdf")
        if not pdf_url and abs_url:
            pdf_url = abs_url.replace("/abs/", "/pdf/")
        authors = []
        for author in entry.findall("atom:author", ns):
            name = child_text(author, "atom:name")
            affiliation = child_text(author, "arxiv:affiliation")
            item = {"name": name}
            if affiliation:
                item["affiliation"] = affiliation
            if name:
                authors.append(item)
        categories = [cat.get("term") for cat in entry.findall("atom:category", ns) if cat.get("term")]
        primary = entry.find("arxiv:primary_category", ns)
        entries.append(
            {
                "id": arxiv_id,
                "title": child_text(entry, "atom:title"),
                "summary": child_text(entry, "atom:summary"),
                "published": child_text(entry, "atom:published"),
                "updated": child_text(entry, "atom:updated"),
                "abs_url": abs_url,
                "pdf_url": pdf_url,
                "authors": authors,
                "first_author": authors[0] if authors else {},
                "categories": categories,
                "primary_category": primary.get("term") if primary is not None else "",
                "comment": child_text(entry, "arxiv:comment"),
                "journal_ref": child_text(entry, "arxiv:journal_ref"),
                "doi": child_text(entry, "arxiv:doi"),
            }
        )
    return {
        "query_url": url,
        "search_query": str(search_query),
        "start": int(start),
        "max_results": int(max_results),
        "count": len(entries),
        "entries": entries,
    }


_SEMANTIC_SCHOLAR_API_BASE = "https://api.semanticscholar.org/graph/v1"
_SEMANTIC_SCHOLAR_PAPER_FIELDS = (
    "paperId,title,abstract,authors,venue,year,citationCount,"
    "externalIds,url,publicationVenue,publicationTypes,openAccessPdf"
)


def _semantic_scholar_clean(value, max_chars=4000):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()[:max_chars]


def _semantic_scholar_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(parsed, maximum))


def _semantic_scholar_normalize_paper(paper):
    if not isinstance(paper, dict):
        paper = {}
    authors = []
    for author in paper.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = _semantic_scholar_clean(author.get("name"), 240)
        if name:
            authors.append({"author_id": str(author.get("authorId") or ""), "name": name})
    open_access_pdf = paper.get("openAccessPdf") if isinstance(paper.get("openAccessPdf"), dict) else {}
    publication_venue = paper.get("publicationVenue") if isinstance(paper.get("publicationVenue"), dict) else {}
    paper_id = str(paper.get("paperId") or "")
    url = str(paper.get("url") or "")
    if not url and paper_id:
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
    return {
        "paper_id": paper_id,
        "title": _semantic_scholar_clean(paper.get("title"), 500),
        "abstract": _semantic_scholar_clean(paper.get("abstract"), 2500),
        "authors": authors,
        "author_names": [author["name"] for author in authors],
        "venue": _semantic_scholar_clean(paper.get("venue") or publication_venue.get("name"), 240),
        "publication_venue": _semantic_scholar_clean(publication_venue.get("name"), 240),
        "year": paper.get("year"),
        "citation_count": paper.get("citationCount"),
        "url": url,
        "pdf_url": str(open_access_pdf.get("url") or ""),
        "external_ids": paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {},
        "publication_types": paper.get("publicationTypes") if isinstance(paper.get("publicationTypes"), list) else [],
    }


def _semantic_scholar_looks_like_paper_id(value):
    return (
        re.match(r"^[0-9a-f]{40}$", value, re.I)
        or re.match(r"^(?:CorpusID|DOI|ARXIV|PMID|PMCID|MAG|ACL|URL):", value, re.I)
        or value.startswith("https://")
        or value.startswith("http://")
    )


def _semantic_scholar_api_json(url, timeout):
    return json.loads(str(http_get(url, headers={"Accept": "application/json"}, timeout=timeout)))


def _semantic_scholar_select_paper(target, search_limit, timeout):
    search_results = []
    if _semantic_scholar_looks_like_paper_id(target):
        paper_lookup_url = f"{_SEMANTIC_SCHOLAR_API_BASE}/paper/{quote(target, safe=':/.')}?" + urlencode(
            {"fields": _SEMANTIC_SCHOLAR_PAPER_FIELDS}
        )
        return _semantic_scholar_normalize_paper(_semantic_scholar_api_json(paper_lookup_url, timeout)), search_results, ""

    search_url = f"{_SEMANTIC_SCHOLAR_API_BASE}/paper/search?" + urlencode(
        {"query": target, "limit": search_limit, "fields": _SEMANTIC_SCHOLAR_PAPER_FIELDS}
    )
    search_payload = _semantic_scholar_api_json(search_url, timeout)
    search_results = [
        _semantic_scholar_normalize_paper(item)
        for item in (search_payload.get("data", []) if isinstance(search_payload, dict) else [])
        if isinstance(item, dict)
    ]
    if not search_results:
        return {}, [], "no_search_results"
    lower_target = _semantic_scholar_clean(target).lower()
    selected = max(
        search_results,
        key=lambda paper: (
            100 if paper.get("title", "").lower() == lower_target else 0,
            60 if lower_target and lower_target in paper.get("title", "").lower() else 0,
            int(paper.get("citation_count") or 0),
        ),
    )
    return selected, search_results, ""


def _semantic_scholar_related_papers(query_or_paper_id, *, relation, row_key, result_key, year, limit, search_limit, timeout):
    target = str(query_or_paper_id or "").strip()
    if not target:
        raise ValueError(f"semantic_scholar_{result_key} requires a title, DOI:, CorpusID:, URL, or paperId")
    limit_int = _semantic_scholar_int(limit or 200, 200, 1, 1000)
    search_limit_int = _semantic_scholar_int(search_limit or 5, 5, 1, 20)
    selected_paper, search_results, diagnosis = _semantic_scholar_select_paper(target, search_limit_int, timeout)
    empty = {
        "query": target,
        "selected_paper": selected_paper,
        "search_results": search_results,
        "count": 0,
        "year_filter": year,
        result_key: [],
    }
    if diagnosis:
        empty["diagnosis"] = diagnosis
        return empty
    paper_id = selected_paper.get("paper_id")
    if not paper_id:
        empty["diagnosis"] = "missing_paper_id"
        return empty

    related = []
    seen_keys = set()
    offset = 0
    page_size = min(100, limit_int)
    relation_fields = row_key + "." + _SEMANTIC_SCHOLAR_PAPER_FIELDS.replace(",", f",{row_key}.")
    while len(related) < limit_int:
        page_limit = min(page_size, limit_int - len(related))
        url = f"{_SEMANTIC_SCHOLAR_API_BASE}/paper/{quote(paper_id, safe='')}/{relation}?" + urlencode(
            {"offset": offset, "limit": page_limit, "fields": relation_fields}
        )
        payload = _semantic_scholar_api_json(url, timeout)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            paper = _semantic_scholar_normalize_paper(row.get(row_key) if isinstance(row, dict) else {})
            if year is not None and str(paper.get("year") or "") != str(year):
                continue
            if not paper.get("paper_id") and not paper.get("title"):
                continue
            key = paper.get("paper_id") or paper.get("title")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            related.append(paper)
            if len(related) >= limit_int:
                break
        next_offset = payload.get("next") if isinstance(payload, dict) else None
        if next_offset is None:
            break
        try:
            offset = int(next_offset)
        except Exception:
            break

    return {
        "query": target,
        "selected_paper": selected_paper,
        "search_results": search_results,
        "count": len(related),
        "year_filter": year,
        result_key: related,
    }


def semantic_scholar_citations(query_or_paper_id, year=None, limit=200, search_limit=5, timeout=20.0):
    """Search/fetch a Semantic Scholar paper and return normalized citing papers."""
    return _semantic_scholar_related_papers(
        query_or_paper_id,
        relation="citations",
        row_key="citingPaper",
        result_key="citations",
        year=year,
        limit=limit,
        search_limit=search_limit,
        timeout=timeout,
    )


def semantic_scholar_references(query_or_paper_id, year=None, limit=200, search_limit=5, timeout=20.0):
    """Search/fetch a Semantic Scholar paper and return normalized referenced papers."""
    return _semantic_scholar_related_papers(
        query_or_paper_id,
        relation="references",
        row_key="citedPaper",
        result_key="references",
        year=year,
        limit=limit,
        search_limit=search_limit,
        timeout=timeout,
    )


def read_document_text(source, headers=None, timeout=30.0, max_chars=120000, binary=None):
    """Fetch/read a document URL or local path and return bounded extracted text.

    Handles text/HTML, DOCX, and PDF. For PDFs it tries pypdf/PyPDF2, then the
    `pdftotext` binary, then a low-fidelity byte-string fallback. Use this for
    FERC filings, investor PDFs, earnings documents, and downloaded files before
    spending browser turns opening each document visually.
    """
    data, final_source, response_headers = _document_source_bytes(source, headers=headers, timeout=timeout, binary=binary)
    kind = _guess_document_kind(final_source, response_headers, data)
    extractor = kind
    if kind == "pdf":
        text, extractor = _extract_pdf_text(data)
    elif kind == "docx":
        text = _extract_docx_text(data)
    elif kind == "html":
        text = _strip_html_text(_decode_text_bytes(data))
    else:
        text = _decode_text_bytes(data)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    truncated = len(text) > int(max_chars)
    if truncated:
        text = text[: int(max_chars)]
    return {
        "source": str(source),
        "final_source": final_source,
        "kind": kind,
        "extractor": extractor,
        "bytes": len(data),
        "chars": len(text),
        "truncated": truncated,
        "text": text,
    }
