"""A small Discord REST client. Always `Authorization: Bot <token>`."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

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

    # -- channel webhooks (Manage Webhooks): post as another name and avatar ------
    def channel_webhooks(self, channel_id: str) -> list[dict]:
        return self.request("GET", f"/channels/{channel_id}/webhooks") or []

    def create_webhook(self, channel_id: str, name: str) -> dict:
        return self.request("POST", f"/channels/{channel_id}/webhooks", {"name": name[:80]})

    def execute_webhook(self, webhook_id: str, token: str, payload: dict,
                        wait: bool = True) -> dict | None:
        """Post through a webhook. Mentions are always off (``parse: []``)."""
        body = dict(payload)
        body["allowed_mentions"] = {"parse": []}
        path = f"/webhooks/{webhook_id}/{token}" + ("?wait=true" if wait else "")
        return self.request("POST", path, body, auth=False)

    def upload(self, channel_id: str, filename: str, data: bytes,
               content_type: str = "application/octet-stream", content: str = "") -> dict:
        """POST one file to a channel (multipart), with ``content`` as its message.

        This is how a generated image, a spoken answer or a document comes back
        out of the bot: nothing in Discord's JSON API carries bytes.
        """
        boundary = "awdiscord" + uuid.uuid4().hex
        safe = "".join(c for c in str(filename or "") if c.isalnum() or c in "._-")
        payload = json.dumps({"content": (content or "")[:2000],
                              "allowed_mentions": {"parse": []}})
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{payload}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
            f"filename=\"{safe[:80] or 'file.bin'}\"\r\n"
            f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n".encode(),
            data, f"\r\n--{boundary}--\r\n".encode()])
        req = urllib.request.Request(
            API + f"/channels/{channel_id}/messages", data=body, method="POST",
            headers={"Authorization": "Bot " + self._token, "User-Agent": USER_AGENT,
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RestError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from exc

    def request_files(self, method: str, path: str, payload: dict,
                      files: list[tuple[str, bytes, str]]) -> dict:
        """Any JSON call that also carries files (``payload_json`` + ``files[i]``).

        ``files`` = ``[(filename, data, content_type)]``; the payload refers to them as
        ``attachment://<filename>`` (an embed image, say) and lists them under
        ``attachments``. A forum post with an attached image is the one caller.
        """
        boundary = "awdiscord" + uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                 f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n".encode()]
        for i, (filename, data, ctype) in enumerate(files):
            safe = "".join(c for c in str(filename or "") if c.isalnum() or c in "._-")
            parts += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{i}]\"; "
                      f"filename=\"{safe[:80] or 'file.bin'}\"\r\n"
                      f"Content-Type: {ctype or 'application/octet-stream'}\r\n\r\n".encode(),
                      data, b"\r\n"]
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            API + path, data=b"".join(parts), method=method,
            headers={"Authorization": "Bot " + self._token, "User-Agent": USER_AGENT,
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RestError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from exc

    def followup(self, app_id: str, token: str, payload: dict):
        """One more message on an interaction (the 2nd+ part of a long answer)."""
        return self.request("POST", f"/webhooks/{app_id}/{token}", payload, auth=False)

    def edit_original(self, app_id: str, token: str, payload: dict):
        return self.request("PATCH", f"/webhooks/{app_id}/{token}/messages/@original",
                            payload, auth=False)
