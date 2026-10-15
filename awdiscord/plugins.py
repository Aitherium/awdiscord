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

Discovery, in order (a later source never replaces an earlier name):
  1. the built-ins shipped in ``awdiscord.builtin``
  2. Python entry points in the group ``awdiscord.plugins``
     (``pip install awdiscord-pack-foo`` is enough)
  3. ``*.py`` files and packages in each dir of ``AWDISCORD_PLUGIN_DIRS``
     (os.pathsep-separated) and in ``~/.awdiscord/plugins``

Handlers are plain functions ``(ctx) -> Result``. Use ``ctx.reply`` for an
answer and ``ctx.defer(fn)`` for slow work (the agent, an HTTP call): Discord
gets an acknowledgement at once and the reply is edited when ``fn`` returns.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ENTRY_POINT_GROUP = "awdiscord.plugins"
BUTTON_PREFIX = "awp:"
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
            body["options"] = [{"type": 1, "name": s.name,
                                "description": s.description[:100],
                                "options": [o.to_discord() for o in s.options]}
                               for s in self.subcommands]
        elif self.options:
            body["options"] = [o.to_discord() for o in self.options]
        if self.admin:
            # Hidden from members by default; admins can re-grant per role in
            # Server Settings > Integrations. The host still checks at run time.
            body["default_member_permissions"] = str(MANAGE_GUILD)
        return body


@dataclass
class Tool:
    """A function the agent may call while answering /ask or a mention.

    ``fn(**arguments) -> str``. Whatever it returns (or the error it raises) is
    handed back to the model, so return text a model can read.
    """
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    operators_only: bool = False      # offered only to the bot's operators

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name,
                                                 "description": self.description[:1000],
                                                 "parameters": self.parameters}}

    def run(self, args: dict) -> str:
        return str(self.fn(**(args or {})))


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
    homepage: str = ""
    source: str = "builtin"                # filled in by the loader

    def button_id(self, payload: str) -> str:
        """A component custom_id that routes back to this plugin's ``on_button``."""
        cid = f"{BUTTON_PREFIX}{self.name}:{payload}"
        if len(cid) > 100:
            raise PluginError("button payload too long (custom_id is capped at 100)")
        return cid


class PluginError(Exception):
    pass


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
        return self.host.agent

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

    def keys(self, prefix: str = "") -> list[str]:
        return self._s.keys(self._g, self._p, prefix)


# ── the host ──────────────────────────────────────────────────────────────────

class PluginHost:
    """Holds the loaded plugins, answers their commands, tracks per-server state."""

    def __init__(self, store, agent=None, operators: set[str] | None = None,
                 plugins: list[Plugin] | None = None):
        self.store = store
        self.agent = agent
        self.operators = set(operators or ())
        self.plugins: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}
        self._by_command: dict[str, Plugin] = {}
        for p in plugins or []:
            self.add(p)

    # -- registry --------------------------------------------------------------
    def add(self, plugin: Plugin, reserved: set[str] | None = None) -> None:
        reserved = (reserved or set()) | {"ask", "share", "status"}
        if plugin.name in self.plugins:
            raise PluginError(f"plugin {plugin.name!r} is already loaded "
                              f"(from {self.plugins[plugin.name].source})")
        for cmd in plugin.commands:
            owner = self._by_command.get(cmd.name)
            if cmd.name in reserved or owner is not None:
                raise PluginError(f"plugin {plugin.name!r}: /{cmd.name} is taken by "
                                  f"{owner.name if owner else 'the core bot'}")
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

    def tools_for(self, guild: str | None, user: str = "") -> list[Tool]:
        """Tools of the plugins on in this server (first plugin wins a name clash).

        An ``operators_only`` tool is offered only when ``user`` is an operator:
        a member of a public server never gets to ask about the host.
        """
        seen: dict[str, Tool] = {}
        is_op = bool(user) and user in self.operators
        for p in self.active(guild):
            for t in p.tools:
                if t.operators_only and not is_op:
                    continue
                seen.setdefault(t.name, t)
        return list(seen.values())

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
            target = next((s for s in cmd.subcommands if s.name == sub), None)
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

    def keep(p: Plugin) -> None:
        if p.name not in names:
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
               only: list[str] | None = None) -> PluginHost:
    """Discover and load plugins. ``only`` (AWDISCORD_PLUGINS) narrows the set."""
    plugins, errors = discover()
    host = PluginHost(store, agent=agent, operators=operators)
    host.errors.update(errors)
    for p in plugins:
        if only and p.name not in only and not p.always_on:
            continue
        try:
            host.add(p)
        except PluginError as exc:
            host.errors[p.source] = str(exc)
    return host
