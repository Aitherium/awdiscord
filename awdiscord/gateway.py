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
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from awdiscord import (
    autochannel,
    containment,
    intake,
    mirror,
    profile_avatar,
    quiet,
    turn_context,
)
from awdiscord.agent import AGENT_SOURCE, A2AAgent, OpenAICompatAgent
from awdiscord.approvals import ApprovalDesk
from awdiscord.brain import served_by_brain
from awdiscord.dispatch import (
    ADMINISTRATOR,
    ASK_MODAL_PREFIX,
    CLAIM_CMD,
    COMMAND,
    EPHEMERAL,
    MANAGE_GUILD,
    MESSAGE_COMMAND,
    STILL_WORKING_TEXT,
    Dispatcher,
    can_read_history,
    commands,
    explicit_private,
    normalize_emoji,
    parse_message_link,
    reply_visibility,
    split_message,
    with_footer,
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


def agent_route_summary(agent) -> dict:
    """The turn hints the bot's agent sends, for one startup log line. No URLs, no keys."""
    return {"event": "agent_route", "kind": type(agent).__name__,
            "model": getattr(agent, "model", None) or getattr(agent, "name", None),
            "priority": getattr(agent, "priority", None),
            "source": getattr(agent, "source", None)
            or (AGENT_SOURCE if getattr(agent, "priority", None) else None)}


def _flat_text(message) -> str:
    """A message's text plus its embeds (cards often carry their id in one)."""
    parts = [message.content or ""]
    for e in getattr(message, "embeds", None) or []:
        parts += [getattr(e, "title", None) or "", getattr(e, "description", None) or ""]
        footer = getattr(e, "footer", None)
        parts.append(getattr(footer, "text", None) or "")
        parts += [f"{f.name} {f.value}" for f in getattr(e, "fields", None) or []]
    return "\n".join(p for p in parts if p)


def _iso_epoch(stamp) -> float:
    """A Discord ISO timestamp as epoch seconds; 0.0 when it does not parse."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _snapshot_text(message) -> str:
    """The text of a forwarded message: a Discord "Forwarded" message has empty
    ``content`` and carries the original in ``message_snapshots`` (content + embeds)."""
    parts = [_flat_text(s) for s in getattr(message, "message_snapshots", None) or []]
    return "\n".join(p for p in parts if p).strip()


def _raw_snapshot_text(m: dict) -> str:
    """``_snapshot_text`` for a REST message dict (``message_snapshots[].message``)."""
    parts = [_raw_text(s.get("message") or {}) for s in m.get("message_snapshots") or []]
    return "\n".join(p for p in parts if p).strip()


def _raw_text(m: dict) -> str:
    """A REST message dict's text plus its embeds: the dict form of ``_flat_text``."""
    return "\n".join([m.get("content") or ""] + [
        "\n".join(str(x or "") for x in (e.get("title"), e.get("description"),
                                          (e.get("footer") or {}).get("text")))
        for e in m.get("embeds") or []])


_USER_MENTION = re.compile(r"<@!?(\d{15,21})>")


def name_mentions(text: str, people: dict, skip: str = "") -> str:
    """``<@id>`` -> ``<@id> (Display Name, @username)`` for every mentioned person.

    Discord hands the bot a mention as a bare id. 2026-11-16: the owner wrote "make plans
    with <@Rested Mind> and <@Patience>"; the agent resolved one id by tool, not the
    other, and answered that "the provided ID resolves to only one person". The names
    ride in the text now, so no tool call is needed to know who was named. ``people`` =
    {id: (display, username)}; ``skip`` = the bot's own id (stripped elsewhere). Pure.
    """
    if not text or not people:
        return text or ""

    def named(m: "re.Match") -> str:
        uid = m.group(1)
        if uid == skip or uid not in people:
            return m.group(0)
        display, username = people[uid]
        label = ", ".join(x for x in (display, f"@{username}" if username and
                                      username != display else "") if x)
        return f"{m.group(0)} ({label})" if label else m.group(0)
    return _USER_MENTION.sub(named, text)


def _mentioned_people(message) -> dict:
    """{id: (display name, username)} of a discord.py message's user mentions."""
    out = {}
    for u in getattr(message, "mentions", None) or []:
        out[str(u.id)] = (str(getattr(u, "display_name", "") or getattr(u, "name", "") or ""),
                          str(getattr(u, "name", "") or ""))
    return out


def _raw_mentioned_people(m: dict) -> dict:
    """``_mentioned_people`` for a REST message dict (``mentions[]``, with ``member.nick``)."""
    out = {}
    for u in m.get("mentions") or []:
        nick = str(((u.get("member") or {}).get("nick")) or "")
        out[str(u.get("id") or "")] = (nick or str(u.get("global_name") or u.get("username")
                                                   or ""), str(u.get("username") or ""))
    return out


#: Discord's cap on chat-input (type 1) commands per scope; message/user menus have
#: their own 5-each caps.
MAX_CHAT_COMMANDS = 100


def split_commands(cmds: list[dict], cap: int = MAX_CHAT_COMMANDS) -> tuple[list, list]:
    """(global, overflow): at most ``cap`` chat commands go global.

    2026-11-16: the develop brick plus the mounted packs reached 104 chat commands and
    Discord answered PUT with 30032 "Maximum number of application commands reached",
    so the bot exited at boot. The overflow is taken from the commands an ordinary
    member cannot run anyway (``default_member_permissions`` set), last first, then
    from the end of the list; the caller registers it in the home server. Pure.
    """
    chat = [c for c in cmds if int(c.get("type", 1) or 1) == 1]
    excess = len(chat) - cap
    if excess <= 0:
        return list(cmds), []
    restricted = [c for c in reversed(chat) if c.get("default_member_permissions")]
    plain = [c for c in reversed(chat) if not c.get("default_member_permissions")]
    moved = (restricted + plain)[:excess]
    ids = {id(c) for c in moved}
    return [c for c in cmds if id(c) not in ids], [c for c in cmds if id(c) in ids]


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


def _replied(message) -> dict | None:
    """The message ``message`` replies to, as a REST-shaped dict (any author).

    ``None`` when it is not a reply. When the gateway did not resolve the target
    (deleted, or not cached) the dict carries only ``id`` and ``channel_id`` and
    ``Bot._turn_context`` fetches it.
    """
    ref = getattr(message, "reference", None)
    mid = getattr(ref, "message_id", None)
    if ref is None or mid is None:
        return None
    out = {"id": str(mid),
           "channel_id": str(getattr(ref, "channel_id", "") or "")}
    resolved = getattr(ref, "resolved", None)
    author = getattr(resolved, "author", None)
    if author is None:                      # deleted or unresolved: fetch it later
        return out
    out["author"] = {"id": str(getattr(author, "id", "") or ""),
                     "username": str(getattr(author, "name", "") or ""),
                     "global_name": str(getattr(author, "display_name", "") or ""),
                     "bot": bool(getattr(author, "bot", False))}
    out["content"] = _flat_text(resolved)
    return out


def _reply_images(message) -> list[dict]:
    """Image attachments of the message ``message`` replies to (any author).

    "what's in this?" sent as a reply to a picture carried no picture: only the
    reply's OWN attachments reached the model, so it read the words alone and
    reached for image generation instead of looking. Empty when the reply is not
    to a resolved message, or that message holds no image.
    """
    ref = getattr(message, "reference", None)
    parent = getattr(ref, "resolved", None) if ref is not None else None
    out: list[dict] = []
    for a in getattr(parent, "attachments", None) or []:
        att = _attachment(a)
        if str(att.get("content_type") or "").lower().startswith("image/"):
            out.append(att)
    return out


def _turn_attachments(message) -> list[dict]:
    """The files a mention turn carries: its own, else the images it replies to."""
    own = [_attachment(a) for a in message.attachments or []]
    return own or _reply_images(message)


_THREAD_TYPES = {10, 11, 12}               # news, public and private thread


def _is_thread(channel) -> bool:
    """True when ``channel`` is a thread (``discord.Thread`` or a thread ``type``)."""
    kind = getattr(channel, "type", None)
    value = getattr(kind, "value", kind)
    try:
        return int(value) in _THREAD_TYPES
    except (TypeError, ValueError):
        return "thread" in str(kind or "").lower()


def _everyone_can_view(channel, guild) -> bool:
    """True only when @everyone may read ``channel`` (False when unknown).

    A private thread is never public; a public thread inherits its parent's
    overwrites through ``permissions_for``. Fails closed: a contained answer
    then quotes nothing but the asker's own words.
    """
    if guild is None or channel is None:
        return False
    kind = getattr(channel, "type", None)
    try:
        if int(getattr(kind, "value", kind)) == 12:     # private thread
            return False
    except (TypeError, ValueError):
        if "private" in str(kind or "").lower():
            return False
    try:
        return bool(channel.permissions_for(guild.default_role).view_channel)
    except Exception:  # noqa: BLE001 -- unknown means private
        return False


IGNORE, CHAT, HOOK, CANDIDATE = "ignore", "chat", "hook", "candidate"


def mentions_bot(message, client_user) -> bool:
    """True when a message addresses the bot: by its user, or by its OWN managed role.

    Discord gives every bot a managed role carrying the bot's name, and the mention
    picker offers that role right beside the user. A question asked through the role
    arrives with the bot in ``role_mentions`` and NOT in ``mentions``, so reading
    ``mentions`` alone drops it (2026-11-15: "@Aither what is zero-authy" sat
    unanswered in #general). Only the role whose ``tags.bot_id`` is this bot counts:
    any other role mention is not a call to the bot.
    """
    if _addressed_directly(message, client_user):
        return True
    # A REPLY carries the call in the message it answers. Owner, 2026-11-17: Marjery
    # replied to Tom's "@Marjery @Aither ..." with "Aither, explain ..." and Aither said
    # nothing -- the only @Aither was in the PARENT. Reading just the current message
    # drops every conversation where people answer the mention instead of repeating it.
    # `resolved` is absent when Discord did not attach the parent; a message is never
    # its own reference, so this cannot loop.
    ref = getattr(getattr(message, "reference", None), "resolved", None)
    if ref is not None and ref is not message:
        return _addressed_directly(ref, client_user)
    return False


def _addressed_directly(message, client_user) -> bool:
    """The mention check for ONE message: the bot's user, or its own managed role."""
    if client_user in (getattr(message, "mentions", None) or []):
        return True
    me = str(getattr(client_user, "id", "") or "")
    if not me:
        return False
    for role in getattr(message, "role_mentions", None) or []:
        if str(getattr(getattr(role, "tags", None), "bot_id", "") or "") == me:
            return True
    return False


class PeerBots:
    """Bots this bot may answer, held to a per-channel turn budget.

    Every bot author is ignored by default, so two bots never feed each other.
    An operator may name peers (``AWDISCORD_PEER_BOTS``, comma-separated user
    ids): a peer that addresses this bot DIRECTLY in a server (a mention, or a
    reply to it) is answered like a member, at most ``AWDISCORD_PEER_TURNS``
    times per channel in ``AWDISCORD_PEER_WINDOW_S`` seconds (8 per 15 min), so
    a volley between two models ends on its own instead of running forever.
    """

    def __init__(self, ids=(), turns: int = 8, window_s: float = 900.0,
                 clock=time.monotonic):
        self.ids = {str(i).strip() for i in ids if str(i).strip()}
        self.turns = max(0, int(turns))
        self.window_s = max(1.0, float(window_s))
        self._clock = clock
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> PeerBots:
        ids = [x for x in _env("AWDISCORD_PEER_BOTS").replace(" ", "").split(",") if x]
        try:
            turns = int(_env("AWDISCORD_PEER_TURNS", default="8"))
        except ValueError:
            turns = 8
        try:
            window = float(_env("AWDISCORD_PEER_WINDOW_S", default="900"))
        except ValueError:
            window = 900.0
        return cls(ids, turns=turns, window_s=window)

    def admit(self, author_id: str, channel_id: str) -> bool:
        """True (and one turn spent) when this peer may be answered here now."""
        if str(author_id) not in self.ids or self.turns <= 0:
            return False
        now = self._clock()
        with self._lock:
            recent = [t for t in self._seen.get(channel_id, []) if now - t < self.window_s]
            admitted = len(recent) < self.turns
            if admitted:
                recent.append(now)
            self._seen[channel_id] = recent
        if not admitted:
            _log(event="peer_bot_budget_spent", channel=channel_id, author=str(author_id),
                 turns=self.turns, window_s=self.window_s)
        return admitted


def message_route(*, own: bool, author_bot: bool, is_dm: bool, mentioned: bool,
                  hooks: bool, candidate: bool = False, peer: bool = False) -> str:
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
    it -- and no bot can feed the judge. The one exception is ``peer``: a bot the
    operator named in ``AWDISCORD_PEER_BOTS`` that addressed this bot directly in
    a server, within its turn budget (``PeerBots.admit``), is answered as chat.
    Stage one stays pure and deterministic: no model is called here.
    """
    if own:
        return IGNORE
    if author_bot:
        return CHAT if (peer and mentioned and not is_dm) else IGNORE
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
                 served: ServedGuilds | None = None, brains=None, solver=None):
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
            # Plugins tell the owner about things that need a person (an access
            # request, a network application) through the same DM path as alerts.
            host.notify_owners = self.notify_owners
        self.forwarder = forwarder  # awdiscord.forward.DmForwarder, or None
        # "solve this": a persona DM turns the alert it just showed into a harness
        # session (awdiscord.solve); None when this bot is not configured for it.
        self.solver = solver
        # DM channel id -> the newest thing that DM is about (forwarded text, an alert
        # page, a card): what "solve this" refers to.
        self._dm_context: dict[str, dict] = {}
        # DM channels with a solve being started right now: a second "solve this"
        # typed while the first is still POSTing must not start a second session.
        self._solve_inflight: set[str] = set()
        self._solve_lock = threading.Lock()
        # Stage 1 of the trigger filter: which channels are watched, and (stage 2)
        # the judge that decides whether a watched message needs work. Both are
        # inert unless AWDISCORD_WATCH_CHANNELS names a channel.
        self.watch = watch if watch is not None else WatchPolicy.from_env()
        # Bots we may answer (AWDISCORD_PEER_BOTS); empty = every bot ignored.
        self.peers = PeerBots.from_env()
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
        # Both paths carry the same turn hints (agent.env_agent_model/priority): model
        # bonsai2-27b, priority user, source chat:awdiscord. Logged so the deployed
        # choice is visible without reading the env.
        agent = agent or A2AAgent.from_env() or OpenAICompatAgent.from_env()
        _log(**agent_route_summary(agent))
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
        # when the allowlist is unset, empty or unparseable. The SAME Genesis
        # client serves the account-linked gate and the hosted resolver, so one
        # object owns the URL, the fleet CA and the bot key.
        from awdiscord.brain import BrainResolver, GenesisInferenceClient
        try:
            genesis_client = GenesisInferenceClient.from_env()
        except Exception:                 # noqa: BLE001 - absent config is none
            genesis_client = None
        served = ServedGuilds.from_env(client=genesis_client)
        _log(event="served_guilds", home=served.home,
             served=sorted(served.guilds), problem=served.problem)
        _log(event="served_binding", live=served.binding_live,
             ttl=served.binding_ttl)
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
        from awdiscord.brainsecrets import BrainSecrets
        tool_loop = _env("AWDISCORD_OWNER_TOOL_LOOP").lower() in ("1", "true", "yes")
        standalone = _env("AWDISCORD_STANDALONE").lower() in ("1", "true", "yes")
        try:
            # Genesis is the authority for a guild's hosted record. With no URL
            # configured this is None and a hosted server simply resolves to
            # none (the feature is off unless AWDISCORD_HOSTED=1 anyway).
            brains = BrainResolver(
                agent, owner_agent=owner_agent, owners=owners,
                owner_tool_loop=tool_loop, served=served, store=store,
                secrets=BrainSecrets(state),
                legacy_agent=agent if standalone else None,
                hosted=False if standalone else None,
                hosted_client=genesis_client)
        except Exception as exc:          # noqa: BLE001 - fail closed, never open
            from awdiscord.brain import DenyResolver
            _log(event="brain_resolver", problem=type(exc).__name__)
            brains = DenyResolver()
        from awdiscord import solve as _solve
        solver = _solve.Solver.from_env(state_dir=str(state))
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
                   served=served, brains=brains, solver=solver)

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
        home = self.dev_guild or _env("AITHER_DISCORD_HOME_GUILD_ID")
        glob, overflow = split_commands(cmds)
        self.rest.put_commands(self.app_id, glob)
        if overflow:
            _log(event="commands_overflow", global_count=len(glob), overflow=len(overflow),
                 names=[c.get("name") for c in overflow], home=bool(home))
        if home and (overflow or self.dev_guild):
            # Discord allows 100 chat commands per scope. The overflow lives in the
            # home server (operators' commands first), never a crash at boot.
            self.rest.put_commands(self.app_id, overflow if overflow else cmds,
                                   guild_id=home)
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

    def _after_deferred(self, interaction: dict, result) -> None:
        """Forget a cached binding verdict once a claim's deferred work is done.

        ``/aitherium setup`` and its button acknowledge first and write the
        binding in deferred work, which runs here. Without this the server an
        admin just claimed keeps reading "not served" from the TTL cache for up
        to ``AWDISCORD_BINDING_TTL`` seconds. A failed claim only costs one
        extra Genesis read.
        """
        guild = str(interaction.get("guild_id") or "")
        served = getattr(self, "served", None)
        if not guild or served is None:
            return
        log = getattr(result, "log", None) or {}
        if str(log.get("plugin") or "") == CLAIM_CMD:
            served.invalidate(guild)

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
            self._after_deferred(interaction, result)
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
                 parts=1 + len(more), turn=result.turn.get("turn_id", ""),
                 tools=[c.get("tool") for c in result.turn.get("tools") or []])
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
        if self._wants_conversation(interaction):
            # /ask and its forms read the conversation LAZILY, inside the deferred
            # work: the 3-second acknowledgement never waits on a history fetch.
            original = interaction
            return {**interaction, "_conversation": lambda mode="recent", link="", anchor="":
                    self._ask_conversation(original, mode, link=link, anchor=anchor)}
        if (interaction.get("type") != 2 or data.get("type") != MESSAGE_COMMAND
                or "summarize" not in str(data.get("name") or "").lower()):
            return interaction
        context = self._channel_context(
            str(interaction.get("channel_id") or ""),
            bot_id=str(interaction.get("application_id") or self.app_id or ""))
        if not context:
            return interaction
        return {**interaction, "_context": context}

    @staticmethod
    def _wants_conversation(interaction: dict) -> bool:
        """``/ask`` (chat input) or one of the ask forms (``core:ask:...``)."""
        data = interaction.get("data") or {}
        if interaction.get("type") == COMMAND:
            return int(data.get("type") or 1) == 1 and data.get("name") == "ask"
        return (interaction.get("type") == MODAL_SUBMIT
                and str(data.get("custom_id") or "").startswith(ASK_MODAL_PREFIX))

    def _channel_messages(self, channel_id: str, limit: int = 50, **where) -> list:
        """One page of a channel's messages (REST dicts), or [] on any failure.

        ``where`` = Discord's ``before``/``after``/``around`` message id. The one
        history fetch: the summarize command, /ask and the ask forms all use it.
        """
        if not channel_id:
            return []
        query = f"limit={max(1, min(100, int(limit)))}"
        for key in ("before", "after", "around"):
            if where.get(key):
                query += f"&{key}={int(str(where[key]))}"
        try:
            messages = self.rest.request(
                "GET", f"/channels/{channel_id}/messages?{query}") or []
        except RestError as exc:
            _log(event="context_fetch_failed", status=exc.status)
            return []
        return messages if isinstance(messages, list) else []

    def _channel_context(self, channel_id: str, limit: int = 50, bot_id: str = "") -> str:
        """The channel's newest ``limit`` messages as ``who: text`` lines, oldest first.

        Formatted by ``turn_context.transcript``, the one formatter for quoted
        channel lines: credentials redacted, markers defused, names unable to
        forge a bot label, abuse-flagged lines left out.
        """
        messages = self._channel_messages(channel_id, limit)
        if not messages:
            return ""
        return turn_context.transcript(messages, bot_id=bot_id, limit=400)

    def _ask_conversation(self, it: dict, mode: str = "recent", link: str = "",
                          anchor: str = "") -> tuple[str, int, str]:
        """What an ``/ask`` reads: ``(context block, messages quoted, footer)``.

        Privacy: only a channel the ASKER can read. Here that is Discord's own
        resolved ``member.permissions`` for this channel (View Channel and Read
        Message History); another channel only when the asker linked a message
        in it AND the gateway's cache says they can read it there (``_can_read``),
        and never a private thread they were not shown. Every fetch is
        best-effort: a failure costs the context, never the answer.
        """
        channel = str(it.get("channel_id") or "")
        guild = str(it.get("guild_id") or "")
        bot_id = str(it.get("application_id") or self.app_id or "")
        if mode == "none" or not guild or not channel:
            return "", 0, ""
        if "permissions" not in (it.get("member") or {}):
            return "", 0, ""                # unknown permissions: read nothing, say nothing
        if not can_read_history(it):
            return "", 0, "-# (did not read this channel: you can't read its history)"
        limit, note, where = turn_context.RECENT_LIMIT, "", "here"
        starter = None
        messages: list = []
        if mode == "since-link":
            ref = parse_message_link(link)
            if ref is None or ref[0] != guild:
                mode, note = "recent", " - no message link from this server was given"
            else:
                _, linked_channel, linked = ref
                if linked_channel not in (channel, str(it.get("channel_parent_id") or "")):
                    can = it.get("_can_read")
                    if not (callable(can) and can(linked_channel) is True):
                        return "", 0, ("-# (did not read the linked channel: you can't "
                                       "read it)")
                    where = f"since the linked message in <#{linked_channel}>"
                else:
                    where = "since the linked message"
                first = self._fetch_message(linked_channel, linked)
                messages = self._channel_messages(linked_channel, limit, after=linked)
                messages += [first] if first else []
        if mode == "thread":
            parent = str(it.get("channel_parent_id") or "")
            if it.get("channel_type") in (10, 11, 12) and parent:
                starter = (self._fetch_message(parent, channel)
                           or self._fetch_message(channel, channel))
                limit, where = 100, "in this thread"
                messages = self._channel_messages(channel, limit)
            else:
                mode, note = "recent", " - this is not a thread"
        if mode in ("recent", "around"):
            if anchor:
                messages = self._channel_messages(channel, limit, around=anchor)
                where = "around that message"
            else:
                messages = self._channel_messages(channel, limit)
        block, count = turn_context.build_counted(starter=starter, recent=messages,
                                                  bot_id=bot_id, limit=limit)
        line = turn_context.footer(count, where)
        if line and note:
            line = line[:-1] + note + ")"
        _log(event="ask_context", mode=mode, read=count, chars=len(block))
        return block, count, line

    def _fetch_message(self, channel_id: str, message_id: str) -> dict | None:
        """One message by id via REST, or ``None`` (missing, no access, or an error)."""
        if not channel_id or not message_id:
            return None
        try:
            m = self.rest.request("GET", f"/channels/{channel_id}/messages/{message_id}")
        except Exception as exc:            # noqa: BLE001 -- context is best-effort
            _log(event="context_message_fetch_failed",
                 status=getattr(exc, "status", type(exc).__name__))
            return None
        return m if turn_context.usable(m) else None

    def _turn_context(self, msg: dict, mode: str = "here") -> str:
        """What a server mention is about, as one labelled block ("" for a DM).

        The replied-to message (any author), the message a thread was started
        from, and the channel's last few messages before this one. A thread
        started on a message has that message's id, so the starter is
        ``/channels/{parent}/messages/{thread}`` (a forum post keeps it inside
        the thread itself). Every fetch is best-effort: a failure costs the
        context, never the answer.

        ``mode`` is where the answer will be posted (``_contained``). Only an
        answer posted HERE may quote other members' lines: a contained answer
        lands in the public bot channel or a DM, so a mention in a staff-only
        channel or a private thread would carry their words out. Then the reply
        target and the starter are kept only when the asker wrote them, or this
        bot did AND the source channel is readable by @everyone (``public``): a
        bot summary posted in a staff channel is as private as the staff's own
        lines. No recent lines are fetched. The starter is fetched only for a
        thread (``is_thread``): a text channel's ``parent_id`` is its category.
        """
        if msg.get("guild_id") is None:
            return ""
        channel = str(msg.get("channel_id") or "")
        here = mode == "here"
        bot_id = str(msg.get("bot_id") or "")
        mine = {str(msg.get("user") or "")} - {""}
        if msg.get("public") is True:
            mine |= {bot_id} - {""}

        def allowed(m):
            if not isinstance(m, dict):
                return None
            if not here and str((m.get("author") or {}).get("id") or "") not in mine:
                return None
            return m

        reply = msg.get("reply_to") or None
        if isinstance(reply, dict) and not turn_context.usable(reply):
            reply = self._fetch_message(str(reply.get("channel_id") or channel),
                                        str(reply.get("id") or ""))
        reply = allowed(reply)
        starter = None
        parent = str(msg.get("parent_id") or "")
        if msg.get("is_thread") and parent and channel and parent != channel:
            # Discord's contract (docs: "Start Thread from Message"): a thread started
            # from a message has the SAME id as that message, and the message lives in
            # the PARENT channel, so the starter is GET /channels/{parent}/messages/
            # {thread_id}. Here `channel` IS the thread id. A forum/media post's starter
            # lives INSIDE the thread with that same id, hence the fallback
            # GET /channels/{thread_id}/messages/{thread_id}. `parent` is a channel id,
            # never a message id. Pinned by test_turn_context's starter tests.
            thread_id = channel
            starter = allowed(self._fetch_message(parent, thread_id)
                              or self._fetch_message(thread_id, thread_id))
        recent: list = []
        mid = str(msg.get("id") or "")
        if not here:
            return turn_context.build(reply_to=reply, starter=starter,
                                      bot_id=bot_id, current_id=mid)
        try:
            got = self.rest.request(
                "GET", f"/channels/{channel}/messages?limit={turn_context.RECENT_LIMIT}"
                + (f"&before={mid}" if mid else ""))
            recent = got if isinstance(got, list) else []
        except Exception as exc:            # noqa: BLE001 -- context is best-effort
            _log(event="context_recent_fetch_failed",
                 status=getattr(exc, "status", type(exc).__name__))
        block, count = turn_context.build_counted(reply_to=reply, starter=starter,
                                                  recent=recent, bot_id=bot_id,
                                                  current_id=mid)
        msg["_read_count"] = count          # the answer's "(read the last N ...)" footer
        return block

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

    def handle_member_change(self, attr: str, guild_id: str, user_id: str,
                             role_ids: list) -> dict:
        """Hand a role change or a leave to the plugins on in that server (blocking).

        Unlike a join, this runs for an unserved server too: the hooks here take
        access AWAY when a role is lost, and skipping them would leave it standing.
        """
        if self.host is None:
            return {}
        event_id = "gw-%s-%s-%d" % (guild_id, user_id, int(time.time() * 1000))
        out = self.host.dispatch_member_change(attr, guild_id, user_id, role_ids, event_id)
        if out:
            _log(event=attr[3:], guild=str(guild_id), plugins=out)
        return out

    def owner_notice(self, text: str) -> bool:
        """DM the owners one line; never raises (an arrival must not fail on it).

        Owner 2026-11-18: "when people install and try to set it up, will I get
        notified?" -- before this an install was one log line and nothing else.
        """
        try:
            self.notify_owners(text[:1900])
            return True
        except Exception as exc:          # noqa: BLE001 - no owners / DM refused
            _log(event="owner_notice_failed", error=type(exc).__name__)
            return False

    def _bot_name(self) -> str:
        state = getattr(self, "state", None) or {}
        return str(state.get("user") or "the bot").split("#")[0]

    @staticmethod
    def _guild_line(guild: dict) -> str:
        g = guild or {}
        name = str(g.get("name") or "a server")
        count = g.get("member_count") or g.get("approximate_member_count")
        size = f", {count} members" if count else ""
        return f"**{name}** (`{g.get('id') or '?'}`{size})"

    def handle_guild_join(self, guild: dict) -> dict:
        """Hand an arrival to the plugins on in that server (blocking; off the loop).

        An unserved server gets the bring-your-own-inference card instead. The bot
        stays -- it is not ours to leave a server someone added -- but nothing
        there reaches the owner's model. Either way the owners get one DM.
        """
        guild_id = str((guild or {}).get("id") or "")
        served_now = served_by_brain(getattr(self, "brains", None), guild_id,
                                     getattr(self, "served", None))
        self.owner_notice(
            f"New install: {self._bot_name()} was added to "
            f"{self._guild_line(guild)}. "
            + ("It is already connected." if served_now else
               "Not connected yet - its first card points a manager at "
               "`/aitherium setup`; applications and access requests will DM you."))
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
        self.owner_notice(f"Removed: {self._bot_name()} left "
                          f"{self._guild_line(guild)}.")
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

    # -- solve this (awdiscord.solve) --------------------------------------------------
    def note_dm_context(self, channel_id: str, text: str, kind: str,
                        message_id: str = "", ts: float | None = None) -> None:
        """Remember what this DM is about: the thing "solve this" will refer to."""
        from awdiscord import solve
        if not (text or "").strip() or solve.is_status_text(text):
            return
        self._dm_context[str(channel_id)] = solve.make_ctx(text, kind, message_id, ts)

    def dm_context(self, channel_id: str) -> dict | None:
        """The newest fresh context item of a DM, from memory, else from DM history.

        History makes it survive a restart, like ``recent_card_text``.
        """
        from awdiscord import solve
        if self.solver is None:
            return None
        ttl = self.solver.config.ttl_s
        have = self._dm_context.get(str(channel_id))
        if solve.fresh(have, ttl):
            return have
        found = self._recent_dm_context(str(channel_id), ttl)
        if found:
            self._dm_context[str(channel_id)] = found
        return found

    def _recent_dm_context(self, channel_id: str, ttl_s: float) -> dict | None:
        """Newest bot DM (or owner forward) within ``ttl_s`` that was not acted on."""
        from awdiscord import solve
        from awdiscord.forward import CARD_ID_RE, is_card
        me = self.state.get("user_id") or ""
        try:
            msgs = self.rest.request("GET", f"/channels/{channel_id}/messages?limit=25") or []
        except RestError:
            return None
        now = time.time()
        for m in msgs:                                   # newest first
            mine = bool(me) and str((m.get("author") or {}).get("id")) == me
            text = _raw_text(m).strip() or _raw_snapshot_text(m)
            if mine and solve.consumes_context(text):
                return None                              # already acted on
            forwarded = bool(m.get("message_snapshots")) and not mine
            if not (mine or forwarded) or not text or solve.is_status_text(text):
                continue
            if mine and m.get("message_reference"):
                continue                                 # a chat answer, not a page
            ts = _iso_epoch(m.get("timestamp"))
            if ts and now - ts > ttl_s:
                return None
            kind = (solve.KIND_FORWARDED if forwarded else
                    solve.KIND_CARD if is_card(text) and CARD_ID_RE.search(text)
                    else solve.KIND_ALERT)
            return solve.make_ctx(text, kind, str(m.get("id") or ""), ts or now)
        return None

    def _post_solve(self, channel: str, text: str, reply_to: str = "") -> dict:
        """Post a solve message to a DM, as a reply to ``reply_to`` when given."""
        payload: dict = {"content": text[:2000], "allowed_mentions": {"parse": []}}
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to,
                                            "fail_if_not_exists": False}
        return self.rest.create_message(channel, payload)

    def _post_solve_quiet(self, channel: str, text: str, reply_to: str = "") -> dict:
        """``_post_solve`` that never raises: a refused post must not lose the session
        it announces (the watcher still owes the owner the report)."""
        try:
            return self._post_solve(channel, text, reply_to) or {}
        except Exception as exc:                          # noqa: BLE001
            _log(event="solve_post_failed", channel=channel,
                 error=getattr(exc, "status", type(exc).__name__))
            return {}

    def _solve_dm(self, msg: dict) -> bool:
        """Owner DM to a persona with a solver: a bare forward, or "solve this".

        True when the message was handled here (the chat model never sees it).
        """
        from awdiscord import solve
        text = (msg.get("content") or "").strip()
        channel, mid = msg["channel_id"], str(msg.get("id") or "")
        if msg.get("forwarded"):
            self.note_dm_context(channel, text, solve.KIND_FORWARDED, mid)
            self._post_solve_quiet(channel, solve.BARE_FORWARD_REPLY, mid)
            _log(event="solve_forward_noted", user=msg["user"], chars=len(text))
            return True
        ctx = self.dm_context(channel)
        if not solve.is_solve_intent(text, ctx, owner=True, ttl_s=self.solver.config.ttl_s):
            return False
        with self._solve_lock:
            busy = channel in self._solve_inflight
            self._solve_inflight.add(channel)
        if busy:
            self._post_solve_quiet(channel, "Already starting that one.", mid)
            return True
        try:
            persona = self.solver.persona
            sid, summary = self.solver.start(persona, ctx, text)
            posted = self._post_solve_quiet(channel, summary, mid)
            if sid:
                self._dm_context.pop(channel, None)      # acted on: not solvable twice
                _log(event="solve_started", session=sid, persona=persona, channel=channel)
                self.solver.watch(sid, channel, persona, self._post_solve,
                                  str((posted or {}).get("id") or mid))
            else:
                if summary.startswith("That one is yours"):
                    self._dm_context.pop(channel, None)
                _log(event="solve_declined", persona=persona, channel=channel,
                     reason=summary[:120])
        finally:
            with self._solve_lock:
                self._solve_inflight.discard(channel)
        return True

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
    #: What a turn that crashed posts, so the ack above is never left hanging.
    TURN_FAILED_TEXT = ("I couldn't finish this answer: the turn failed on our side "
                        "({error}). It has been logged; this is a bug, not your question.")

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

    def _quiet_privileged(self, msg: dict) -> bool:
        """Owner, operator or server admin: may hear about models and serving."""
        operators = getattr(getattr(self, "host", None), "operators", set()) or set()
        return quiet.is_admin(self.rest, str(msg.get("guild_id") or ""),
                              str(msg.get("user") or ""), operators)

    def _quiet_reply(self, msg: dict, text: str) -> None:
        self.rest.create_message(msg["channel_id"], {
            "content": text[:2000], "allowed_mentions": {"parse": []},
            "message_reference": {"message_id": msg["id"], "fail_if_not_exists": False}})

    def _quiet_first(self, msg: dict) -> bool:
        """Mute, unmute, a muted place, or a room ask: True when it is handled here."""
        store = getattr(getattr(self, "host", None), "store", None)
        if store is None:
            return False
        guild, channel = str(msg["guild_id"]), str(msg["channel_id"])
        user, content = str(msg.get("user") or ""), msg.get("content") or ""
        row = None
        try:
            row = quiet.muted(store, guild, channel, str(msg.get("parent_id") or ""))
            if quiet.is_unmute_request(content):
                if row is None:
                    return False
                if quiet.may_unmute(row, user, admin=self._quiet_privileged(msg)):
                    quiet.clear_mute(store, guild, row["channel"])
                    self._quiet_reply(msg, quiet.UNMUTED_REPLY)
                    _log(event="unmuted", guild=guild, channel=row["channel"], user=user)
                elif quiet.notice_due(store, guild, channel, user):
                    self._dm_text(user, quiet.NOT_YOURS % row.get("by"))
                return True
            if quiet.is_mute_request(content):
                if row is not None and not quiet.may_unmute(
                        row, user, admin=self._quiet_privileged(msg)):
                    # quiet_mute_owned: someone else's mute is never overwritten (that
                    # would let anyone lift it with "mute" then "unmute").
                    if quiet.notice_due(store, guild, channel, user):
                        self._dm_text(user, quiet.NOT_YOURS % row.get("by"))
                    return True
                quiet.set_mute(store, guild, channel, user)
                self._quiet_reply(msg, quiet.MUTED_REPLY)
                _log(event="muted", guild=guild, channel=channel, user=user)
                return True
            if row is not None:
                if quiet.notice_due(store, guild, channel, user):
                    self._dm_text(user, quiet.notice_text(guild, row["channel"]))
                _log(event="muted_mention_skipped", guild=guild, channel=channel)
                return True
            answer = quiet.run_room_tool(getattr(self, "host", None), msg)
            if answer is not None:
                self._quiet_reply(msg, answer)
                _log(event="room_ask_handled", guild=guild, channel=channel)
                return True
        except RestError as exc:          # a refused reply/DM: logged, never retried
            _log(event="quiet_failed", status=exc.status)
            return True
        except Exception as exc:          # noqa: BLE001 - never kill the mention path
            _log(event="quiet_error", error=type(exc).__name__)
            return row is not None      # a muted chat stays silent even on an error
        return False

    def _ops_digest_to_dm(self, msg: dict, payload: dict) -> dict:
        """An operator ops digest never lands in a channel everyone can read.

        Who asked decides who may SEE it; the channel decides who WOULD see it. The
        operator who asked gets the digest by DM and the channel one line; anyone else
        gets the line only. Returns the payload to post in the channel.
        """
        long_form = payload.get("long_form") or {}
        parts = list(long_form.get("parts") or []) or (
            [payload.get("content") or ""] + list(payload.get("continuation") or []))
        delivered = False
        if self._quiet_privileged(msg):
            try:
                dm = self.rest.dm_channel(str(msg["user"]))["id"]
                for part in parts:
                    for piece in split_message(str(part or "")):
                        if piece.strip():
                            self.rest.create_message(dm, {
                                "content": piece, "allowed_mentions": {"parse": []}})
                delivered = True
            except RestError as exc:          # DMs closed: withhold, never post it here
                _log(event="ops_digest_dm_failed", user=str(msg["user"]), status=exc.status)
        _log(event="ops_digest_withheld", channel=str(msg["channel_id"]), dm=delivered)
        return {"content": quiet.OPS_TO_DM if delivered else quiet.OPS_WITHHELD,
                "allowed_mentions": {"parse": []}}

    def _dm_text(self, user: str, text: str) -> None:
        try:
            dm = self.rest.dm_channel(user)["id"]
            self.rest.create_message(dm, {"content": text[:2000],
                                          "allowed_mentions": {"parse": []}})
        except RestError as exc:          # DMs closed: the notice is best effort
            _log(event="quiet_notice_failed", user=user, status=exc.status)

    def rehandle(self, channel_id: str, message_id: str) -> dict:
        """Run an OPERATOR's message through ``handle_message`` again, as if it had just
        arrived (inbox ``POST /rehandle``, bearer-gated like every inbox route).

        Owner rule (2026-11-15): nobody hand-posts the bot's work. When a turn failed
        (the 2026-11-16 vision and planning failures), the fix is deployed and the bot
        answers the original message itself: same text, attachments, mentions and
        author, so every check (limits, intake, containment) runs unchanged. Only a
        message whose author is an operator can be re-handled, so the inbox bearer
        cannot make the bot answer a member's words in the operator's name.
        """
        if not (str(channel_id).isdigit() and str(message_id).isdigit()):
            raise ValueError("channel_id and message_id must be Discord ids")
        m = self.rest.request("GET", f"/channels/{channel_id}/messages/{message_id}")
        author = (m or {}).get("author") or {}
        user = str(author.get("id") or "")
        if user not in set(self.owners):
            raise ValueError("only an operator's message can be re-handled")
        chan = self.rest.request("GET", f"/channels/{channel_id}") or {}
        me = str(self.state.get("user_id") or "")
        content = m.get("content") or ""
        forwarded = False
        if not content.strip():
            content = _raw_snapshot_text(m)
            forwarded = bool(content)
        content = name_mentions(content, _raw_mentioned_people(m), me)
        ref = (m.get("message_reference") or {}).get("message_id")
        msg = {"id": str(message_id), "channel_id": str(channel_id),
               "parent_id": str(chan.get("parent_id") or ""),
               "is_thread": int(chan.get("type") or 0) in (10, 11, 12),
               "public": False, "guild_id": str(chan.get("guild_id") or "") or None,
               "user": user, "content": content, "forwarded": forwarded,
               "author_name": str(author.get("global_name") or author.get("username")
                                  or ""),
               "bot_id": me, "reference_id": "", "reference_text": "",
               "reply_to": ({"id": str(ref), "channel_id": str(channel_id)}
                            if ref else None),
               "attachments": [{"id": str(a.get("id") or ""), "filename": a.get("filename"),
                                "url": a.get("url"), "proxy_url": a.get("proxy_url") or "",
                                "content_type": a.get("content_type") or "",
                                "size": a.get("size") or 0}
                               for a in m.get("attachments") or []],
               "role_names": []}
        _log(event="rehandle", channel=str(channel_id), message=str(message_id),
             attachments=len(msg["attachments"]))
        threading.Thread(target=self.handle_message, args=(msg,), daemon=True,
                         name="rehandle").start()
        return {"rehandling": str(message_id)}

    def handle_message(self, msg: dict) -> None:
        """Answer one mention or DM as a reply (blocking; run it off the event loop).

        Outside the server's bot channel the answer is not posted where it was
        asked (``awdiscord.containment``): it goes to the bot channel, or by DM
        when the server has none, and the original message gets a reaction.
        """
        if msg.get("guild_id") and self._quiet_first(msg):
            return
        mode, target = self._contained(msg.get("guild_id"), msg["channel_id"],
                                       msg.get("parent_id"))
        if msg.get("guild_id") and quiet.answer_in_place():
            # A mention is answered where it was asked, as a reply (awdiscord.quiet).
            mode, target = "here", str(msg["channel_id"])
        if mode != "here":
            self._mark_seen(msg["channel_id"], str(msg.get("id") or ""))
        typing_in = target if mode == "bot" else (msg["channel_id"] if mode == "here" else "")
        try:
            if typing_in:
                self.rest.request("POST", f"/channels/{typing_in}/typing")
        except Exception as exc:                  # noqa: BLE001 -- cosmetic; never block
            _log(event="typing_failed", status=getattr(exc, "status", type(exc).__name__))
        if msg.get("guild_id") is None and self.forwarder is not None \
                and not msg.get("forwarded"):
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
        if msg.get("guild_id") is None and self.solver is not None \
                and msg["user"] in self.owners and self._solve_dm(msg):
            return
        # A plugin that knows the sender (a customer's contact) takes the message
        # before the chat brain: without one, a DM from anyone but the owner ends at
        # the bring-your-own-model card (awdiscord.intake).
        if intake.registered() and intake.offer(
                intake.IntakeEvent(
                    user=msg["user"], channel_id=msg["channel_id"],
                    message_id=str(msg.get("id") or ""), content=msg.get("content") or "",
                    guild_id=msg.get("guild_id"),
                    author_name=str(msg.get("author_name") or ""),
                    reference_id=str(msg.get("reference_id") or ""),
                    reference_text=str(msg.get("reference_text") or ""),
                    attachments=list(msg.get("attachments") or []),
                    agent=self.agent, rest=self.rest),
                on_error=lambda exc: _log(event="intake_error",
                                          error=type(exc).__name__)):
            _log(event="intake_taken", user=msg["user"], dm=msg.get("guild_id") is None)
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
                                           author_name=str(msg.get("author_name") or ""),
                                           # lazy: no REST spent before the
                                           # silence and abuse checks pass
                                           about=lambda: self._turn_context(msg, mode))
        except Exception as exc:                  # noqa: BLE001 - the ack PROMISED a reply
            # "I'll post the answer right here" must be kept: a turn that crashed
            # still gets a short honest line instead of an ack that never resolves.
            _log(event="turn_failed", error=type(exc).__name__,
                 channel=msg["channel_id"])
            payload = {"content": self.TURN_FAILED_TEXT.format(error=type(exc).__name__),
                       "allowed_mentions": {"parse": []}}
        finally:
            stop_ack()
        self._drop_ack(msg["channel_id"], ack)
        if payload is None:
            return
        if msg.get("guild_id") and isinstance(payload.get("content"), str):
            payload["content"] = quiet.polish(
                payload["content"], privileged=self._quiet_privileged(msg))
        if msg.get("guild_id") and msg.get("public") and mode != "dm" and \
                quiet.carries_ops_digest(payload.get("content"),
                                         *(payload.get("continuation") or []),
                                         *((payload.get("long_form") or {}).get("parts") or [])):
            payload = self._ops_digest_to_dm(msg, payload)
        if mode == "here" and msg.get("_read_count"):
            with_footer(payload, turn_context.footer(int(msg["_read_count"])))
        if mode == "dm" and self._deliver_dm(msg, payload):
            return
        if mode == "bot" and self._deliver_bot_channel(msg, target, payload, turn):
            return
        more = payload.pop("continuation", None) or []
        if msg.get("guild_id") and more:
            privileged = self._quiet_privileged(msg)
            more = [quiet.polish(part, privileged=privileged) if isinstance(part, str)
                    else part for part in more]
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
        if msg.get("peer_bot"):
            # A bot reacts only to a mention: an un-pinged reply leaves the peer deaf
            # and the conversation ends after one line. Ping the replied-to peer only.
            mentions = dict(payload.get("allowed_mentions") or {"parse": []})
            mentions["replied_user"] = True
            payload["allowed_mentions"] = mentions
        posted = self._post_reply(msg["channel_id"], payload)
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
                           "name", ""), turn=turn.get("turn_id", ""),
             tools=[c.get("tool") for c in turn.get("tools") or []],
             # The route the scheduler reported: without it a turn served by a stand-in
             # is indistinguishable from one served by the model that was asked for.
             requested_model=str((turn.get("route") or {}).get("requested") or ""),
             served_model=str((turn.get("route") or {}).get("served_by") or ""))

    def _post_reply(self, channel: str, payload: dict) -> dict:
        """Post an answer; a sticker Discord refuses is dropped, never the answer."""
        try:
            return self.rest.create_message(channel, payload)
        except RestError as exc:
            if not payload.get("sticker_ids") or exc.status not in (400, 403):
                raise
            _log(event="reply_sticker_refused", channel=channel, status=exc.status)
            payload = {k: v for k, v in payload.items() if k != "sticker_ids"}
            return self.rest.create_message(channel, payload)

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
            posted = self._post_reply(target, payload)
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
            if is_dm and self.solver is not None and getattr(message, "reference", None) is None:
                # An alert page or card the bot sent on its own (a reply to the
                # owner is chat, not a thing to solve): what "solve this" means next.
                self.note_dm_context(str(message.channel.id), _flat_text(message),
                                     "alert", str(message.id))
            return
        guild_id = str(message.guild.id) if message.guild else None
        channel_id = str(message.channel.id)
        # The Discord<->Relay mirror sees EVERY message in a mirrored guild (bots and
        # webhooks too: the bridge drops its own echoes structurally). Never raises,
        # never blocks -- a queue and one daemon thread (awdiscord.mirror).
        mirror.feed_guild("message", message, guild_id)
        unserved = bool(guild_id and not served_by_brain(
            getattr(self, "brains", None), guild_id, getattr(self, "served", None)))
        mentioned = mentions_bot(message, client_user)
        author_bot = bool(message.author.bot)
        # A named peer bot that addressed us directly spends one turn of its budget;
        # nothing else is charged (an unaddressed bot line costs no budget).
        peers = getattr(self, "peers", None)
        peer = bool(author_bot and mentioned and not is_dm and peers is not None
                    and peers.admit(str(message.author.id), channel_id))
        # A mention a plugin can answer deterministically runs BEFORE the model: a
        # production brief opens a media-forge job instead of paying a busy chat
        # lane for a prose answer (owner/partner 2026-11-20). Every hook returns
        # False for anything that is not its kind, so ordinary chat is untouched.
        if (mentioned and not is_dm and not author_bot and guild_id
                and self.host is not None and self.host.has_mention_hooks(guild_id)):
            try:
                handled = await asyncio.to_thread(self.host.dispatch_mention, {
                    "id": str(message.id), "channel_id": channel_id,
                    "guild_id": guild_id, "user": str(message.author.id),
                    "content": message.content or "",
                    "attachments": [_attachment(a) for a in message.attachments or []]})
            except Exception as exc:      # noqa: BLE001 - never cost the member a reply
                _log(event="mention_hook_error", error=type(exc).__name__)
                handled = False
            if handled:
                _log(event="mention_hook_handled", user=str(message.author.id),
                     guild=guild_id)
                return
        route = message_route(
            own=False, author_bot=author_bot, is_dm=is_dm,
            mentioned=mentioned,
            hooks=bool(self.host is not None and not is_dm
                       and self.host.has_message_hooks(guild_id)),
            candidate=bool(not is_dm and self.watch.is_candidate(channel_id)),
            peer=peer)
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
                    # Discord's message type (7 = "X joined the server"), so a
                    # welcome hook can tell a join line from a member speaking
                    "type": _message_type(message),
                    # when the author joined this server (epoch s, 0 unknown), so a
                    # newcomer hook never mistakes a regular for a new member
                    "joined_at": _joined_at(message.author),
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
        # A Discord "Forwarded" message has empty content; the text is in its snapshots.
        content = message.content or ""
        forwarded = False
        if not content.strip():
            content = _snapshot_text(message)
            forwarded = bool(content)
        content = name_mentions(content, _mentioned_people(message), str(client_user.id))
        try:
            await asyncio.to_thread(self.handle_message, {
                "id": str(message.id), "channel_id": str(message.channel.id),
                "parent_id": str(getattr(message.channel, "parent_id", "") or ""),
                # a text channel's parent_id is its CATEGORY; only a thread has a starter
                "is_thread": _is_thread(message.channel),
                # may a contained answer quote this bot's own posts from here?
                "public": _everyone_can_view(message.channel, message.guild),
                "guild_id": str(message.guild.id) if message.guild else None,
                "user": str(message.author.id), "content": content,
                "forwarded": forwarded,
                "author_name": str(getattr(message.author, "display_name", "")
                                   or getattr(message.author, "name", "") or ""),
                "bot_id": str(client_user.id),
                "reference_id": ref_id, "reference_text": ref_text,
                # Any author: what "this" in a server reply means (turn_context).
                "reply_to": None if is_dm else _replied(message),
                # a named peer bot: our reply must ping it or it never hears it
                "peer_bot": peer,
                # a reply to a picture carries that picture, so "what is this?" looks
                "attachments": _turn_attachments(message),
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
        if self.solver is not None:
            # sessions started before a restart still owe the owner their report
            _log(event="solve_resumed", watching=self.solver.resume(self._post_solve))
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

        # Edits, deletes and purges cross to the Relay record too (one-surface #16333).
        @client.event
        async def on_raw_message_edit(payload):
            mirror.feed_guild("message_edit", payload, getattr(payload, "guild_id", None))

        @client.event
        async def on_raw_message_delete(payload):
            mirror.feed_guild("message_delete", payload, getattr(payload, "guild_id", None))

        @client.event
        async def on_raw_bulk_message_delete(payload):
            mirror.feed_guild("bulk_message_delete", payload,
                              getattr(payload, "guild_id", None))

        def _guild_of(obj):
            return getattr(getattr(obj, "guild", None), "id", None)

        @client.event
        async def on_thread_create(thread):
            mirror.feed_guild("thread_create", thread, _guild_of(thread))

        @client.event
        async def on_thread_update(before, after):
            mirror.feed_guild("thread_update", after, _guild_of(after))

        @client.event
        async def on_thread_delete(thread):
            mirror.feed_guild("thread_delete", thread, _guild_of(thread))

        @client.event
        async def on_ready():
            bot.state.update(connected=True, guilds=len(client.guilds), since=time.time(),
                             user_id=str(client.user.id))
            _log(event="ready", bot=str(client.user), guilds=len(client.guilds))
            bot.dispatcher.known_servers = sorted(
                str(getattr(g, "name", "") or "") for g in client.guilds)
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
            # A persona bot wears its own portrait (Saga wore Iris's, 2026-11-18).
            persona = _env("AWDISCORD_OWNER_AGENT_PERSONA", default="")
            if persona and not bot.state.get("profile_avatar_checked"):
                bot.state["profile_avatar_checked"] = True
                try:
                    state = _env("AWDISCORD_STATE_DIR",
                                 default=str(Path.home() / ".awdiscord"))
                    outcome = await asyncio.to_thread(
                        profile_avatar.sync, bot.rest, persona, state)
                    _log(event="profile_avatar", persona=persona, outcome=outcome)
                except Exception as exc:      # noqa: BLE001 - never fatal
                    _log(event="profile_avatar_error", persona=persona,
                         error=type(exc).__name__, detail=str(exc)[:200])
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
            # A channel made private must reach the mirror's privacy re-judge.
            mirror.feed_guild("channel_update", after, _guild_of(after))
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

        if members:
            def _role_ids(member) -> list:
                # @everyone carries the guild's own id and is not a grantable role.
                gid = getattr(getattr(member, "guild", None), "id", None)
                return [str(r.id) for r in (getattr(member, "roles", None) or [])
                        if r.id != gid]

            @client.event
            async def on_member_update(before, after):
                if getattr(after, "bot", False) or after.guild is None:
                    return
                if _role_ids(before) == _role_ids(after):
                    return                      # a nickname or avatar change
                try:
                    await asyncio.to_thread(bot.handle_member_change, "on_member_update",
                                            str(after.guild.id), str(after.id),
                                            _role_ids(after))
                except Exception as exc:        # a plugin hook never takes the gateway down
                    _log(event="member_update_hook_error", error=type(exc).__name__,
                         detail=str(exc)[:200])

            @client.event
            async def on_raw_member_remove(payload):
                user = getattr(payload, "user", None)
                if user is None or getattr(user, "bot", False):
                    return
                try:
                    await asyncio.to_thread(bot.handle_member_change, "on_member_remove",
                                            str(payload.guild_id), str(user.id), [])
                except Exception as exc:
                    _log(event="member_remove_hook_error", error=type(exc).__name__,
                         detail=str(exc)[:200])

        if reactions:
            async def _reaction(payload, added: bool):
                if client.user is None or payload.user_id == client.user.id:
                    return
                mirror.feed_guild("reaction_add" if added else "reaction_remove",
                                  payload, getattr(payload, "guild_id", None))
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
                elif self.path.startswith("/voice/avatar/events"):
                    if not self._authed():
                        self._send(401, {"error": "bearer required"})
                    elif bot.voice is None:
                        self._send(409, {"error": "voice is not enabled"})
                    else:
                        from urllib.parse import parse_qs, urlparse
                        q = parse_qs(urlparse(self.path).query)
                        try:
                            self._send(200, bot.voice.avatar.events_since(
                                int((q.get("since") or ["0"])[0]),
                                float((q.get("timeout") or ["20"])[0])))
                        except ValueError as exc:
                            self._send(400, {"error": str(exc)[:200]})
                elif self.path.startswith("/voice/transcript"):
                    if not self._authed():
                        self._send(401, {"error": "bearer required"})
                    elif bot.voice is None:
                        self._send(409, {"error": "voice is not enabled"})
                    else:
                        from urllib.parse import parse_qs, urlparse
                        q = parse_qs(urlparse(self.path).query)
                        try:
                            self._send(200, bot.voice.transcript_tail(
                                (q.get("guild_id") or [""])[0],
                                int((q.get("n") or ["40"])[0])))
                        except (RuntimeError, ValueError) as exc:
                            self._send(409, {"error": str(exc)[:200]})
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
                    if not isinstance(body, dict):
                        raise ValueError("body must be a JSON object")
                    if self.path == "/approvals":
                        out = bot.request_approval(
                            str(body["title"]), str(body.get("body", "")),
                            callback_url=body.get("callback_url"),
                            channel_id=body.get("channel_id"), meta=body.get("meta"))
                    elif self.path == "/post":
                        out = bot.post(str(body["channel_id"]), str(body["content"]))
                    elif self.path == "/rehandle":
                        out = bot.rehandle(str(body["channel_id"]),
                                           str(body["message_id"]))
                    elif self.path == "/notify/owner":
                        out = bot.notify_owners(str(body.get("message") or body["content"]))
                    elif self.path == "/notify/channel":
                        out = bot.notify_channel(str(body.get("message") or body["content"]))
                    elif self.path == "/voice/join":
                        if bot.voice is None:
                            self._send(409, {"error": "voice is not enabled"})
                            return
                        if body.get("pin"):
                            bot.voice.pin(str(body["guild_id"]), str(body["channel_id"]))
                        bot.voice._schedule(bot.voice.join_channel(
                            str(body["guild_id"]), str(body["channel_id"])))
                        out = {"joining": str(body["channel_id"])}
                    elif self.path == "/voice/leave":
                        if bot.voice is None:
                            self._send(409, {"error": "voice is not enabled"})
                            return
                        bot.voice.pin(str(body["guild_id"]))      # release any hangout pin
                        bot.voice._schedule(bot.voice.leave(str(body["guild_id"])))
                        out = {"leaving": str(body["guild_id"])}
                    elif self.path in ("/voice/avatar", "/voice/emote", "/voice/notes",
                                       "/voice/inject"):
                        if bot.voice is None:
                            self._send(409, {"error": "voice is not enabled"})
                            return
                        if self.path == "/voice/avatar":
                            out = bot.voice.set_avatar(str(body.get("url", "")),
                                                       str(body.get("token", "")))
                        elif self.path == "/voice/inject":
                            out = bot.voice.inject_utterance(
                                str(body["guild_id"]), str(body["text"]),
                                str(body.get("voice") or "kokoro:am_michael"),
                                str(body.get("name") or "Rehearsal speaker"))
                        elif self.path == "/voice/emote":
                            out = bot.voice.emote(str(body["guild_id"]),
                                                  str(body["gesture"]))
                        else:
                            out = bot.voice.notes_control(
                                str(body["guild_id"]), str(body["action"]),
                                str(body.get("by") or "the host"))
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


def _message_type(message) -> int:
    """Discord's integer message type (0 default, 7 member join); 0 when unknown."""
    kind = getattr(message, "type", None)
    try:
        return int(getattr(kind, "value", kind) or 0)
    except (TypeError, ValueError):
        return 0


def _joined_at(member) -> float:
    """``member.joined_at`` (a discord.py datetime) as epoch seconds; 0.0 unknown."""
    when = getattr(member, "joined_at", None)
    try:
        return float(when.timestamp()) if when is not None else 0.0
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return 0.0


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
    kind = getattr(getattr(interaction, "channel", None), "type", None)
    kind = getattr(kind, "value", kind)
    if isinstance(kind, int):       # unknown: /ask context:thread says it is no thread
        raw["channel_type"] = kind
    if interaction.guild_id:
        raw["_can_read"] = _reader_check(getattr(interaction, "guild", None),
                                         interaction.user)
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


def _reader_check(guild, member):
    """``channel id -> True | False | None``: may ``member`` read that channel's history?

    From discord.py's cached channels and the member's roles (``permissions_for``
    applies the @everyone, role and member overwrites, owner and Administrator).
    A private thread is never True (membership is not in the cache); an unknown
    channel or any error is None, which every caller treats as "no".
    """
    def can_read(channel_id: str):
        try:
            channel = guild.get_channel_or_thread(int(channel_id))
            if channel is None:
                return None
            if "private" in str(getattr(channel, "type", "")):
                return False
            perms = channel.permissions_for(member)
            return bool(perms.view_channel and perms.read_message_history)
        except Exception:                    # noqa: BLE001 - unknown is "no"
            return None
    return can_read


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
