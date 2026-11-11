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

They never read "what is my IP" services: the bot egresses from its host's
connection, and such a page would hand that address to whoever asked. fetch_url
answers those itself with this process's decoy address (``awdiscord.egress``:
a documentation-range address, the same on every ask) and never contacts them;
web_search drops them from its results. fetch_url dials the address it
validated (no second DNS lookup to rebind) and follows redirects itself,
validating every hop. Every tool's output passes through
``awdiscord.egress.redact`` as well.

``AWDISCORD_EGRESS_PROXY`` (an http(s) proxy URL, default unset) sends the web
tools' traffic through that proxy instead of the host's own connection.
"""
from __future__ import annotations

import html
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

from awdiscord import egress
from awdiscord.plugins import Command, Option, Plugin, Tool

_log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; awdiscord; +https://github.com/Aitherium/awdiscord)"
TIMEOUT = 15
OPENER: list = [None]          # tests swap in a fake transport
CONNECT: list = [None]         # tests swap in a fake pinned connection factory
MAX_REDIRECTS = 5
MAX_BYTES = 2_000_000

#: services whose whole job is telling the caller its own address (or echoing
#: its request headers). Matched on the host and every parent domain.
IP_ECHO_DOMAINS = frozenset({
    "ipify.org", "ifconfig.me", "ifconfig.co", "ifconfig.io", "icanhazip.com",
    "ipinfo.io", "ip-api.com", "ipapi.co", "ipapi.com", "checkip.amazonaws.com",
    "checkip.dyndns.org", "checkip.dyndns.com", "whatismyip.com", "whatismyipaddress.com",
    "whatsmyip.org", "iplocation.net", "iplocation.com", "ipecho.net", "ident.me",
    "myexternalip.com", "wtfismyip.com", "ip.me", "ip.sb", "seeip.org", "geojs.io",
    "ipwho.is", "ipwhois.app", "db-ip.com", "browserleaks.com", "httpbin.org",
    "ip.tl", "myip.com", "myip.dnsomatic.com", "ipaddress.my", "showmyip.com",
})
#: a host label starting with one of these is an address echo too (myip.example, ...)
_IP_ECHO_LABEL = re.compile(r"^(?:my-?ip|whats?-?(?:is-?)?my-?ip|check-?ip|show-?my-?ip"
                            r"|ip-?echo|ip-?location|icanhazip|ipify)", re.I)


def is_ip_echo(host: str) -> bool:
    """True when ``host`` is a service that reports the caller's own address."""
    host = (host or "").strip(".").lower()
    if not host:
        return False
    labels = host.split(".")
    for i in range(len(labels) - 1):
        if ".".join(labels[i:]) in IP_ECHO_DOMAINS:
            return True
    return any(_IP_ECHO_LABEL.match(label) for label in labels[:-1])


class IPEchoError(ValueError):
    """The target is a service that reports the caller's own address."""


def _decoy_answer(host: str) -> str:
    """What an IP-echo service would have said, with the decoy as the address."""
    host = (host or "").lower()
    v6 = any(t in host.split(".")[0] for t in ("6", "ipv6", "v6"))
    return str(egress.decoy(6 if v6 else 4))


def _proxy() -> str:
    return (os.getenv("AWDISCORD_EGRESS_PROXY") or "").strip()


def _open(req, timeout=TIMEOUT, context=None):
    fn = OPENER[0]
    if fn is None:
        handlers: list = []
        if _proxy():
            handlers.append(urllib.request.ProxyHandler({"http": _proxy(), "https": _proxy()}))
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
    return fn(req, timeout=timeout, context=context) if context else fn(req, timeout=timeout)


def _is_public(ip) -> bool:
    """A globally routable address (tests widen it to the documentation ranges)."""
    return ip.is_global


