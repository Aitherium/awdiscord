"""Redact a phrase (a name, an address) from one channel's recent messages.

Measured 2026-11-16: an owner asked the bot to purge his full name from a channel
and was told "I don't have a tool that edits or deletes messages". Both halves of
that were wrong, and this module is the capability:

* the bot's OWN messages that contain the phrase are EDITED, the phrase replaced by
  ``[redacted]`` (a bot may always edit what it posted);
* other members' messages that contain it can only be DELETED (Discord lets nobody
  edit another person's message), and only where the bot holds Manage Messages in
  that channel. They are listed first; nothing is deleted until the person who
  asked presses a second, private confirmation button. Without the permission the
  list is returned and nothing is touched.

Who may run it: a member holding Manage Messages, Manage Server or Administrator in
that channel, or one of the bot's operators (``AWDISCORD_OPERATORS``, the platform
owner notion the host already has). Everybody else is refused.

Privacy: the phrase is never logged, stored or posted. Logs carry
``sha256(phrase)[:12]`` and its length; the pending confirmation stores message ids
only; every reply is ephemeral or a DM.

This module decides and talks REST; ``builtin/community.py`` wires the slash
command, the agent tool and the confirmation buttons.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("awdiscord.redact")

MANAGE_MESSAGES = 1 << 13
MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
READ_HISTORY = 1 << 16
ALL_PERMISSIONS = (1 << 53) - 1
STAFF_BITS = MANAGE_MESSAGES | MANAGE_GUILD | ADMINISTRATOR

DEFAULT_DAYS = 30
MAX_DAYS = 90
MAX_MESSAGES = 1000
MIN_PHRASE = 3                      # "a" would match every message in the channel
PAGE = 100
PACE_S = 0.3                        # between writes; Rest.request also honours 429
CONFIRM_TTL_S = 15 * 60
PREVIEW_ROWS = 15
REPLACEMENT = "[redacted]"
DISCORD_EPOCH_MS = 1420070400000
THREAD_TYPES = (10, 11, 12)
PENDING_PREFIX = "redact:"


def fingerprint(phrase: str) -> str:
    """What a log line may say about a phrase: a hash prefix and a length, never it."""
    raw = str(phrase or "").strip().casefold().encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()[:12]} len={len(raw)}"


def clamp_days(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, n))


def snowflake_ms(sid: Any) -> int:
    """A Discord id's creation time in epoch milliseconds (0 when it is not an id)."""
    try:
        return (int(str(sid)) >> 22) + DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return 0


def _bits(value: Any) -> int:
    try:
        return int(str(value if value is not None else "0"), 10)
    except (TypeError, ValueError):
        return 0


def is_staff(permissions: int) -> bool:
    return bool(int(permissions or 0) & STAFF_BITS)


# ── effective channel permissions (Discord's documented algorithm) ─────────────

def effective_permissions(rest, guild: str, channel: str, user: str) -> int:
    """``user``'s permissions in ``channel``: owner, roles, Administrator, overwrites.

    A thread takes its parent channel's overwrites. Raises what ``rest`` raises; a
    caller treats a failure as "no permission" (fail closed).
    """
    info = rest.request("GET", f"/guilds/{guild}") or {}
    if str(info.get("owner_id") or "") == str(user):
        return ALL_PERMISSIONS
    roles = {str(r.get("id")): r for r in rest.request("GET", f"/guilds/{guild}/roles") or []}
    member = rest.request("GET", f"/guilds/{guild}/members/{user}") or {}
    mine = [str(r) for r in member.get("roles") or []]
    perms = _bits((roles.get(str(guild)) or {}).get("permissions"))
    for rid in mine:
        perms |= _bits((roles.get(rid) or {}).get("permissions"))
    if perms & ADMINISTRATOR:
        return ALL_PERMISSIONS
    chan = rest.request("GET", f"/channels/{channel}") or {}
    if int(chan.get("type") or 0) in THREAD_TYPES and chan.get("parent_id"):
        chan = rest.request("GET", f"/channels/{chan['parent_id']}") or {}
    overwrites = {str(o.get("id")): o for o in chan.get("permission_overwrites") or []}
    everyone = overwrites.get(str(guild))
    if everyone:
        perms = (perms & ~_bits(everyone.get("deny"))) | _bits(everyone.get("allow"))
    allow = deny = 0
    for rid in mine:
        o = overwrites.get(rid)
        if o and int(o.get("type", 0)) == 0:
            allow |= _bits(o.get("allow"))
            deny |= _bits(o.get("deny"))
    perms = (perms & ~deny) | allow
    own = overwrites.get(str(user))
    if own and int(own.get("type", 1)) == 1:
        perms = (perms & ~_bits(own.get("deny"))) | _bits(own.get("allow"))
    return perms


def _perms_or_zero(rest, guild: str, channel: str, user: str) -> int:
    try:
        return effective_permissions(rest, guild, channel, user)
    except Exception as exc:  # noqa: BLE001 - unknown permissions are no permissions
        log.warning("redact: permission lookup failed (%s)", type(exc).__name__)
        return 0


