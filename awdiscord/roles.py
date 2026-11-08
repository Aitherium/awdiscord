"""Hand a member a role — but only a role an admin marked self-assignable.

A self-assignable role is the one place a member changes their own permissions, so
every rule here is a refusal, in the order that matters:

1. **The allowlist is the gate.** A role id that is not in the server's
   self-assignable list is refused before its permission bits are even read. An
   allowlist that named a role by NAME would let a rename quietly leave the door
   open, so the caller resolves names to ids first and only ids are compared.
2. **Privileged bits are refused at ASSIGN time, not at allowlist time.** The
   server may have been laid out months ago and the role edited since; the bits
   are read from the live role immediately before the write. A permission string
   that will not parse is refused, never treated as zero.
3. **The bot must be able to do it.** Discord answers 403 and explains nothing,
   so a missing Manage Roles bit and a hierarchy problem (the role sits at or
   above the bot's highest role) are detected up front and said in words.
4. **A managed role is never assigned.** Bot, integration and booster roles are
   created by Discord; putting one on a member by hand is a permission grant to
   somebody else's integration.

The REST call is small on purpose: this module decides, ``rest.py`` sends.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

from . import guildspec, install

MANAGE_ROLES = 1 << 28
ADMINISTRATOR = 1 << 3

#: A role carrying any of these reaches beyond the channel it was made for. The
#: union of the three existing lists keeps one definition from drifting from the
#: others: ``guildspec`` refuses them in a layout, ``install`` refuses them in an
#: install link — and ``MANAGE_FORBIDDEN`` adds MODERATE_MEMBERS, which neither of
#: the other two covers.
PRIVILEGED_BITS: dict[str, int] = dict(guildspec.DANGEROUS_ROLE_BITS)
PRIVILEGED_BITS.update(install.FORBIDDEN_PERMISSIONS)
PRIVILEGED_BITS.update(install.MANAGE_FORBIDDEN)
#: The three lists above guard install links and layouts, not self-service. A role
#: an admin allowlisted while harmless and later edited to manage channels,
#: messages, nicknames, threads, events, expressions or voice members must still be
#: refused at assign time, so those server-wide bits are privileged here too.
PRIVILEGED_BITS.update({
    "MANAGE_CHANNELS": 1 << 4,
    "VIEW_AUDIT_LOG": 1 << 7,
    "MANAGE_MESSAGES": 1 << 13,
    "MUTE_MEMBERS": 1 << 22,
    "DEAFEN_MEMBERS": 1 << 23,
    "MOVE_MEMBERS": 1 << 24,
    "MANAGE_NICKNAMES": 1 << 27,
    "MANAGE_GUILD_EXPRESSIONS": 1 << 30,
    "MANAGE_EVENTS": 1 << 33,
    "MANAGE_THREADS": 1 << 34,
})

#: Managed/integration roles are Discord's to hand out, never ours.
_SKIP_TAGS = ("bot_id", "integration_id", "premium_subscriber")

#: Discord caps a member's role count; refusing early reads better than a 400.
MAX_ROLES = 250


class RoleError(RuntimeError):
    """A refusal whose message is safe to show the member who asked."""


def bits(value: Any) -> Optional[int]:
    """Permission bits from Discord's decimal string; ``None`` when unreadable.

    Public so a caller building a permission union does not import the private
    parser and accidentally get a different answer.

    An ABSENT value is unreadable, not zero: ``guildspec._bits`` defaults ``None``
    to ``0`` because a layout entry may legitimately say nothing, but here the
    question is "does this role carry a privileged bit" and the answer we do not
    have is not "no".
    """
    if value is None:
        return None
    return guildspec._bits(value)  # noqa: SLF001 - the one parser in the brick


def privileged_names(permissions: Any) -> List[str]:
    """The privileged bits this permission value carries.

    Unparseable is ``["UNREADABLE_PERMISSIONS"]`` — a role whose bits cannot be
    read is treated as privileged, because the answer we do not have is not "no".
    """
    value = bits(permissions)
    if value is None:
        return ["UNREADABLE_PERMISSIONS"]
    return sorted(name for name, bit in PRIVILEGED_BITS.items() if value & bit)


def _managed(role: dict) -> bool:
    tags = role.get("tags") or {}
    return bool(role.get("managed")) or any(k in tags for k in _SKIP_TAGS)


def denial(role: Any, allowed_role_ids: Iterable[str]) -> str:
    """Why this role may not be self-assigned, or "" when it may.

    FAIL CLOSED at every arm: a role that is not a dict, has no id, is not on the
    allowlist, is managed, or carries a privileged bit is refused.
    """
    if not isinstance(role, dict):
        return "that is not a role in this server"
    rid = str(role.get("id") or "")
    if not rid:
        return "that role has no id"
    allowed = {str(x) for x in allowed_role_ids or () if str(x).strip()}
    if rid not in allowed:
        return "that role is not one members may give themselves"
    if _managed(role):
        return "that role belongs to an integration, not to members"
    bad = privileged_names(role.get("permissions"))
    if bad:
        return "that role carries %s, so only a server manager can hand it out" % ", ".join(bad)
    return ""


def parse_limit(value: Any, default: int = 3) -> int:
    """How many self-assigned roles a member may hold at once, bounded."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, MAX_ROLES))


