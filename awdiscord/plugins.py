"""Plugins: how a pack adds slash commands to the bot, per server.

A plugin is a Python module that exposes ``PLUGIN = Plugin(...)``. It brings
slash commands, and optionally a persona fragment and skills the agent sees
when the plugin is on in that server. Server admins switch plugins on and off
with ``/plugins``; nothing a plugin adds is visible in a server that has not
enabled it.

    from awdiscord.plugins import Command, Option, Plugin

    def hello(ctx):
        return ctx.reply(f"hello <@{ctx.user}>")

    PLUGIN = Plugin(
        name="hello", version="0.1.0", description="Says hello",
        commands=[Command("hello", "Say hello", hello)])

Discovery, in order (a later source never replaces an earlier name, unless it
says so with ``overrides``):
  1. the built-ins shipped in ``awdiscord.builtin``
  2. Python entry points in the group ``awdiscord.plugins``
     (``pip install awdiscord-pack-foo`` is enough)
  3. ``*.py`` files and packages in each dir of ``AWDISCORD_PLUGIN_DIRS``
     (os.pathsep-separated) and in ``~/.awdiscord/plugins``

A pack that replaces a built-in names it: ``Plugin(name="team-board",
overrides=("projects",), ...)`` drops the built-in ``projects`` plugin and takes
over its command names. Without ``overrides`` a duplicate command name is still
refused, so two packs cannot silently fight over ``/task``.

A plugin may also greet new members: ``on_member_join=[fn]``, each
``fn(event: MemberJoin)``. The bot calls them for a guild only where the plugin
is on, and only when the gateway asks Discord for member events
(``AWDISCORD_MEMBERS=1``; the members intent is privileged). A button a plugin
posts may open a modal (``ctx.modal``); its submission comes back to the same
plugin's ``on_modal(ctx, payload, values)``.

And the bot being added to or removed from a server: ``on_guild_join=[fn]``
and ``on_guild_remove=[fn]``, each ``fn(event: GuildEvent)``. Guild events
need only the guilds intent, so they always arrive; the host runs a hook only
for plugins ON in that server, because a server the bot just joined has no
enablement state and ``enabled`` falls back to ``default_enabled``/``always_on``.
This is how a plugin posts a setup card on arrival and cleans up on removal.

A plugin may also read ordinary channel messages: ``on_message=[fn]``, each
``fn(event: MessageEvent) -> bool``. The gateway hands a human, non-mention
server message to ``PluginHost.dispatch_message`` only when a plugin ON in that
server registered such a hook (and the bot asked Discord for message content,
``AWDISCORD_MESSAGE_CONTENT=1``; the intent is privileged). A hook returns True
when it answered; the first one that does ends the dispatch. Bot-authored
messages (webhooks included) never reach a hook, so a hook cannot loop.

A plugin may also see every answer the agent gives: ``on_reply=[fn]``, each
``fn(event: ReplyEvent) -> list | None``. After a mention, DM or /ask answer is
posted, the host calls the hooks of the plugins ON in that server with the
question, the answer and the posted message id; component rows a hook returns
are added to that message (a feedback pack puts its rating buttons there).

And reactions: ``on_reaction=[fn]``, each ``fn(event: ReactionEvent)``. The
gateway forwards a reaction added to (or removed from) any message only when
it asks Discord for reaction events (``AWDISCORD_REACTIONS=1``; the reactions
intent is not privileged). Reactions by bots, the bot itself included, never
reach a hook.

A command may nest one level of groups: ``Command("aither", ..., subcommands=[
Command("feedback", ..., subcommands=[Command("stats", ..., handler)])])`` is
``/aither feedback stats``; its handler sees ``ctx.sub == "feedback stats"``.

Handlers are plain functions ``(ctx) -> Result``. Use ``ctx.reply`` for an
answer and ``ctx.defer(fn)`` for slow work (the agent, an HTTP call): Discord
gets an acknowledgement at once and the reply is edited when ``fn`` returns.
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("awdiscord.plugins")

ENTRY_POINT_GROUP = "awdiscord.plugins"
BUTTON_PREFIX = "awp:"
MODAL = 9                      # interaction response type: open a modal
MODAL_SUBMIT = 5               # interaction type: a modal was submitted
MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3

_TYPES = {"sub": 1, "group": 2, "string": 3, "integer": 4, "boolean": 5, "user": 6,
          "channel": 7, "role": 8, "number": 10}


@dataclass
class Option:
    name: str
    description: str
    type: str = "string"
    required: bool = False
    choices: list[str] = field(default_factory=list)
    max_length: int | None = None

    def to_discord(self) -> dict:
        out: dict[str, Any] = {"type": _TYPES[self.type], "name": self.name,
                               "description": self.description[:100],
                               "required": self.required}
        if self.choices:
            out["choices"] = [{"name": c, "value": c} for c in self.choices[:25]]
        if self.max_length and self.type == "string":
            out["max_length"] = self.max_length
        return out


@dataclass
class Command:
    """One slash command, or a group of subcommands when ``subcommands`` is set."""
    name: str
    description: str
    handler: Callable[["Ctx"], Any] | None = None
    options: list[Option] = field(default_factory=list)
    subcommands: list["Command"] = field(default_factory=list)
    admin: bool = False            # needs Manage Server (or an operator)
    dm: bool = False               # also usable in DMs

    def to_discord(self) -> dict:
        body: dict[str, Any] = {"name": self.name, "description": self.description[:100],
                                "type": 1, "contexts": [0, 1] if self.dm else [0],
                                "integration_types": [0]}
        if self.subcommands:
            body["options"] = [_sub_def(s) for s in self.subcommands]
        elif self.options:
            body["options"] = [o.to_discord() for o in self.options]
        if self.admin:
            # Hidden from members by default; admins can re-grant per role in
            # Server Settings > Integrations. The host still checks at run time.
            body["default_member_permissions"] = str(MANAGE_GUILD)
        return body


def _sub_def(sub: "Command") -> dict:
    """A subcommand (type 1), or a group of subcommands (type 2) one level deep."""
    if sub.subcommands:
        return {"type": 2, "name": sub.name, "description": sub.description[:100],
                "options": [{"type": 1, "name": s.name, "description": s.description[:100],
                             "options": [o.to_discord() for o in s.options]}
                            for s in sub.subcommands]}
    return {"type": 1, "name": sub.name, "description": sub.description[:100],
            "options": [o.to_discord() for o in sub.options]}


def _find_sub(cmd: "Command", sub: str | None) -> "Command | None":
    """The subcommand ``sub`` names: ``"name"`` or ``"group name"``."""
    group, _, leaf = (sub or "").partition(" ")
    target = next((s for s in cmd.subcommands if s.name == group), None)
    if target is not None and leaf:
        return next((s for s in target.subcommands if s.name == leaf), None)
    if target is not None and target.subcommands:
        return None                     # a group alone is not runnable
    return target


@dataclass
class Tool:
    """A function the agent may call while answering /ask or a mention.

    ``fn(**arguments) -> str``. Whatever it returns (or the error it raises) is
    handed back to the model, so return text a model can read.

    A tool that must know WHO is asking sets ``wants_caller = True``: the host
    hands the agent a copy bound to the asker (``bind``), and ``fn`` is then
    called with ``caller={"user", "guild", "operator", "grounding", "channel",
    "message"}`` as well.
    ``caller`` is the host's, never the model's: a ``caller`` argument the model
    sends is dropped. An unbound ``wants_caller`` tool is called with
    ``caller=None`` and must refuse whatever needs an identity.

    A tool that produces a file (an image, a spoken reply) puts it in
    ``caller["outbox"]`` (``awdiscord.outbox.Outbox``); the answer goes out with
    the file attached. The gateway pops ``files`` off the reply payload and
    uploads them after the text.
    """
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    operators_only: bool = False      # offered only to the bot's operators
    wants_caller: bool = False        # fn also gets caller= (who asked, from the host)
    caller: dict | None = None        # set by bind(); None = nobody is known
    # offered(caller) -> bool: offer the tool on THIS turn only when True. The
    # caller is the host's (who, which guild and channel), never the model's, so
    # a tool scoped to one channel or one customer is simply absent elsewhere.
    offered: Callable[[dict], bool] | None = None

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name,
                                                 "description": self.description[:1000],
                                                 "parameters": self.parameters}}

    def bind(self, caller: dict) -> "Tool":
        """A copy of this tool that knows who is asking (``wants_caller`` only)."""
        if not getattr(self, "wants_caller", False):
            return self
        bound = copy.copy(self)       # keeps ad-hoc attributes such as ``grounds``
        bound.caller = dict(caller)
        return bound

    def run(self, args: dict) -> str:
        args = dict(args or {})
        if getattr(self, "wants_caller", False):
            args.pop("caller", None)  # the model never says who is asking
            return str(self.fn(**args, caller=self.caller))
        return str(self.fn(**args))


@dataclass
class Plugin:
    name: str
    version: str
    description: str
    commands: list[Command] = field(default_factory=list)
    persona: str = ""                      # appended to the agent's system prompt
    skills: dict[str, str] = field(default_factory=dict)   # name -> markdown
    default_enabled: bool = False          # on in a server until an admin turns it off
    always_on: bool = False                # cannot be disabled (the plugin manager)
    defaults: dict = field(default_factory=dict)   # preset settings (a pack's board, ...)
    tools: list[Tool] = field(default_factory=list)  # the agent may call these
    on_button: Callable[["Ctx", str], Any] | None = None   # custom_id "awp:<name>:<payload>"
    # A modal this plugin opened was submitted: (ctx, payload, {field id: value}).
    on_modal: Callable[["Ctx", str, dict], Any] | None = None
    # A member joined a server where this plugin is on: fn(MemberJoin) -> None.
    on_member_join: list[Callable[["MemberJoin"], Any]] = field(default_factory=list)
    # The bot was added to a server where this plugin is on: fn(GuildEvent) -> None.
    on_guild_join: list[Callable[["GuildEvent"], Any]] = field(default_factory=list)
    # The bot was removed from a server where this plugin was on: fn(GuildEvent) -> None.
    on_guild_remove: list[Callable[["GuildEvent"], Any]] = field(default_factory=list)
    # A human posted in a server where this plugin is on (not a bot mention, not a DM):
    # fn(MessageEvent) -> bool, True when the hook answered it.
    on_message: list[Callable[["MessageEvent"], Any]] = field(default_factory=list)
    # The gateway connected: fn(host) -> None, run ONCE per process off the loop.
    # A background daemon that must survive a restart (a sweep, a backfill) starts
    # here, not on the first message -- a quiet night has no first message, and a
    # note posted before anyone else speaks would wait for one.
    on_ready: list[Callable[["PluginHost"], Any]] = field(default_factory=list)
    # The agent's answer was posted where this plugin is on:
    # fn(ReplyEvent) -> component rows to add to that message, or None.
    on_reply: list[Callable[["ReplyEvent"], Any]] = field(default_factory=list)
    # A human added or removed a reaction (needs AWDISCORD_REACTIONS=1):
    # fn(ReactionEvent) -> None.
    on_reaction: list[Callable[["ReactionEvent"], Any]] = field(default_factory=list)
    # Who is asking, per chat turn: fn(caller) -> text added to the agent's context
    # (caller = {"user", "guild", "author_name", "role_names"}, the host's, never the
    # model's). An account-link pack names the asker's account here, so "do you know
    # who I am?" is answered from the link instead of "I can't tell who you are".
    turn_context: Callable[[dict], str] | None = None
    homepage: str = ""
    source: str = "builtin"                # filled in by the loader
    # Plugins this one replaces (by name). Their commands become this plugin's
    # to define; a plugin not named here still cannot take a command name.
    overrides: tuple[str, ...] = ()

    def button_id(self, payload: str) -> str:
        """A component custom_id that routes back to this plugin's ``on_button``."""
        cid = f"{BUTTON_PREFIX}{self.name}:{payload}"
        if len(cid) > 100:
            raise PluginError("button payload too long (custom_id is capped at 100)")
        return cid


