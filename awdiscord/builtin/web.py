"""Web: news headlines, web search and page reading, as tools the agent calls.

On by default. Nothing here needs an API key:

  news_headlines(topic?)   Google News RSS (top stories, or a topic search)
  web_search(query)        AWDISCORD_SEARCH_URL: an AitherSearch-style service
                           (POST /search) or SearXNG (GET /search?format=json);
                           without one, DuckDuckGo's HTML endpoint
  fetch_url(url)           a public web page as text

fetch_url and search refuse private, loopback and link-local addresses: a
Discord member must not be able to make the bot read services on its own
network. ``AWDISCORD_SEARCH_URL`` is the operator's choice and is exempt.
"""
from __future__ import annotations

import html
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import urllib.parse
import urllib.request
from xml.etree import ElementTree

from awdiscord.plugins import Command, Option, Plugin, Tool

_log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; awdiscord; +https://github.com/Aitherium/awdiscord)"
TIMEOUT = 15
OPENER: list = [None]          # tests swap in a fake transport


def _open(req, timeout=TIMEOUT, context=None):
    fn = OPENER[0] or urllib.request.urlopen
    return fn(req, timeout=timeout, context=context) if context else fn(req, timeout=timeout)


def public_url(url: str) -> str:
    """The URL, or ValueError when it is not http(s) to a public address."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{parts.hostname} is not a public address")
    return url


def _text(raw: bytes, limit: int) -> str:
    body = raw.decode("utf-8", "replace")
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()[:limit]


# ── tools ─────────────────────────────────────────────────────────────────────

def news_headlines(topic: str = "", limit: int = 8, links: bool = False) -> str:
    if topic.strip():
        url = ("https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
               + urllib.parse.quote(topic.strip()))
    else:
        url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    with _open(urllib.request.Request(url, headers={"User-Agent": UA})) as resp:
        root = ElementTree.fromstring(resp.read())
    rows = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        when = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        if title:
            # Google News links are ~300 chars each: eight of them alone fill a
            # Discord message, so they are only included when asked for.
            rows.append(f"- {title} ({source or 'news'}, {when[:16]})"
                        + (f"\n  {link}" if links else ""))
        if len(rows) >= max(1, min(int(limit), 15)):
            break
    return "\n".join(rows) or "No headlines returned."


def _search_service(base: str, query: str, limit: int) -> list[dict]:
    ca = os.getenv("AWDISCORD_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=ca) if ca else None
    base = base.rstrip("/")
    try:   # AitherSearch shape
        req = urllib.request.Request(
            base + "/search", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"query": query, "limit": limit, "synthesize": False,
                             "auto_learn": False}).encode())
        with _open(req, context=ctx) as resp:
            data = json.loads(resp.read())
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("snippet") or r.get("content", "")}
                for r in data.get("results", [])]
    except Exception as exc:  # not the AitherSearch shape: fall through to SearXNG
        _log.debug("AitherSearch query failed, trying the SearXNG shape: %s", exc)
    req = urllib.request.Request(  # SearXNG shape
        base + "/search?format=json&q=" + urllib.parse.quote(query),
        headers={"User-Agent": UA})
    with _open(req, context=ctx) as resp:
        data = json.loads(resp.read())
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", "")} for r in data.get("results", [])][:limit]


def _search_ddg(query: str, limit: int) -> list[dict]:
    req = urllib.request.Request("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query),
                                 headers={"User-Agent": UA})
    with _open(req) as resp:
        page = resp.read().decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'(?s)class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
                         r'class="result__snippet"[^>]*>(.*?)</a>', page):
        href = html.unescape(m.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        out.append({"url": q.get("uddg", [href])[0], "title": _text(m.group(2).encode(), 200),
                    "snippet": _text(m.group(3).encode(), 300)})
        if len(out) >= limit:
            break
    return out


def web_search(query: str, limit: int = 5) -> str:
    query = (query or "").strip()[:300]
    if not query:
        return "Give a search query."
    limit = max(1, min(int(limit), 10))
    base = os.getenv("AWDISCORD_SEARCH_URL", "").strip()
    results = _search_service(base, query, limit) if base else _search_ddg(query, limit)
    if not results:
        return f"No results for {query!r}. Try fewer or more specific words."
    return "\n".join(f"- {r['title']}\n  {r['url']}\n  {r['snippet'][:300]}" for r in results)


def fetch_url(url: str, max_chars: int = 6000) -> str:
    public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with _open(req) as resp:
        final = resp.geturl() if hasattr(resp, "geturl") else url
        if final != url:
            public_url(final)            # a redirect must not land inside the network
        raw = resp.read(2_000_000)
    return _text(raw, max(500, min(int(max_chars), 12000)))


# ── a slash command for people, too ────────────────────────────────────────────

def _news(ctx):
    topic = str(ctx.opt("topic") or "")
    return ctx.defer(lambda: news_headlines(topic, 8))


TOOLS = [
    Tool("news_headlines", "Current news headlines with dates and links. Use for anything "
         "about today's news or recent events. Optional topic narrows it.", news_headlines,
         {"type": "object", "properties": {
             "topic": {"type": "string", "description": "e.g. 'AI', 'Ukraine', empty = top"},
             "limit": {"type": "integer", "description": "1-15, default 8"},
             "links": {"type": "boolean", "description": "include article links (long; "
                       "only when the user asks for links)"}}}),
    Tool("web_search", "Search the web. Use for facts that may have changed or that you "
         "are not sure of. Returns titles, links and snippets.", web_search,
         {"type": "object", "properties": {"query": {"type": "string"},
                                           "limit": {"type": "integer"}},
          "required": ["query"]}),
    Tool("fetch_url", "Read a public web page as plain text (to check a search result).",
         fetch_url, {"type": "object", "properties": {"url": {"type": "string"}},
                     "required": ["url"]}),
]

PLUGIN = Plugin(
    name="web",
    version="0.1.0",
    description="News, web search and page reading for the agent; /news for people",
    default_enabled=True,
    persona=("You can look things up: call news_headlines for current events and "
             "web_search for facts you are unsure of, then answer from what the tools "
             "returned, naming the sources. Never say you lack internet access - use "
             "the tools. If a tool fails, say what you could not get. Keep answers under "
             "1500 characters: Discord cuts longer messages."),
    tools=TOOLS,
    commands=[Command("news", "Current headlines", _news, dm=True, options=[
        Option("topic", "Optional topic", max_length=100)])],
)