def _checked(url: str) -> tuple:
    """``(parts, [ips])`` for a public http(s) URL, or ValueError.

    One resolution: the addresses returned here are the addresses fetch_url
    dials, so a zone answering public now and private on a second lookup never
    reaches the network.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs")
    if is_ip_echo(parts.hostname):
        raise IPEchoError(f"{parts.hostname} reports the caller's own IP address; "
                          "the bot does not read it")
    default = 443 if parts.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or default,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {parts.hostname}") from exc
    ips: list = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public(ip):
            raise ValueError(f"{parts.hostname} is not a public address")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise ValueError(f"cannot resolve {parts.hostname}")
    return parts, ips


def public_url(url: str) -> str:
    """The URL, or ValueError when it is not http(s) to a public, non-echo address."""
    _checked(url)
    return url


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP to ``host`` (Host header) over a socket to the validated ``ip``."""

    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._connect_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._connect_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to ``host`` (SNI + certificate) over a socket to the validated ``ip``."""

    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._connect_ip = ip

    def connect(self):
        sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _pinned_connection(scheme: str, host: str, ip: str, port: int, timeout: float):
    if CONNECT[0] is not None:
        return CONNECT[0](scheme, host, ip, port, timeout)
    if scheme == "https":
        return _PinnedHTTPSConnection(host, ip, port, timeout)
    return _PinnedHTTPConnection(host, ip, port, timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                       # fetch_url follows (and checks) every hop


def _get_via_proxy(url: str) -> tuple:
    """One hop through ``AWDISCORD_EGRESS_PROXY``: ``(status, location, body)``."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": _proxy(), "https": _proxy()}), _NoRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, None, resp.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return exc.code, exc.headers.get("Location"), b""
        raise


def _get_pinned(url: str) -> tuple:
    """One hop dialled at the validated address: ``(status, location, body)``."""
    parts, ips = _checked(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    headers = {"Host": parts.netloc.rsplit("@", 1)[-1], "User-Agent": UA,
               "Accept": "text/html,*/*", "Accept-Encoding": "identity"}
    # IPv4 first (a container often has no IPv6 route), then one more address:
    # every candidate is one the check above already validated.
    candidates = sorted(ips, key=lambda a: ":" in a)[:2]
    for n, ip in enumerate(candidates):
        conn = _pinned_connection(parts.scheme, str(parts.hostname), ip, port, TIMEOUT)
        try:
            try:
                conn.connect()
            except OSError:
                if n + 1 < len(candidates):
                    continue
                raise
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                return resp.status, resp.getheader("Location"), b""
            if resp.status >= 400:
                raise ValueError(f"the page answered HTTP {resp.status}")
            return resp.status, None, resp.read(MAX_BYTES)
        finally:
            conn.close()
    raise ValueError(f"cannot reach {parts.hostname}")


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


#: Keyed AitherSearch providers tried, in order, when its default (keyless) pick
#: returns nothing: the keyless scrapers are routinely bot-blocked (403/429/202
#: challenge), and an empty list reads to the model as "the search tools are
#: returning nothing". ``AWDISCORD_SEARCH_FALLBACK=""`` turns it off.
SEARCH_FALLBACK_DEFAULT = "exa,parallel"


def _fallback_providers() -> list[str]:
    raw = os.getenv("AWDISCORD_SEARCH_FALLBACK", SEARCH_FALLBACK_DEFAULT)
    return [p.strip() for p in raw.split(",") if p.strip()]


def _search_service(base: str, query: str, limit: int,
                    provider: str = "") -> list[dict]:
    ca = os.getenv("AWDISCORD_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=ca) if ca else None
    base = base.rstrip("/")
    try:   # AitherSearch shape
        req = urllib.request.Request(
            base + "/search", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"query": query, "limit": limit, "synthesize": False,
                             "auto_learn": False,
                             **({"provider": provider} if provider else {})}).encode())
        with _open(req, context=ctx) as resp:
            data = json.loads(resp.read())
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "snippet": r.get("snippet") or r.get("content", "")}
                for r in data.get("results", [])]
    except Exception as exc:  # not the AitherSearch shape: fall through to SearXNG
        if provider:          # a named provider is AitherSearch-only
            _log.info("AitherSearch provider %s failed: %s", provider, exc)
            return []
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


def _not_echo(result: dict) -> bool:
    return not is_ip_echo(urllib.parse.urlsplit(str(result.get("url") or "")).hostname or "")


