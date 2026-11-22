"""ShadowBroker: Aitherium's own OSINT board, readable from Discord chat.

2026-11-18: a member asked Aither about "ShadowBroker" reports and Aither
web-searched the NAME, found an unrelated X account, and answered "no credible
reports" -- because the board's tools were an awdk pack the Discord bot never
loaded. This plugin gives the chat agent the board itself:

  shadowbroker_news(query)       the board's news layer (search_news)
  shadowbroker_telemetry(query)  its live telemetry layers (search_telemetry)
  shadowbroker_status()          is it configured, reachable, which tier

The wire contract is the board's HMAC command channel, the same one
``AitherOS/lib/history/shadowbroker.py`` speaks (that module lives in the
platform tree, which the bot image does not carry, so the ~40 lines of client
are repeated here, stdlib only):

    POST /api/ai/channel/command   {"cmd": ..., "args": {...}}
    X-SB-Timestamp, X-SB-Nonce, X-SB-Signature =
      HMAC-SHA256(secret, "METHOD|PATH|TS|NONCE|sha256(body).hexdigest()")

Config: SHADOWBROKER_URL + SHADOWBROKER_HMAC_SECRET (or ..._AGENT_SECRET) from
the environment, else KEY=VALUE lines in ``$SHADOWBROKER_ENV_FILE`` (default
``/state/shadowbroker.env``, the bot's private volume). Unconfigured, the tools
say so -- they never fall back to a web search for the name.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from awdiscord.plugins import Command, Option, Plugin, ScopedStore, Tool

log = logging.getLogger("awdiscord.shadowbroker")

COMMAND_PATH = "/api/ai/channel/command"
CAPABILITIES_PATH = "/api/ai/capabilities"
DEFAULT_URL = "https://shadowbroker.aitherium.com"
MAX_ROWS = 15
TIMEOUT_S = 20.0


def _file_values() -> dict:
    path = Path(os.environ.get("SHADOWBROKER_ENV_FILE") or "/state/shadowbroker.env")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def config() -> tuple[str, str]:
    """(base_url, secret); secret is "" when the board is not configured here."""
    f = _file_values()
    url = os.environ.get("SHADOWBROKER_URL") or f.get("SHADOWBROKER_URL") or DEFAULT_URL
    secret = (os.environ.get("SHADOWBROKER_HMAC_SECRET")
              or os.environ.get("SHADOWBROKER_AGENT_SECRET")
              or f.get("SHADOWBROKER_HMAC_SECRET") or f.get("SHADOWBROKER_AGENT_SECRET")
              or "")
    return url.rstrip("/"), secret


def sign_headers(method: str, path: str, body: bytes, secret: str,
                 ts: int | None = None, nonce: str | None = None) -> dict:
    """The exact header set the board verifies (pinned by a test)."""
    ts_str = str(ts if ts is not None else int(time.time()))
    nonce = nonce or secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    message = f"{method.upper()}|{path}|{ts_str}|{nonce}|{digest}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"X-SB-Timestamp": ts_str, "X-SB-Nonce": nonce, "X-SB-Signature": sig}


USER_AGENT = "awdiscord/1.0 (+https://aitherium.com)"


def _jb(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _body(payload: dict) -> bytes:
    """Byte-identical to the platform client: {"cmd":<json>,"args":<json>}."""
    return (b'{"cmd":' + _jb(payload.get("cmd")) + b',"args":'
            + _jb(payload.get("args") or {}) + b"}")


def _open(req, timeout=TIMEOUT_S):          # seam for tests
    return urllib.request.urlopen(req, timeout=timeout)


def call(method: str, path: str, payload: dict | None = None) -> dict:
    """One signed request. {"ok", "status", "data", "reason"}; never raises."""
    base, secret = config()
    if not secret:
        return {"ok": False, "status": 0, "data": None, "reason": "unconfigured"}
    body = _body(payload) if payload is not None else b""
    headers = sign_headers(method, path, body, secret)
    # urllib's default "Python-urllib/x" agent is refused (HTTP 403) by the board's
    # edge -- measured 2026-11-18 with a key the platform client uses successfully.
    headers["User-Agent"] = USER_AGENT
    headers["Accept"] = "application/json"
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body or None, method=method,
                                 headers=headers)
    try:
        with _open(req) as resp:
            return {"ok": True, "status": resp.status,
                    "data": json.loads(resp.read().decode("utf-8") or "null"), "reason": ""}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "data": None,
                "reason": f"refused (HTTP {exc.code})"}
    except Exception as exc:  # noqa: BLE001 - an outage is an answer, not a crash
        return {"ok": False, "status": 0, "data": None,
                "reason": f"unreachable: {type(exc).__name__}"}


def _rows(data) -> list:
    """Rows from the board's envelope.

    Measured 2026-11-18: {"ok", "command_id", "tier", "status",
    "result": {"ok", "data": {"results": [...], "version", "truncated"}}}.
    Each row: source_layer, title, summary, source, link, lat, lng, risk_score.
    """
    node = data
    for key in ("result", "data"):
        if isinstance(node, dict) and isinstance(node.get(key), (dict, list)):
            node = node[key]
    if isinstance(node, dict):
        for key in ("results", "items", "rows"):
            if isinstance(node.get(key), list):
                return node[key]
    return node if isinstance(node, list) else []


def _fmt(row: dict) -> str:
    title = str(row.get("title") or row.get("headline") or row.get("name") or "").strip()
    src = str(row.get("source") or row.get("source_layer") or "").strip()
    layer = str(row.get("source_layer") or "").strip()
    risk = row.get("risk_score")
    where = ""
    if row.get("lat") is not None and row.get("lng") is not None:
        where = f"{float(row['lat']):.2f},{float(row['lng']):.2f}"
    url = str(row.get("url") or row.get("link") or "").strip()
    head = title or json.dumps(row)[:160]
    meta = " · ".join(x for x in (
        src, layer if layer and layer != src else "",
        f"risk {risk}" if risk not in (None, "") else "", where) if x)
    summary = str(row.get("summary") or "").strip()
    return (f"- {head}" + (f" ({meta})" if meta else "")
            + (f"\n  {summary[:200]}" if summary else "") + (f"\n  {url}" if url else ""))


def _command(cmd: str, query: str, limit: int) -> str:
    query = str(query or "").strip()[:200]
    limit = max(1, min(int(limit or 8), MAX_ROWS))
    out = call("POST", COMMAND_PATH, {"cmd": cmd, "args": {"query": query, "limit": limit}})
    if not out["ok"]:
        if out["reason"] == "unconfigured":
            return ("ShadowBroker (Aitherium's own OSINT board) is not configured on this "
                    "bot, so I cannot read it. Do not substitute a web search for its name.")
        return (f"ShadowBroker did not answer ({out['reason']}). Say so plainly; do not "
                "substitute a web search for its name.")
    rows = _rows(out["data"])
    if not rows:
        return f"ShadowBroker returned no {cmd.split('_', 1)[-1]} rows for {query!r}."
    return (f"ShadowBroker {cmd} for {query!r} — {len(rows[:limit])} row(s), from "
            "Aitherium's OSINT board:\n" + "\n".join(_fmt(r) for r in rows[:limit]
                                                    if isinstance(r, dict)))


def shadowbroker_news(query: str = "", limit: int = 8) -> str:
    return _command("search_news", query, limit)


def shadowbroker_telemetry(query: str = "", limit: int = 8) -> str:
    return _command("search_telemetry", query, limit)


def shadowbroker_status() -> str:
    base, secret = config()
    if not secret:
        return f"ShadowBroker at {base}: not configured on this bot (no HMAC secret)."
    out = call("GET", CAPABILITIES_PATH)
    if not out["ok"]:
        return f"ShadowBroker at {base}: configured but {out['reason']}."
    tier = ((out["data"] or {}).get("trust_boundary") or {}).get("access_tier", "unknown")
    return f"ShadowBroker at {base}: reachable, access tier {tier}."


# ── the alert watch: new board items posted to a channel, as cards ────────────
#
# Owner, 2026-11-18: "we need aither, atlas, demi, lyra keeping the people
# informed ... investigate and identify trends, alerts, news from shadowbroker
# and post alerts." A manager turns it on per server with
# /shadowbroker alerts channel:<#channel>; every ALERT_S seconds the board is
# swept across ALERT_TOPICS, items this server has not been shown are posted as
# ONE message of cards (newest risk first) with a trend line, and remembered.
# The config lives in THIS bot's store, so turning it on through one bot never
# makes the other persona bots post the same alert.

ALERT_S = int(os.environ.get("SHADOWBROKER_ALERT_SECONDS") or 900)
ALERT_TOPICS = ("attack", "explosion", "missile", "drone", "strike", "outage", "cyber",
                "ransomware", "breach", "sanction", "evacuation", "earthquake", "protest")
ALERT_MAX_CARDS = 10
SEEN_KEEP = 800
# Only items PUBLISHED within this window are alerts. Measured 2026-11-18: the
# first watch posted a 07 Nov missile report as "new" because a Telegram post
# without a parseable date is stored with the FETCH time (see
# backend/services/fetchers/telegram_osint.py: published falls back to now), so
# "not seen before" is not "new on the board". An item with no readable date is
# never alerted: better to miss it than to page the channel with old news.
# Measured 2026-11-18 (6h window): news 24 items, liveuamap 23, telegram 25 in the
# window at once -- a flood on the first run. 2h is the sweep interval's order of
# magnitude; "seen" suppresses the overlap, and GDELT carries no dates at all so
# its rows never alert.
ALERT_FRESH_S = int(os.environ.get("SHADOWBROKER_ALERT_FRESH_SECONDS") or 2 * 3600)
_ALERT_LAYERS = ("news", "gdelt", "liveuamap", "telegram_osint")
LAYER_COLOR = {"news": 0x3B82F6, "liveuamap": 0xE11D48, "cyber": 0xA855F7,
               "outages": 0xF59E0B}
_STOP = frozenset(("the", "and", "for", "was", "were", "with", "from", "after", "over",
                   "said", "says", "into", "that", "this", "has", "have", "are", "its",
                   "reported", "report", "city", "region", "near"))
_watch_started = threading.Event()
_HOST: dict = {}


def _alert_store(host, guild: str) -> ScopedStore:
    return ScopedStore(host.store, guild, PLUGIN.name)


def _key(row: dict) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(row.get("title") or "").lower()).strip()[:120] \
        or str(row.get("link") or "")


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_AGO_RE = re.compile(r"(\d+)\s*(minute|hour|day|week|month)s?\s+ago", re.I)
_AGO_S = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000}
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_RFC_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})\s+(\d{1,2}):(\d{2})"
                     r"(?::(\d{2}))?\s*(GMT|UTC|[+-]\d{4})")


def published_at(text, now: float | None = None) -> float | None:
    """Epoch seconds for a board date string, or None when it cannot be read.

    Measured formats (2026-11-18): news "Wed, 18 Nov 2026 09:03:15 GMT";
    liveuamap "21 hour ago" / "1 week ago". Anything else -- or a liveuamap
    marker with no number -- is None, and an undated item is never an alert.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    now = time.time() if now is None else now
    if _ISO_RE.match(raw):               # telegram_osint: 2026-11-18T13:42:50+00:00
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            return None
    m = _RFC_RE.search(raw)
    if m:
        day, mon, year, hh, mm, ss, zone = m.groups()
        try:
            epoch = calendar.timegm((int(year), _MONTHS[mon.lower()], int(day),
                                     int(hh), int(mm), int(ss or 0), 0, 0, 0))
        except (KeyError, ValueError):
            return None
        if zone.upper() in ("GMT", "UTC"):
            return float(epoch)
        off = int(zone[1:3]) * 3600 + int(zone[3:5]) * 60
        return float(epoch - (off if zone[0] == "+" else -off))
    m = _AGO_RE.search(raw)
    if m:
        return now - int(m.group(1)) * _AGO_S[m.group(2).lower()]
    return None


