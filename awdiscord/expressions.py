"""The server's own custom emoji and stickers, handed to the agent as context.

Nothing is trained: the agent learns a server's expressions from a short block in
its prompt, rebuilt from the guild's live list (``GET /guilds/{id}/emojis`` and
``/stickers``) and cached for ``TTL_S``. Every failure is soft -- a REST error, a
missing client or a broken store gives an empty block, never a broken reply.

Emoji go inline as ``<:name:id>`` (``<a:name:id>`` when animated). Stickers cannot
live in message text, so the agent writes ``[sticker:name]``; :func:`take_sticker`
removes the marker and returns the id the reply sends as ``sticker_ids``.

An operator may describe expressions per server in the plugin store:
``store.set(guild, "expressions", "meanings", {"pepe_laugh": "something is funny"})``.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

log = logging.getLogger("awdiscord.expressions")

#: Seconds a guild's list is reused before it is fetched again.
TTL_S = 600.0
#: Seconds a FAILED fetch is remembered (as empty), so an outage is not hit once
#: per message.
FAIL_TTL_S = 60.0
#: Most entries the block lists (emoji + stickers).
MAX_ENTRIES = 40
#: Most stickers the block lists (the rest of the budget goes to emoji).
MAX_STICKERS = 10
#: Where the operator's per-server meanings live in the plugin store.
STORE_PLUGIN = "expressions"
STORE_KEY = "meanings"

#: ``[sticker:name]`` anywhere in an answer; the name is matched case-insensitively.
STICKER_RE = re.compile(r"\[sticker:\s*([^\]\n]{1,60}?)\s*\]", re.IGNORECASE)

USAGE = ("How to use them: optional. At most 1-2 per message, and only when one "
         "matches the tone. None in serious, support, error or moderation replies. "
         "Never use one in place of words. Use only the ones listed here, written "
         "exactly as shown.")


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(w) > 1}


def _humanize(name: str) -> str:
    return re.sub(r"[_\-]+", " ", str(name or "")).strip()


class ExpressionCache:
    """Per-guild ``(emojis, stickers)`` with a TTL; never raises.

    ``rest`` is the bot's Rest client, or a zero-argument callable returning it
    (the gateway sets the host's client after the dispatcher is built).
    """

    def __init__(self, rest: Any = None, ttl: float = TTL_S,
                 fail_ttl: float = FAIL_TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._rest = rest
        self.ttl, self.fail_ttl, self._clock = float(ttl), float(fail_ttl), clock
        self._cache: dict[str, tuple[float, list[dict], list[dict]]] = {}

    def _client(self) -> Any:
        rest = self._rest
        if callable(rest) and not hasattr(rest, "guild_emojis"):
            try:
                rest = rest()
            except Exception:  # noqa: BLE001 - no client means no expressions
                return None
        return rest

    def get(self, guild: str | None) -> tuple[list[dict], list[dict]]:
        """This guild's (emojis, stickers); ``([], [])`` when they cannot be read."""
        if not guild:
            return [], []
        key = str(guild)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1], hit[2]
        rest = self._client()
        if rest is None:
            return [], []
        try:
            emojis = rest.guild_emojis(key) or []
            stickers = rest.guild_stickers(key) or []
            if not isinstance(emojis, list) or not isinstance(stickers, list):
                raise TypeError("expression list is not a list")
            emojis = [e for e in emojis if isinstance(e, dict)]
            stickers = [s for s in stickers if isinstance(s, dict)]
            expires = now + self.ttl
        except Exception as exc:  # noqa: BLE001 - fail soft: no expressions this turn
            log.warning("guild %s expressions unavailable: %s", key, type(exc).__name__)
            emojis, stickers, expires = [], [], now + self.fail_ttl
        self._cache[key] = (expires, emojis, stickers)
        if len(self._cache) > 1000:                  # bound memory across guilds
            self._cache.pop(next(iter(self._cache)))
        return emojis, stickers


def usable_emojis(emojis: list[dict]) -> list[dict]:
    """Emoji the bot can actually post: available and not role-restricted."""
    out = []
    for e in emojis or []:
        if not e.get("id") or not e.get("name"):
            continue
        if e.get("available") is False or e.get("roles"):
            continue
        out.append(e)
    return out


