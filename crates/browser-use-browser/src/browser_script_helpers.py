"""Browser-script helpers.

Rust owns the CDP websocket and session state. This file owns the
LLM-readable browser interaction helpers. Keep these helpers close to
browser-harness semantics so the model sees one coherent browser API.
"""

import base64
import concurrent.futures
import gzip
import html
import io
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
from urllib.parse import urlencode, urlparse
import zipfile
import xml.etree.ElementTree as ET


INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")
__last_domain_skills = []


def _send_meta(meta, **params):
    return _bridge({"kind": "meta", "meta": meta, **params})


def cdp(method, session_id=None, **params):
    """Raw CDP. Example: cdp("Page.navigate", url="https://example.com")."""
    return _bridge({"kind": "cdp", "method": method, "session_id": session_id, "params": params})


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
    """Fetch from the current page context, preserving browser credentials.

    Use this after direct http_get/http_get_many is blocked but the browser has
    loaded the site and may have useful cookies or same-origin API access.
    """
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
    """Fetch many URLs from the current page context with browser credentials.

    Results preserve input order and include per-URL errors so one blocked API
    endpoint does not discard the whole batch.
    """
    urls = list(urls or [])
    expression = f"""
(async () => {{
  const urls = {json.dumps(urls)};
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
  const fetchOne = async (url, index) => {{
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
  const results = new Array(urls.length);
  let next = 0;
  const workers = Array.from({{length: Math.min(maxConcurrent, urls.length)}}, async () => {{
    while (next < urls.length) {{
      const index = next++;
      results[index] = await fetchOne(urls[index], index);
    }}
  }});
  await Promise.all(workers);
  return results;
}})()
"""
    return js(expression)


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
    result = cdp("Page.navigate", url=url)
    __last_domain_skills = []
    if _domain_skills_enabled():
        skills = domain_skills_for_url(url, include_content=False)
        if skills:
            __last_domain_skills = [{"url": url, **skill} for skill in skills]
            result = {**result, "domain_skills": __last_domain_skills}
    wait_for_load(timeout=15)
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
    """Find repeated visible record/card groups and suggest an extraction selector.

    This is for pages where the answer is in repeated cards, listings, rows, or
    product tiles. It returns compact candidates; call extract_repeated_items()
    with the recommended selector to get the records.
    """
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
    let imageCount = 0;
    for (const el of items.slice(0, 10)) {{
      if (el.matches('a[href]')) links.add(el.href);
      for (const a of el.querySelectorAll('a[href]')) links.add(a.href);
      imageCount += el.querySelectorAll('img[src],img[srcset],picture source[srcset]').length;
    }}
    let score = items.length * 2 + Math.min(avgLen / 30, 8) + Math.min(links.size, 8) + Math.min(imageCount, 6);
    if (priceSignals) score += priceSignals * 6;
    if (/^(li|div|article|section|tr)($|[.#])/.test(selector)) score += 1;
    return {{ selector, count: items.length, price_signal_count: priceSignals, link_count: links.size, image_count: imageCount, score, samples }};
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


def wait_for_network_idle(timeout=10.0, idle_ms=500):
    timeout = _timeout_seconds(timeout)
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
            cdp("Emulation.setDeviceMetricsOverride", width=1280, height=720, deviceScaleFactor=1, mobile=False)
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


def screenshot(label="screenshot", full=False):
    return capture_screenshot(label=label, full=full, attach=True)


def screenshot_clip(label, x, y, width, height):
    return capture_screenshot(label=label, clip={"x": x, "y": y, "width": width, "height": height, "scale": 1}, attach=True)


def click_at_xy(x, y, button="left", clicks=1):
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=clicks)
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=clicks)
    return True


def type_text(text):
    cdp("Input.insertText", text=text)
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
    return True


def scroll(x=0, y=0, dy=600, dx=0):
    cdp("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)
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
    """Return compact rendered form fields with labels/placeholders/selectors."""
    expression = f"""
(() => {{
 const clean=(t,m=220)=>(t||'').replace(/\\s+/g,' ').trim().slice(0,m),sel=e=>{{if(e.id)return '#'+CSS.escape(e.id);for(const a of ['name','aria-label','placeholder','data-testid','data-test']){{const v=e.getAttribute(a);if(v)return `${{e.tagName.toLowerCase()}}[${{a}}="${{CSS.escape(v)}}"]`}}return e.tagName.toLowerCase()}};
 const label=e=>{{const id=e.id,ls=[];if(id)for(const l of document.querySelectorAll(`label[for="${{CSS.escape(id)}}"]`))ls.push(l.innerText);const p=e.closest('label');if(p)ls.push(p.innerText);const wrap=e.closest('[aria-label],[data-label],.field,.form-group,li,div');if(wrap)ls.push(wrap.getAttribute('aria-label')||wrap.getAttribute('data-label')||'');return clean([e.getAttribute('aria-label'),...ls].filter(Boolean).join(' '))}};
 return [...document.querySelectorAll('input:not([type=hidden]),textarea,select,[contenteditable="true"],[role="combobox"],[role="textbox"]')].filter(e=>!e.disabled).slice(0,{int(limit)}).map((e,i)=>{{const r=e.getBoundingClientRect(),type=(e.getAttribute('type')||e.tagName).toLowerCase();return {{index:i,selector:sel(e),tag:e.tagName.toLowerCase(),type,label:label(e),placeholder:clean(e.getAttribute('placeholder')),name:clean(e.getAttribute('name')),value:clean(e.value||e.textContent,300),required:!!e.required,autocomplete:clean(e.getAttribute('autocomplete')),aria_expanded:e.getAttribute('aria-expanded'),rect:{{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),in_viewport:r.bottom>=0&&r.top<=innerHeight}}}}}});
}})()
"""
    fields = js(expression) or []
    return {"count": len(fields), "fields": fields}


def fill_form_field(label_or_placeholder, value, clear=True, timeout=3.0):
    """Fill a rendered form field by label, placeholder, name, selector, or nearby text."""
    needle = str(label_or_placeholder or "").strip().lower()
    if not needle:
        raise RuntimeError("fill_form_field requires a label_or_placeholder")
    expression = f"""
(() => {{
 const needle={json.dumps(needle)},clean=t=>(t||'').replace(/\\s+/g,' ').trim(),sel=e=>{{if(e.id)return '#'+CSS.escape(e.id);for(const a of ['name','aria-label','placeholder','data-testid','data-test']){{const v=e.getAttribute(a);if(v)return `${{e.tagName.toLowerCase()}}[${{a}}="${{CSS.escape(v)}}"]`}}return ''}};
 const text=e=>{{const bits=[e.getAttribute('aria-label'),e.getAttribute('placeholder'),e.getAttribute('name'),e.id];if(e.id)for(const l of document.querySelectorAll(`label[for="${{CSS.escape(e.id)}}"]`))bits.push(l.innerText);const p=e.closest('label,[aria-label],[data-label],.field,.form-group,li,div');if(p)bits.push(p.innerText,p.getAttribute('aria-label'),p.getAttribute('data-label'));return clean(bits.filter(Boolean).join(' ')).toLowerCase()}};
 let best=null,bestScore=-1;for(const e of document.querySelectorAll('input:not([type=hidden]),textarea,select,[contenteditable="true"],[role="combobox"],[role="textbox"]')){{if(e.disabled)continue;const s=sel(e),hay=text(e);let score=hay===needle?100:hay.includes(needle)?50:needle.includes(hay)&&hay.length>2?20:0;if(s&&s.toLowerCase()===needle)score=120;if(score>bestScore){{best=e;bestScore=score}}}}
 return best&&bestScore>0?{{selector:sel(best),score:bestScore,matched_text:text(best),tag:best.tagName.toLowerCase(),type:(best.getAttribute('type')||best.tagName).toLowerCase()}}:null;
}})()
"""
    match = js(expression)
    if not match or not match.get("selector"):
        fields = form_fields_snapshot(limit=20)
        raise RuntimeError(f"fill_form_field: no rendered field matched {label_or_placeholder!r}; fields={fields}")
    fill_input(match["selector"], value, clear=clear, timeout=timeout)
    return {"filled": True, "selector": match["selector"], "matched_text": match.get("matched_text", ""), "score": match.get("score")}


def action_controls_snapshot(limit=30):
    """Return compact rendered buttons/links/actions with text/selectors/rects."""
    expression = f"""
(() => {{
 const clean=(t,m=180)=>(t||'').replace(/\\s+/g,' ').trim().slice(0,m),sel=e=>{{if(e.id)return '#'+CSS.escape(e.id);for(const a of ['name','aria-label','value','title','data-testid','data-test']){{const v=e.getAttribute(a);if(v)return `${{e.tagName.toLowerCase()}}[${{a}}="${{CSS.escape(v)}}"]`}}return e.tagName.toLowerCase()}};
 const text=e=>clean([e.innerText,e.value,e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('name'),e.id].filter(Boolean).join(' '));
 const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'}};
 return [...document.querySelectorAll('button,input[type=button],input[type=submit],input[type=reset],a[href],[role=button],[role=link],[onclick]')].filter(e=>!e.disabled&&visible(e)).slice(0,{int(limit)}).map((e,i)=>{{const r=e.getBoundingClientRect();return {{index:i,selector:sel(e),tag:e.tagName.toLowerCase(),type:(e.getAttribute('type')||''),text:text(e),href:e.href||'',name:clean(e.getAttribute('name')),aria_label:clean(e.getAttribute('aria-label')),rect:{{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),in_viewport:r.bottom>=0&&r.top<=innerHeight&&r.right>=0&&r.left<=innerWidth}}}}}});
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
 const needle={json.dumps(needle)},clean=t=>(t||'').replace(/\\s+/g,' ').trim(),sel=e=>{{if(e.id)return '#'+CSS.escape(e.id);for(const a of ['name','aria-label','value','title','data-testid','data-test']){{const v=e.getAttribute(a);if(v)return `${{e.tagName.toLowerCase()}}[${{a}}="${{CSS.escape(v)}}"]`}}return ''}};
 const txt=e=>clean([e.innerText,e.value,e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('name'),e.id].filter(Boolean).join(' ')).toLowerCase();
 const visible=e=>{{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'}};
 let best=null,bestScore=-1,bestText='';for(const e of document.querySelectorAll('button,input[type=button],input[type=submit],input[type=reset],a[href],[role=button],[role=link],[onclick]')){{if(e.disabled||!visible(e))continue;const s=sel(e),hay=txt(e);let score=hay===needle?140:hay.startsWith(needle)?100:hay.includes(needle)?70:needle.includes(hay)&&hay.length>2?30:0;if(s&&s.toLowerCase()===needle)score=160;const r=e.getBoundingClientRect();if(score>0&&r.bottom>=0&&r.top<=innerHeight&&r.right>=0&&r.left<=innerWidth)score+=5;if(score>bestScore){{best=e;bestScore=score;bestText=hay}}}}
 if(!best||bestScore<=0)return null;
 const r=best.getBoundingClientRect();return {{selector:sel(best),score:bestScore,matched_text:bestText,tag:best.tagName.toLowerCase(),type:(best.getAttribute('type')||''),x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),rect:{{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}}}};
}})()
"""
    match = js(expression)
    if not match:
        actions = action_controls_snapshot(limit=20)
        raise RuntimeError(f"click_button: no rendered action matched {label_or_text!r}; actions={actions}")
    click_at_xy(match["x"], match["y"])
    if timeout:
        _time.sleep(min(float(timeout), 3.0))
    return {"clicked": True, "selector": match.get("selector", ""), "matched_text": match.get("matched_text", ""), "score": match.get("score"), "x": match.get("x"), "y": match.get("y")}


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
            f"{exc.code} for {url}. If this is bot/login protection, retry from the browser with browser_fetch(...), "
            "pass site-specific headers/cookies, or configure the Browser Use fetch proxy with BROWSER_USE_API_KEY."
        )
        raise RuntimeError(guidance) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"http_get failed for {url}: {exc}. Try a shorter timeout, browser_fetch(...), or a configured proxy if the site blocks direct HTTP."
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
        return "" if found is None or found.text is None else re.sub(r"\s+", " ", found.text).strip()

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