def _published(row: dict) -> float | None:
    for k in ("published", "timestamp", "date", "pub_date"):
        at = published_at(row.get(k))
        if at is not None:
            return at
    return None


def sweep_board(topics=ALERT_TOPICS, per_topic: int = 80,
                fresh_s: int | None = None, now: float | None = None) -> list:
    """Rows PUBLISHED within fresh_s, deduped by title, each tagged with its topic.

    Reads whole dated layers (get_layer_slice) rather than keyword search:
    search_news rows carry no date at all, and a topic search over "attack"
    matches months-old text. Only ALERT_TOPICS entries are kept.
    """
    fresh_s = ALERT_FRESH_S if fresh_s is None else fresh_s
    now = time.time() if now is None else now
    seen, rows = set(), []
    wanted = tuple(t.lower() for t in topics)
    for layer in _ALERT_LAYERS:
        out = call("POST", COMMAND_PATH, {"cmd": "get_layer_slice",
                                          "args": {"layers": [layer],
                                                   "limit_per_layer": per_topic}})
        if not out["ok"]:
            if out["reason"] == "unconfigured":
                return []
            continue
        for r in _layer_items(out["data"], layer):
            if not isinstance(r, dict):
                continue
            at = _published(r)
            if at is None or now - at > fresh_s:
                continue
            text = " ".join(str(r.get(k) or "") for k in
                            ("title", "summary", "description")).lower()
            topic = next((t for t in wanted if t in text), None)
            if topic is None:
                continue
            k = _key(r)
            if k and k not in seen:
                seen.add(k)
                rows.append(dict(r, _topic=topic, _published=at))
    return rows


