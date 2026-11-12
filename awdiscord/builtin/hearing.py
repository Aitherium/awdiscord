"""``/aither settings``: one private view of what Aither hears here, with buttons.

The switches and their setters live in :mod:`awdiscord.hearing`; this is the Discord
face. Anyone may look (what is listened to is never hidden); the buttons re-check
who pressed them. ``/aither`` is a shared group, so a pack's ``/aither feedback``
sits beside it.
"""
from __future__ import annotations

import time

from awdiscord import hearing
from awdiscord.plugins import Command, Plugin

_VIS_LABEL = {"public": "public", "private": "private", "bot_channel": "bot channel"}


def _button(label: str, payload: str, *, style: int = 2, disabled: bool = False) -> dict:
    return {"type": 2, "style": style, "label": label[:80], "disabled": disabled,
            "custom_id": PLUGIN.button_id(payload)}


def render(ctx, expanded: bool = False, said: str = "") -> tuple:
    """``(text, components)`` for this server, read fresh on every call."""
    rows = hearing.snapshot(ctx.guild, ctx.host.store, host=ctx.host)
    v = hearing.view(rows, expanded)
    owner = ctx.user in hearing.owners_from_env()
    lines = [(said + "\n" if said else "") + hearing.headline(rows)]
    for i, r in enumerate(v["shown"], 1):
        lines.append(f"{i}. **{r['label']}**: `{r['value']}` - {r['detail']}. "
                     f"_Changed by {r['who']}._")
    if v["more"]:
        lines.append(f"+{v['more']} more (of {v['total']})")
    lines.append(hearing.next_action(rows, ctx.is_admin or owner))
    by = {r["key"]: r for r in rows}
    row1 = []
    calls = by["calls"]["change"]
    if calls:
        # Turning listening ON is the owner's alone; the button says so by being off.
        row1.append(_button(f"Calls: turn {calls}", f"set:calls:{calls}",
                            style=4 if calls == "off" else 2,
                            disabled=calls == "on" and not owner))
    current = by["visibility"]["value"]
    for mode in ("public", "private", "bot_channel"):
        row1.append(_button(f"Replies: {_VIS_LABEL[mode]}", f"set:visibility:{mode}",
                            style=1 if mode == current else 2, disabled=mode == current))
    row2 = []
    if v["more"]:
        row2.append(_button(f"+{v['more']} more", "show:all"))
    elif expanded and v["total"] > hearing.VISIBLE_CAP:
        row2.append(_button("Show fewer", "show:top"))
    row2.append(_button("History", "history"))
    return "\n".join(lines), [{"type": 1, "components": row1},
                              {"type": 1, "components": row2}]


def _settings(ctx):
    text, components = render(ctx)
    return ctx.reply(text, private=True, components=components)


def _history(ctx) -> str:
    entries, total = hearing.audit_log(ctx.host.store, ctx.guild)
    if not entries:
        return "**History**: no setting has been changed from Discord in this server."
    lines = [f"**History**: {total} change(s), newest first."]
    for i, e in enumerate(entries, 1):
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(e.get("at", 0))))
        lines.append(f"{i}. {when} - <@{e.get('by')}> set **{e.get('what')}** "
                     f"`{e.get('old')}` -> `{e.get('new')}`")
    if total > len(entries):
        lines.append(f"+{total - len(entries)} more")
    return "\n".join(lines)


def _on_button(ctx, payload: str):
    kind, _, rest = payload.partition(":")
    if kind == "history":
        return ctx.reply(_history(ctx), private=True)
    if kind == "show":
        text, components = render(ctx, expanded=rest == "all")
        return ctx.update(text, components=components)
    if kind == "set":
        key, _, value = rest.partition(":")
        _, said = hearing.change(ctx.host.store, ctx.guild, ctx.user, key, value,
                                 is_manager=ctx.is_admin,
                                 owners=hearing.owners_from_env())
        text, components = render(ctx, said=said)
        return ctx.update(text, components=components)
    return ctx.reply("Unknown button.", private=True)


PLUGIN = Plugin(
    name="hearing",
    version="0.1.0",
    description="What Aither hears and answers in this server, and the switches for it",
    always_on=True,
    on_button=_on_button,
    commands=[Command("aither", "Aither in this server", shared=True, subcommands=[
        Command("settings", "What Aither hears and answers here, and how to change it",
                _settings)])],
)
