"""The install links a server admin clicks, with the least permission that works.

Slash commands need NO channel permission at all: the `applications.commands`
scope is enough, and Discord delivers the interaction regardless. The bot
permissions below exist only for the two things that post outside an
interaction reply - `/share` and approval cards - and each bit is named so a
reviewer can see what is asked for and why.

A SECOND link exists for the setup flow: `manage_url` asks for what building a
starter layout needs (create channels and roles, set their overwrites, post a
welcome) and nothing that can take the server over. It is a SEPARATE OAuth
grant so a partner never has to give the chat bot those bits up front, and it
still refuses administrator, manage-server, moderation and mention-everyone.

THREE LINKS, ONE POLICY:

1. `install_url()` - chat only, ``MINIMAL_PERMISSIONS``. What the bot needs to
   answer. Safe to publish anywhere.
2. `setup_install_url()` - the elevated ``MANAGE_PERMISSIONS`` set with NO
   ``guild_id``, so Discord's own picker asks the person which server. This is
   the link the public site offers as "add it with setup powers": a manager who
   already knows they want the layout flow can grant it up front instead of
   doing a second OAuth round trip.
3. `manage_url()` - the SAME elevated set, but pinned to one ``guild_id`` with
   guild selection disabled. Only an in-Discord setup card can produce it,
   because only the interaction knows which server the person is in.

All three share `_authorize`, so there is exactly one place where a permission
bit is validated and exactly one place where the query string is built.
"""
from __future__ import annotations

from urllib.parse import urlencode

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
MANAGE_CHANNELS = 1 << 4
ADD_REACTIONS = 1 << 6
ATTACH_FILES = 1 << 15
CONNECT = 1 << 20
SPEAK = 1 << 21
CHANGE_NICKNAME = 1 << 26
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
MANAGE_EVENTS = 1 << 33
MANAGE_THREADS = 1 << 34
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38

#: The whole ask. Read: see the channel it posts into, post there, render an
#: embed, and read back the message it is replying to. No manage, no mention
#: everyone, no admin.
MINIMAL_PERMISSIONS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY

#: The elevated (but never admin) set the setup flow's second link asks for:
#: create and arrange the starter channels/roles, set their overwrites, and
#: speak in voice. No moderator powers and no server-wide management.
MANAGE_PERMISSIONS = MINIMAL_PERMISSIONS | MANAGE_CHANNELS | ADD_REACTIONS \
    | ATTACH_FILES | CONNECT | SPEAK | CHANGE_NICKNAME | MANAGE_ROLES \
    | MANAGE_WEBHOOKS | MANAGE_EVENTS | MANAGE_THREADS | CREATE_PUBLIC_THREADS \
    | SEND_MESSAGES_IN_THREADS

#: Bits an install link must never request. Any of these turns a chat bot into
#: something that can take a server over if its token leaks.
FORBIDDEN_PERMISSIONS = {
    "ADMINISTRATOR": 1 << 3,
    "MANAGE_GUILD": 1 << 5,
    "MANAGE_ROLES": 1 << 28,
    "MANAGE_WEBHOOKS": 1 << 29,
    "BAN_MEMBERS": 1 << 2,
    "KICK_MEMBERS": 1 << 1,
    "MENTION_EVERYONE": 1 << 17,
}

#: Bits the elevated setup link refuses on top of MINIMAL_PERMISSIONS. Note the
#: deliberately absent MANAGE_ROLES/MANAGE_WEBHOOKS: layout building needs them,
#: and neither can remove a role above the bot or bypass channel permissions.
MANAGE_FORBIDDEN = {
    "ADMINISTRATOR": 1 << 3,
    "MANAGE_GUILD": 1 << 5,
    "BAN_MEMBERS": 1 << 2,
    "KICK_MEMBERS": 1 << 1,
    "MENTION_EVERYONE": 1 << 17,
    "MODERATE_MEMBERS": 1 << 40,
}

SCOPES = ("bot", "applications.commands")


def _authorize(app_id: str | int, permissions: int, *,
               guild_id: str | int | None = None,
               forbidden: dict[str, int] | None = None,
               what: str = "install") -> str:
    """Build an authorize URL, refusing every bit in ``forbidden``.

    The single builder behind all three public links: app id and permission
    validation happen here and nowhere else. ``guild_id`` pins the link to one
    server (with guild selection disabled); ``None`` leaves Discord's own
    server picker in charge.
    """
    app = str(app_id).strip()
    if not app.isdigit():
        raise ValueError("app_id must be the numeric application id")
    blocked = FORBIDDEN_PERMISSIONS if forbidden is None else forbidden
    bad = [name for name, bit in blocked.items() if permissions & bit]
    if bad:
        raise ValueError(f"{what} link refuses dangerous permissions: "
                         + ", ".join(sorted(bad)))
    query = {
        "client_id": app,
        "scope": " ".join(SCOPES),
        "permissions": str(int(permissions)),
    }
    if guild_id is not None:
        guild = str(guild_id).strip()
        if not guild.isdigit():
            raise ValueError("guild_id must be the numeric server id")
        query["guild_id"] = guild
        query["disable_guild_select"] = "true"
    return "https://discord.com/oauth2/authorize?" + urlencode(query)


def install_url(app_id: str | int, permissions: int = MINIMAL_PERMISSIONS,
                guild_id: str | int | None = None) -> str:
    """The OAuth2 authorize URL that adds the bot to a server.

    Raises ValueError for a non-numeric app id or a dangerous permission bit,
    so a typo cannot ship an install link that asks for administrator.
    """
    return _authorize(app_id, permissions, guild_id=guild_id,
                      forbidden=FORBIDDEN_PERMISSIONS, what="install")


def setup_install_url(app_id: str | int,
                      permissions: int = MANAGE_PERMISSIONS) -> str:
    """The elevated install link a public page can offer: any server, no pin.

    Same permission set as `manage_url`, but with NO ``guild_id``, so the link
    works for whoever clicks it and Discord asks them which server to add the
    bot to. That is what makes it safe to publish on a site: the builder cannot
    know the visitor's servers, and a link that guessed would land on the wrong
    one. Validates against `MANAGE_FORBIDDEN`, so it can never ask for
    administrator, manage-server, moderation or mention-everyone.
    """
    return _authorize(app_id, permissions, guild_id=None,
                      forbidden=MANAGE_FORBIDDEN, what="setup")


def manage_url(app_id: str | int, guild_id: str | int,
               permissions: int = MANAGE_PERMISSIONS) -> str:
    """The SEPARATE OAuth link that grants the elevated layout-building set.

    ``guild_id`` is required and must be numeric, and guild selection is
    disabled: this link can only ever land on the server the setup card named,
    never on whichever server the person happens to be an admin of. Raises
    ValueError for a dangerous bit so a typo cannot ship an admin grant.
    """
    guild = str(guild_id).strip()
    if not guild.isdigit():
        raise ValueError("guild_id must be the numeric server id")
    return _authorize(app_id, permissions, guild_id=guild,
                      forbidden=MANAGE_FORBIDDEN, what="manage")
