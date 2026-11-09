"""Template: set a server up as a software dev and deployment business, in one press.

    /template plan  kind:minimal|full|partner [projects:"api, web"]   what it would create
    /template apply kind:minimal|full|partner [projects:"api, web"]   the plan, then Confirm
    /template names set:professional|themed                             how roles read
    /template project repo     a LISTED verified project's card, then Confirm (operators)

``partner`` adds ONE "Aitherium" category to somebody else's server (#bot, #join and a
verified-projects forum) and touches nothing else. ``/template project`` posts a listed
project's card there with the program's own fields (repo, verified date, report, looking
for contributors); a project looking for contributors also gets a "+ Join as
contributor" button, a squad role with no permissions and a squad in the join form.

WHO: a server's own admins (Manage Server or Administrator IN THAT SERVER) and the
bot's operators may plan and apply a layout; every command, card button and Confirm
press re-checks. Posting a verified project is operator-only: the badge is granted by
the Aitherium Verified program (its own table, ``AWDISCORD_VERIFIED_PROJECTS``), never
self-applied. Nothing is written until Confirm, and Confirm re-plans first.

When the bot joins a server it posts ONE card in the server's system channel offering
"Set up this server as a dev business (Minimal | Full | Aitherium section)". A press
shows the plan, a Confirm applies it. ``apply`` is additive only
(``awdiscord.guildspec``): it never deletes, renames or edits a channel or role that
exists. A missing Manage Roles /
Manage Channels permission is named before anything is written, and nothing is.

After a successful apply the template's plugins are switched on where no admin has
already chosen (``awdiscord.devbiz.enable_packs``).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import urllib.error

from awdiscord import devbiz
from awdiscord.plugins import ADMINISTRATOR, MANAGE_GUILD, Command, Option, Plugin

log = logging.getLogger("awdiscord.template")

NAME = "template"
PENDING_TTL = 15 * 60
CARD_KEY = "card_posted"
MAX_LINES = 25

#: Tests (and an embedding without a gateway) set this to a guildspec-style client.
CLIENT_FACTORY = None
#: Reads the Aitherium Verified table (tests replace it).
VERIFIED_LOADER = devbiz.load_verified


def _client(host):
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(host)
    if getattr(host, "rest", None) is None:
        return None
    return devbiz.RestDiscord(host.rest)


def is_operator(ctx) -> bool:
    """One of the bot's operators (Aitherium staff)."""
    return bool(ctx.user) and ctx.user in ctx.host.operators


def can_layout(ctx) -> bool:
    """An operator, or an admin OF THIS SERVER (Manage Server or Administrator).

    ``ctx.permissions`` is what Discord computed for the presser in the guild the
    interaction came from, so an admin of one server is never an admin of another.
    """
    if is_operator(ctx):
        return True
    return bool(ctx.guild) and bool(ctx.permissions & (MANAGE_GUILD | ADMINISTRATOR))


def _refuse(ctx):
    return ctx.reply("Only this server's admins (Manage Server) or the bot's operators "
                     "can lay out a server.", private=True)


def _refuse_project(ctx):
    return ctx.reply("Only the bot's operators post verified projects: the badge is "
                     "granted by the Aitherium Verified program, never self-applied.",
                     private=True)


def _kind(raw) -> str:
    kind = str(raw or "minimal").strip().lower()
    if kind not in devbiz.TEMPLATES:
        raise ValueError("kind must be one of: " + ", ".join(devbiz.TEMPLATES))
    return kind


