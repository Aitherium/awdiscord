"""The runtime: a real bot on the Discord gateway, plus a local inbox.

    pip install "awdiscord[gateway]"
    DISCORD_BOT_TOKEN=... AWDISCORD_AGENT_URL=http://127.0.0.1:8080/v1 awdiscord run

discord.py holds the gateway connection only. Every interaction is turned into
the plain dict Discord sent and answered through `Dispatcher` + raw REST, so
the bot behaves the same whichever transport delivers the interaction.

The inbox (127.0.0.1 only, bearer-gated) is how your own processes reach the
bot without holding its token:

    POST /approvals {"title", "body", "callback_url"?, "channel_id"?}
    POST /post      {"channel_id", "content"}
    GET  /health
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from awdiscord.agent import OpenAICompatAgent
from awdiscord.approvals import ApprovalDesk
from awdiscord.dispatch import Dispatcher, commands
from awdiscord.guard import TokenRefusedError
from awdiscord.ratelimit import RateLimiter
from awdiscord.rest import Rest, RestError


def _log(**fields) -> None:
    fields.setdefault("ts", round(time.time(), 3))
    print(json.dumps(fields, sort_keys=True), flush=True)


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip()
    return default


def _flat_text(message) -> str:
    """A message's text plus its embeds (cards often carry their id in one)."""
    parts = [message.content or ""]
    for e in getattr(message, "embeds", None) or []:
        parts += [getattr(e, "title", None) or "", getattr(e, "description", None) or ""]
        footer = getattr(e, "footer", None)
        parts.append(getattr(footer, "text", None) or "")
        parts += [f"{f.name} {f.value}" for f in getattr(e, "fields", None) or []]
    return "\n".join(p for p in parts if p)


def _raw_text(m: dict) -> str:
    """A REST message dict's text plus its embeds: the dict form of ``_flat_text``."""
    return "\n".join([m.get("content") or ""] + [
        "\n".join(str(x or "") for x in (e.get("title"), e.get("description"),
                                          (e.get("footer") or {}).get("text")))
        for e in m.get("embeds") or []])


def _attachment(a) -> dict:
    """A discord.py Attachment as the dict Discord's API sends."""
    return {"id": str(a.id), "filename": a.filename, "url": a.url,
            "proxy_url": getattr(a, "proxy_url", "") or "",
            "content_type": a.content_type or "", "size": a.size}


def _reference(message, me_id) -> tuple[str, str]:
    """(replied-to message id, its text when the gateway resolved it to OUR message)."""
    ref = getattr(message, "reference", None)
    mid = getattr(ref, "message_id", None)
    if ref is None or mid is None:
        return "", ""
    resolved = getattr(ref, "resolved", None)
    author = getattr(resolved, "author", None)
    if author is not None and getattr(author, "id", None) == me_id:
        return str(mid), _flat_text(resolved)
    return str(mid), ""


