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

from awdiscord import autochannel, containment
from awdiscord.agent import A2AAgent, OpenAICompatAgent
from awdiscord.approvals import ApprovalDesk
from awdiscord.brain import served_by_brain
from awdiscord.dispatch import (
    ADMINISTRATOR,
    COMMAND,
    EPHEMERAL,
    MANAGE_GUILD,
    MESSAGE_COMMAND,
    STILL_WORKING_TEXT,
    Dispatcher,
    clip,
    commands,
    explicit_private,
    normalize_emoji,
    reply_visibility,
    split_message,
)
from awdiscord.guard import TokenRefusedError
from awdiscord.plugins import MODAL_SUBMIT
from awdiscord.ratelimit import RateLimiter
from awdiscord.rest import Rest, RestError
from awdiscord.served import SETUP_TEXT, ServedGuilds
from awdiscord.triage import (
    Candidate,
    Harness,
    Triage,
    WatchPolicy,
    speculative_requests,
)

#: Member commands whose answer is for the room: outside the bot channel it is
#: posted publicly THERE (or in place with no bot channel), never hidden from
#: everyone but the asker. Operator commands (hub, admin, setup) stay contained.
RELAYED_COMMANDS = frozenset({"ask", "share"})
#: Core commands that answer only their operator: contained, never made public.
OPERATOR_COMMANDS = frozenset({"model", "selfhost", "settings", "aither-ignore",
                               "aither-unignore"})

NO_OPS_CHANNEL = "no_ops_channel"   # the 409 reason /notify/channel gives with no channel


class NoOpsChannelError(RuntimeError):
    """``/notify/channel`` on a bot with no ``AWDISCORD_OPS_CHANNEL``."""


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


def _reaction_brief(message: dict, guild: str, channel: str,
                    message_id: str) -> dict:
    """A REST message dict as the dispatcher's action target (``message_brief``)."""
    author = message.get("author") or {}
    return {
        "id": message_id,
        "content": _raw_text(message).strip(),
        "author": str(author.get("id") or ""),
        "author_name": str(author.get("global_name") or author.get("username") or ""),
        "channel": channel,
        "attachments": [str(a.get("filename") or "")
                        for a in message.get("attachments") or []],
        "link": f"https://discord.com/channels/{guild}/{channel}/{message_id}",
    }


def _candidate(message) -> Candidate:
    """A watched-channel message as the stage-two judge sees it.

    A message inside a Discord thread arrives with ``channel`` already set to
    that thread, so the channel id IS the thread id. A message in a plain
    channel uses the channel id for both, which is the id a worker needs to
    reproduce the report.
    """
    channel = message.channel
    guild = message.guild
    author = message.author
    name = getattr(author, "display_name", "") or getattr(author, "name", "") or author.id
    return Candidate(
        channel=str(channel.id),
        text=message.content or "",
        guild=str(guild.id) if guild is not None else "",
        user=str(author.id),
        message_id=str(message.id),
        thread=str(channel.id),
        author=str(name),
        channel_name=str(getattr(channel, "name", "") or ""),
        guild_name=str(getattr(guild, "name", "") or "") if guild is not None else "",
    )


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


IGNORE, CHAT, HOOK, CANDIDATE = "ignore", "chat", "hook", "candidate"


def message_route(*, own: bool, author_bot: bool, is_dm: bool, mentioned: bool,
                  hooks: bool, candidate: bool = False) -> str:
    """What the gateway does with one message: ``ignore``, ``chat``, ``hook`` or
    ``candidate``.

    ``chat`` (the agent answers as a reply) is a DM or a message that mentions
    the bot, exactly as before. ``hook`` is any other human server message, and
    only when a plugin on in that server registered a message hook. ``candidate``
    is any other human server message in a channel an operator put in
    ``AWDISCORD_WATCH_CHANNELS``: it is handed to the stage-two judge
    (``awdiscord.triage``), which stays silent unless it decides the message
    needs work. A hook wins over a candidate, so a plugin's own answer is never
    starved by the watch policy. The bot's own messages and every bot-authored
    message (webhooks too) are ignored, so nothing a hook posts can come back to
    it -- and no bot can feed the judge. Stage one stays pure and deterministic:
    no model is called here.
    """
    if own or author_bot:
        return IGNORE
    if is_dm or mentioned:
        return CHAT
    if hooks:
        return HOOK
    return CANDIDATE if candidate else IGNORE