def _summary(pv: dict) -> str:
    would = pv["would"]
    roles = [x for x in would if x.startswith("would create role ")]
    chans = [x for x in would if x not in roles]
    head = (f"**Dev business ({pv['kind']})**, names: {pv['name_set']}\n"
            f"{len(roles)} role(s) and {len(chans)} channel(s) to create; nothing is "
            f"deleted, renamed or edited.")
    lines = [head]
    if not would:
        lines.append("This server already has everything in the template.")
    shown = would[:MAX_LINES]
    lines += [f"- {x[len('would '):]}" for x in shown]
    if len(would) > len(shown):
        lines.append(f"- +{len(would) - len(shown)} more")
    lines += [f"- {n}" for n in pv["notes"][:5]]
    if pv["missing"]:
        lines.append("**The bot is missing:** " + "; ".join(pv["missing"])
                     + ". Grant it to the bot's role, then press again. Nothing was written.")
    packs = devbiz.packs_for(pv["kind"])
    lines.append("Plugins it turns on (where no admin has chosen): " + ", ".join(packs))
    return "\n".join(lines)[:1990]


def _plan_and_offer(ctx, kind: str, projects: str, confirm: bool):
    """Defer: read the server, show the plan, and (``confirm``) a Confirm button."""
    host, guild, user = ctx.host, str(ctx.guild or ""), ctx.user
    names = devbiz.name_set(host.store, guild)
    store = ctx.store

    def work() -> dict:
        d = _client(host)
        if d is None:
            return {"content": "No Discord connection in this bot; nothing to plan against."}
        try:
            pv = devbiz.preview(d, guild, kind, names, projects)
        except ValueError as exc:
            return {"content": f"Refused: {exc}"}
        out: dict = {"content": _summary(pv)}
        if confirm and pv["would"] and not pv["missing"]:
            nonce = secrets.token_hex(4)
            store.set(f"pending:{nonce}", {"kind": kind, "projects": projects,
                                           "names": names, "by": user, "at": time.time()})
            out["components"] = [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": f"Confirm: create {len(pv['would'])}",
                 "custom_id": ctx.plugin.button_id(f"y:{nonce}")},
                {"type": 2, "style": 2, "label": "Cancel",
                 "custom_id": ctx.plugin.button_id(f"n:{nonce}")}]}]
        return out
    return ctx.defer(work, private=True)


def _apply_pending(ctx, nonce: str):
    pending = ctx.store.get(f"pending:{nonce}")
    ctx.store.delete(f"pending:{nonce}")
    if not pending or time.time() - float(pending.get("at") or 0) > PENDING_TTL:
        return ctx.reply("That plan expired. Run `/template apply` again.", private=True)
    host, guild = ctx.host, str(ctx.guild or "")

    def work() -> dict:
        d = _client(host)
        if d is None:
            return {"content": "No Discord connection in this bot; nothing was written."}
        res = devbiz.run_apply(d, guild, pending["kind"], pending["names"],
                               pending.get("projects") or "")
        if res["missing"]:
            return {"content": "Not applied. The bot is missing: " + "; ".join(res["missing"])
                    + ". Nothing was written."}
        made = [x for x in res["done"] if x.startswith("created")]
        lines = [f"**Applied dev business ({res['kind']})**: created {len(made)} item(s)."]
        if res["failures"]:
            lines.append("Refused by Discord: " + "; ".join(res["failures"][:8]))
            lines.append("A retry is safe and additive." if res["transient"]
                         else "Retrying will not change these; check the bot's permissions.")
        packs = devbiz.enable_packs(host, guild, res["kind"])
        if packs["enabled"]:
            lines.append("Plugins on: " + ", ".join(packs["enabled"]))
        if packs["kept"]:
            lines.append("Left as an admin set them: " + ", ".join(packs["kept"]))
        if packs["not_installed"]:
            lines.append("Not installed on this bot: " + ", ".join(packs["not_installed"]))
        log.info("template applied guild=%s kind=%s created=%d failures=%d", guild,
                 res["kind"], len(made), len(res["failures"]))
        return {"content": "\n".join(lines)[:1990]}
    return ctx.defer(work, private=True)


def _plan_cmd(ctx):
    if not can_layout(ctx):
        return _refuse(ctx)
    try:
        kind = _kind(ctx.opt("kind"))
        projects = ",".join(devbiz.parse_projects(ctx.opt("projects", "")))
    except ValueError as exc:
        return ctx.reply(str(exc), private=True)
    return _plan_and_offer(ctx, kind, projects, confirm=False)


