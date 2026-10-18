"""awdiscord - put any agent on Discord, as a real bot, with an operator in the loop.

    from awdiscord import Bot, OpenAICompatAgent

    agent = OpenAICompatAgent("http://127.0.0.1:8080/v1", model="my-agent")
    Bot.from_env(agent=agent).run()          # /ask, /share, /status

The pieces are usable alone:

    install_url(app_id)                      one-click install link, minimal permissions
    RateLimiter                              per-user / per-server / global token buckets
    ApprovalDesk                             signed Approve/Deny cards only operators can press
    post_webhook(url, "text")                post to a channel webhook, mentions disabled
    Dispatcher                               interaction JSON in, response JSON out

Bot tokens only. A user token ("self-bot") is refused at startup: it is against
Discord's terms and it is how accounts get banned.
"""
from __future__ import annotations

from awdiscord.agent import (
    AgentError,
    CallableAgent,
    EchoAgent,
    OpenAICompatAgent,
    load_personas,
)
from awdiscord.approvals import ApprovalDesk, ApprovalError, Decision
from awdiscord.dispatch import Dispatcher
from awdiscord.guard import TokenRefusedError, check_bot_token
from awdiscord.install import MINIMAL_PERMISSIONS, install_url
from awdiscord.ratelimit import RateLimiter
from awdiscord.webhook import WebhookError, post_webhook

__version__ = "0.3.0"

__all__ = [
    "AgentError",
    "ApprovalDesk",
    "ApprovalError",
    "CallableAgent",
    "Decision",
    "Dispatcher",
    "EchoAgent",
    "MINIMAL_PERMISSIONS",
    "OpenAICompatAgent",
    "RateLimiter",
    "TokenRefusedError",
    "WebhookError",
    "check_bot_token",
    "install_url",
    "load_personas",
    "post_webhook",
    "Bot",
]


def __getattr__(name):
    # The gateway needs discord.py; import it lazily so every other piece of
    # this package works with zero dependencies.
    if name == "Bot":
        from awdiscord.gateway import Bot
        return Bot
    raise AttributeError(name)