class Bot:
    def __init__(self, token: str, agent, app_id: str | None = None,
                 desk: ApprovalDesk | None = None, limiter: RateLimiter | None = None,
                 approval_channel: str | None = None, dev_guild: str | None = None,
                 inbox_port: int = 0, inbox_token: str = "", inbox_host: str = "127.0.0.1",
                 host=None, forwarder=None, owner_agent=None, owners=(),
                 internal_key: str = ""):
        from awdiscord import __version__
        self.rest = Rest(token)
        # Owners get /notify/owner DMs. A fleet service may authenticate that one
        # route with the shared internal key (X-Internal-Key) instead of the
        # inbox bearer, so a caller needs no extra secret just to page the owner.
        self.owners = [str(o) for o in owners]
        self._internal_key = internal_key
        self._token = token
        self.agent = agent
        self.app_id = app_id
        self.desk = desk
        self.approval_channel = approval_channel
        self.dev_guild = dev_guild
        self.host = host            # awdiscord.plugins.PluginHost, or None
        self.forwarder = forwarder  # awdiscord.forward.DmForwarder, or None
        self.dispatcher = Dispatcher(agent, limiter or RateLimiter(), desk,
                                     version=__version__, host=host,
                                     owner_agent=owner_agent, owners=owners)
        if forwarder is not None and getattr(forwarder, "_lookup", 0) is None:
            forwarder._lookup = self.recent_card_text
        self.inbox_port = inbox_port
        self.inbox_host = inbox_host
        self._inbox_token = inbox_token
        self.state = {"connected": False, "user": None, "guilds": 0, "since": None,
                      "interactions": 0, "version": __version__}

    @classmethod
    def from_env(cls, agent=None) -> "Bot":
        token = _env("AWDISCORD_TOKEN", "DISCORD_BOT_TOKEN")
        agent = agent or OpenAICompatAgent.from_env()
        operators = [o for o in _env("AWDISCORD_OPERATORS").replace(" ", "").split(",") if o]
        secret = _env("AWDISCORD_APPROVAL_SECRET")
        state = Path(_env("AWDISCORD_STATE_DIR", default=str(Path.home() / ".awdiscord")))
        desk = None
        if operators and secret:
            desk = ApprovalDesk(state / "approvals", secret, operators)
        host = None
        if _env("AWDISCORD_NO_PLUGINS") not in ("1", "true", "yes"):
            from awdiscord.plugins import build_host
            from awdiscord.store import GuildStore
            only = [p for p in _env("AWDISCORD_PLUGINS").replace(" ", "").split(",") if p]
            host = build_host(GuildStore(state / "guilds.db"), agent=agent,
                              operators=set(operators), only=only or None)
        # Owners = who may answer decision cards and who talks to the fleet
        # agent. Defaults to the operators; the card receiver re-checks anyway.
        owners = ([o for o in _env("AWDISCORD_OWNERS").replace(" ", "").split(",") if o]
                  or operators)
        forwarder = None
        if _env("AWDISCORD_DM_FORWARD_URL"):
            from awdiscord.forward import DmForwarder
            forwarder = DmForwarder(_env("AWDISCORD_DM_FORWARD_URL"),
                                    _env("AWDISCORD_DM_FORWARD_TOKEN"), owners=owners)
        from awdiscord.agent import GenesisAgent
        owner_agent = GenesisAgent.from_env()
        return cls(token, agent, app_id=_env("AWDISCORD_APP_ID", "DISCORD_APP_ID") or None,
                   desk=desk, approval_channel=_env("AWDISCORD_APPROVAL_CHANNEL") or None,
                   dev_guild=_env("AWDISCORD_DEV_GUILD") or None,
                   inbox_port=int(_env("AWDISCORD_INBOX_PORT", default="0") or 0),
                   inbox_token=_env("AWDISCORD_INBOX_TOKEN"),
                   inbox_host=_env("AWDISCORD_INBOX_HOST", default="127.0.0.1"),
                   host=host, forwarder=forwarder, owner_agent=owner_agent,
                   owners=owners, internal_key=_env("AITHER_INTERNAL_SECRET"))

    # -- setup -----------------------------------------------------------------
    def prepare(self) -> dict:
        """Prove the token is a bot, learn the app id, register the commands."""
        me = self.rest.me()
        if not self.app_id:
            self.app_id = str(self.rest.application()["id"])
        cmds = commands(sorted(getattr(self.agent, "personas", {}) or {}), host=self.host)
        self.rest.put_commands(self.app_id, cmds)
        if self.dev_guild:
            self.rest.put_commands(self.app_id, cmds, guild_id=self.dev_guild)
        self.state["user"] = f"{me.get('username')}#{me.get('discriminator', '0')} ({me['id']})"
        self.state["user_id"] = str(me["id"])
        _log(event="prepared", bot=self.state["user"], app_id=self.app_id,
             commands=[c["name"] for c in cmds], approvals=bool(self.desk),
             plugins=sorted(self.host.plugins) if self.host else [],
             plugin_errors=self.host.errors if self.host else {})
        return me

    # -- approvals and posting (used by the inbox and by callers) ------------------
    def request_approval(self, title: str, body: str, callback_url: str | None = None,
                         channel_id: str | None = None, meta: dict | None = None) -> dict:
        if self.desk is None:
            raise RuntimeError("approvals need AWDISCORD_OPERATORS and AWDISCORD_APPROVAL_SECRET")
        record = self.desk.raise_card(title, body, callback_url=callback_url, meta=meta)
        card = self.desk.card(record)
        targets = []
        channel = channel_id or self.approval_channel
        if channel:
            targets.append(channel)
        else:
            for op in sorted(self.desk.operators):
                targets.append(self.rest.dm_channel(op)["id"])
        posted = [self.rest.create_message(t, card)["id"] for t in targets]
        _log(event="approval_raised", id=record["id"], targets=len(posted))
        return {"id": record["id"], "messages": posted}

    def post(self, channel_id: str, content: str) -> dict:
        msg = self.rest.create_message(channel_id, {"content": content[:2000]})
        return {"id": msg["id"], "channel_id": channel_id}

    def notify_owners(self, content: str) -> dict:
        """DM every owner. Fail-closed: no owners is an error, never a silent drop."""
        if not self.owners:
            raise RuntimeError("no owners: set AWDISCORD_OWNERS or AWDISCORD_OPERATORS")
        sent = []
        for owner in self.owners:
            channel = self.rest.dm_channel(owner)["id"]
            sent.append(self.post(channel, content)["id"])
        _log(event="owner_notified", owners=len(sent))
        return {"messages": sent}

    # -- interactions ------------------------------------------------------------
    def handle_raw(self, interaction: dict) -> None:
        """Answer one interaction (blocking; run it off the event loop)."""
        self.state["interactions"] += 1
        result = self.dispatcher.handle(interaction)
        _log(event="interaction", type=interaction.get("type"), **result.log)
        self.rest.interaction_callback(interaction["id"], interaction["token"], result.response)
        if result.deferred is not None:
            payload = result.deferred()
            self.rest.edit_original(self.app_id, interaction["token"], payload)
            _log(event="answered", chars=len(payload.get("content", "")))

    def recent_card_text(self, channel_id: str) -> str:
        """The newest decision card the bot itself posted in a DM, from history.

        Survives a restart, unlike the map the gateway fills as cards arrive.
        """
        from awdiscord.forward import CARD_ID_RE
        me = self.state.get("user_id") or ""
        try:
            msgs = self.rest.request("GET", f"/channels/{channel_id}/messages?limit=25") or []
        except RestError:
            return ""
        for m in msgs:                                   # newest first
            if me and str((m.get("author") or {}).get("id")) != me:
                continue
            text = _raw_text(m)
            if CARD_ID_RE.search(text):
                return text
        return ""

    def own_message_text(self, channel_id: str, message_id: str) -> str:
        """Text of message ``message_id`` if the bot itself posted it, else "".

        A DM that is a Discord reply names the message it replies to; when that
        is one of the bot's cards, the reply answers THAT card.
        """
        me = self.state.get("user_id") or ""
        if not me or not message_id:
            return ""                     # cannot prove authorship: not a card reply
        try:
            m = self.rest.request("GET", f"/channels/{channel_id}/messages/{message_id}") or {}
        except RestError:
            return ""
        if not isinstance(m, dict) or str((m.get("author") or {}).get("id")) != me:
            return ""
        return _raw_text(m)

    def handle_message(self, msg: dict) -> None:
        """Answer one mention or DM as a reply (blocking; run it off the event loop)."""
        try:
            self.rest.request("POST", f"/channels/{msg['channel_id']}/typing")
        except RestError as exc:                  # cosmetic; never block the answer
            _log(event="typing_failed", status=exc.status)
        if msg.get("guild_id") is None and self.forwarder is not None:
            ref_text = msg.get("reference_text") or ""
            if not ref_text and msg.get("reference_id") and msg["user"] in self.forwarder.owners:
                ref_text = self.own_message_text(msg["channel_id"], msg["reference_id"])
            reply = self.forwarder.reply_for(msg["channel_id"], msg["user"], msg["content"],
                                             reference_text=ref_text)
            if reply:
                self.rest.create_message(msg["channel_id"], {
                    "content": reply[:2000], "allowed_mentions": {"parse": []}})
                _log(event="dm_forwarded", user=msg["user"])
                return
        payload = self.dispatcher.chat(msg["user"], msg.get("guild_id"), msg["content"],
                                       bot_id=msg.get("bot_id", ""),
                                       channel=msg["channel_id"],
                                       attachments=msg.get("attachments") or None)
        if payload is None:
            return
        payload["message_reference"] = {"message_id": msg["id"],
                                        "fail_if_not_exists": False}
        self.rest.create_message(msg["channel_id"], payload)
        _log(event="mention_answered", user=msg["user"], chars=len(payload["content"]),
             agent=getattr(self.dispatcher.agent_for(msg["user"]), "name", ""))

    def run(self) -> None:
        try:
            import discord
        except ImportError:
            sys.exit("the gateway needs discord.py: pip install 'awdiscord[gateway]'")
        self.prepare()
        if self.inbox_port:
            self._start_inbox()
        intents = discord.Intents.none()
        intents.guilds = True
        # Mentions and DMs: both non-privileged. A message that mentions the bot
        # carries its text even without the message-content intent.
        chat = _env("AWDISCORD_MENTIONS", default="1") not in ("0", "false", "no")
        intents.guild_messages = chat
        intents.dm_messages = chat
        client = discord.Client(intents=intents)
        bot = self

        @client.event
        async def on_message(message):
            if not chat or client.user is None:
                return
            is_dm = message.guild is None
            if message.author.id == client.user.id:
                if is_dm and bot.forwarder is not None:     # a card the bot just DMed
                    bot.forwarder.note_own_message(str(message.channel.id),
                                                   _flat_text(message))
                return
            if message.author.bot:
                return
            if not is_dm and client.user not in message.mentions:
                return
            ref_id, ref_text = _reference(message, client.user.id) if is_dm else ("", "")
            try:
                await asyncio.to_thread(bot.handle_message, {
                    "id": str(message.id), "channel_id": str(message.channel.id),
                    "guild_id": str(message.guild.id) if message.guild else None,
                    "user": str(message.author.id), "content": message.content or "",
                    "bot_id": str(client.user.id),
                    "reference_id": ref_id, "reference_text": ref_text,
                    "attachments": [_attachment(a) for a in message.attachments or []]})
            except RestError as exc:
                _log(event="message_error", status=exc.status, error=str(exc)[:200])

        @client.event
        async def on_ready():
            bot.state.update(connected=True, guilds=len(client.guilds), since=time.time(),
                             user_id=str(client.user.id))
            _log(event="ready", bot=str(client.user), guilds=len(client.guilds))

        @client.event
        async def on_disconnect():
            bot.state["connected"] = False

        # discord.py RESUMES a dropped session without firing on_ready again, so
        # without these /health said 503 forever after the first network blip
        # while the bot kept answering (container "unhealthy", measured 2026-10-04).
        @client.event
        async def on_resumed():
            bot.state["connected"] = True
            _log(event="resumed")

        @client.event
        async def on_connect():
            bot.state["connected"] = True

        @client.event
        async def on_guild_join(guild):
            bot.state["guilds"] = len(client.guilds)
            _log(event="guild_join", guild=str(guild.id))

        @client.event
        async def on_interaction(interaction):
            raw = _raw_interaction(interaction)
            try:
                await asyncio.to_thread(bot.handle_raw, raw)
            except RestError as exc:
                _log(event="interaction_error", status=exc.status, error=str(exc)[:200])

        client.run(self._token, log_handler=None)

    # -- inbox ---------------------------------------------------------------------
    def _start_inbox(self) -> None:
        if not self._inbox_token:
            raise TokenRefusedError("AWDISCORD_INBOX_TOKEN is required to open the inbox")
        bot = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                got = self.headers.get("Authorization", "")
                if hmac.compare_digest(got, "Bearer " + bot._inbox_token):
                    return True
                # The internal key opens /notify/owner only, never cards or /post.
                key = self.headers.get("X-Internal-Key", "")
                return (self.path == "/notify/owner" and bool(bot._internal_key)
                        and hmac.compare_digest(key, bot._internal_key))

            def do_GET(self):
                if self.path == "/health":
                    self._send(200 if bot.state["connected"] else 503, bot.state)
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                # Read the body BEFORE refusing: answering 401 with the body still
                # unread makes the client see a connection reset, not the 401.
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = 0
                raw = self.rfile.read(min(n, 65536))
                if not self._authed():
                    self._send(401, {"error": "bearer required"})
                    return
                try:
                    body = json.loads(raw or b"{}")
                    if self.path == "/approvals":
                        out = bot.request_approval(
                            str(body["title"]), str(body.get("body", "")),
                            callback_url=body.get("callback_url"),
                            channel_id=body.get("channel_id"), meta=body.get("meta"))
                    elif self.path == "/post":
                        out = bot.post(str(body["channel_id"]), str(body["content"]))
                    elif self.path == "/notify/owner":
                        out = bot.notify_owners(str(body.get("message") or body["content"]))
                    else:
                        self._send(404, {"error": "not found"})
                        return
                    self._send(200, out)
                except (KeyError, ValueError) as exc:
                    self._send(400, {"error": str(exc)[:200]})
                except RestError as exc:
                    self._send(502, {"error": str(exc)[:200]})
                except RuntimeError as exc:
                    self._send(409, {"error": str(exc)[:200]})

        server = ThreadingHTTPServer((self.inbox_host, self.inbox_port), Handler)
        self.inbox_port = server.server_address[1]
        self.inbox_server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _log(event="inbox", host=self.inbox_host, port=self.inbox_port)


def _raw_interaction(interaction) -> dict:
    """The dict Discord sent, rebuilt from discord.py's Interaction."""
    user = {"id": str(interaction.user.id)}
    raw = {"id": str(interaction.id), "token": interaction.token,
           "type": interaction.type.value, "data": interaction.data or {},
           "channel_id": str(interaction.channel_id or "")}
    if interaction.guild_id:
        raw["guild_id"] = str(interaction.guild_id)
        # The invoker's resolved permissions in this channel: plugins gate
        # manager-only commands on Manage Server, so it must survive the rebuild.
        perms = getattr(interaction, "permissions", None)
        raw["member"] = {"user": user, "permissions": str(getattr(perms, "value", 0) or 0)}
    else:
        raw["user"] = user
    return raw