def _layer_items(data, layer: str) -> list:
    """Rows of one layer from the get_layer_slice envelope
    ({"layers": {"news": [...], "telegram_osint": {"posts": [...]}}})."""
    # Measured envelope (2026-11-18): {"ok","status","data":{"ok","command_id",
    # "result":{"ok","data":{"layers":{...}}}}} -- descend every wrapper, then layers.
    node = data
    for _ in range(4):
        if not isinstance(node, dict):
            break
        nxt = next((node[k] for k in ("result", "data", "layers")
                    if isinstance(node.get(k), dict)), None)
        if nxt is None:
            break
        node = nxt
    node = node.get(layer) if isinstance(node, dict) else node
    if isinstance(node, dict):
        node = node.get("posts") or node.get("items") or []
    return node if isinstance(node, list) else []


def trend_line(rows: list) -> str:
    """Topic and word counts across this batch -- what the board keeps repeating."""
    topics: dict = {}
    words: dict = {}
    for r in rows:
        topics[r.get("_topic", "?")] = topics.get(r.get("_topic", "?"), 0) + 1
        for w in set(re.findall(r"[a-z]{4,}", str(r.get("title") or "").lower())):
            if w not in _STOP:
                words[w] = words.get(w, 0) + 1
    top_t = sorted(topics.items(), key=lambda kv: -kv[1])[:4]
    top_w = [w for w, n in sorted(words.items(), key=lambda kv: -kv[1]) if n >= 2][:5]
    line = "By topic: " + ", ".join(f"{t} {n}" for t, n in top_t)
    if top_w:
        line += " · recurring: " + ", ".join(top_w)
    return line