def bot_ceiling_problem(role: dict, bot_top_position: int, bot_bits: Any,
                        *, bot_role_id: str = "") -> str:
    """Why the BOT cannot edit this role, or "" when it can.

    Discord needs Manage Roles and a highest role strictly ABOVE the role being
    handed out. @everyone sits at position 0 and can never be assigned.
    """
    value = bits(bot_bits)
    if value is None:
        return "I cannot read my own permissions in this server"
    if not value & MANAGE_ROLES:
        return ("I need the **Manage Roles** permission to do that — a server manager "
                "can re-run the setup link from `/aitherium setup`")
    if str(role.get("id") or "") == str(bot_role_id or ""):
        return "that is my own role"
    if int(role.get("position") or 0) >= int(bot_top_position):
        return ("that role sits at or above my highest role, so Discord will not let me "
                "assign it — move my role higher in Server Settings > Roles")
    return ""


def resolve_role(roles: Sequence[dict], name: str) -> Optional[dict]:
    """The one role whose name matches ``name`` (case-insensitive), else None.

    A name matching two roles resolves to NEITHER: picking one arbitrarily is how
    a member ends up with the wrong permissions by typo.
    """
    want = str(name or "").strip().casefold()
    if not want:
        return None
    hits = [r for r in roles
            if isinstance(r, dict) and str(r.get("name") or "").strip().casefold() == want]
    return hits[0] if len(hits) == 1 else None


def assignable(roles: Sequence[dict], allowed_role_ids: Iterable[str]) -> List[dict]:
    """The roles a member may currently give themselves (for the picker)."""
    return [r for r in roles
            if isinstance(r, dict) and not denial(r, allowed_role_ids)]


def assign(rest, guild: str, user_id: str, role_id: str) -> None:
    """PUT the role onto the member. Raises :class:`RoleError` with a real reason."""
    _write(rest, "PUT", guild, user_id, role_id, "add")


def remove(rest, guild: str, user_id: str, role_id: str) -> None:
    """DELETE the role from the member. Idempotent on Discord's side."""
    _write(rest, "DELETE", guild, user_id, role_id, "remove")


def _write(rest, method: str, guild: str, user_id: str, role_id: str, verb: str) -> None:
    from .rest import RestError

    if rest is None:
        raise RoleError("this bot cannot reach Discord right now")
    path = "/guilds/%s/members/%s/roles/%s" % (guild, user_id, role_id)
    try:
        rest.request(method, path)
    except RestError as exc:
        if exc.status == 403:
            raise RoleError(
                "Discord refused: I need **Manage Roles** and a role above that one. "
                "A server manager can fix both in Server Settings > Roles.") from exc
        if exc.status == 404:
            raise RoleError("that member or role no longer exists in this server") from exc
        if exc.status == 429:
            raise RoleError("Discord is rate-limiting me; try again in a moment") from exc
        raise RoleError("Discord refused the change (%s)" % exc.status) from exc
    except (OSError, ValueError) as exc:
        raise RoleError("I could not reach Discord to %s that role" % verb) from exc
