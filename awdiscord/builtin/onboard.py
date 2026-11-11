"""/signup: link this Discord account to a new Aitherium account, after the terms.

NAMES ARE TAKEN, SO THIS IS ``link-onboard`` / ``/signup``. The plugin name ``onboard``
and the slash command ``/onboard`` belong to the Collective onboarding pack
(``Library/packs/aitherium_collective_onboard``: join DM, signup button, newsletter
modal). ``discover()`` loads built-ins first and silently drops a later plugin whose name
is taken, and a duplicate command name refuses the whole pack -- either would make that
deployed pack vanish. ``/link`` (an EXISTING account) belongs to
``aitherium_collective_core``; ``/join`` to the voice transport; ``/enlist`` to the
member-number flow in ``builtin.join``. ``dev/tests/test_discord_pack_names.py`` asserts
no real pack is shadowed. Unlike the pack's signup button, this path shows the terms and
privacy policy and needs an explicit accept before any link is minted.

The flow, three steps, every one private (ephemeral) to the member:

  1. ``/signup`` shows the terms of use and privacy policy links and says plainly what
     linking does, with an **I accept** button. Nothing is minted yet.
  2. The button opens a modal; the member types ``I ACCEPT``. Anything else links
     nothing.
  3. On that submit the bot asks Genesis ``POST /v1/workspace/discord-collective/
     signup-link`` (platform caller: ``X-Internal-Key`` + ``X-Caller-Type: platform``)
     for a signed, single-use, one-hour link and replies with it.

THE DISCORD ID IS THE ONE DISCORD SIGNED: ``ctx.user`` from the interaction, never a
typed modal field. The bot never links anything itself: the link is made only when the
member finishes sign-in with that URL, by Genesis ``redeem_signup_link`` (fresh account,
jti consumed once, a Discord id on another account refused). The one welcome grant is
recorded there too (``lib/economy/OnboardGrant.py``), once per account and once per
Discord user. Aitherium tokens have no cash value and cannot be spent yet.

FAIL CLOSED: no ``AITHER_INTERNAL_SECRET``, a non-https Genesis URL, or a Genesis
refusal means no link, and the member is told so -- never an unsigned URL.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable

from awdiscord.plugins import Command, Plugin

log = logging.getLogger("awdiscord.link_onboard")

NAME = "link-onboard"
COMMAND = "signup"
DEFAULT_GENESIS_URL = "https://aitheros-genesis:8001"
SIGNUP_LINK_PATH = "/v1/workspace/discord-collective/signup-link"
ACCEPT_PHRASE = "I ACCEPT"
TIMEOUT_S = 10.0

# The terms text is owned by awdiscord.terms when that module ships (the member terms
# lane); until then the same two published pages are linked here.
try:
    from awdiscord.terms import PRIVACY_URL, TERMS_URL  # type: ignore[import-not-found]
except ImportError:
    PRIVACY_URL = "https://aitherium.com/privacy"
    TERMS_URL = "https://aitherium.com/terms"

#: Said next to every mention of the grant. Mirrors lib/economy/OnboardGrant.MEMBER_WORDING
#: (this package cannot import lib/); test_onboard pins the two together.
TOKEN_WORDING = ("Aitherium tokens have no cash value and cannot be spent yet. "
                 "They cannot be transferred, traded or cashed out.")

#: OWNER-EDITABLE WORDING. Every sentence must stay true of Genesis redeem_signup_link.
NOTICE_TEXT = (
    "**Link your Discord account to Aitherium**\n"
    "Before linking, please read and accept:\n"
    f"- Terms of use: <{TERMS_URL}>\n"
    f"- Privacy policy: <{PRIVACY_URL}>\n"
    "\n"
    "What linking does:\n"
    "- I ask Aitherium for a sign-in link made for your Discord account only. It works "
    "once and expires in one hour.\n"
    "- When you create your Aitherium account with that link, your Discord user id is "
    "saved on that account.\n"
    "- The link only connects an account created in the last 15 minutes.\n"
    "- Your first link can record a one-time welcome grant of Aitherium tokens. "
    + TOKEN_WORDING + "\n"
    "\n"
    "Press **I accept** to continue."
)

OPENER: list = [None]         # tests swap in a fake transport


class OnboardError(Exception):
    """No link could be minted; the message is shown to the member."""


def genesis_url() -> str:
    """Genesis base URL (``AWDISCORD_GENESIS_URL``), https only."""
    url = (os.getenv("AWDISCORD_GENESIS_URL") or DEFAULT_GENESIS_URL).strip().rstrip("/")
    if not url.startswith("https://"):
        raise OnboardError("account linking is not configured on this bot (Genesis URL "
                           "must be https)")
    return url


def _tls() -> ssl.SSLContext | None:
    ca = os.getenv("AWDISCORD_CA_BUNDLE") or ""
    return ssl.create_default_context(cafile=ca) if ca else None


def mint_link(discord_user_id: str) -> dict:
    """Ask Genesis for this member's signup link. Raises OnboardError.

    Args:
        discord_user_id: the id Discord signed on the interaction (``ctx.user``).

    Returns:
        Genesis's answer: ``{"url", "token", "expires_in_seconds"}``.
    """
    if not str(discord_user_id or "").isdigit():
        raise OnboardError("could not read your Discord id from this interaction")
    key = (os.getenv("AITHER_INTERNAL_SECRET") or "").strip()
    if not key:
        raise OnboardError("account linking is not configured on this bot (no service "
                           "identity)")
    req = urllib.request.Request(
        genesis_url() + SIGNUP_LINK_PATH,
        data=json.dumps({"discord_user_id": str(discord_user_id)}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Caller-Type": "platform",
                 "X-Internal-Key": key, "X-Service-Name": "awdiscord"})
    opener: Callable[..., Any] = OPENER[0] or urllib.request.urlopen
    try:
        with opener(req, timeout=TIMEOUT_S, context=_tls()) as resp:
            body = json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        log.warning("onboard: signup-link refused (HTTP %s)", exc.code)
        raise OnboardError(f"Aitherium could not make a link right now (HTTP {exc.code})"
                           ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OnboardError("Aitherium is unreachable right now") from exc
    except ValueError as exc:
        raise OnboardError("Aitherium returned something unreadable") from exc
    url = str((body or {}).get("url") or "") if isinstance(body, dict) else ""
    if not url.startswith("https://"):
        raise OnboardError("Aitherium returned no usable link")
    return body


def link_text(body: dict) -> str:
    """The private reply that carries the link."""
    minutes = max(1, int(body.get("expires_in_seconds") or 3600) // 60)
    return ("**Your sign-in link** (works once, expires in "
            f"{minutes} minutes; do not share it):\n<{body['url']}>\n\n"
            "Create your Aitherium account with it to finish linking.\n" + TOKEN_WORDING)


# ── handlers ─────────────────────────────────────────────────────────────────

def _onboard(ctx):
    return ctx.reply(NOTICE_TEXT, private=True, components=[{"type": 1, "components": [
        {"type": 2, "style": 3, "label": "I accept", "custom_id": PLUGIN.button_id("accept")}]}])


def _on_button(ctx, payload: str):
    if payload != "accept":
        return ctx.reply("Unknown button.", private=True)
    return ctx.modal("accept", "Accept the terms to link", [
        {"id": "confirm", "label": f"Type {ACCEPT_PHRASE} to agree to the terms",
         "placeholder": ACCEPT_PHRASE, "max_length": 20}])


def _on_modal(ctx, payload: str, values: dict):
    if payload != "accept":
        return ctx.reply("That form is no longer handled.", private=True)
    if " ".join(str(values.get("confirm") or "").split()).upper() != ACCEPT_PHRASE:
        return ctx.reply(f"Nothing was linked: type {ACCEPT_PHRASE} to accept the terms "
                         f"(<{TERMS_URL}>, <{PRIVACY_URL}>). Run /{COMMAND} to try again.",
                         private=True)
    user = ctx.user   # Discord-signed; a typed field is never an identity

    def work() -> str:
        try:
            return link_text(mint_link(user))
        except OnboardError as exc:
            return f"No link was made: {exc}."
    return ctx.defer(work, private=True)


PLUGIN = Plugin(
    name=NAME,
    version="0.1.0",
    description="Link your Discord account to an Aitherium account, after the terms",
    default_enabled=False,
    on_button=_on_button,
    on_modal=_on_modal,
    commands=[Command(COMMAND, "Link Discord to a new Aitherium account (terms first)",
                      _onboard)],
)