def alert_message(rows: list, when: str | None = None,
                  window_s: int | None = None) -> tuple[str, list]:
    """(content, cards) for one alert post: newest first, then highest risk."""
    def rank(r):
        risk = r.get("risk_score")
        return (-(float(r.get("_published") or 0.0)),
                -(float(risk) if isinstance(risk, (int, float)) else 0.0),
                0 if r.get("lat") is not None else 1)
    rows = sorted(rows, key=rank)
    stamp = when or time.strftime("%d %b %H:%M UTC", time.gmtime())
    cards = []
    for r in rows[:ALERT_MAX_CARDS]:
        risk = r.get("risk_score")
        loc = (f"{float(r['lat']):.2f}, {float(r['lng']):.2f}"
               if r.get("lat") is not None and r.get("lng") is not None else "")
        card = {"title": str(r.get("title") or "")[:256],
                "color": LAYER_COLOR.get(str(r.get("source_layer") or ""), 0x888888),
                "footer": {"text": " · ".join(x for x in (
                    str(r.get("_topic") or "").capitalize(), str(r.get("source") or ""),
                    f"risk {risk}" if risk not in (None, "") else "", loc) if x)}}
        if str(r.get("link") or "").startswith("http"):
            card["url"] = str(r["link"])[:2048]
        if r.get("summary"):
            card["description"] = str(r["summary"])[:300]
        cards.append(card)
    more = len(rows) - len(cards)
    content = (f"**ShadowBroker alert — {stamp}** · {len(rows)} new item(s),"
               f" published in the last {(window_s or ALERT_FRESH_S) // 3600}h"
               + (f", top {len(cards)} below" if more > 0 else "") + "\n"
               + trend_line(rows) + "\n_Reply or react to discuss._")
    return content, cards


def run_alerts(host, guild: str) -> int:
    """One sweep for one server. Returns how many items it posted (0 = quiet)."""
    store = _alert_store(host, guild)
    conf = store.get("alerts") or {}
    channel = str(conf.get("channel") or "") if isinstance(conf, dict) else ""
    rest = getattr(host, "rest", None)
    if not channel or rest is None:
        return 0
    seen = list(store.get("seen") or [])
    known = set(seen)
    fresh = [r for r in sweep_board() if _key(r) not in known]
    if not fresh:
        return 0
    content, cards = alert_message(fresh, window_s=ALERT_FRESH_S)
    try:
        rest.request("POST", f"/channels/{channel}/messages",
                     {"content": content, "embeds": cards,
                      "allowed_mentions": {"parse": []}})
    except Exception as exc:  # noqa: BLE001 - try the same items again next sweep
        log.warning("shadowbroker alert post to %s failed: %s", channel, type(exc).__name__)
        return 0
    store.set("seen", (seen + [_key(r) for r in fresh])[-SEEN_KEEP:])
    return len(fresh)


