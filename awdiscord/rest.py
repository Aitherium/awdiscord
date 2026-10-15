"""A small Discord REST client. Always `Authorization: Bot <token>`."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from awdiscord.guard import check_bot_token, require_bot_identity

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/Aitherium/awdiscord, 0.2)"


class RestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Rest:
    def __init__(self, token: str, timeout: float = 20.0, opener=None):
        self._token = check_bot_token(token)
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen

    def request(self, method: str, path: str, body=None, auth: bool = True):
        headers = {"User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = "Bot " + self._token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        for attempt in range(3):
            req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
            try:
                with self._open(req, timeout=self._timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")[:300]
                if exc.code == 429 and attempt < 2:
                    try:
                        wait = float(json.loads(text).get("retry_after", 1.0))
                    except (ValueError, AttributeError):
                        wait = 1.0
                    time.sleep(min(wait, 10.0))
                    continue
                raise RestError(exc.code, text) from exc
        raise RestError(429, "rate limited")

    # -- the calls this package makes ------------------------------------------
    def me(self) -> dict:
        """Who the token belongs to; refuses anything that is not a bot."""
        return require_bot_identity(self.request("GET", "/users/@me"))

    def application(self) -> dict:
        return self.request("GET", "/applications/@me")

    def put_commands(self, app_id: str, commands: list[dict], guild_id: str | None = None):
        path = f"/applications/{app_id}/commands"
        if guild_id:
            path = f"/applications/{app_id}/guilds/{guild_id}/commands"
        return self.request("PUT", path, commands)

    def guilds(self) -> list[dict]:
        return self.request("GET", "/users/@me/guilds")

    def create_message(self, channel_id: str, payload: dict) -> dict:
        payload = dict(payload)
        payload.setdefault("allowed_mentions", {"parse": []})
        return self.request("POST", f"/channels/{channel_id}/messages", payload)

    def dm_channel(self, user_id: str) -> dict:
        return self.request("POST", "/users/@me/channels", {"recipient_id": str(user_id)})

    def interaction_callback(self, interaction_id: str, token: str, payload: dict):
        return self.request("POST", f"/interactions/{interaction_id}/{token}/callback",
                            payload, auth=False)

    def edit_original(self, app_id: str, token: str, payload: dict):
        return self.request("PATCH", f"/webhooks/{app_id}/{token}/messages/@original",
                            payload, auth=False)
