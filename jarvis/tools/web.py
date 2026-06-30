"""Web tools (AGENTIC_PLAN — internet access + search).

`web.search` queries DuckDuckGo (no API key) and returns titles/URLs/snippets.
`web.fetch` GETs a URL and returns readable text. Both are risk 'read' (auto under
confirm-risky). Uses httpx (already a dependency); search prefers the `ddgs` package
if installed and otherwise scrapes DuckDuckGo's HTML endpoint.
"""
from __future__ import annotations

import html
import re
import urllib.parse

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\f\v]+")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


def _strip_html(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


class SearchArgs(BaseModel):
    query: str = Field(..., description="What to search the web for.")
    k: int = Field(6, description="How many results to return (max 15).")


@tool(name="web.search", risk="read", timeout_s=30,
      capabilities=["web.search", "internet"], output_kind="list",
      output_schema={"results": [{"title": "str", "url": "str", "snippet": "str"}]},
      examples=[{"request": "search the web for X", "args": {"query": "X"}}])
def web_search(args: SearchArgs, ctx: ToolContext) -> ToolResult:
    """Search the web (DuckDuckGo) and return a list of {title, url, snippet}."""
    k = max(1, min(15, args.k))
    # 1) Prefer the ddgs/duckduckgo_search package if it's installed.
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except Exception:
            from duckduckgo_search import DDGS  # type: ignore
        results = []
        with DDGS() as ddg:
            for r in ddg.text(args.query, max_results=k):
                results.append({"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
                                "snippet": r.get("body", "")})
        if results:
            return ToolResult(ok=True, data={"query": args.query, "results": results})
    except Exception:
        pass
    # 2) Fallback: scrape the no-JS HTML endpoint.
    try:
        import httpx  # type: ignore
        r = httpx.post("https://html.duckduckgo.com/html/", data={"q": args.query},
                       headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        body = r.text
    except Exception as e:
        return ToolResult(ok=False, error=ToolError(code="search_failed", category="execution",
                                                    message=f"web search failed: {e}"))
    results = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href, title = m.group(1), _strip_html(m.group(2))
        if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
            q = urllib.parse.urlparse(href).query
            uddg = urllib.parse.parse_qs(q).get("uddg")
            if uddg:
                href = urllib.parse.unquote(uddg[0])
        results.append({"title": title, "url": href, "snippet": ""})
        if len(results) >= k:
            break
    snips = [_strip_html(m.group(1)) for m in
             re.finditer(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)]
    for i, s in enumerate(snips[:len(results)]):
        results[i]["snippet"] = s
    if not results:
        return ToolResult(ok=False, error=ToolError(code="no_results", category="not_found",
                                                    message="no results parsed (DuckDuckGo layout may have changed)"))
    return ToolResult(ok=True, data={"query": args.query, "results": results})


class FetchArgs(BaseModel):
    url: str = Field(..., description="The URL to fetch (http/https).")
    max_chars: int = Field(6000, description="Truncate the returned text to this many characters.")


@tool(name="web.fetch", risk="read", timeout_s=40,
      capabilities=["web.fetch", "web.read", "internet"], output_kind="prose")
def web_fetch(args: FetchArgs, ctx: ToolContext) -> ToolResult:
    """Fetch a web page and return its readable text content (HTML stripped)."""
    if not re.match(r"^https?://", args.url, re.I):
        return ToolResult(ok=False, error=ToolError(code="bad_args", category="validation",
                                                    message="url must start with http:// or https://"))
    try:
        import httpx  # type: ignore
        r = httpx.get(args.url, headers={"User-Agent": _UA}, timeout=30, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        return ToolResult(ok=False, error=ToolError(code="fetch_failed", category="execution",
                                                    message=f"fetch failed: {e}"))
    ctype = r.headers.get("content-type", "")
    text = r.text
    if "html" in ctype or text.lstrip()[:1] == "<":
        text = re.sub(r"(?is)<(script|style|noscript|head)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", text)
        text = _strip_html(text)
        text = _WS.sub(" ", text)
        text = _BLANKS.sub("\n\n", text)
    title = ""
    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
    if mt:
        title = _strip_html(mt.group(1))
    return ToolResult(ok=True, data={"url": str(r.url), "title": title, "status": r.status_code,
                                     "content": text.strip()[:max(500, args.max_chars)]})