def _on_ready(host) -> None:
    _HOST["host"] = host
    if _watch_started.is_set():
        return
    _watch_started.set()

    def loop() -> None:
        time.sleep(30)                                   # let the gateway settle
        while True:
            try:
                for guild in host.store.guilds():
                    n = run_alerts(host, guild)
                    if n:
                        log.info("shadowbroker alerts: posted %d item(s) in %s", n, guild)
            except Exception as exc:                     # noqa: BLE001
                log.warning("shadowbroker alert sweep failed: %s", exc)
            time.sleep(ALERT_S)

    threading.Thread(target=loop, name="shadowbroker-alerts", daemon=True).start()


def _alerts_on(ctx):
    channel = str(ctx.opt("channel") or "").strip()
    if not channel:
        return ctx.reply("Pick the channel to post alerts in.", private=True)
    _alert_store(ctx.host, str(ctx.guild)).set("alerts", {"channel": channel,
                                                          "by": ctx.user,
                                                          "at": int(time.time())})
    return ctx.reply(f"ShadowBroker alerts will post in <#{channel}> every "
                     f"{ALERT_S // 60} minutes, only items this server has not seen. "
                     "`/shadowbroker alerts-off` stops them.", private=True)


def _alerts_off(ctx):
    _alert_store(ctx.host, str(ctx.guild)).delete("alerts")
    return ctx.reply("ShadowBroker alerts are off in this server.", private=True)


def _alerts_now(ctx):
    host, guild = ctx.host, str(ctx.guild)

    def work() -> str:
        n = run_alerts(host, guild)
        return (f"Posted {n} new item(s)." if n else
                "Nothing new on the board since the last alert (or alerts are off here).")
    return ctx.defer(work, private=True)


_Q = {"type": "object", "properties": {
    "query": {"type": "string", "description": "what to look for, e.g. 'ransomware', "
              "'Ukraine', 'internet outage', a company or a place"},
    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
      "required": ["query"]}

NEWS = Tool(
    "shadowbroker_news",
    "Search ShadowBroker -- Aitherium's OWN OSINT board (shadowbroker.aitherium.com: "
    "conflict, cyber, outages, aviation, maritime and civic feeds) -- for news and "
    "reports. Use it whenever a member mentions ShadowBroker or asks about world, "
    "security or cyber events. ShadowBroker is our board, not a person or account: "
    "never web-search its name.",
    shadowbroker_news, _Q)

TELEMETRY = Tool(
    "shadowbroker_telemetry",
    "Search ShadowBroker's live telemetry layers (flights, ships, internet outage "
    "probes, conflict and civic layers) for a place or topic.",
    shadowbroker_telemetry, _Q)

STATUS = Tool(
    "shadowbroker_status",
    "Whether ShadowBroker (Aitherium's OSINT board) is configured and reachable from "
    "this bot, and its access tier.",
    shadowbroker_status, {"type": "object", "properties": {}})

PLUGIN = Plugin(
    name="shadowbroker",
    version="0.1.0",
    description="Aitherium's ShadowBroker OSINT board, readable from chat",
    default_enabled=True,
    # Free-first launch (owner 2026-11-18): the board is a gated feature. A server
    # holds the intel tier by approval (or grandfathered if it already used this);
    # only then are these tools offered and the alert commands allowed.
    requires_tier="intel",
    tools=[NEWS, TELEMETRY, STATUS],
    on_ready=[_on_ready],
    commands=[
        Command("shadowbroker", "Aitherium's OSINT board", subcommands=[
            Command("alerts", "Post new board items in a channel (managers)", _alerts_on,
                    admin=True, options=[Option("channel", "Where alerts go",
                                                type="channel", required=True)]),
            Command("alerts-off", "Stop posting alerts here (managers)", _alerts_off,
                    admin=True),
            Command("alerts-now", "Sweep the board and post anything new now (managers)",
                    _alerts_now, admin=True),
        ]),
    ],
    persona=("ShadowBroker is Aitherium's own OSINT board (shadowbroker.aitherium.com). "
             "When anyone mentions ShadowBroker, or asks about world, security, cyber "
             "or outage events, call shadowbroker_news / shadowbroker_telemetry and "
             "answer from what it returns, naming it as the source. Never web-search "
             "the name ShadowBroker; if the board is down or unconfigured, say exactly "
             "that."),
)
