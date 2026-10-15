"""The install link a server admin clicks, with the least permission that works.

Slash commands need NO channel permission at all: the `applications.commands`
scope is enough, and Discord delivers the interaction regardless. The bot
permissions below exist only for the two things that post outside an
interaction reply - `/share` and approval cards - and each bit is named so a
reviewer can see what is asked for and why.
"""
from __future__ import annotations

from urllib.parse import urlencode

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16

#: The whole ask. Read: see the channel it posts into, post there, render an
#: embed, and read back the message it is replying to. No manage, no mention
#: everyone, no admin.
MINIMAL_PERMISSIONS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY

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

SCOPES = ("bot", "applications.commands")


def install_url(app_id: str | int, permissions: int = MINIMAL_PERMISSIONS,
                guild_id: str | int | None = None) -> str:
    """The OAuth2 authorize URL that adds the bot to a server.

    Raises ValueError for a non-numeric app id or a dangerous permission bit,
    so a typo cannot ship an install link that asks for administrator.
    """
    app = str(app_id).strip()
    if not app.isdigit():
        raise ValueError("app_id must be the numeric application id")
    bad = [name for name, bit in FORBIDDEN_PERMISSIONS.items() if permissions & bit]
    if bad:
        raise ValueError("install link refuses dangerous permissions: " + ", ".join(sorted(bad)))
    query = {
        "client_id": app,
        "scope": " ".join(SCOPES),
        "permissions": str(int(permissions)),
    }
    if guild_id is not None:
        query["guild_id"] = str(guild_id)
        query["disable_guild_select"] = "true"
    return "https://discord.com/oauth2/authorize?" + urlencode(query)