class Bot:
    def __init__(self, token: str, agent, app_id: str | None = None,
                 desk: ApprovalDesk | None = None, limiter: RateLimiter | None = None,
                 approval_channel: str | None = None, dev_guild: str | None = None,
                 inbox_port: int = 0, inbox_token: str = "", inbox_host: str = "127.0.0.1",
                 host=None, forwarder=None, owner_agent=None, owners=(),
                 internal_key: str = "", ops_channel: str = "",
                 owner_tool_loop: bool = False, watch: WatchPolicy | None = None,
                 triage: Triage | None = None, harness: Harness | None = None,
                 served: ServedGuilds | None = None, brains=None):
        from awdiscord import __version__
        self.rest = Rest(token)
        # Owners get /notify/owner DMs. A fleet service may authenticate that one
        # route with the shared internal key (X-Internal-Key) instead of the
        # inbox bearer, so a caller needs no extra secret just to page the owner.
        self.owners = [str(o) for o in owners]
        # Which servers may answer with the owner's model at all. None = no gate
        # (building the pieces directly); the deployed bot sets it in from_env().
        self.served = served
        self._internal_key = internal_key
        # /notify/channel posts here (AWDISCORD_OPS_CHANNEL): the team's ops room.
        self.ops_channel = str(ops_channel or "")
        self._token = token
        self.agent = agent
        self.app_id = app_id
        self.desk = desk
        self.approval_channel = approval_channel
        self.dev_guild = dev_guild
        self.host = host            # awdiscord.plugins.PluginHost, or None
        # Output containment: which channel per server the bot talks in.
        self.bot_channels = self._new_bot_channels()
        if host is not None:
            host.rest = self.rest   # message hooks post through the bot's REST client
        self.forwarder = forwarder  # awdiscord.forward.DmForwarder, or None
        # Stage 1 of the trigger filter: which channels are watched, and (stage 2)
        # the judge that decides whether a watched message needs work. Both are
        # inert unless AWDISCORD_WATCH_CHANNELS names a channel.
        self.watch = watch if watch is not None else WatchPolicy.from_env()
        self.triage = triage if triage is not None else (
            Triage(agent) if self.watch.candidate_channels else None)
        # The silent hand-off for a bug verdict; None = configured off (logged).
        self.harness = harness if harness is not None else Harness.from_env()
        self.dispatcher = Dispatcher(agent, limiter or RateLimiter(), desk,
                                     version=__version__, host=host,
                                     owner_agent=owner_agent, owners=owners,
                                     owner_tool_loop=owner_tool_loop,
                                     served=served, brains=brains)
        #: The one (guild, user) -> Brain predicate. Threaded into the plugin host
        #: so ``Ctx.agent`` cannot bypass it; a plugin on an unserved server gets
        #: None and degrades to its non-agent answer.
        self.brains = self.dispatcher.brains
        if host is not None and self.brains is not None \
                and getattr(host, "resolver", None) is None:
            host.resolver = self.brains
        if forwarder is not None and getattr(forwarder, "_lookup", 0) is None:
            forwarder._lookup = self.recent_card_text
        self.inbox_port = inbox_port
        self.inbox_host = inbox_host
        self._inbox_token = inbox_token
        self.state = {"connected": False, "user": None, "guilds": 0, "since": None,
                      "interactions": 0, "version": __version__}
        # One agent action per (message, emoji): a reaction added twice (a client
        # retry, two gateway events) must not run the work twice. Bounded TTL,
        # because message ids are never forgotten by Discord.
        self._reaction_seen: dict[tuple[str, str], float] = {}
        self._reaction_lock = threading.Lock()
        # Voice is opt-in (AWDISCORD_VOICE=1) and imported lazily: the core brick
        # stays dependency-free for every bot that only speaks text. Turning it on
        # needs the voice extra (PyNaCl + davey + the receive extension) and the
        # two AitherVoice doors; a half-configured voice setup fails at startup.
        self.voice = None
        if _env("AWDISCORD_VOICE").lower() in ("1", "true", "yes", "on"):
            from awdiscord.voice import DiscordVoice
            self.voice = DiscordVoice.from_env(self)

    def _log_voice(self, **fields) -> None:
        """The voice transport's logger, so every line is the bot's JSON format."""
        _log(**fields)

    @classmethod
    def from_env(cls, agent=None) -> "Bot":
        token = _env("AWDISCORD_TOKEN", "DISCORD_BOT_TOKEN")
        # A2A is opt-in: its from_env returns None unless AWDISCORD_A2A_URL is set,
        # so a bot that never configured one keeps today's default exactly.
        agent = agent or A2AAgent.from_env() or OpenAICompatAgent.from_env()
        operators = [o for o in _env("AWDISCORD_OPERATORS").replace(" ", "").split(",") if o]
        secret = _env("AWDISCORD_APPROVAL_SECRET")
        state = Path(_env("AWDISCORD_STATE_DIR", default=str(Path.home() / ".awdiscord")))
        desk = None
        if operators and secret:
            desk = ApprovalDesk(state / "approvals", secret, operators)
        host = None
        from awdiscord.store import GuildStore
        store = GuildStore(state / "guilds.db")
        if _env("AWDISCORD_NO_PLUGINS") not in ("1", "true", "yes"):
            from awdiscord.plugins import build_host
            only = [p for p in _env("AWDISCORD_PLUGINS").replace(" ", "").split(",") if p]
            host = build_host(store, agent=agent,
                              operators=set(operators), only=only or None)
        # Owners = who may answer decision cards and who talks to the fleet
        # agent. Defaults to the operators; the card receiver re-checks anyway.
        owners = ([o for o in _env("AWDISCORD_OWNERS").replace(" ", "").split(",") if o]
                  or operators)
        # Servers allowed to spend the owner's inference. Fail closed: home only
        # when the allowlist is unset, empty or unparseable.
        served = ServedGuilds.from_env()
        _log(event="served_guilds", home=served.home,
             served=sorted(served.guilds), problem=served.problem)
        forwarder = None
        if _env("AWDISCORD_DM_FORWARD_URL"):
            from awdiscord.forward import DmForwarder
            forwarder = DmForwarder(_env("AWDISCORD_DM_FORWARD_URL"),
                                    _env("AWDISCORD_DM_FORWARD_TOKEN"), owners=owners)
        from awdiscord.agent import GenesisAgent
        owner_agent = GenesisAgent.from_env()
        # The one predicate. A hosted deployment (a served gate or a Genesis
        # owner agent) fails CLOSED: a guild not in the allowlist gets `none`,
        # and a resolver that fails to build denies everything rather than
        # serving everyone. A standalone self-hosted bot opts in EXPLICITLY with
        # AWDISCORD_STANDALONE=1; only then does its env agent answer every
        # guild it is in (mode `byo`, reason `legacy_env`). Absence of the
        # allowlist is not an opt-in -- an empty allowlist serves nobody.
        from awdiscord.brain import BrainResolver, GenesisInferenceClient
        from awdiscord.brainsecrets import BrainSecrets
        tool_loop = _env("AWDISCORD_OWNER_TOOL_LOOP").lower() in ("1", "true", "yes")
        standalone = _env("AWDISCORD_STANDALONE").lower() in ("1", "true", "yes")
        try:
            # Genesis is the authority for a guild's hosted record. With no URL
            # configured this is None and a hosted server simply resolves to
            # none (the feature is off unless AWDISCORD_HOSTED=1 anyway).
            try:
                hosted_client = GenesisInferenceClient.from_env()
            except Exception:             # noqa: BLE001 - absent config is none
                hosted_client = None
            brains = BrainResolver(
                agent, owner_agent=owner_agent, owners=owners,
                owner_tool_loop=tool_loop, served=served, store=store,
                secrets=BrainSecrets(state),
                legacy_agent=agent if standalone else None,
                hosted=False if standalone else None,
                hosted_client=hosted_client)
        except Exception as exc:          # noqa: BLE001 - fail closed, never open
            from awdiscord.brain import DenyResolver
            _log(event="brain_resolver", problem=type(exc).__name__)
            brains = DenyResolver()
        return cls(token, agent, app_id=_env("AWDISCORD_APP_ID", "DISCORD_APP_ID") or None,
                   desk=desk, approval_channel=_env("AWDISCORD_APPROVAL_CHANNEL") or None,
                   dev_guild=_env("AWDISCORD_DEV_GUILD") or None,
                   inbox_port=int(_env("AWDISCORD_INBOX_PORT", default="0") or 0),
                   inbox_token=_env("AWDISCORD_INBOX_TOKEN"),
                   inbox_host=_env("AWDISCORD_INBOX_HOST", default="127.0.0.1"),
                   host=host, forwarder=forwarder, owner_agent=owner_agent,
                   owners=owners, internal_key=_env("AITHER_INTERNAL_SECRET"),
                   ops_channel=_env("AWDISCORD_OPS_CHANNEL"),
                   # Operators' mentions run the plugins' tools too; the fleet
                   # agent is then the `ask_fleet` tool (Dispatcher.owner_tool_loop).
                   owner_tool_loop=tool_loop,
                   served=served, brains=brains)

    # -- setup -----------------------------------------------------------------
    def prepare(self) -> dict:
        """Prove the token is a bot, learn the app id, register the commands."""
        me = self.rest.me()
        if not self.app_id:
            self.app_id = str(self.rest.application()["id"])
        cmds = commands(sorted(getattr(self.agent, "personas", {}) or {}), host=self.host)
        voice = getattr(self, "voice", None)
        if voice is not None:
            cmds = cmds + voice.command_defs()
        self.rest.put_commands(self.app_id, cmds)
        if self.dev_guild:
            self.rest.put_commands(self.app_id, cmds, guild_id=self.dev_guild)
        self.state["user"] = f"{me.get('username')}#{me.get('discriminator', '0')} ({me['id']})"
        self.state["user_id"] = str(me["id"])
        try:
            from awdiscord.version_check import versions
            seen = versions()
            if not seen["match"]:
                # An overlay updated the code but not the dist-info: `pip show`
                # misreports what is running. __version__ is the truth.
                _log(event="version_drift", code=seen["code"], metadata=seen["metadata"])
        except Exception:                 # noqa: BLE001 - diagnostic only
            pass
        _log(event="prepared", bot=self.state["user"], app_id=self.app_id,
             commands=[c["name"] for c in cmds], approvals=bool(self.desk),
             plugins=sorted(self.host.plugins) if self.host else [],
             plugin_errors=self.host.errors if self.host else {},
             watched=len(self.watch.candidate_channels), triage=bool(self.triage),
             harness=bool(self.harness))
        return me

    def start_plugin_daemons(self) -> dict:
        """Run each plugin's ``on_ready`` once. Returns {plugin: "ok" | error}.

        A background daemon that must exist before the first human speaks (the media
        ingester's backfill sweep) starts here. One plugin's failure is logged and
        kept in the return value; it never stops the bot or the other plugins.
        """
        out: dict = {}
        if self.host is None:
            return out
        for name, plugin in sorted((self.host.plugins or {}).items()):
            for fn in getattr(plugin, "on_ready", []) or []:
                try:
                    fn(self.host)
                    out[name] = "ok"
                except Exception as exc:      # noqa: BLE001 - reported, never fatal
                    out[name] = f"{type(exc).__name__}: {exc}"[:200]
                    _log(event="plugin_ready_error", plugin=name,
                         error=type(exc).__name__, detail=str(exc)[:200])
        return out

    # -- output containment ------------------------------------------------------
    def _new_bot_channels(self) -> containment.BotChannels:
        # The lambdas read self.rest/self.host late, so a swapped client is honoured.
        return containment.BotChannels(
            lambda guild: self.rest.request("GET", f"/guilds/{guild}/channels"),
            setting=self._bot_channel_setting)

    def _channels(self) -> containment.BotChannels:
        """The resolver (built on first use for a Bot made without ``__init__``)."""
        found = getattr(self, "bot_channels", None)
        if found is None:
            found = self.bot_channels = self._new_bot_channels()
        return found

    def _bot_channel_setting(self, guild: str) -> str:
        """The server's own ``core``/``bot_channel`` setting (id or name), or ``""``."""
        store = getattr(getattr(self, "host", None), "store", None)
        if store is None:
            return ""
        return str(store.get(guild, "core", "bot_channel", "") or "")

    def _contained(self, guild, channel, parent="") -> tuple[str, str]:
        """Where output for ``channel`` goes: ``("here"|"bot"|"dm", channel id)``.

        ``here`` = the channel itself (a DM, the bot channel, containment off, or
        a server whose channel list is unknown); ``bot`` = the server's bot
        channel; ``dm`` = the server has no bot channel.
        """
        if not guild or not containment.enabled():
            return "here", str(channel or "")
        if self._channels().is_bot_channel(guild, channel, parent):
            return "here", str(channel or "")
        if not parent:
            # Not told which channel this one sits in: a thread inside the bot
            # channel must count as the bot channel, so ask Discord (cached).
            parent = self._parent_of(str(channel or ""))
            if parent and self._channels().is_bot_channel(guild, channel, parent):
                return "here", str(channel or "")
        bot = self._channels().for_guild(guild)
        return ("bot", str(bot)) if bot else ("dm", "")

    def _parent_of(self, channel: str) -> str:
        """The channel a thread sits in (``""`` for a plain channel or on failure)."""
        cache = self.__dict__.setdefault("_parent_cache", {})
        now = time.monotonic()
        hit = cache.get(channel)
        if hit is not None and hit[1] > now:
            return hit[0]
        try:
            info = self.rest.request("GET", f"/channels/{channel}") or {}
        except Exception as exc:              # noqa: BLE001 -- cosmetic lookup
            _log(event="channel_parent_lookup_failed", error=type(exc).__name__)
            return ""
        parent = str(info.get("parent_id") or "") if isinstance(info, dict) else ""
        # only a thread's parent is a channel; a category parent is not a bot channel
        if isinstance(info, dict) and int(info.get("type") or 0) not in (10, 11, 12):
            parent = ""
        cache[channel] = (parent, now + 3600)
        return parent

    def _mark_seen(self, channel: str, message: str) -> None:
        """React to the original message: the answer was taken, and lives elsewhere."""
        if not (channel and message):
            return
        import urllib.parse
        emoji = urllib.parse.quote(containment.EYES)
        try:
            self.rest.request("PUT", f"/channels/{channel}/messages/{message}"
                              f"/reactions/{emoji}/@me")
        except Exception as exc:              # noqa: BLE001 -- cosmetic; the answer lands
            _log(event="contained_reaction_failed",
                 status=getattr(exc, "status", type(exc).__name__))

    def _settings_store(self):
        """The per-server settings store (the dispatcher's, else the plugin host's)."""
        store = getattr(getattr(self, "dispatcher", None), "store", None)
        if store is None:
            store = getattr(getattr(self, "host", None), "store", None)
        return store

    def _operator_only(self, data: dict) -> bool:
        """An admin/operator command (or admin subcommand): never public by default."""
        name = str(data.get("name") or "")
        if name in OPERATOR_COMMANDS:
            return True
        host = getattr(self, "host", None)
        plugin = getattr(host, "_by_command", {}).get(name) if host is not None else None
        node = next((c for c in getattr(plugin, "commands", []) or []
                     if getattr(c, "name", "") == name), None)
        options = data.get("options") or []
        while node is not None:
            if getattr(node, "admin", False):
                return True
            sub = next((o for o in options if isinstance(o, dict)
                        and o.get("type") in (1, 2)), None)
            if sub is None:
                return False
            node = next((c for c in getattr(node, "subcommands", []) or []
                         if getattr(c, "name", "") == sub.get("name")), None)
            options = sub.get("options") or []
        return False

    @staticmethod
    def _make_ephemeral(response: dict) -> None:
        data = dict(response.get("data") or {})
        data["flags"] = int(data.get("flags") or 0) | EPHEMERAL
        response["data"] = data

    def _visibility_plan(self, interaction: dict,
                         response: dict) -> tuple[bool, str | None, dict]:
        """Who sees a slash command's answer: ``(ephemeral, relay channel, log)``.

        1. The command's own ``private`` option wins: true = only the asker;
           false = public (in place, or in the bot channel under ``bot_channel``).
        2. Else the server's ``core/reply_visibility``: ``public`` (default)
           answers in place for everyone; ``private`` answers only the asker;
           ``bot_channel`` posts ``/ask``/``/share`` in the bot channel as a
           SEPARATE channel message mentioning the asker, whose own reply becomes
           an ephemeral pointer (an ephemeral deferred response makes every
           followup ephemeral, so the public answer can never be a followup).
        Operator commands (hub/admin/setup) keep the bot-channel containment.
        """
        if (interaction.get("type") != COMMAND or not isinstance(response, dict)
                or response.get("type") not in (4, 5)):
            return self._contain_interaction(interaction, response), None, {}
        data = interaction.get("data") or {}
        name = str(data.get("name") or "")
        if int(data.get("type") or 1) != 1 or self._operator_only(data):
            return self._contain_interaction(interaction, response), None, {}
        guild = interaction.get("guild_id")
        if not guild:
            return False, None, {}            # a DM is the asker's own channel
        if int((response.get("data") or {}).get("flags") or 0) & EPHEMERAL:
            return True, None, {"visibility": "private"}   # a refusal, private:true
        explicit = explicit_private(interaction)
        mode = reply_visibility(self._settings_store(), guild)
        if explicit is True or (mode == "private" and explicit is None):
            self._make_ephemeral(response)
            return True, None, {"visibility": "private"}
        if mode == "bot_channel":
            where, target = self._contained(guild, interaction.get("channel_id"),
                                            interaction.get("channel_parent_id"))
            user = self._asker(interaction)["user"]
            if where == "bot" and name in RELAYED_COMMANDS and not (
                    explicit is False and user in self.owners):
                self._make_ephemeral(response)
                return True, target, {"visibility": "bot_channel",
                                      "public_channel": target}
            if where == "bot" and explicit is None and name not in RELAYED_COMMANDS:
                self._make_ephemeral(response)      # contained, as before
                return True, None, {"visibility": "contained"}
        return False, None, {"visibility": "public"}

    @staticmethod
    def _asker(interaction: dict) -> dict:
        """The message-shaped asker of an interaction, for the bot-channel relay."""
        member = interaction.get("member") or {}
        user = str((member.get("user") or interaction.get("user") or {}).get("id") or "")
        return {"guild_id": str(interaction.get("guild_id") or ""), "user": user,
                "channel_id": str(interaction.get("channel_id") or ""), "id": ""}

    def _pointer(self, guild: str, channel: str, message: str, verb: str) -> dict:
        """The asker's one ephemeral line pointing at the public answer."""
        return {"content": f"{verb} in <#{channel}>: "
                           f"{containment.jump_link(guild, channel, message)}",
                "allowed_mentions": {"parse": []}}

    def _relay_share(self, interaction: dict, target: str, response: dict) -> str:
        """Post a ``/share`` in the bot channel; the response becomes the pointer.

        Returns the public message id, ``""`` when the post failed (the response is
        then untouched: the asker still gets the text, privately).
        """
        asker = self._asker(interaction)
        data = dict(response.get("data") or {})
        try:
            posted = self.rest.create_message(target, {
                "content": str(data.get("content") or ""),
                "allowed_mentions": ({"parse": [], "users": [asker["user"]]}
                                     if asker["user"] else {"parse": []})})
        except RestError as exc:
            _log(event="contained_post_failed", channel=target, status=exc.status)
            self._channels().forget(asker["guild_id"])
            return ""
        posted_id = str((posted or {}).get("id") or "")
        pointer = self._pointer(asker["guild_id"], target, posted_id, "Shared")
        response["data"] = {**pointer, "flags": int(data.get("flags") or 0) | EPHEMERAL}
        return posted_id

    def _contain_interaction(self, interaction: dict, response: dict) -> bool:
        """Make a command's reply ephemeral outside the bot channel. True when it did.

        An operator who EXPLICITLY sets an existing ``private`` option to false
        keeps a public reply (the option is the flag; no new permission).
        """
        if interaction.get("type") != COMMAND or not isinstance(response, dict):
            return False
        mode, _ = self._contained(interaction.get("guild_id"),
                                  interaction.get("channel_id"),
                                  interaction.get("channel_parent_id"))
        if mode == "here":
            return False
        options = (interaction.get("data") or {}).get("options") or []
        explicit_public = any(o.get("name") == "private" and o.get("value") is False
                              for o in options if isinstance(o, dict))
        member = interaction.get("member") or {}
        user = str((member.get("user") or interaction.get("user") or {}).get("id") or "")
        if explicit_public and user in self.owners:
            return False
        kind = response.get("type")
        if kind not in (4, 5):            # a modal, an autocomplete: nothing posts
            return False
        data = dict(response.get("data") or {})
        data["flags"] = int(data.get("flags") or 0) | EPHEMERAL
        response["data"] = data
        return True

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

    def notify_channel(self, content: str) -> dict:
        """Post to the ops channel. No channel configured is an error (409), never a drop."""
        if not self.ops_channel:
            raise NoOpsChannelError("no ops channel: set AWDISCORD_OPS_CHANNEL")
        out = self.post(self.ops_channel, content)
        _log(event="ops_channel_notified")
        return out

    def _unserved_guild_id(self, guild_id) -> bool:
        """True for a server the brain resolver does not serve (no model reach)."""
        return bool(guild_id and not served_by_brain(
            getattr(self, "brains", None), guild_id, getattr(self, "served", None)))

    # -- interactions ------------------------------------------------------------
    def handle_raw(self, interaction: dict) -> None:
        """Answer one interaction (blocking; run it off the event loop)."""
        self.state["interactions"] += 1
        voice = getattr(self, "voice", None)
        # A voice join in an unserved server would speak with the owner's model;
        # do not even take the command there.
        if (voice is not None
                and not self._unserved_guild_id(interaction.get("guild_id"))
                and voice.intercept(interaction)):
            _log(event="voice_command",
                 name=str((interaction.get("data") or {}).get("name") or ""))
            return
        result = None
        if interaction.get("type") == MODAL_SUBMIT:
            # Route by custom_id, never blindly: the core /brain form is
            # ``core:brain:setup`` and must reach the dispatcher (it re-checks
            # admin), while plugin forms are ``awp:...``. Sending a core modal to
            # the plugin host used to return None and crash on ``result.log``.
            from awdiscord.plugins import BUTTON_PREFIX
            cid = str((interaction.get("data") or {}).get("custom_id") or "")
            if (self.host is not None and cid.startswith(BUTTON_PREFIX)
                    and not self._unserved_guild_id(interaction.get("guild_id"))):
                result = self.host.handle_modal(interaction)
        if result is None:
            interaction = self._with_action_context(interaction)
            result = self.dispatcher.handle(interaction)
        # Who sees the answer: the command's own ``private`` option, else the
        # server's ``core/reply_visibility`` (public | private | bot_channel).
        contained, relay, relayed = self._visibility_plan(interaction, result.response)
        if relay and result.deferred is None:
            relayed["public_message"] = self._relay_share(interaction, relay,
                                                          result.response)
        _log(event="interaction", type=interaction.get("type"),
             **({"contained": True} if contained else {}), **relayed, **result.log)
        self.rest.interaction_callback(interaction["id"], interaction["token"], result.response)
        if result.deferred is not None:
            def still_working() -> None:
                self.rest.edit_original(self.app_id, interaction["token"], {
                    "content": STILL_WORKING_TEXT, "allowed_mentions": {"parse": []}})
            result.progress["fn"] = still_working
            payload = result.deferred()
            if relay and self._relay_answer(interaction, relay, payload, result.turn):
                return
            # The edit of an ephemeral original stays ephemeral on its own (an
            # edit may only set SUPPRESS_EMBEDS); the followups need the flag.
            flags = int(payload.get("flags") or 0) | (EPHEMERAL if contained else 0)
            more = payload.pop("continuation", None) or []
            files = payload.pop("files", None) or []
            # An interaction edit carries text only, and a declared file in a
            # private reply would leak it into a public channel: say so instead.
            if files and not flags & EPHEMERAL:
                channel = str(interaction.get("channel_id") or "")
                if channel:
                    self.post_files(channel, files)
                files = []
            posted = self.rest.edit_original(self.app_id, interaction["token"], payload)
            if files:                     # private reply: name them, never post them
                self.rest.followup(self.app_id, interaction["token"], {
                    "content": "Files this answer produced (not posted in a private "
                               "reply): " + ", ".join(f.get("filename", "file") for f in files),
                    "allowed_mentions": {"parse": []},
                    **({"flags": flags} if flags else {})})
            for part in more:                     # the rest of a long answer
                self.rest.followup(self.app_id, interaction["token"], {
                    "content": part, "allowed_mentions": {"parse": []},
                    **({"flags": flags} if flags else {})})
            _log(event="answered", chars=len(payload.get("content", "")),
                 parts=1 + len(more))
            if result.turn.get("reply"):
                member = interaction.get("member") or {}
                user = (member.get("user") or interaction.get("user") or {}).get("id", "")
                rows = self._reply_rows(result.turn, posted, {
                    "guild_id": interaction.get("guild_id"),
                    "channel_id": interaction.get("channel_id"), "user": str(user)})
                if rows:
                    self.rest.edit_original(self.app_id, interaction["token"],
                                            {"components": rows})

    def _relay_answer(self, interaction: dict, target: str, payload: dict,
                      turn: dict) -> bool:
        """Post a deferred ``/ask`` answer publicly in the bot channel.

        The asker's ephemeral original becomes a one-line pointer to it. False =
        the bot channel refused the post; the caller then answers privately.
        """
        asker = self._asker(interaction)
        posted = self._deliver_bot_channel(asker, target, payload, turn)
        if not posted:
            return False
        posted_id = str(posted.get("id") or "") if isinstance(posted, dict) else ""
        self.rest.edit_original(self.app_id, interaction["token"], self._pointer(
            asker["guild_id"], target, posted_id, "Answered"))
        _log(event="ask_relayed", channel=target, message=posted_id,
             user=asker["user"])
        return True

    def _reply_rows(self, turn: dict, posted, where: dict) -> list:
        """The plugins' component rows for one posted answer (``on_reply``).

        ``posted`` = the message Discord returned for the answer. Nothing runs
        unless a plugin on there registered a reply hook; a hook failure is the
        host's to log, never the answer's.
        """
        if self.host is None or not isinstance(posted, dict) or not posted.get("id"):
            return []
        guild = where.get("guild_id")
        if not self.host.has_reply_hooks(guild):
            return []
        rows = self.host.dispatch_reply({
            **turn, "guild_id": guild or "", "channel_id": str(where.get("channel_id") or
                                                               posted.get("channel_id") or ""),
            "user": str(where.get("user") or ""), "message_id": str(posted["id"]),
            "prompt_message_id": str(where.get("prompt_message_id") or "")})
        if rows:
            _log(event="reply_hooked", rows=len(rows), via=turn.get("via", ""))
        return rows

    def _with_action_context(self, interaction: dict) -> dict:
        """Attach the channel's recent messages for a "summarize" message command.

        A summarize command names ONE message, but the conversation it must
        summarize lives in the channel. Discord sends no history with the
        interaction, so it is fetched here (off the event loop: this runs in
        ``handle_raw``'s worker thread) and handed to the dispatcher as
        ``_context``. A fetch failure costs the surrounding messages, never the
        command: the target message alone still answers.
        """
        data = interaction.get("data") or {}
        if (interaction.get("type") != 2 or data.get("type") != MESSAGE_COMMAND
                or "summarize" not in str(data.get("name") or "").lower()):
            return interaction
        context = self._channel_context(str(interaction.get("channel_id") or ""))
        if not context:
            return interaction
        return {**interaction, "_context": context}

    def _channel_context(self, channel_id: str, limit: int = 50) -> str:
        """The channel's newest ``limit`` messages as ``who: text`` lines, oldest first."""
        if not channel_id:
            return ""
        try:
            messages = self.rest.request(
                "GET", f"/channels/{channel_id}/messages?limit={int(limit)}") or []
        except RestError as exc:
            _log(event="context_fetch_failed", status=exc.status)
            return ""
        if not isinstance(messages, list):
            return ""
        lines = []
        for m in reversed(messages):            # newest first from Discord
            if not isinstance(m, dict):
                continue
            author = m.get("author") or {}
            who = (author.get("global_name") or author.get("username")
                   or author.get("id") or "someone")
            text = _raw_text(m).strip()
            if text:
                lines.append(f"{who}: {clip(text, 400)}")
        return "\n".join(lines)

    def handle_reaction(self, evt: dict) -> dict:
        """Hand one human reaction to the plugins' hooks (blocking; off the loop).

        Then, when it was an ADD by an operator or a server admin and the emoji is
        in the server's reaction map, Aither acts on the message it was added to
        (``act_on_reaction``). The trigger is silence unless every condition
        holds: a reaction is cheap, so anything ambiguous is ignored, never run.
        """
        out = self.host.dispatch_reaction(evt) if self.host is not None else {}
        if out:
            _log(event="reaction", guild=str(evt.get("guild_id") or ""),
                 added=bool(evt.get("added")), plugins=out)
        if evt.get("added"):
            try:
                acted = self.act_on_reaction(evt)
            except RestError as exc:
                _log(event="reaction_action_error", status=exc.status,
                     error=str(exc)[:200])
            except Exception as exc:          # noqa: BLE001 - never take the gateway down
                _log(event="reaction_action_error", error=type(exc).__name__,
                     detail=str(exc)[:200])
                acted = {}
            if acted:
                out = {**out, "_action": acted}
        return out

    def act_on_reaction(self, evt: dict) -> dict:
        """An operator/admin reaction acts on the message it was added to.

        Ignored silently (a reaction must never explain itself to a stranger): a
        non-operator/non-admin, the bot's own reaction, an emoji the server's map
        does not name, a second reaction for the same (message, emoji), and a
        server the brain resolver does not serve. The target message's text is
        not in the event, so it is fetched over REST; the reply is a Discord
        reply to it, so the action is attached to what it acts on.
        """
        guild = str(evt.get("guild_id") or "")
        user = str(evt.get("user") or "")
        channel = str(evt.get("channel_id") or "")
        message_id = str(evt.get("message_id") or "")
        emoji = normalize_emoji(str(evt.get("emoji") or ""))
        if not (guild and user and channel and message_id and emoji):
            return {}
        if evt.get("user_bot"):
            return {}
        me = str(self.state.get("user_id") or self.app_id or "")
        if me and user == me:
            return {}
        if not self._may_act_on_reaction(user, evt.get("permissions")):
            return {}
        action = self.dispatcher.reaction_action(guild, emoji)
        if not action:
            return {}
        if self._unserved_guild_id(guild):
            _log(event="reaction_action_skipped", guild=guild, reason="unserved_guild")
            return {}
        if not self._reaction_once(f"{message_id}:{emoji}"):
            _log(event="reaction_duplicate", guild=guild, message=message_id,
                 emoji=emoji)
            return {}
        try:
            message = self.rest.request(
                "GET", f"/channels/{channel}/messages/{message_id}") or {}
        except RestError as exc:
            _log(event="reaction_fetch_failed", status=exc.status)
            return {}
        if not isinstance(message, dict):
            return {}
        brief = _reaction_brief(message, guild, channel, message_id)
        payload = self.dispatcher.act_on_message(user, guild, channel, action, brief)
        more = payload.pop("continuation", None) or []
        files = payload.pop("files", None) or []
        payload.setdefault("allowed_mentions", {"parse": []})
        payload["message_reference"] = {"message_id": message_id,
                                        "fail_if_not_exists": False}
        mode, target = self._contained(guild, channel)
        posted = None
        if mode == "bot":
            # Containment: the answer lands in the bot channel, naming the message.
            moved = dict(payload)
            moved.pop("message_reference", None)
            parts = split_message(containment.relayed_header(user, guild, channel,
                                                             message_id)
                                  + "\n" + payload.get("content", ""))
            moved["content"] = parts[0]
            moved["allowed_mentions"] = {"parse": [], "users": [user]}
            try:
                posted = self.rest.create_message(target, moved)
                more = parts[1:] + list(more)
                channel = target
            except RestError as exc:      # cannot post in the bot channel: in place
                _log(event="contained_post_failed", channel=target, status=exc.status)
                self._channels().forget(guild)
        if posted is None:
            posted = self.rest.create_message(channel, payload)
        self.post_files(channel, files)
        for part in more:                       # the rest of a long answer, in order
            self.rest.create_message(channel, {"content": part,
                                               "allowed_mentions": {"parse": []}})
        _log(event="reaction_action", guild=guild, user=user, action=action,
             message=message_id, chars=len(payload.get("content", "")))
        return {"action": action,
                "reply": str((posted or {}).get("id") or "")}

    def _may_act_on_reaction(self, user: str, permissions) -> bool:
        """An operator by id, or a member Discord says manages this server.

        ``permissions`` is the author's guild permissions at the moment of the
        reaction (from ``payload.member``); it is not something the user sends.
        """
        if user and user in self.owners:
            return True
        try:
            bits = int(permissions or 0)
        except (TypeError, ValueError):
            return False
        return bool(bits & (MANAGE_GUILD | ADMINISTRATOR))

    def _reaction_once(self, key: str) -> bool:
        """True the first time ``key`` is seen inside the TTL, then False."""
        now = time.monotonic()
        with self._reaction_lock:
            for old, when in list(self._reaction_seen.items()):
                if now - when > 6 * 3600:
                    del self._reaction_seen[old]
            if key in self._reaction_seen:
                return False
            self._reaction_seen[key] = now
            while len(self._reaction_seen) > 2000:
                self._reaction_seen.pop(next(iter(self._reaction_seen)))
            return True

    def send_dm(self, user_id: str, payload: dict) -> dict:
        """DM one user. Raises RestError on refusal (403 = the member closed DMs)."""
        channel = self.rest.dm_channel(str(user_id))["id"]
        body = dict(payload)
        body.setdefault("allowed_mentions", {"parse": []})
        return self.rest.create_message(channel, body)

    def handle_member_join(self, guild_id: str, member: dict) -> dict:
        """Hand a join to the plugins on in that server (blocking; off the loop).

        A server the resolver does not serve gets nothing from the plugins: a
        join hook can post, DM or reach a model on the owner's account.
        """
        if self.host is None:
            return {}
        if not served_by_brain(getattr(self, "brains", None), guild_id,
                               getattr(self, "served", None)):
            _log(event="member_join_skipped", guild=str(guild_id),
                 reason="unserved_guild")
            return {}
        out = self.host.dispatch_member_join(guild_id, member, self.send_dm)
        _log(event="member_join", guild=str(guild_id), plugins=out)
        return out

    def handle_guild_join(self, guild: dict) -> dict:
        """Hand an arrival to the plugins on in that server (blocking; off the loop).

        An unserved server gets the bring-your-own-inference card instead. The bot
        stays -- it is not ours to leave a server someone added -- but nothing
        there reaches the owner's model.
        """
        guild_id = str((guild or {}).get("id") or "")
        try:
            # The bot channel first: the setup card and every answer land there.
            made = autochannel.ensure_guild(self, guild)
            _log(event="auto_bot_channel", guild=guild_id, outcome=made)
        except Exception as exc:          # noqa: BLE001 - never blocks an arrival
            _log(event="auto_bot_channel_error", guild=guild_id,
                 error=type(exc).__name__)
        if not served_by_brain(getattr(self, "brains", None), guild_id,
                               getattr(self, "served", None)):
            return self._setup_card(guild)
        if self.host is None:
            return {}
        out = self.host.dispatch_guild_join(guild)
        _log(event="guild_join_hooks", guild=guild_id, plugins=out)
        return out

    def _setup_card(self, guild: dict) -> dict:
        """Post the setup card once: in the server's bot channel (containment), or
        with containment off in its system channel."""
        guild_id = str((guild or {}).get("id") or "")
        channel = str((guild or {}).get("system_channel_id") or "")
        if containment.enabled():
            bot = self._channels().for_guild(guild_id)
            if bot:                      # none or unknown: the system channel
                channel = bot
        served = getattr(self, "served", None)
        if served is None or not channel:
            _log(event="setup_card_skipped", guild=guild_id, reason="no_system_channel")
            return {"setup": "skipped"}
        if not served.allow_prompt(served.key(guild_id, "")):
            _log(event="setup_card_skipped", guild=guild_id, reason="rate_limited")
            return {"setup": "skipped"}
        try:
            self.post(channel, SETUP_TEXT)
        except RestError as exc:
            _log(event="setup_card_failed", guild=guild_id, status=exc.status)
            return {"setup": "failed"}
        _log(event="setup_card_posted", guild=guild_id)
        return {"setup": "posted"}

    def handle_guild_remove(self, guild: dict) -> dict:
        """Hand a removal to the plugins that were on in that server."""
        if self.host is None:
            return {}
        out = self.host.dispatch_guild_remove(guild)
        _log(event="guild_remove_hooks", guild=str((guild or {}).get("id") or ""), plugins=out)
        return out

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

    def post_files(self, channel_id: str, files: list[dict]) -> list[str]:
        """Upload the turn's files (``awdiscord.outbox``) after the answer.

        A failed upload never costs the answer that is already posted: it is
        logged and the next file still goes.
        """
        sent: list[str] = []
        for f in files:
            try:
                got = self.rest.upload(channel_id, f.get("filename", "file.bin"),
                                       f.get("data") or b"",
                                       f.get("content_type") or "application/octet-stream",
                                       f.get("caption") or "")
                sent.append(str((got or {}).get("id") or ""))
            except RestError as exc:
                _log(event="upload_failed", channel=channel_id, status=exc.status,
                     file=str(f.get("filename"))[:80])
        if sent:
            _log(event="files_posted", channel=channel_id, files=len(sent))
        return sent

    #: Seconds before a slow answer gets a visible "working on it" reply
    #: (AWDISCORD_ACK_AFTER_S; 0 turns it off). Discord's typing dot lasts ~10 s, so a
    #: member who sees nothing for minutes re-asks or gives up on a turn that is
    #: still running and will answer.
    ACK_TEXT = ("Working on it - the model queue is busy. I'll post the answer "
                "right here when it's ready.")

    def _ack_when_slow(self, msg: dict, ack: dict):
        """Post ACK_TEXT as a reply if the turn is still running after the delay.

        Returns a stop() that cancels a pending ack. The posted message id lands in
        ``ack["id"]`` so the real answer can replace it.
        """
        try:
            delay = float(os.getenv("AWDISCORD_ACK_AFTER_S") or 20)
        except ValueError:
            delay = 20.0
        if delay <= 0:
            return lambda: None
        done = threading.Event()
        lock = ack.setdefault("lock", threading.Lock())

        def fire() -> None:
            if done.wait(delay):
                return
            with lock:              # _drop_ack takes it too: no ack posted after it
                if done.is_set():
                    return
                self._post_ack(msg, ack, delay)

        threading.Thread(target=fire, name="awdiscord-ack", daemon=True).start()
        return done.set

    def _post_ack(self, msg: dict, ack: dict, delay: float) -> None:
        try:
            posted = self.rest.create_message(msg["channel_id"], {
                "content": self.ACK_TEXT, "allowed_mentions": {"parse": []},
                "message_reference": {"message_id": msg.get("id"),
                                      "fail_if_not_exists": False}})
            ack["id"] = str((posted or {}).get("id") or "")
            _log(event="slow_turn_acked", channel=msg["channel_id"], after_s=delay)
        except RestError as exc:              # cosmetic; the answer still comes
            _log(event="slow_ack_failed", status=exc.status)

    def _drop_ack(self, channel_id: str, ack: dict) -> None:
        """Delete the interim ack once the real answer is ready to post."""
        lock = ack.get("lock")
        if lock is None:
            return
        with lock:
            mid = ack.get("id")
        if not mid:
            return
        try:
            self.rest.request("DELETE", f"/channels/{channel_id}/messages/{mid}")
        except RestError as exc:              # a stale ack is noise, never an error
            _log(event="slow_ack_delete_failed", status=exc.status)

    def handle_message(self, msg: dict) -> None:
        """Answer one mention or DM as a reply (blocking; run it off the event loop).

        Outside the server's bot channel the answer is not posted where it was
        asked (``awdiscord.containment``): it goes to the bot channel, or by DM
        when the server has none, and the original message gets a reaction.
        """
        mode, target = self._contained(msg.get("guild_id"), msg["channel_id"],
                                       msg.get("parent_id"))
        if mode != "here":
            self._mark_seen(msg["channel_id"], str(msg.get("id") or ""))
        typing_in = target if mode == "bot" else (msg["channel_id"] if mode == "here" else "")
        try:
            if typing_in:
                self.rest.request("POST", f"/channels/{typing_in}/typing")
        except Exception as exc:                  # noqa: BLE001 -- cosmetic; never block
            _log(event="typing_failed", status=getattr(exc, "status", type(exc).__name__))
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
        turn: dict = {}
        ack: dict = {}
        # A contained answer already has its reaction; a "working on it" reply in
        # the original channel is exactly the output containment keeps out.
        stop_ack = self._ack_when_slow(msg, ack) if mode == "here" else (lambda: None)
        try:
            payload = self.dispatcher.chat(msg["user"], msg.get("guild_id"), msg["content"],
                                           bot_id=msg.get("bot_id", ""),
                                           channel=msg["channel_id"],
                                           attachments=msg.get("attachments") or None,
                                           message_id=str(msg.get("id") or ""),
                                           turn=turn,
                                           role_names=msg.get("role_names"),
                                           author_name=str(msg.get("author_name") or ""))
        finally:
            stop_ack()
        self._drop_ack(msg["channel_id"], ack)
        if payload is None:
            return
        if mode == "dm" and self._deliver_dm(msg, payload):
            return
        if mode == "bot" and self._deliver_bot_channel(msg, target, payload, turn):
            return
        more = payload.pop("continuation", None) or []
        files = payload.pop("files", None) or []
        long_form = payload.pop("long_form", None)
        dm_sent = False
        if long_form:
            # A long answer in a public channel: the channel gets the summary, the
            # asker gets the full text by DM (or a thread when DMs are closed).
            dm_sent = self._dm_long_form(msg, long_form)
            payload["content"] = payload.get("content", "") + (
                "\n-# Full answer sent to you by DM." if dm_sent else
                "\n-# Couldn't DM you (DMs closed?) - the full answer is in the thread.")
        payload["message_reference"] = {"message_id": msg["id"],
                                        "fail_if_not_exists": False}
        posted = self.rest.create_message(msg["channel_id"], payload)
        if long_form and not dm_sent:
            self._thread_long_form(msg, posted, long_form)
        self.post_files(msg["channel_id"], files)
        for part in more:                         # the rest of a long answer, in order
            self.rest.create_message(msg["channel_id"], {
                "content": part, "allowed_mentions": {"parse": []}})
        if turn.get("reply"):
            rows = self._reply_rows(turn, posted, {**msg, "prompt_message_id": msg["id"]})
            if rows:
                try:
                    self.rest.request("PATCH", f"/channels/{msg['channel_id']}/messages/"
                                      f"{posted['id']}", {"components": rows})
                except RestError as exc:      # the answer is out; buttons are extra
                    _log(event="reply_rows_failed", status=exc.status)
        _log(event="mention_answered", user=msg["user"], chars=len(payload["content"]),
             agent=getattr(self.dispatcher.agent_for(msg.get("guild_id"), msg["user"]),
                           "name", ""))

    def _deliver_bot_channel(self, msg: dict, target: str, payload: dict,
                             turn: dict) -> dict | bool:
        """Post a contained answer in the bot channel, mentioning the asker.

        Returns the first posted message (truthy). False when the bot cannot post
        there at all (no Send Messages, channel gone): ``payload`` is untouched and
        the caller answers in place.
        """
        payload = dict(payload)
        guild, user = str(msg.get("guild_id") or ""), str(msg.get("user") or "")
        more = payload.pop("continuation", None) or []
        files = payload.pop("files", None) or []
        long_form = payload.pop("long_form", None)
        if long_form:
            # The bot channel is where long answers are welcome: post them whole.
            more = list(long_form.get("parts") or [])
            payload["content"] = ""
        header = containment.relayed_header(user, guild, msg["channel_id"],
                                            str(msg.get("id") or ""))
        body = payload.get("content", "") or (more.pop(0) if more else "")
        # The header and the start of the answer travel in ONE message: a failure
        # then leaves nothing behind (no orphan "asked in" line), and an answer
        # too long for it overflows into the next parts instead of being cut.
        first, *overflow = split_message(header + ("\n" + body if body else ""))
        more = overflow + list(more)
        payload.pop("message_reference", None)
        payload["content"] = first
        payload["allowed_mentions"] = {"parse": [], "users": [user]} if user else {
            "parse": []}
        try:
            posted = self.rest.create_message(target, payload)
        except RestError as exc:
            _log(event="contained_post_failed", channel=target, status=exc.status)
            self._channels().forget(guild)
            return False
        try:
            self.post_files(target, files)
        except RestError as exc:
            _log(event="contained_files_failed", channel=target, status=exc.status)
        for part in more:
            try:
                self.rest.create_message(target, {"content": part,
                                                  "allowed_mentions": {"parse": []}})
            except RestError as exc:      # the answer is out; a lost tail is logged
                _log(event="contained_part_failed", channel=target, status=exc.status)
        if turn.get("reply") and posted:
            rows = self._reply_rows(turn, posted, {**msg, "channel_id": target,
                                                   "prompt_message_id": msg["id"]})
            if rows:
                try:
                    self.rest.request("PATCH", f"/channels/{target}/messages/"
                                      f"{posted['id']}", {"components": rows})
                except RestError as exc:      # the answer is out; buttons are extra
                    _log(event="reply_rows_failed", status=exc.status)
        _log(event="mention_answered", user=user, contained="bot_channel",
             chars=len(body))
        return posted if isinstance(posted, dict) and posted else {"id": ""}

    def _deliver_dm(self, msg: dict, payload: dict) -> bool:
        """DM a contained answer (the server has no bot channel). False = DMs closed.

        On False nothing was consumed: the caller answers in the original channel.
        """
        guild, user = str(msg.get("guild_id") or ""), str(msg.get("user") or "")
        long_form = payload.get("long_form") or {}
        parts = (list(long_form.get("parts") or []) if long_form
                 else [payload.get("content", "")] + list(payload.get("continuation") or []))
        link = containment.jump_link(guild, msg["channel_id"], str(msg.get("id") or ""))
        head = f"You asked in {link}:"
        try:
            dm = self.rest.dm_channel(user)["id"]
            self.rest.create_message(dm, {"content": head, "allowed_mentions": {"parse": []}})
        except RestError as exc:          # DMs closed: nothing sent, answer in place
            _log(event="contained_dm_failed", user=user, status=exc.status)
            return False
        # The DM is open: from here a failure is logged, never a second full answer
        # in the original channel.
        for part in parts:
            if not part:
                continue
            try:
                self.rest.create_message(dm, {"content": part,
                                              "allowed_mentions": {"parse": []}})
            except RestError as exc:
                _log(event="contained_dm_part_failed", user=user, status=exc.status)
        try:
            self.post_files(dm, payload.get("files") or [])
        except RestError as exc:
            _log(event="contained_dm_files_failed", user=user, status=exc.status)
        _log(event="mention_answered", user=user, contained="dm",
             chars=sum(len(p) for p in parts))
        return True

    def _dm_long_form(self, msg: dict, long_form: dict) -> bool:
        """DM every part of a long public answer to the asker. False on any refusal.

        A partial DM counts as a failure: the thread fallback then carries the
        whole answer, so nothing is ever lost (a repeat beats a gap).
        """
        user = str(long_form.get("user") or msg.get("user") or "")
        parts = list(long_form.get("parts") or [])
        if not user or not parts:
            return False
        head = f"The full answer to your question in <#{msg['channel_id']}>:"
        try:
            self.send_dm(user, {"content": head})
            for part in parts:
                self.send_dm(user, {"content": part})
        except RestError as exc:
            _log(event="long_answer_dm_failed", user=user, status=exc.status)
            return False
        _log(event="long_answer_dm", user=user, parts=len(parts),
             summary=str(long_form.get("how") or ""))
        return True

    def _thread_long_form(self, msg: dict, posted, long_form: dict) -> None:
        """Post a long answer's parts in a thread on the summary; inline as a last resort."""
        channel = msg["channel_id"]
        parts = list(long_form.get("parts") or [])
        anchor = str((posted or {}).get("id") or msg.get("id") or "")
        target = channel
        try:
            thread = self.rest.request(
                "POST", f"/channels/{channel}/messages/{anchor}/threads",
                {"name": "Full answer", "auto_archive_duration": 1440}) or {}
            target = str(thread.get("id") or "") or channel
        except RestError as exc:             # e.g. already inside a thread: post inline
            _log(event="long_answer_thread_failed", status=exc.status)
        for part in parts:
            self.rest.create_message(target, {"content": part,
                                              "allowed_mentions": {"parse": []}})
        _log(event="long_answer_thread", inline=target == channel, parts=len(parts))

    def handle_guild_message(self, msg: dict) -> bool:
        """Offer a human, non-mention server message to the plugins' message hooks
        (blocking; run it off the event loop). True when a hook answered."""
        if not served_by_brain(getattr(self, "brains", None), msg.get("guild_id"),
                               getattr(self, "served", None)):
            return False        # a plugin hook can reach a model; an unserved server may not
        if self.host is None:
            return False
        handled = self.host.dispatch_message(msg)
        if handled:
            _log(event="message_hooked", guild=str(msg.get("guild_id") or ""),
                 user=str(msg.get("user") or ""))
        return handled

    def handle_candidate(self, candidate: Candidate) -> list[dict]:
        """Stage two for one watched-channel message (blocking; off the event loop).

        Judges the message (or the batch it merged into) and hands a confirmed
        ``bug`` to the harness daemon for silent work. It NEVER posts to the
        channel: an unacted candidate produces no reply at all, and a bug is
        answered by a worker, not by the bot. Returns the events it posted.
        An unserved server is never judged: stage two is a model call too.
        """
        guild = str(getattr(candidate, "guild", "") or "")
        if guild and not served_by_brain(getattr(self, "brains", None), guild,
                                         getattr(self, "served", None)):
            _log(event="candidate_skipped", reason="unserved_guild", guild=guild)
            return []
        if self.triage is None:
            _log(event="candidate_skipped", reason="no_triage")
            return []
        try:
            judgements = self.triage.submit(candidate)
        except Exception as exc:      # the judge must never take the gateway down
            _log(event="candidate_error", error=type(exc).__name__)
            return []
        try:
            posted = speculative_requests(judgements, self.harness,
                                          bot_id=self.state.get("user_id")
                                          or self.app_id or "")
        except Exception as exc:      # a harness failure is a logged drop, not a reply
            _log(event="speculative_failed", error=type(exc).__name__)
            return []
        if posted:
            _log(event="speculative_requested", events=len(posted))
        return posted

    async def dispatch_message(self, message, client_user, chat: bool = True) -> None:
        """Route one gateway message: the body of discord.py's ``on_message``.

        Split out of :meth:`run` so the wiring that makes the filter live -- in
        particular the ``candidate`` arm -- is testable without a Discord
        connection. ``message`` is the discord.py Message (or a stand-in with
        the same attributes) and ``client_user`` is the bot's own user.
        """
        if not chat or client_user is None:
            return
        is_dm = message.guild is None
        if message.author.id == client_user.id:
            if is_dm and self.forwarder is not None:     # a card the bot just DMed
                self.forwarder.note_own_message(str(message.channel.id),
                                                _flat_text(message))
            return
        guild_id = str(message.guild.id) if message.guild else None
        channel_id = str(message.channel.id)
        unserved = bool(guild_id and not served_by_brain(
            getattr(self, "brains", None), guild_id, getattr(self, "served", None)))
        route = message_route(
            own=False, author_bot=bool(message.author.bot), is_dm=is_dm,
            mentioned=client_user in message.mentions,
            hooks=bool(self.host is not None and not is_dm
                       and self.host.has_message_hooks(guild_id)),
            candidate=bool(not is_dm and self.watch.is_candidate(channel_id)))
        if route == IGNORE:
            return
        if route == HOOK:
            if unserved:
                _log(event="message_hook_skipped", guild=guild_id, reason="unserved_guild")
                return
            handled = False
            try:
                handled = await asyncio.to_thread(self.handle_guild_message, {
                    "id": str(message.id), "channel_id": str(message.channel.id),
                    "guild_id": guild_id, "user": str(message.author.id),
                    "content": message.content or "",
                    # a hook that ingests a voice note needs the files, not just text
                    "attachments": [_attachment(a) for a in message.attachments or []],
                    # a hook that gates on roles needs them (ids + names)
                    **_member_roles(message.author)})
            except RestError as exc:
                _log(event="message_hook_error", status=exc.status,
                     error=str(exc)[:200])
            if handled or not self.watch.is_candidate(channel_id):
                return
            # A hook that stayed silent must not consume a watched message: the
            # operator's watch policy still gets its one model look.
            route = CANDIDATE
        if route == CANDIDATE:
            if unserved:
                _log(event="candidate_skipped", guild=guild_id, reason="unserved_guild")
                return
            # No mention, no hook, but an operator put this channel on the
            # watch list: judge it off the loop. The bot posts nothing unless
            # the judge says "bug", and then a worker answers, not the bot.
            try:
                await asyncio.to_thread(self.handle_candidate, _candidate(message))
            except RestError as exc:
                _log(event="candidate_error", status=exc.status,
                     error=str(exc)[:200])
            return
        ref_id, ref_text = _reference(message, client_user.id) if is_dm else ("", "")
        try:
            await asyncio.to_thread(self.handle_message, {
                "id": str(message.id), "channel_id": str(message.channel.id),
                "parent_id": str(getattr(message.channel, "parent_id", "") or ""),
                "guild_id": str(message.guild.id) if message.guild else None,
                "user": str(message.author.id), "content": message.content or "",
                "author_name": str(getattr(message.author, "display_name", "")
                                   or getattr(message.author, "name", "") or ""),
                "bot_id": str(client_user.id),
                "reference_id": ref_id, "reference_text": ref_text,
                "attachments": [_attachment(a) for a in message.attachments or []],
                # Same roles a slash command carries, so a capability-gated tool
                # resolves the member identically on the mention path.
                "role_names": (_member_roles(message.author)["role_names"]
                               if message.guild else [])})
        except RestError as exc:
            _log(event="message_error", status=exc.status, error=str(exc)[:200])

    def run(self) -> None:
        try:
            import discord
        except ImportError:
            sys.exit("the gateway needs discord.py: pip install 'awdiscord[gateway]'")
        self.prepare()
        if self.inbox_port:
            self._start_inbox()
        intents, chat, members = build_intents(discord)
        reactions = bool(getattr(intents, "guild_reactions", False))
        client = discord.Client(intents=intents)
        bot = self
        voice = getattr(bot, "voice", None)
        if voice is not None:
            voice.attach(client)

        @client.event
        async def on_message(message):
            await bot.dispatch_message(message, client.user, chat)

        @client.event
        async def on_ready():
            bot.state.update(connected=True, guilds=len(client.guilds), since=time.time(),
                             user_id=str(client.user.id))
            _log(event="ready", bot=str(client.user), guilds=len(client.guilds))
            if voice is not None:
                try:
                    await voice.on_ready()
                except Exception as exc:      # voice must never take the gateway down
                    _log(event="voice_ready_error", error=type(exc).__name__,
                         detail=str(exc)[:200])
            # A sweep/backfill daemon needs a start that is not "the first human
            # message": a quiet night has none, and a note posted into it would wait.
            if not bot.state.get("auto_bot_channel_swept"):
                bot.state["auto_bot_channel_swept"] = True
                try:
                    swept = await asyncio.to_thread(autochannel.sweep, bot)
                    _log(event="auto_bot_channel_sweep", outcomes=swept)
                except Exception as exc:      # noqa: BLE001 - never fatal
                    _log(event="auto_bot_channel_error", error=type(exc).__name__)
            try:
                started = await asyncio.to_thread(bot.start_plugin_daemons)
                if started:
                    _log(event="plugin_daemons", started=started)
            except Exception as exc:          # noqa: BLE001 - never fatal
                _log(event="plugin_daemon_error", error=type(exc).__name__,
                     detail=str(exc)[:200])

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
            try:
                await asyncio.to_thread(bot.handle_guild_join, {
                    "id": str(guild.id), "name": str(getattr(guild, "name", "") or ""),
                    "owner_id": str(getattr(guild, "owner_id", "") or ""),
                    "system_channel_id": str(getattr(guild, "system_channel_id", "") or "")})
            except Exception as exc:          # a plugin hook never takes the gateway down
                _log(event="guild_join_hook_error", error=type(exc).__name__,
                     detail=str(exc)[:200])

        def _channel_changed(channel) -> None:
            # A new, renamed or deleted channel may be (or have been) the bot
            # channel: resolve that server again on its next message.
            guild = getattr(channel, "guild", None)
            if guild is not None:
                bot._channels().forget(str(guild.id))
                _log(event="bot_channel_cache_dropped", guild=str(guild.id))

        @client.event
        async def on_guild_channel_create(channel):
            _channel_changed(channel)

        @client.event
        async def on_guild_channel_delete(channel):
            _channel_changed(channel)

        @client.event
        async def on_guild_channel_update(before, after):
            if getattr(before, "name", None) != getattr(after, "name", None):
                _channel_changed(after)

        @client.event
        async def on_guild_remove(guild):
            bot.state["guilds"] = len(client.guilds)
            _log(event="guild_remove", guild=str(guild.id))
            try:
                await asyncio.to_thread(bot.handle_guild_remove, {
                    "id": str(guild.id), "name": str(getattr(guild, "name", "") or ""),
                    "owner_id": str(getattr(guild, "owner_id", "") or ""),
                    "system_channel_id": str(getattr(guild, "system_channel_id", "") or "")})
            except Exception as exc:          # a plugin hook never takes the gateway down
                _log(event="guild_remove_hook_error", error=type(exc).__name__,
                     detail=str(exc)[:200])

        if voice is not None:
            @client.event
            async def on_voice_state_update(member, before, after):
                # A drag is a command: follow the bot wherever a human puts it.
                try:
                    await voice.on_voice_state_update(member, before, after)
                except Exception as exc:
                    _log(event="voice_state_error", error=type(exc).__name__,
                         detail=str(exc)[:200])

        if members:
            @client.event
            async def on_member_join(member):
                if getattr(member, "bot", False) or member.guild is None:
                    return
                try:
                    await asyncio.to_thread(bot.handle_member_join, str(member.guild.id), {
                        "user": {"id": str(member.id), "username": str(member.name),
                                 "global_name": str(getattr(member, "global_name", "")
                                                    or "")}})
                except RestError as exc:
                    _log(event="member_join_error", status=exc.status, error=str(exc)[:200])

        if reactions:
            async def _reaction(payload, added: bool):
                if client.user is None or payload.user_id == client.user.id:
                    return
                member = getattr(payload, "member", None)
                if member is not None and getattr(member, "bot", False):
                    return
                try:
                    await asyncio.to_thread(bot.handle_reaction, {
                        "guild_id": str(payload.guild_id or ""),
                        "channel_id": str(payload.channel_id),
                        "message_id": str(payload.message_id),
                        "user": str(payload.user_id), "emoji": str(payload.emoji),
                        "added": added,
                        # An operator/admin reaction trigger needs the member's
                        # permissions at the moment of the reaction. Absent in a
                        # DM, hence 0 (never an owner gate on a payload).
                        "permissions": str(getattr(
                            getattr(member, "guild_permissions", None), "value", 0) or 0),
                        "user_bot": bool(getattr(member, "bot", False)),
                        "author": str(getattr(payload, "message_author_id", "") or "")})
                except RestError as exc:
                    _log(event="reaction_error", status=exc.status, error=str(exc)[:200])

            @client.event
            async def on_raw_reaction_add(payload):
                await _reaction(payload, True)

            @client.event
            async def on_raw_reaction_remove(payload):
                await _reaction(payload, False)

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
                # The internal key opens the two fixed-destination notify routes
                # only, never cards or /post (which names any channel).
                key = self.headers.get("X-Internal-Key", "")
                return (self.path in ("/notify/owner", "/notify/channel")
                        and bool(bot._internal_key)
                        and hmac.compare_digest(key, bot._internal_key))

            def do_GET(self):
                if self.path == "/health":
                    self._send(200 if bot.state["connected"] else 503, bot.state)
                elif self.path == "/voice/status":
                    if not self._authed():
                        self._send(401, {"error": "bearer required"})
                    elif bot.voice is None:
                        self._send(409, {"error": "voice is not enabled"})
                    else:
                        self._send(200, bot.voice.status())
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
                    elif self.path == "/notify/channel":
                        out = bot.notify_channel(str(body.get("message") or body["content"]))
                    elif self.path == "/voice/join":
                        if bot.voice is None:
                            self._send(409, {"error": "voice is not enabled"})
                            return
                        bot.voice._schedule(bot.voice.join_channel(
                            str(body["guild_id"]), str(body["channel_id"])))
                        out = {"joining": str(body["channel_id"])}
                    elif self.path == "/voice/leave":
                        if bot.voice is None:
                            self._send(409, {"error": "voice is not enabled"})
                            return
                        bot.voice._schedule(bot.voice.leave(str(body["guild_id"])))
                        out = {"leaving": str(body["guild_id"])}
                    else:
                        self._send(404, {"error": "not found"})
                        return
                    self._send(200, out)
                except (KeyError, ValueError) as exc:
                    self._send(400, {"error": str(exc)[:200]})
                except RestError as exc:
                    self._send(502, {"error": str(exc)[:200]})
                except NoOpsChannelError as exc:
                    # The ONE "no channel" signal callers may treat as skipped
                    # (lib.core.owner_discord matches this reason, not a bare status).
                    self._send(409, {"error": str(exc)[:200], "reason": NO_OPS_CHANNEL})
                except RuntimeError as exc:
                    self._send(409, {"error": str(exc)[:200]})

        server = ThreadingHTTPServer((self.inbox_host, self.inbox_port), Handler)
        self.inbox_port = server.server_address[1]
        self.inbox_server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _log(event="inbox", host=self.inbox_host, port=self.inbox_port)