def usable_stickers(stickers: list[dict]) -> list[dict]:
    """Stickers the bot can send (``available`` not False, with an id and name)."""
    return [s for s in stickers or []
            if s.get("id") and s.get("name") and s.get("available") is not False]


def emoji_code(emoji: dict) -> str:
    """``<:name:id>``, or ``<a:name:id>`` for an animated emoji."""
    prefix = "a" if emoji.get("animated") else ""
    return f"<{prefix}:{emoji['name']}:{emoji['id']}>"


def _meaning(item: dict, meanings: dict, sticker: bool) -> str:
    name = str(item.get("name") or "")
    custom = meanings.get(name) or meanings.get(name.lower())
    if custom:
        return str(custom).strip()[:80]
    if sticker:
        desc = str(item.get("description") or "").strip()
        tags = str(item.get("tags") or "").strip()
        bits = [b for b in (desc, f"tags: {tags}" if tags else "") if b]
        if bits:
            return "; ".join(bits)[:80]
    return _humanize(name)[:80]


def _relevance(item: dict, meanings: dict, query_words: set[str]) -> int:
    name = str(item.get("name") or "")
    text = " ".join([_humanize(name), str(item.get("tags") or ""),
                     str(item.get("description") or ""), str(meanings.get(name) or "")])
    return len(_words(text) & query_words)


def _pick(items: list[dict], limit: int, meanings: dict,
          query_words: set[str]) -> list[dict]:
    """``items`` if they fit, else the ``limit`` most relevant to the query (stable)."""
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    ranked = sorted(enumerate(items),
                    key=lambda p: (-_relevance(p[1], meanings, query_words), p[0]))
    keep = {i for i, _ in ranked[:limit]}
    return [it for i, it in enumerate(items) if i in keep]


def context_block(emojis: list[dict], stickers: list[dict], meanings: dict | None = None,
                  query: str = "", max_entries: int = MAX_ENTRIES,
                  max_stickers: int = MAX_STICKERS, allow_stickers: bool = True) -> str:
    """The prompt block naming this server's expressions; "" when there are none.

    ``allow_stickers=False`` (a slash-command answer: an interaction edit cannot
    carry ``sticker_ids``) lists emoji only.
    """
    meanings = meanings if isinstance(meanings, dict) else {}
    qw = _words(query)
    em = usable_emojis(emojis)
    st = usable_stickers(stickers) if allow_stickers else []
    st = _pick(st, min(max_stickers, max_entries), meanings, qw)
    em = _pick(em, max_entries - len(st), meanings, qw)
    if not em and not st:
        return ""
    lines = ["This server's own custom emoji and stickers:"]
    if em:
        lines.append("Emoji (paste the code exactly):")
        lines += [f"- {emoji_code(e)} {_meaning(e, meanings, False)}" for e in em]
    if st:
        lines.append("Stickers (write [sticker:name] to attach one; at most one per "
                     "reply):")
        lines += [f"- {s['name']}: {_meaning(s, meanings, True)}" for s in st]
    lines.append(USAGE)
    return "\n".join(lines)


def strip_markers(text: str) -> str:
    """``text`` with every ``[sticker:...]`` marker removed and the spacing tidied."""
    if not text or "[sticker:" not in text.lower():
        return text
    out = STICKER_RE.sub("", text)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def take_sticker(text: str, stickers: list[dict]) -> tuple[str, str | None]:
    """``text`` without any ``[sticker:name]`` markers, and the first known sticker id.

    Unknown names are dropped silently; only one sticker is ever returned.
    """
    by_name = {str(s.get("name") or "").strip().lower(): str(s["id"])
               for s in usable_stickers(stickers)}
    chosen: str | None = None
    for m in STICKER_RE.finditer(text or ""):
        sid = by_name.get(m.group(1).strip().lower())
        if sid:
            chosen = sid
            break
    return strip_markers(text), chosen


def meanings_for(store: Any, guild: str | None) -> dict:
    """The operator's per-server meanings map; {} when there is none or it fails."""
    if store is None or not guild:
        return {}
    try:
        got = store.get(str(guild), STORE_PLUGIN, STORE_KEY, None)
    except Exception:  # noqa: BLE001 - meanings are optional
        return {}
    return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}