def _apply_cmd(ctx):
    if not can_layout(ctx):
        return _refuse(ctx)
    try:
        kind = _kind(ctx.opt("kind"))
        projects = ",".join(devbiz.parse_projects(ctx.opt("projects", "")))
    except ValueError as exc:
        return ctx.reply(str(exc), private=True)
    return _plan_and_offer(ctx, kind, projects, confirm=True)


def _names_cmd(ctx):
    if not can_layout(ctx):
        return _refuse(ctx)
    guild = str(ctx.guild or "")
    try:
        choice = devbiz.canonical(str(ctx.opt("set", "")).strip().lower())
    except ValueError:
        current = devbiz.name_set(ctx.host.store, guild)
        return ctx.reply(f"Names here: **{current}**. Pick one of: "
                         + ", ".join(sorted(devbiz.NAME_SETS)), private=True)
    devbiz.set_name_set(ctx.host.store, guild, choice)
    s = devbiz.NAME_SETS[choice]
    return ctx.reply(f"Roles will read as **{choice}**: {s['umbrella']}, "
                     f"{', '.join(s['ideals'])}; partner role {s['partner']}. Existing "
                     "roles are never renamed.", private=True)


def _on_button(ctx, payload: str):
    kind, _, rest = payload.partition(":")
    if kind == "py":
        if not is_operator(ctx):
            return _refuse_project(ctx)
        return _post_pending(ctx, rest)
    if not can_layout(ctx):
        return _refuse(ctx)
    if kind == "p":                         # the arrival card: minimal | full
        try:
            return _plan_and_offer(ctx, _kind(rest), "", confirm=True)
        except ValueError as exc:
            return ctx.reply(str(exc), private=True)
    if kind == "y":
        return _apply_pending(ctx, rest)
    if kind == "n":
        ctx.store.delete(f"pending:{rest}")
        return ctx.reply("Cancelled; nothing was written.", private=True)
    return ctx.reply("Unknown button.", private=True)


def _badge() -> str | None:
    return (os.getenv(devbiz.BADGE_ENV) or "").strip() or None


def _badge_png() -> bytes | None:
    return devbiz.read_badge((os.getenv(devbiz.BADGE_FILE_ENV) or "").strip() or None)


def _project_cmd(ctx):
    if not is_operator(ctx):
        return _refuse_project(ctx)
    repo = str(ctx.opt("repo", "") or "")
    host, guild, store, user = ctx.host, str(ctx.guild or ""), ctx.store, ctx.user

    def work() -> dict:
        try:
            proj = devbiz.project(repo, VERIFIED_LOADER())
        except (ValueError, devbiz.VerifiedSourceError) as exc:
            return {"content": f"Not posted: {exc}"}
        d = _client(host)
        if d is None:
            return {"content": "No Discord connection in this bot; nothing to plan against."}
        posted = bool(store.get(f"project:{proj['key']}"))
        pv = devbiz.plan_project(d, guild, proj, posted=posted)
        lines = [f"**Verified project: {proj['name']}**"]
        lines += [f"- {x[len('would '):]}" for x in pv["would"]]
        if _badge_png():
            lines.append("- the badge image is attached to the post")
        elif not _badge():
            lines.append(f"- no badge image: set {devbiz.BADGE_FILE_ENV} (a PNG) or "
                         f"{devbiz.BADGE_ENV} to show one")
        out: dict = {"embeds": devbiz.project_card(proj, _badge())["embeds"]}
        if pv["missing"]:
            lines.append("**Not possible yet:** " + "; ".join(pv["missing"]))
        else:
            nonce = secrets.token_hex(4)
            store.set(f"pending:{nonce}", {"project": proj, "by": user, "at": time.time()})
            out["components"] = [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Confirm: post it",
                 "custom_id": ctx.plugin.button_id(f"py:{nonce}")},
                {"type": 2, "style": 2, "label": "Cancel",
                 "custom_id": ctx.plugin.button_id(f"n:{nonce}")}]}]
        out["content"] = "\n".join(lines)[:1990]
        return out
    return ctx.defer(work, private=True)


