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
from urllib.parse import urlencode, urljoin, urlparse


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