def build_intents(discord) -> tuple:
    """(intents, chat on, member events on) for this bot's env.

    Reaction events (``AWDISCORD_REACTIONS=1``) are not privileged; they are off by
default and then reach the plugins' ``on_reaction`` hooks.

Mentions and DMs are non-privileged: a message that mentions the bot carries
    its text even without the message-content intent. Member events need the
    PRIVILEGED members intent (switched on in the developer portal too), so it is
    requested only when ``AWDISCORD_MEMBERS=1``; Discord refuses the session
    otherwise.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    # Voice state events are NOT privileged. They are wanted only when voice is
    # on: they are what turns "a human dragged the bot into a channel" into a
    # join, and what reports who else is in the room.
    if _env("AWDISCORD_VOICE").lower() in ("1", "true", "yes", "on"):
        intents.voice_states = True
    chat = _env("AWDISCORD_MENTIONS", default="1") not in ("0", "false", "no")
    intents.guild_messages = chat
    intents.dm_messages = chat
    members = _env("AWDISCORD_MEMBERS") in ("1", "true", "yes")
    intents.members = members
    # Plugin message hooks read messages that do not mention the bot; Discord
    # sends their text only with the PRIVILEGED message-content intent.
    intents.message_content = chat and _env("AWDISCORD_MESSAGE_CONTENT") in (
        "1", "true", "yes")
    # Plugin reaction hooks (a feedback pack's thumbs). Not privileged, but OFF
    # unless asked for, so a bot that never reads reactions is not sent them.
    reactions = _env("AWDISCORD_REACTIONS") in ("1", "true", "yes")
    intents.guild_reactions = reactions
    intents.dm_reactions = reactions
    return intents, chat, members


def _raw_interaction(interaction) -> dict:
    """The dict Discord sent, rebuilt from discord.py's Interaction."""
    user = {"id": str(interaction.user.id)}
    # The display name rides along so a plugin can attribute work to a person
    # without ever printing the numeric id (an idea's issue body says "by Ana").
    for attr in ("username", "global_name"):
        value = getattr(interaction.user, "name" if attr == "username" else attr, None)
        if isinstance(value, str) and value:
            user[attr] = value
    raw = {"id": str(interaction.id), "token": interaction.token,
           "type": interaction.type.value, "data": interaction.data or {},
           "channel_id": str(interaction.channel_id or "")}
    parent = getattr(getattr(interaction, "channel", None), "parent_id", None)
    if parent:                      # a thread: containment judges its parent channel
        raw["channel_parent_id"] = str(parent)
    if interaction.guild_id:
        raw["guild_id"] = str(interaction.guild_id)
        # The invoker's resolved permissions in this channel: plugins gate
        # manager-only commands on Manage Server, so it must survive the rebuild.
        perms = getattr(interaction, "permissions", None)
        raw["member"] = {"user": user, "permissions": str(getattr(perms, "value", 0) or 0),
                         **_member_roles(interaction.user)}
        guild = getattr(interaction, "guild", None)
        if getattr(guild, "name", None):
            raw["guild_name"] = str(guild.name)
    else:
        raw["user"] = user
    return raw


def _member_roles(member) -> dict:
    """The invoker's guild roles, as Discord sends them (``roles``: id strings).

    discord.py turns them into Role objects; a plugin that maps roles to what a
    member may do needs them back. ``@everyone`` is left out (every member has it,
    so it can never distinguish one). ``role_names`` rides alongside because a
    role mapping written by a person names roles, not snowflakes.
    """
    roles = [r for r in (getattr(member, "roles", None) or [])
             if not (callable(getattr(r, "is_default", None)) and r.is_default())]
    return {"roles": [str(r.id) for r in roles],
            "role_names": [str(getattr(r, "name", "")) for r in roles]}