def _post_pending(ctx, nonce: str):
    pending = ctx.store.get(f"pending:{nonce}")
    ctx.store.delete(f"pending:{nonce}")
    if not pending or "project" not in pending \
            or time.time() - float(pending.get("at") or 0) > PENDING_TTL:
        return ctx.reply("That preview expired. Run `/template project` again.",
                         private=True)
    proj, host, guild, store = pending["project"], ctx.host, str(ctx.guild or ""), ctx.store

    def work() -> dict:
        d = _client(host)
        if d is None:
            return {"content": "No Discord connection in this bot; nothing was written."}
        if store.get(f"project:{proj['key']}"):
            return {"content": f"{proj['name']} is already posted; nothing was written."}
        try:
            res = devbiz.post_project(d, guild, proj, _badge(), store=host.store,
                                      badge_png=_badge_png())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            code = getattr(exc, "code", None)
            why = f"HTTP {code}" if code else type(exc).__name__
            return {"content": f"Discord refused the post ({why}). Check the bot can "
                               "post in #projects and manage roles."}
        if not res["ok"]:
            return {"content": "Not posted: " + "; ".join(res["missing"])}
        store.set(f"project:{proj['key']}", {"id": res.get("id"), "at": int(time.time())})
        return {"content": "Done: " + "; ".join(res["done"])}
    return ctx.defer(work, private=True)


def card(plugin: Plugin) -> dict:
    """The arrival card: one line of what it does, two buttons."""
    return {
        "content": ("**Set up this server as a dev business?** Projects, releases, squads, "
                    "a #join card and a #bot channel for the agents. Minimal or Full, or "
                    "just an Aitherium section (one category) inside your server. You see "
                    "the plan before anything is created; nothing is deleted. "
                    "Server admins only."),
        "allowed_mentions": {"parse": []},
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 1, "label": "Minimal", "custom_id": plugin.button_id("p:minimal")},
            {"type": 2, "style": 2, "label": "Full", "custom_id": plugin.button_id("p:full")},
            {"type": 2, "style": 2, "label": "Aitherium section",
             "custom_id": plugin.button_id("p:partner")}]}],
    }


def _on_guild_join(event) -> None:
    """Post the card once per server, in its system channel (none = skip, logged)."""
    if event.store.get(CARD_KEY):
        return
    if not event.system_channel_id or event.rest is None:
        log.info("template card skipped guild=%s reason=no_system_channel", event.guild_id)
        return
    event.rest.create_message(event.system_channel_id, card(event.plugin))
    event.store.set(CARD_KEY, int(time.time()))


_kind_opt = Option("kind", "minimal, full, or partner (one Aitherium category)",
                   choices=list(devbiz.TEMPLATES))
_projects_opt = Option("projects", "Project names, comma-separated (one channel each)",
                       max_length=300)

PLUGIN = Plugin(
    name=NAME,
    version="0.1.0",
    description="Set this server up as a software dev and deployment business",
    default_enabled=True,
    on_button=_on_button,
    on_guild_join=[_on_guild_join],
    commands=[Command("template", "Lay this server out as a dev business (operators)",
                      admin=True, subcommands=[
        Command("plan", "What the layout would create (writes nothing)", _plan_cmd,
                options=[_kind_opt, _projects_opt]),
        Command("apply", "Show the plan, then create it on Confirm (additive only)",
                _apply_cmd, options=[_kind_opt, _projects_opt]),
        Command("names", "How roles read here", _names_cmd,
                options=[Option("set", "professional (default) or themed",
                                choices=sorted(devbiz.NAME_SETS))]),
        Command("project", "Post a verified project's card (operators; shows it first)",
                _project_cmd,
                options=[Option("repo", "The project's https:// repository URL, as listed",
                                required=True, max_length=300)]),
    ])],
)