class PluginError(Exception):
    pass


class PluginCommandConflictError(PluginError):
    """A plugin command name is already owned by the core bot or another plugin.

    A distinct type so the loader can surface it at ERROR: this is a silently
    disabled feature (a whole pack can vanish from the live bot), not a routine
    "this plugin is off" note.
    """


class Ctx:
    """What a handler gets: who asked, where, the options, and a scoped store."""

    def __init__(self, host: "PluginHost", plugin: Plugin, interaction: dict,
                 options: dict, sub: str | None):
        self.host = host
        self.plugin = plugin
        self.interaction = interaction
        self.options = options
        self.sub = sub
        self.guild = interaction.get("guild_id")
        self.channel = interaction.get("channel_id")
        member = interaction.get("member") or {}
        self.user = str(((member.get("user") or interaction.get("user")) or {}).get("id", ""))
        self.permissions = int(member.get("permissions") or 0)

    # -- state ---------------------------------------------------------------
    @property
    def store(self) -> "ScopedStore":
        if not self.guild:
            raise PluginError("this command only works in a server")
        return ScopedStore(self.host.store, self.guild, self.plugin.name)

    @property
    def agent(self):
        """This server's brain, or None when nothing answers here.

        Plugins use ``ctx.agent`` for the model, so the resolver must be threaded
        through here: a plugin on an unserved server must degrade to its own
        non-agent answer instead of borrowing the public bot's model.
        """
        resolver = getattr(self.host, "resolver", None)
        if resolver is None:
            return self.host.agent
        try:
            brain = resolver.resolve(self.guild, self.user)
        except Exception:                  # noqa: BLE001 - a resolver bug is "none"
            return None
        return getattr(brain, "agent", None)

    @property
    def is_admin(self) -> bool:
        return bool(self.permissions & (MANAGE_GUILD | ADMINISTRATOR)) \
            or self.user in self.host.operators

    def opt(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    # -- answers ---------------------------------------------------------------
    def update(self, text: str = "", embeds: list | None = None,
               components: list | None = None):
        """Edit the message a button sits on (for ``on_button`` handlers)."""
        from awdiscord.dispatch import NO_MENTIONS, UPDATE, Result, clip
        data: dict[str, Any] = {"content": clip(text), "allowed_mentions": NO_MENTIONS}
        if embeds is not None:
            data["embeds"] = embeds[:10]
        if components is not None:
            data["components"] = components
        return Result({"type": UPDATE, "data": data}, log={"plugin": self.plugin.name})

    def modal(self, payload: str, title: str, fields: list[dict]):
        """Open a modal; its submission reaches this plugin's ``on_modal``.

        ``fields``: ``{"id", "label", "placeholder"?, "max_length"?, "required"?,
        "paragraph"?}``, each a text input (at most five); ``paragraph: True``
        makes it a multi-line one.
        """
        rows = []
        for f in fields[:5]:
            comp = {"type": 4, "custom_id": str(f["id"])[:100],
                    "style": 2 if f.get("paragraph") else 1,
                    "label": str(f["label"])[:45],
                    "required": bool(f.get("required", True))}
            if f.get("placeholder"):
                comp["placeholder"] = str(f["placeholder"])[:100]
            if f.get("max_length"):
                comp["max_length"] = int(f["max_length"])
            rows.append({"type": 1, "components": [comp]})
        from awdiscord.dispatch import Result
        return Result({"type": MODAL, "data": {"custom_id": self.plugin.button_id(payload),
                                               "title": title[:45], "components": rows}},
                      log={"plugin": self.plugin.name, "user": self.user, "modal": payload})

    def reply(self, text: str = "", private: bool = False, embeds: list | None = None,
              components: list | None = None):
        from awdiscord.dispatch import EPHEMERAL, MESSAGE, NO_MENTIONS, Result, clip
        data: dict[str, Any] = {"content": clip(text), "allowed_mentions": NO_MENTIONS}
        if embeds:
            data["embeds"] = embeds[:10]
        if components:
            data["components"] = components
        if private:
            data["flags"] = EPHEMERAL
        return Result({"type": MESSAGE, "data": data},
                      log={"plugin": self.plugin.name, "user": self.user})

    def defer(self, work: Callable[[], str | dict], private: bool = False):
        """Acknowledge now, edit the reply with ``work()`` when it finishes."""
        from awdiscord.dispatch import DEFERRED, NO_MENTIONS, Result, clip

        def run() -> dict:
            try:
                out = work()
            except Exception as exc:  # a plugin bug must still answer the user
                return {"content": f"{self.plugin.name} failed: {type(exc).__name__}: {exc}"[:500],
                        "allowed_mentions": NO_MENTIONS}
            if isinstance(out, dict):
                out.setdefault("allowed_mentions", NO_MENTIONS)
                return out
            return {"content": clip(str(out)), "allowed_mentions": NO_MENTIONS}

        return Result({"type": DEFERRED, "data": {"flags": 64} if private else {}},
                      deferred=run, log={"plugin": self.plugin.name, "user": self.user})


@dataclass
class MemberJoin:
    """A member joined a server where the plugin is on.

    ``send_dm(user_id, payload)`` posts a message to that member's DMs and raises
    on refusal (a member who closed DMs answers 403); the host logs a raise and
    moves on, so one closed inbox never stops another plugin's greeting.
    """
    host: "PluginHost"
    plugin: Plugin
    guild: str
    user: str
    member: dict
    send_dm: Callable[[str, dict], Any]

    @property
    def store(self) -> "ScopedStore":
        return ScopedStore(self.host.store, self.guild, self.plugin.name)


@dataclass
class GuildEvent:
    """The bot was added to (join) or removed from (remove) a server.

    The same event serves both hooks; which hook was called says which it is.
    ``system_channel_id`` is where Discord says the bot may post its system
    messages, and is empty when the server has none. A hook that needs to act
    uses ``store`` (state scoped to this guild and plugin) and ``rest`` (the
    bot's REST client, None in a host with no transport).
    """
    host: "PluginHost"
    plugin: Plugin
    guild_id: str
    name: str
    owner_id: str
    system_channel_id: str

    @property
    def store(self) -> "ScopedStore":
        return ScopedStore(self.host.store, self.guild_id, self.plugin.name)

    @property
    def rest(self):
        return self.host.rest


@dataclass
class MessageEvent:
    """A human's message in a server where the plugin is on.

    ``rest`` is the bot's REST client (``awdiscord.rest.Rest``) when the gateway
    set one, so a hook can post an answer; None in a host with no transport.
    """
    host: "PluginHost"
    plugin: Plugin
    guild: str
    channel: str
    user: str
    message_id: str
    content: str
    raw: dict

    @property
    def store(self) -> "ScopedStore":
        return ScopedStore(self.host.store, self.guild, self.plugin.name)

    @property
    def rest(self):
        return self.host.rest


#: The store "guild" key for state a hook keeps about a DM (no guild).
DM_SCOPE = "@dm"


@dataclass
class ReplyEvent:
    """The agent answered and the answer is posted.

    ``message_id`` is the POSTED answer (what a reaction or a button later
    names); ``prompt_message_id`` the message that asked (empty for /ask).
    ``reply`` is the full answer before Discord's 2000-character clip. ``via``
    is ``mention``, ``dm`` or ``ask``. Every field comes from the gateway and
    the dispatcher, never from the model or the message text.
    """
    host: "PluginHost"
    plugin: Plugin
    guild: str
    channel: str
    user: str
    message_id: str
    prompt_message_id: str
    prompt: str
    reply: str
    agent: str
    via: str
    raw: dict

    @property
    def store(self) -> "ScopedStore":
        return ScopedStore(self.host.store, self.guild or DM_SCOPE, self.plugin.name)


@dataclass
class ReactionEvent:
    """A human added (``added``) or removed a reaction on a message.

    ``emoji`` is the unicode emoji, or ``<:name:id>`` for a custom one.
    ``author`` = the reacted message's author id when Discord sent it.
    """
    host: "PluginHost"
    plugin: Plugin
    guild: str
    channel: str
    user: str
    message_id: str
    emoji: str
    added: bool
    author: str
    raw: dict

    @property
    def store(self) -> "ScopedStore":
        return ScopedStore(self.host.store, self.guild or DM_SCOPE, self.plugin.name)

    @property
    def rest(self):
        return self.host.rest


class ScopedStore:
    """The guild store narrowed to one plugin's keys."""

    def __init__(self, store, guild: str, plugin: str):
        self._s, self._g, self._p = store, guild, plugin

    def get(self, key: str, default: Any = None) -> Any:
        return self._s.get(self._g, self._p, key, default)

    def set(self, key: str, value: Any) -> None:
        self._s.set(self._g, self._p, key, value)

    def delete(self, key: str) -> None:
        self._s.delete(self._g, self._p, key)

    def update(self, key: str, fn: Callable[[Any], tuple[Any, Any]],
               default: Any = None) -> Any:
        """Atomic read-modify-write (``GuildStore.update``) of one plugin key."""
        return self._s.update(self._g, self._p, key, fn, default)

    def keys(self, prefix: str = "") -> list[str]:
        return self._s.keys(self._g, self._p, prefix)


# ── the host ──────────────────────────────────────────────────────────────────

def _offered(gate: Callable[[dict], bool], caller: dict, name: str) -> bool:
    """A tool's ``offered`` gate; a gate that raises withholds the tool (fail closed)."""
    try:
        return bool(gate(dict(caller)))
    except Exception as exc:  # a broken gate must not offer a scoped tool everywhere
        log.warning("tool %s: offered() raised %s; not offered", name, exc)
        return False


class PluginHost:
    """Holds the loaded plugins, answers their commands, tracks per-server state."""

    def __init__(self, store, agent=None, operators: set[str] | None = None,
                 plugins: list[Plugin] | None = None, resolver=None, limiter=None):
        self.store = store
        self.agent = agent
        #: (guild, user) -> Brain. When set, ``Ctx.agent`` reads THIS rather than
        #: ``agent``, so a plugin cannot reach the owner's model for a server the
        #: bot does not serve. None keeps the library path (a plain agent).
        self.resolver = resolver
        self.operators = set(operators or ())
        #: The Dispatcher's token buckets, set by it. A plugin MODAL submission is
        #: routed straight here by the gateway, so it never passes
        #: ``Dispatcher._plugin``: without this the only brake on a form is none.
        #: None = build one on first use (the library embedding and tests).
        self.limiter = limiter
        self.plugins: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}
        self._by_command: dict[str, Plugin] = {}
        self._replaced: dict[str, str] = {}     # replaced plugin -> the one that replaced it
        self.desk = None        # the bot's ApprovalDesk, set by the Dispatcher when present
        self.rest = None        # the bot's Rest client, set by the gateway Bot when present
        for p in plugins or []:
            self.add(p)

    # -- registry --------------------------------------------------------------
    def add(self, plugin: Plugin, reserved: set[str] | None = None) -> None:
        # "model" is the core BYO command (/model setup|test|status|off).
        # "brain" is NOT reserved: the deployed aitherium_collective_brain pack
        # owns /brain, and reserving it silently disabled that whole pack.
        reserved = (reserved or set()) | {"ask", "share", "status", "artifact",
                                          "model"}
        if plugin.name in self._replaced:
            raise PluginError(f"plugin {plugin.name!r} is replaced by "
                              f"{self._replaced[plugin.name]!r}")
        if plugin.name in self.plugins:
            raise PluginError(f"plugin {plugin.name!r} is already loaded "
                              f"(from {self.plugins[plugin.name].source})")
        evict = [n for n in plugin.overrides if n in self.plugins]
        for name in evict:
            if self.plugins[name].always_on:
                raise PluginError(f"plugin {plugin.name!r} cannot replace {name!r}: "
                                  "it is always on")
        for cmd in plugin.commands:
            owner = self._by_command.get(cmd.name)
            if owner is not None and owner.name in evict:
                owner = None            # declared replacement: the name is freed
            if cmd.name in reserved or owner is not None:
                raise PluginCommandConflictError(
                    f"plugin {plugin.name!r}: /{cmd.name} is taken by "
                    f"{owner.name if owner else 'the core bot'}")
        for name in evict:              # only after every check passed
            old = self.plugins.pop(name)
            for cmd in old.commands:
                if self._by_command.get(cmd.name) is old:
                    del self._by_command[cmd.name]
            log.info("plugin %s (%s) replaces %s (%s)", plugin.name, plugin.source,
                     name, old.source)
        for name in plugin.overrides:
            self._replaced[name] = plugin.name
        self.plugins[plugin.name] = plugin
        for cmd in plugin.commands:
            self._by_command[cmd.name] = plugin

    def command_defs(self) -> list[dict]:
        """Every plugin command. Registered globally; enablement is checked per server."""
        return [c.to_discord() for p in self.plugins.values() for c in p.commands]

    def owns(self, command: str) -> bool:
        return command in self._by_command

    # -- per-server enablement ----------------------------------------------------
    def enabled(self, guild: str | None, name: str) -> bool:
        p = self.plugins.get(name)
        if p is None:
            return False
        if p.always_on:
            return True
        if not guild:
            return p.default_enabled
        state = self.store.plugin_state(guild, name)
        return p.default_enabled if state is None else state

    def set_enabled(self, guild: str, name: str, on: bool) -> None:
        p = self.plugins.get(name)
        if p is None:
            raise PluginError(f"no plugin named {name!r}")
        if p.always_on and not on:
            raise PluginError(f"{name} cannot be turned off")
        self.store.set_plugin_state(guild, name, on)

    def active(self, guild: str | None) -> list[Plugin]:
        return [p for p in self.plugins.values() if self.enabled(guild, p.name)]

    def tools_for(self, guild: str | None, user: str = "", channel: str = "",
                  message: str = "", outbox=None,
                  role_names=None, question: str = "") -> list[Tool]:
        """Tools of the plugins on in this server (first plugin wins a name clash).

        An ``operators_only`` tool is offered only when ``user`` is an operator:
        a member of a public server never gets to ask about the host.
        ``channel`` / ``message`` = where the ask was made (the mention's message
        id; empty for /ask), so a ``wants_caller`` tool can answer in a thread on
        it. ``role_names`` = the invoker's guild roles as Discord sent them, so a
        tool can ask Genesis for the member's capability view without the plugin
        holding a role map. Like ``user`` they come from Discord's event, never
        from the model. ``question`` = the asker's own words this turn (the host's
        copy), so a tool can tell what the PERSON stated from what the model
        filled in. ``outbox`` = this turn's tray for files a tool produces
        (``awdiscord.outbox.Outbox``); the caller reaches it as
        ``caller["outbox"]`` and what lands there is posted with the answer.
        """
        seen: dict[str, Tool] = {}
        is_op = bool(user) and user in self.operators
        caller = {"user": str(user or ""), "guild": str(guild or ""), "operator": is_op,
                  "grounding": False, "channel": str(channel or ""),
                  "message": str(message or ""), "outbox": outbox,
                  "role_names": [str(r) for r in (role_names or ())],
                  "question": str(question or "")[:4000]}
        for p in self.active(guild):
            for t in p.tools:
                if t.operators_only and not is_op:
                    continue
                gate = getattr(t, "offered", None)
                if gate is not None and not _offered(gate, caller, t.name):
                    continue
                seen.setdefault(t.name, t.bind(caller))
        return list(seen.values())

    def turn_context_for(self, guild: str | None, caller: dict) -> str:
        """Each ON plugin's ``turn_context(caller)``, joined; a failing hook adds nothing."""
        parts = []
        for p in self.active(guild):
            fn = getattr(p, "turn_context", None)
            if fn is None:
                continue
            try:
                text = str(fn(dict(caller)) or "").strip()
            except Exception as exc:  # noqa: BLE001 - one pack must not break the turn
                log.warning("turn_context of %s failed: %s", p.name, type(exc).__name__)
                continue
            if text:
                parts.append(text)
        return "\n".join(parts)

    def persona_for(self, guild: str | None) -> str:
        """Persona fragments and skill names of the plugins on in this server."""
        parts = []
        for p in self.active(guild):
            if p.persona:
                parts.append(p.persona.strip())
            if p.skills:
                parts.append(f"Skills from {p.name}: " + ", ".join(sorted(p.skills)))
        return "\n\n".join(parts)

    # -- dispatch --------------------------------------------------------------
    def handle(self, interaction: dict):
        from awdiscord.dispatch import Dispatcher
        data = interaction.get("data") or {}
        name = data.get("name", "")
        plugin = self._by_command.get(name)
        if plugin is None:
            return None
        guild = interaction.get("guild_id")
        if not self.enabled(guild, plugin.name):
            return Dispatcher._reply(
                f"/{name} belongs to the **{plugin.name}** plugin, which is off in this "
                f"server. An admin can turn it on with `/plugins enable {plugin.name}`.",
                private=True, log={"plugin": plugin.name, "disabled": True})
        cmd = next(c for c in plugin.commands if c.name == name)
        options, sub = _flatten(data.get("options") or [])
        target = cmd
        if cmd.subcommands:
            target = _find_sub(cmd, sub)
            if target is None:
                return Dispatcher._reply("Pick a subcommand.", private=True)
        ctx = Ctx(self, plugin, interaction, options, sub)
        if (cmd.admin or target.admin) and not ctx.is_admin:
            return Dispatcher._reply("Only server managers can do that.", private=True,
                                     log={"plugin": plugin.name, "denied": ctx.user})
        if target.handler is None:
            return Dispatcher._reply("Not implemented.", private=True)
        try:
            return target.handler(ctx)
        except PluginError as exc:
            return Dispatcher._reply(str(exc), private=True)


    def handle_button(self, interaction: dict):
        """A press on a button a plugin posted, or None when it is not a plugin's."""
        from awdiscord.dispatch import Dispatcher
        cid = (interaction.get("data") or {}).get("custom_id", "")
        if not cid.startswith(BUTTON_PREFIX):
            return None
        name, _, payload = cid[len(BUTTON_PREFIX):].partition(":")
        plugin = self.plugins.get(name)
        if plugin is None or plugin.on_button is None:
            return Dispatcher._reply("That button is no longer handled.", private=True)
        if not self.enabled(interaction.get("guild_id"), name):
            return Dispatcher._reply(f"The {name} plugin is off in this server.", private=True)
        ctx = Ctx(self, plugin, interaction, {}, None)
        try:
            return plugin.on_button(ctx, payload)
        except PluginError as exc:
            return Dispatcher._reply(str(exc), private=True)


    def handle_modal(self, interaction: dict):
        """A modal a plugin opened was submitted, or None when it is not a plugin's."""
        from awdiscord.dispatch import Dispatcher
        data = interaction.get("data") or {}
        cid = data.get("custom_id", "")
        if not cid.startswith(BUTTON_PREFIX):
            return None
        name, _, payload = cid[len(BUTTON_PREFIX):].partition(":")
        plugin = self.plugins.get(name)
        if plugin is None or plugin.on_modal is None:
            return Dispatcher._reply("That form is no longer handled.", private=True)
        if not self.enabled(interaction.get("guild_id"), name):
            return Dispatcher._reply(f"The {name} plugin is off in this server.", private=True)
        member = interaction.get("member") or {}
        user = str(((member.get("user") or interaction.get("user")) or {}).get("id") or "")
        limiter = self.limiter
        if limiter is None:
            from awdiscord.ratelimit import RateLimiter
            limiter = self.limiter = RateLimiter()
        verdict = limiter.check(user, interaction.get("guild_id"))
        if not verdict.allowed:
            return Dispatcher._reply(f"Slow down - try again in {verdict.retry_after:g}s "
                                     f"({verdict.scope} limit).", private=True,
                                     log={"limited": verdict.scope})
        values = {}
        for row in data.get("components") or []:
            for comp in row.get("components") or []:
                if comp.get("custom_id"):
                    values[str(comp["custom_id"])] = str(comp.get("value") or "")
        ctx = Ctx(self, plugin, interaction, values, None)
        try:
            return plugin.on_modal(ctx, payload, values)
        except PluginError as exc:
            return Dispatcher._reply(str(exc), private=True)

    def dispatch_member_join(self, guild: str, member: dict,
                             send_dm: Callable[[str, dict], Any]) -> dict[str, str]:
        """Run every enabled plugin's join hooks. Returns plugin -> "ok" | the error.

        Only plugins ON in ``guild`` see the event. A hook that raises is logged
        and recorded; the rest still run.
        """
        guild = str(guild or "")
        user = str(((member or {}).get("user") or {}).get("id") or "")
        out: dict[str, str] = {}
        if not guild or not user:
            return out
        for p in self.active(guild):
            if not p.on_member_join:
                continue
            event = MemberJoin(self, p, guild, user, member or {}, send_dm)
            for hook in p.on_member_join:
                try:
                    hook(event)
                    out.setdefault(p.name, "ok")
                except Exception as exc:  # one plugin's failure never stops the rest
                    log.warning("member-join hook %s failed: %s", p.name, exc)
                    out[p.name] = f"{type(exc).__name__}: {exc}"[:200]
        return out

    def _dispatch_guild(self, attr: str, guild: dict) -> dict[str, str]:
        """Run every enabled plugin's ``attr`` hooks with one GuildEvent.

        Returns plugin -> "ok" | the error. Only plugins ON in that server see
        the event (a guild the bot just joined has no state, so ``enabled``
        falls back to ``default_enabled``/``always_on``). A hook that raises is
        logged and recorded; the rest still run.
        """
        guild = dict(guild or {})
        guild_id = str(guild.get("id") or "")
        out: dict[str, str] = {}
        if not guild_id:
            return out
        for p in self.active(guild_id):
            hooks = getattr(p, attr)
            if not hooks:
                continue
            event = GuildEvent(self, p, guild_id, str(guild.get("name") or ""),
                               str(guild.get("owner_id") or ""),
                               str(guild.get("system_channel_id") or ""))
            for hook in hooks:
                try:
                    hook(event)
                    out.setdefault(p.name, "ok")
                except Exception as exc:  # one plugin's failure never stops the rest
                    log.warning("%s hook %s failed: %s", attr, p.name, exc)
                    out[p.name] = f"{type(exc).__name__}: {exc}"[:200]
        return out

    def dispatch_guild_join(self, guild: dict) -> dict[str, str]:
        """The bot joined ``guild`` (keys id, name, owner_id, system_channel_id)."""
        return self._dispatch_guild("on_guild_join", guild)

    def dispatch_guild_remove(self, guild: dict) -> dict[str, str]:
        """The bot was removed from ``guild`` (same keys as a join)."""
        return self._dispatch_guild("on_guild_remove", guild)


    def has_message_hooks(self, guild: str | None) -> bool:
        """True when a plugin ON in ``guild`` registered an ``on_message`` hook."""
        if not guild:
            return False
        return any(p.on_message for p in self.active(str(guild)))

    def dispatch_message(self, msg: dict) -> bool:
        """Offer one human server message to the active plugins' message hooks.

        ``msg``: ``{"id", "channel_id", "guild_id", "user", "content", ...}``.
        Returns True when a hook answered (the first one that does wins). A hook
        that raises is logged and the next one still runs.
        """
        guild = str(msg.get("guild_id") or "")
        if not guild:
            return False
        for p in self.active(guild):
            if not p.on_message:
                continue
            event = MessageEvent(self, p, guild, str(msg.get("channel_id") or ""),
                                 str(msg.get("user") or ""), str(msg.get("id") or ""),
                                 str(msg.get("content") or ""), msg)
            for hook in p.on_message:
                try:
                    if hook(event):
                        return True
                except Exception as exc:  # one plugin's failure never stops the rest
                    log.warning("message hook %s failed: %s", p.name, exc)
        return False


    # -- answers and reactions --------------------------------------------------
    def _hooked(self, guild: str | None, attr: str) -> list[Plugin]:
        """Plugins with an ``attr`` hook, ON in ``guild``; in a DM, the ones on
        by default (a DM has no server whose admin could switch a plugin on)."""
        if guild:
            return [p for p in self.active(str(guild)) if getattr(p, attr)]
        return [p for p in self.plugins.values()
                if getattr(p, attr) and (p.always_on or p.default_enabled)]

    def has_reply_hooks(self, guild: str | None) -> bool:
        return bool(self._hooked(guild, "on_reply"))

    def dispatch_reply(self, turn: dict) -> list[dict]:
        """Run the reply hooks for one posted answer; the component rows to add.

        ``turn``: ``{"guild_id", "channel_id", "user", "message_id",
        "prompt_message_id", "prompt", "reply", "agent", "via"}``. At most five
        rows come back (Discord's cap); a hook that raises is logged and skipped.
        """
        guild = str(turn.get("guild_id") or "")
        rows: list[dict] = []
        if not str(turn.get("message_id") or ""):
            return rows
        for p in self._hooked(guild, "on_reply"):
            event = ReplyEvent(self, p, guild, str(turn.get("channel_id") or ""),
                               str(turn.get("user") or ""), str(turn["message_id"]),
                               str(turn.get("prompt_message_id") or ""),
                               str(turn.get("prompt") or ""), str(turn.get("reply") or ""),
                               str(turn.get("agent") or ""), str(turn.get("via") or ""),
                               turn)
            for hook in p.on_reply:
                try:
                    got = hook(event)
                except Exception as exc:  # one plugin's failure never stops the rest
                    log.warning("reply hook %s failed: %s", p.name, exc)
                    continue
                if isinstance(got, list):
                    rows += [r for r in got if isinstance(r, dict)]
        return rows[:5]

    def has_reaction_hooks(self, guild: str | None = None) -> bool:
        """Any reaction hook at all (``guild`` None), or one ON in ``guild``."""
        if guild is None:
            return any(p.on_reaction for p in self.plugins.values())
        return bool(self._hooked(guild, "on_reaction"))

    def dispatch_reaction(self, evt: dict) -> dict[str, str]:
        """Hand one reaction to the hooks; plugin -> "ok" | the error.

        ``evt``: ``{"guild_id", "channel_id", "user", "message_id", "emoji",
        "added", "author"?, "user_bot"?}``. A bot's reaction is dropped here
        as well as in the gateway, so no transport can loop a hook.
        """
        out: dict[str, str] = {}
        user = str(evt.get("user") or "")
        if not user or evt.get("user_bot") or not str(evt.get("message_id") or ""):
            return out
        guild = str(evt.get("guild_id") or "")
        for p in self._hooked(guild, "on_reaction"):
            event = ReactionEvent(self, p, guild, str(evt.get("channel_id") or ""), user,
                                  str(evt["message_id"]), str(evt.get("emoji") or ""),
                                  bool(evt.get("added", True)),
                                  str(evt.get("author") or ""), evt)
            for hook in p.on_reaction:
                try:
                    hook(event)
                    out.setdefault(p.name, "ok")
                except Exception as exc:  # one plugin's failure never stops the rest
                    log.warning("reaction hook %s failed: %s", p.name, exc)
                    out[p.name] = f"{type(exc).__name__}: {exc}"[:200]
        return out


