"""Access tiers: which gated features a server is allowed to switch on.

Owner decision 2026-11-18: Discord servers will pay through BOTH Stripe checkout
and Discord App Subscriptions LATER; launch is FREE FIRST as approve-on-request.
This module is the one grant store that both paid paths will write into, so the
launch path (staff approval) and the later ones differ only in a *source*:

    grant(store, guild, "intel", source="approval", by="staff-id")
    revoke(store, guild, "intel")
    has(store, guild, "intel")            -> bool
    tiers_for(store, guild)               -> {"intel"}

Sources: ``approval``, ``stripe``, ``discord_sku``, ``operator``,
``grandfathered``. A grant records who wrote it and when, and may carry an
``expires`` (epoch seconds or an ISO-8601 string); an expired grant is not held.

WHAT IS NOT BUILT HERE, ON PURPOSE. A Stripe webhook handler and Discord's
``ENTITLEMENT_CREATE`` / ``ENTITLEMENT_DELETE`` gateway events will call
``grant(..., source="stripe"|"discord_sku")`` and ``revoke(...)`` when the paid
paths open. Nothing in this module talks to Stripe or Discord.

WHERE IT LIVES. Every grant is stored in the bot's per-server ``GuildStore``
(kv namespace ``_access``), so one server can never read or change another's.
No tier data is ever federated: each guild is its own tenant.

GRANDFATHERING. A server that was already using a gated feature before tiers
existed keeps it: :func:`grandfather` grants ``intel`` once, idempotently, to
every guild with ShadowBroker alerts configured and to every guild the bot is
served for (``AWDISCORD_SERVED_GUILDS`` plus the configured home guild).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

#: The kv "plugin" namespace that holds the grants (not a real plugin).
SLOT = "_access"
#: The one key in that namespace: ``{tier: {"source", "by", "at", "expires"}}``.
KEY = "grants"

#: The tiers that exist today. ShadowBroker is the first gated feature.
INTEL = "intel"
TIERS = (INTEL,)

#: Every source a grant may come from. Paid sources are documented above.
SOURCES = ("approval", "stripe", "discord_sku", "operator", "grandfathered")

#: A stronger source is never overwritten by a weaker one (an approval survives a
#: later grandfather sweep; a paid grant survives a re-run of the approval path).
_RANK = {"grandfathered": 1, "approval": 2, "discord_sku": 3, "stripe": 3,
         "operator": 4}

#: Servers the platform itself runs, served even when the env allowlist is unset.
HOME_ENV = "AITHER_DISCORD_HOME_GUILD_ID"
HOME_ENV_ALT = "AWDISCORD_HOME_GUILD_ID"
SERVED_ENV = "AWDISCORD_SERVED_GUILDS"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(
        timespec="seconds")


def _epoch(value) -> float | None:
    """Epoch seconds from a float/int or an ISO-8601 string; None = no expiry."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _live(rec, now: float | None = None) -> bool:
    """A grant record is held while it exists and its expiry has not passed."""
    if not isinstance(rec, dict):
        return False
    expires = _epoch(rec.get("expires"))
    if expires is None:
        return True
    return (time.time() if now is None else now) < expires


def _records(store, guild: str) -> dict:
    got = store.get(str(guild), SLOT, KEY, {})
    return got if isinstance(got, dict) else {}


def grant(store, guild: str, tier: str, source: str, by: str = "",
          expires=None) -> dict:
    """Grant ``tier`` to ``guild``; returns the stored record.

    Idempotent for an equal or weaker source: re-running a grandfather sweep (or
    a retried approval) keeps the first record's ``by``/``at``. A live grant from
    a STRONGER source is never downgraded. An empty guild, an unknown tier or an
    unknown source raises (a caller bug must not silently grant nothing).
    """
    guild = str(guild or "").strip()
    tier = str(tier or "").strip()
    source = str(source or "").strip()
    if not guild:
        raise ValueError("access.grant needs a guild id")
    if not tier:
        raise ValueError("access.grant needs a tier")
    if source not in SOURCES:
        raise ValueError(f"unknown access source {source!r}; one of {SOURCES}")
    until = _epoch(expires)
    stamp = {"source": source, "by": str(by or ""), "at": _iso(time.time()),
             "expires": None if until is None else _iso(until)}

    def put(cur):
        cur = dict(cur) if isinstance(cur, dict) else {}
        existing = cur.get(tier)
        if _live(existing) and _RANK.get(existing.get("source"), 0) >= _RANK[source]:
            return cur, existing
        cur[tier] = stamp
        return cur, stamp
    return store.update(guild, SLOT, KEY, put, {})


def revoke(store, guild: str, tier: str) -> bool:
    """Remove ``tier`` from ``guild``. True when a grant was there to remove."""
    tier = str(tier or "").strip()
    if not tier:
        return False

    def drop(cur):
        cur = dict(cur) if isinstance(cur, dict) else {}
        if tier not in cur:
            return cur, False
        del cur[tier]
        return cur, True
    return bool(store.update(str(guild), SLOT, KEY, drop, {}))


def has(store, guild: str, tier: str) -> bool:
    """True when ``guild`` holds a live grant for ``tier``."""
    if not guild or not tier:
        return False
    return _live(_records(store, str(guild)).get(str(tier)))


def tiers_for(store, guild: str) -> set[str]:
    """Every tier ``guild`` currently holds."""
    if not guild:
        return set()
    return {tier for tier, rec in _records(store, str(guild)).items() if _live(rec)}


def describe(store, guild: str, tier: str) -> dict:
    """The live record for ``tier`` (source/by/at/expires), or {}."""
    rec = _records(store, str(guild)).get(str(tier))
    return dict(rec) if _live(rec) else {}


def served_guilds() -> set[str]:
    """The guilds the bot is served for: env allowlist plus the configured home."""
    out: set[str] = set()
    for raw in (os.getenv(SERVED_ENV) or "").replace(",", " ").split():
        token = raw.strip()
        if token.isdigit():
            out.add(token)
    for env in (HOME_ENV, HOME_ENV_ALT):
        home = (os.getenv(env) or "").strip()
        if home.isdigit():
            out.add(home)
    return out


def grandfather(store, served=None) -> list[str]:
    """Grant ``intel`` (source ``grandfathered``) to servers already using it.

    Triggers, either one: ShadowBroker alerts are configured in that guild's
    store (``kv plugin=shadowbroker key=alerts``), or the guild is served
    (``AWDISCORD_SERVED_GUILDS`` / the home guild). Idempotent: a second call
    changes nothing and never downgrades an approval or a paid grant. Returns the
    guilds it considered (whether or not a grant was newly written).
    """
    triggers = {str(g) for g in (served if served is not None else served_guilds()) if g}
    guilds = {str(g) for g in store.guilds() if str(g)} | triggers
    for guild in sorted(guilds):
        if guild in triggers or store.get(guild, "shadowbroker", "alerts"):
            grant(store, guild, INTEL, "grandfathered", by="grandfather")
    return sorted(guilds)
