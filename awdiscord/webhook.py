"""Post to a Discord channel webhook. Mentions are off unless you ask for them."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

ALLOWED_HOSTS = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
USER_AGENT = "DiscordBot (https://github.com/Aitherium/awdiscord, 0.1)"


class WebhookError(Exception):
    pass


def post_webhook(url: str, content: str, username: str | None = None,
                 allow_mentions: bool = False, timeout: float = 15.0) -> dict:
    """Send one message. Returns Discord's message object (wait=true)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS \
            or not parsed.path.startswith("/api/webhooks/"):
        raise WebhookError("not a Discord webhook URL")
    if not content or len(content) > 2000:
        raise WebhookError("content must be 1..2000 characters")
    body = {"content": content,
            "allowed_mentions": {"parse": ["users"] if allow_mentions else []}}
    if username:
        body["username"] = username[:80]
    sep = "&" if parsed.query else "?"
    req = urllib.request.Request(url + sep + "wait=true", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise WebhookError(f"Discord answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WebhookError("Discord is unreachable") from exc