# ── the scan ──────────────────────────────────────────────────────────────────

@dataclass
class Hit:
    id: str
    author: str
    author_name: str
    at_ms: int
    own: bool


@dataclass
class Scan:
    guild: str
    channel: str
    scanned: int = 0
    own: list[Hit] = field(default_factory=list)
    others: list[Hit] = field(default_factory=list)
    truncated: bool = False


def _matcher(phrase: str) -> re.Pattern:
    return re.compile(re.escape(phrase.strip()), re.IGNORECASE)


def scan(rest, guild: str, channel: str, phrase: str, bot_id: str,
         days: int = DEFAULT_DAYS, now_ms: int | None = None,
         max_messages: int = MAX_MESSAGES) -> Scan:
    """Page back through ``channel`` for ``days`` (at most ``max_messages``)."""
    pattern = _matcher(phrase)
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cutoff = now_ms - clamp_days(days) * 86_400_000
    out = Scan(guild=str(guild), channel=str(channel))
    before = ""
    while out.scanned < max_messages:
        path = f"/channels/{channel}/messages?limit={PAGE}" + (
            f"&before={before}" if before else "")
        page = rest.request("GET", path) or []
        if not page:
            break
        stop = False
        for m in page:
            mid = str(m.get("id") or "")
            at = snowflake_ms(mid)
            if at and at < cutoff:
                stop = True
                break
            out.scanned += 1
            if pattern.search(str(m.get("content") or "")):
                author = m.get("author") or {}
                aid = str(author.get("id") or "")
                own = bool(aid) and aid == str(bot_id) and not m.get("webhook_id")
                hit = Hit(mid, aid, str(author.get("global_name") or author.get("username")
                                        or aid), at, own)
                (out.own if own else out.others).append(hit)
            if out.scanned >= max_messages:
                out.truncated = True
                stop = True
                break
            before = mid
        if stop or len(page) < PAGE:
            break
    return out


def edit_own(rest, channel: str, ids: list[str], phrase: str,
             sleep: Callable[[float], None] = time.sleep) -> tuple[int, int]:
    """Replace the phrase in each of the bot's own messages. ``(edited, failed)``."""
    pattern = _matcher(phrase)
    done = failed = 0
    for mid in ids:
        try:
            msg = rest.request("GET", f"/channels/{channel}/messages/{mid}") or {}
            new = pattern.sub(REPLACEMENT, str(msg.get("content") or ""))
            rest.request("PATCH", f"/channels/{channel}/messages/{mid}",
                         {"content": new, "allowed_mentions": {"parse": []}})
            done += 1
        except Exception as exc:  # noqa: BLE001 - one message must not stop the rest
            failed += 1
            log.warning("redact: edit failed (%s)", type(exc).__name__)
        sleep(PACE_S)
    return done, failed


def delete_many(rest, channel: str, ids: list[str],
                sleep: Callable[[float], None] = time.sleep) -> tuple[int, int]:
    """Delete each message (needs Manage Messages for others'). ``(deleted, failed)``."""
    done = failed = 0
    for mid in ids:
        try:
            rest.request("DELETE", f"/channels/{channel}/messages/{mid}")
            done += 1
        except Exception as exc:  # noqa: BLE001 - one message must not stop the rest
            status = getattr(exc, "status", None)
            if status == 404:
                done += 1               # already gone is what was asked for
            else:
                failed += 1
                log.warning("redact: delete failed (%s %s)", type(exc).__name__, status)
        sleep(PACE_S)
    return done, failed


def jump(guild: str, channel: str, mid: str) -> str:
    return f"https://discord.com/channels/{guild}/{channel}/{mid}"


def preview_lines(s: Scan) -> list[str]:
    rows = []
    for h in s.others[:PREVIEW_ROWS]:
        when = time.strftime("%Y-%m-%d", time.gmtime(h.at_ms / 1000)) if h.at_ms else "?"
        rows.append(f"- {h.author_name} ({when}) {jump(s.guild, s.channel, h.id)}")
    if len(s.others) > PREVIEW_ROWS:
        rows.append(f"- ...and {len(s.others) - PREVIEW_ROWS} more")
    return rows


# ── the whole request ─────────────────────────────────────────────────────────

@dataclass
class Outcome:
    text: str                       # ephemeral/DM wording; never contains the phrase
    pending: dict | None = None     # what a confirmation button would delete
    refused: bool = False
    summary: str = ""               # one line for the agent (counts only)
    no_permission: bool = False     # others matched, but the bot lacks Manage Messages