def _flatten(options: list[dict]) -> tuple[dict, str | None]:
    """Options of a plain command, or of its chosen subcommand."""
    if options and options[0].get("type") in (1, 2):
        first = options[0]
        if first.get("type") == 2:          # group -> subcommand
            inner = (first.get("options") or [{}])[0]
            return ({o["name"]: o.get("value") for o in inner.get("options") or []},
                    f"{first['name']} {inner.get('name', '')}".strip())
        return ({o["name"]: o.get("value") for o in first.get("options") or []},
                first.get("name"))
    return {o["name"]: o.get("value") for o in options}, None


# ── loading ───────────────────────────────────────────────────────────────────

def plugin_dirs() -> list[Path]:
    dirs = [Path(d) for d in os.getenv("AWDISCORD_PLUGIN_DIRS", "").split(os.pathsep) if d]
    dirs.append(Path(os.getenv("AWDISCORD_STATE_DIR", str(Path.home() / ".awdiscord")))
                / "plugins")
    return dirs


def _from_module(mod, source: str) -> Plugin:
    plugin = getattr(mod, "PLUGIN", None)
    if not isinstance(plugin, Plugin):
        raise PluginError(f"{source}: no PLUGIN = Plugin(...) defined")
    plugin.source = source
    return plugin


def discover(include_builtin: bool = True) -> tuple[list[Plugin], dict[str, str]]:
    """Every plugin that can be found, and the reason each broken one failed."""
    found: list[Plugin] = []
    errors: dict[str, str] = {}
    names: set[str] = set()

    replaced: set[str] = set()

    def keep(p: Plugin) -> None:
        if p.name in names or p.name in replaced:
            return
        for old in p.overrides:          # an explicit replacement, never an accident
            if any(q.name == old and q.always_on for q in found):
                continue                 # PluginHost.add refuses it and records why
            replaced.add(old)
            if old in names:
                names.discard(old)
                found[:] = [q for q in found if q.name != old]
        names.add(p.name)
        found.append(p)

    if include_builtin:
        from awdiscord.builtin import BUILTIN_MODULES
        for modname in BUILTIN_MODULES:
            keep(_from_module(importlib.import_module(modname), "builtin"))

    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") \
            else eps.get(ENTRY_POINT_GROUP, [])
        for ep in group:
            try:
                obj = ep.load()
                if isinstance(obj, Plugin):
                    obj.source = f"entry-point:{ep.name}"
                    keep(obj)
                else:
                    keep(_from_module(obj, f"entry-point:{ep.name}"))
            except Exception as exc:
                errors[f"entry-point:{ep.name}"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - metadata itself broken
        errors["entry-points"] = str(exc)

    from awdiscord.packs import agent_pack_plugin, is_agent_pack
    for d in plugin_dirs():
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            if path.name.startswith(("_", ".")):
                continue
            if is_agent_pack(path):
                try:
                    p = agent_pack_plugin(path)
                    p.source = str(path)
                    keep(p)
                except Exception as exc:
                    errors[str(path)] = f"{type(exc).__name__}: {exc}"
                continue
            target = path / "__init__.py" if path.is_dir() else path
            if target.suffix != ".py" or not target.exists():
                continue
            modname = f"awdiscord_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(
                    modname, target,
                    submodule_search_locations=[str(path)] if path.is_dir() else None)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[modname] = mod
                spec.loader.exec_module(mod)
                keep(_from_module(mod, str(path)))
            except Exception as exc:
                errors[str(path)] = f"{type(exc).__name__}: {exc}"
    return found, errors


def build_host(store, agent=None, operators: set[str] | None = None,
               only: list[str] | None = None, resolver=None) -> PluginHost:
    """Discover and load plugins. ``only`` (AWDISCORD_PLUGINS) narrows the set."""
    plugins, errors = discover()
    host = PluginHost(store, agent=agent, operators=operators, resolver=resolver)
    host.errors.update(errors)
    for p in plugins:
        if only and p.name not in only and not p.always_on:
            continue
        try:
            host.add(p)
        except PluginCommandConflictError as exc:
            # A whole pack disappears when one command name collides. Say so at
            # ERROR with the plugin's name; host.errors alone is swallowed into
            # the startup log and nobody notices a feature is gone.
            host.errors[p.source] = str(exc)
            log.error("plugin %s failed to load: %s", p.name, exc)
        except PluginError as exc:
            host.errors[p.source] = str(exc)
    return host
