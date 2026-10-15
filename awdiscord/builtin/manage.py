"""/plugins - list, enable and disable plugins in this server. Always on."""
from __future__ import annotations

from awdiscord.plugins import Command, Option, Plugin, PluginError


def _list(ctx):
    host = ctx.host
    lines = []
    for p in sorted(host.plugins.values(), key=lambda p: p.name):
        on = host.enabled(ctx.guild, p.name)
        mark = "on " if on else "off"
        cmds = " ".join(f"/{c.name}" for c in p.commands)
        lines.append(f"`{mark}` **{p.name}** {p.version} - {p.description}"
                     + (f"\n      {cmds}" if cmds else ""))
    if host.errors and ctx.is_admin:
        lines.append("\nFailed to load:")
        lines += [f"- {src}: {err}" for src, err in sorted(host.errors.items())]
    return ctx.reply("\n".join(lines) or "No plugins installed.", private=True)


def _toggle(on: bool):
    def handler(ctx):
        name = str(ctx.opt("name", "")).strip()
        try:
            ctx.host.set_enabled(ctx.guild, name, on)
        except PluginError as exc:
            return ctx.reply(str(exc), private=True)
        verb = "on" if on else "off"
        return ctx.reply(f"**{name}** is {verb} in this server.", private=True)
    return handler


def _info(ctx):
    name = str(ctx.opt("name", "")).strip()
    p = ctx.host.plugins.get(name)
    if p is None:
        return ctx.reply(f"No plugin named {name!r}.", private=True)
    lines = [f"**{p.name}** {p.version} ({p.source})", p.description]
    for c in p.commands:
        subs = ", ".join(s.name for s in c.subcommands)
        lines.append(f"- /{c.name}{' [' + subs + ']' if subs else ''} - {c.description}"
                     + (" (managers)" if c.admin else ""))
    if p.skills:
        lines.append("skills: " + ", ".join(sorted(p.skills)))
    if p.homepage:
        lines.append(p.homepage)
    return ctx.reply("\n".join(lines), private=True)


_name = Option("name", "Plugin name", required=True)

PLUGIN = Plugin(
    name="plugins",
    version="0.1.0",
    description="Turn plugins on and off in this server",
    always_on=True,
    commands=[Command("plugins", "Manage this server's plugins", subcommands=[
        Command("list", "What is installed and what is on here", _list),
        Command("info", "What a plugin does", _info, options=[_name]),
        Command("enable", "Turn a plugin on in this server", _toggle(True),
                options=[_name], admin=True),
        Command("disable", "Turn a plugin off in this server", _toggle(False),
                options=[_name], admin=True),
    ])],
)