def run(rest, *, guild: str, channel: str, user: str, phrase: str,
        days: Any = DEFAULT_DAYS, operator: bool = False,
        caller_permissions: int | None = None, bot_id: str = "",
        sleep: Callable[[float], None] = time.sleep,
        now_ms: int | None = None) -> Outcome:
    """Check who asks, scan, edit the bot's own hits, and stage the rest for confirm.

    ``caller_permissions`` = Discord's resolved permissions for the asker in this
    channel when the interaction carried them (a slash command); None reads them
    over REST (an @mention turn).
    """
    phrase = str(phrase or "").strip()
    fp = fingerprint(phrase)
    if not guild or not channel:
        return Outcome("Redaction works in a server channel only.", refused=True)
    if len(phrase) < MIN_PHRASE:
        return Outcome(f"Give a phrase of at least {MIN_PHRASE} characters to redact.",
                       refused=True)
    if rest is None:
        return Outcome("I can't reach Discord from here right now, so nothing was changed.",
                       refused=True)
    perms = caller_permissions
    if perms is None and not operator:
        perms = _perms_or_zero(rest, guild, channel, user)
    if not operator and not is_staff(int(perms or 0)):
        log.info("redact refused guild=%s channel=%s user=%s phrase=%s", guild, channel,
                 user, fp)
        return Outcome("Only server staff (Manage Messages or Manage Server) can redact "
                       "messages in this channel.", refused=True)
    if not bot_id:
        bot_id = str((rest.me() or {}).get("id") or "")
    span = clamp_days(days)
    s = scan(rest, guild, channel, phrase, bot_id, days=span, now_ms=now_ms)
    # The ASKER is staff (checked above). Editing the bot's OWN messages needs no
    # Discord permission at all, so it runs now; only deleting OTHER members' messages
    # is gated on the BOT holding Manage Messages, and only after the asker confirms.
    edited, edit_failed = edit_own(rest, channel, [h.id for h in s.own], phrase,
                                   sleep=sleep)
    log.info("redact guild=%s channel=%s user=%s phrase=%s scanned=%d own=%d edited=%d "
             "others=%d", guild, channel, user, fp, s.scanned, len(s.own), edited,
             len(s.others))
    lines = [f"Scanned {s.scanned} messages from the last {span} days"
             + (" (stopped at the 1000-message cap)" if s.truncated else "") + "."]
    lines.append(f"My own messages: edited {edited} to say {REPLACEMENT}"
                 + (f", {edit_failed} could not be edited" if edit_failed else "") + "."
                 if s.own else "None of my own messages contained it.")
    summary = (f"Scanned {s.scanned} messages ({span} days); edited {edited} of my own "
               f"messages to {REPLACEMENT}; {len(s.others)} by other members matched.")
    if not s.others:
        lines.append("No other member's message contained it.")
        return Outcome("\n".join(lines), summary=summary)
    bot_perms = _perms_or_zero(rest, guild, channel, bot_id)
    lines.append(f"{len(s.others)} message(s) by other members contain it "
                 "(I can't edit someone else's message; I can only delete it):")
    lines += preview_lines(s)
    if not bot_perms & MANAGE_MESSAGES:
        lines.append("I do NOT have Manage Messages in this channel, so I can't delete "
                     "these. Give my role Manage Messages here and run it again, or ask "
                     "the authors to edit them.")
        return Outcome("\n".join(lines), summary=summary, no_permission=True)
    lines.append("Press **Delete these** to delete them, or Cancel. Nothing is deleted "
                 "until you confirm.")
    pending = {"by": str(user), "guild": str(guild), "channel": str(channel),
               "ids": [h.id for h in s.others], "expires": time.time() + CONFIRM_TTL_S,
               "fp": fp}
    return Outcome("\n".join(lines), pending=pending, summary=summary)


def new_token() -> str:
    return secrets.token_urlsafe(8)


def confirm(rest, pending: dict | None, *, presser: str, operator: bool = False,
            presser_permissions: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> str:
    """The second confirmation: delete the staged messages, re-checking everything."""
    if not pending:
        return "That redaction has expired or was already done. Run it again."
    if time.time() > float(pending.get("expires") or 0):
        return "That redaction expired (15 minutes). Run it again."
    if str(presser) != str(pending.get("by")):
        return "Only the person who asked for this redaction can confirm it."
    guild, channel = str(pending.get("guild")), str(pending.get("channel"))
    if rest is None:
        return "I can't reach Discord from here right now, so nothing was deleted."
    perms = presser_permissions
    if perms is None and not operator:
        perms = _perms_or_zero(rest, guild, channel, presser)
    if not operator and not is_staff(int(perms or 0)):
        return "You no longer have Manage Messages or Manage Server there; nothing deleted."
    bot_id = str((rest.me() or {}).get("id") or "")
    if not _perms_or_zero(rest, guild, channel, bot_id) & MANAGE_MESSAGES:
        return "I no longer have Manage Messages in that channel; nothing was deleted."
    ids = [str(i) for i in pending.get("ids") or []][:MAX_MESSAGES]
    deleted, failed = delete_many(rest, channel, ids, sleep=sleep)
    log.info("redact confirm guild=%s channel=%s user=%s phrase=%s deleted=%d failed=%d",
             guild, channel, presser, pending.get("fp", ""), deleted, failed)
    return f"Deleted {deleted} message(s)" + (f"; {failed} could not be deleted" if failed
                                              else "") + "."
