"""Bot tokens only.

A Discord USER token driving an account ("self-bot") is against Discord's
terms of service and gets the account banned. This package never sends one:
every request carries `Authorization: Bot <token>`, and the runtime asks
Discord who the token belongs to before it does anything else, refusing to
start unless the answer is a bot.
"""
from __future__ import annotations

import re


class TokenRefusedError(ValueError):
    """The credential is not a bot token, or is shaped like a user token."""


# Legacy user tokens with 2FA carry this prefix; no bot token ever does.
_USER_MFA = re.compile(r"^mfa\.")
# A bot token is three dot-separated base64url segments.
_BOT_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{20,}$")


def check_bot_token(token: str | None) -> str:
    """Return the bare token, or raise TokenRefusedError. Never echoes the value."""
    if not token or not token.strip():
        raise TokenRefusedError("no bot token set (DISCORD_BOT_TOKEN)")
    tok = token.strip()
    lowered = tok.lower()
    if lowered.startswith("bot "):
        tok = tok[4:].strip()
    elif lowered.startswith("bearer "):
        raise TokenRefusedError("a Bearer token is an OAuth user grant, not a bot token")
    if _USER_MFA.match(tok):
        raise TokenRefusedError("this is a user token (mfa.*); self-bots are refused")
    if not _BOT_SHAPE.match(tok):
        raise TokenRefusedError("token is not shaped like a Discord bot token")
    return tok


def require_bot_identity(me: dict) -> dict:
    """Judge GET /users/@me. Anything but a bot account is refused."""
    if not isinstance(me, dict) or not me.get("id"):
        raise TokenRefusedError("Discord did not say who this token belongs to")
    if me.get("bot") is not True:
        raise TokenRefusedError("token belongs to a user account, not a bot; self-bots are refused")
    return me