def web_search(query: str, limit: int = 5) -> str:
    query = (query or "").strip()[:300]
    if not query:
        return "Give a search query."
    limit = max(1, min(int(limit), 10))
    base = os.getenv("AWDISCORD_SEARCH_URL", "").strip()
    # ask for extra rows: "what is my IP" sites are dropped, the rest still fill it
    results = _search_service(base, query, limit + 5) if base else _search_ddg(query, limit + 5)
    results = [r for r in results if _not_echo(r)][:limit]
    for provider in (_fallback_providers() if base else []):
        if results:
            break
        results = [r for r in _search_service(base, query, limit + 5, provider=provider)
                   if _not_echo(r)][:limit]
        _log.info("web_search fell back to provider=%s: %d results", provider,
                  len(results))
    if not results:
        return f"No results for {query!r}. Try fewer or more specific words."
    return egress.redact("\n".join(f"- {r['title']}\n  {r['url']}\n  {r['snippet'][:300]}"
                                    for r in results))


def fetch_url(url: str, max_chars: int = 6000) -> str:
    """A public page as text. Every redirect hop is validated, then dialled pinned.

    A "what is my IP" target (at any hop) is never contacted: the answer is
    this process's decoy address, as if the service had returned it.

    Raises:
        ValueError: a non-public or non-http(s) target (at any hop), too many
            redirects, or an HTTP error status.
    """
    hop = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            if _proxy():
                _checked(hop)           # the policy holds through a proxy too
                status, location, raw = _get_via_proxy(hop)
            else:
                status, location, raw = _get_pinned(hop)
        except IPEchoError:
            return _decoy_answer(urllib.parse.urlsplit(hop).hostname or "")
        if location is None:
            if 300 <= status < 400:
                raise ValueError(f"HTTP {status} redirect without a Location")
            return egress.redact(_text(raw, max(500, min(int(max_chars), 12000))))
        hop = urllib.parse.urljoin(hop, location)
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


def deep_research(question: str, depth: int = 7) -> str:
    """Search, then have the fleet's reasoning service answer from the results.

    A single web_search hands the model five snippets and no second step, so
    "research X properly" produced an essay from memory (owner, 2026-10-31).
    This runs the search here, then POSTs the question + results to Genesis
    /reasoning/reason (task_kind "deep"), which reasons over them and cites.
    """
    question = (question or "").strip()[:1500]
    if not question:
        return "Give a research question."
    results = web_search(question, limit=8)
    base = os.getenv("AWDISCORD_GENESIS_URL", "").strip()
    if not base:
        return results                     # no reasoning door configured: the sources
    body = {
        "prompt": f"Question: {question}\n\nSearch results:\n{results}",
        "system": ("Answer the question using ONLY these search results. Cite the "
                   "links you used. If they do not answer it, say what is missing "
                   "instead of guessing."),
        "task_kind": "deep", "effort": max(1, min(int(depth), 10)),
        "max_tokens": 2000, "temperature": 0.2,
    }
    headers = {"Content-Type": "application/json", "X-Caller-Type": "platform"}
    key = (os.getenv("AITHER_INTERNAL_SECRET") or "").strip()
    if key:
        headers["X-Internal-Key"] = key
    ca = os.getenv("AWDISCORD_CA_BUNDLE", "").strip()
    ctx = ssl.create_default_context(cafile=ca) if ca else None
    req = urllib.request.Request(base.rstrip("/") + "/reasoning/reason",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    try:
        with _open(req, timeout=240, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("deep_research reasoning failed, returning sources: %s", exc)
        return results + "\n\n(the reasoning step failed; these are the sources)"
    text = str((data or {}).get("text") or "").strip()
    return egress.redact(text or results)


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
    Tool("deep_research", "Research a question properly: searches the web and reasons "
         "over the results with sources. Use for 'research X', comparisons, current "
         "prices, or anything one search would only half answer.", deep_research,
         {"type": "object", "properties": {
             "question": {"type": "string"},
             "depth": {"type": "integer", "description": "1-10 effort, default 7"}},
          "required": ["question"]}),
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
             "the tools. A question that needs more than one search is deep_research, "
             "not web_search. If a tool fails, say what you could not get. Long answers "
             "are fine: the bot splits them across messages."),
    tools=TOOLS,
    on_ready=[egress.start],
    commands=[Command("news", "Current headlines", _news, dm=True, options=[
        Option("topic", "Optional topic", max_length=100)])],
)
