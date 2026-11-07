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

from awdiscord.agent import A2AAgent, OpenAICompatAgent
from awdiscord.approvals import ApprovalDesk
from awdiscord.brain import served_by_brain
from awdiscord.dispatch import (
    ADMINISTRATOR,
    EPHEMERAL,
    MANAGE_GUILD,
    MESSAGE_COMMAND,
    Dispatcher,
    clip,
    commands,
    normalize_emoji,
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
        _log(event="interaction", type=interaction.get("type"), **result.log)
        self.rest.interaction_callback(interaction["id"], interaction["token"], result.response)
        if result.deferred is not None:
            payload = result.deferred()
            more = payload.pop("continuation", None) or []
            files = payload.pop("files", None) or []
            # An interaction edit carries text only, and a declared file in a
            # private reply would leak it into a public channel: say so instead.
            if files and not (payload.get("flags") or 0) & EPHEMERAL:
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
                    **({"flags": payload["flags"]} if payload.get("flags") else {})})
            for part in more:                     # the rest of a long answer
                self.rest.followup(self.app_id, interaction["token"], {
                    "content": part, "allowed_mentions": {"parse": []},
                    **({"flags": payload["flags"]} if payload.get("flags") else {})})
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
        if not served_by_brain(getattr(self, "brains", None), guild_id,
                               getattr(self, "served", None)):
            return self._setup_card(guild)
        if self.host is None:
            return {}
        out = self.host.dispatch_guild_join(guild)
        _log(event="guild_join_hooks", guild=guild_id, plugins=out)
        return out

    def _setup_card(self, guild: dict) -> dict:
        """Post the setup card once, in the server's system channel if it has one."""
        guild_id = str((guild or {}).get("id") or "")
        channel = str((guild or {}).get("system_channel_id") or "")
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
        turn: dict = {}
        payload = self.dispatcher.chat(msg["user"], msg.get("guild_id"), msg["content"],
                                       bot_id=msg.get("bot_id", ""),
                                       channel=msg["channel_id"],
                                       attachments=msg.get("attachments") or None,
                                       message_id=str(msg.get("id") or ""),
                                       turn=turn)
        if payload is None:
            return
        more = payload.pop("continuation", None) or []
        files = payload.pop("files", None) or []
        payload["message_reference"] = {"message_id": msg["id"],
                                        "fail_if_not_exists": False}
        posted = self.rest.create_message(msg["channel_id"], payload)
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
                "guild_id": str(message.guild.id) if message.guild else None,
                "user": str(message.author.id), "content": message.content or "",
                "bot_id": str(client_user.id),
                "reference_id": ref_id, "reference_text": ref_text,
                "attachments": [_attachment(a) for a in message.attachments or []]})
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
    raw = {"id": str(interaction.id), "token": interaction.token,
           "type": interaction.type.value, "data": interaction.data or {},
           "channel_id": str(interaction.channel_id or "")}
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
