"""Join: a call-to-action form that hands a new member their number, squad and track.

Plugin ``join``. OFF everywhere until BOTH switches are on in a server: the plugin
(``/plugins enable join``) and the server's own join config (``/enlist
action:enable``, a server manager). The voice command already owns ``/join``, so
the member command is ``/enlist``.

    /enlist                       open the form (private): four pickers, then a modal
    [Join] button                 the same form, from a card a manager posted
    /enlist action:me             your card again (private)
    /enlist action:post channel   (managers) post the persistent Join card there
    /enlist action:status         (managers) config, counts, and every permission gap
    /enlist action:setup          (managers) create the missing roles (and squad
                                  channels when ``squad_channels`` is on)
    /enlist action:export         (managers) every submission as CSV, sent by DM
    /enlist action:audit          (managers) the last certify/lead/config changes
    /enlist action:digest         (managers) post each squad's merged-PR board now
    /enlist action:layout         (managers) the roles/channels as a guildspec layout
    /enlist action:set key value  (managers) change one config value (see SETTABLE)
    /certify user track [revoke]  (managers) the "Certified <track>" role, audited
    /lead user squad [revoke]     (managers) the "Squad Lead" role, audited

The form is two steps because a Discord modal holds at most five TEXT inputs and
no pickers: step 1 is a private message with four select menus (skills, squads,
track, availability) and a Continue button; step 2 is a modal for handle,
GitHub and what they want to build.

WHAT IS SCARCE. Every joiner gets a member number (``#001``...). The first
``cohort_cap`` (default 100) also get the **Founding Cohort** role. The number is
allocated in ONE atomic store update (``GuildStore.update``), so concurrent joins
can never share a number or overshoot the cap, and re-submitting keeps your
number. **Certified <track>** and **Squad Lead** are NEVER given by the join path:
``_protected`` names them and ``assign_join_roles`` refuses any role whose name or
id is on that list, even if a misconfigured track role shares its name. Only
``/certify`` and ``/lead`` grant them, and each grant is written to the audit log.

ROLES. Track and squad roles are created if missing, with permissions ``0``; an
existing role with that name that carries a privileged bit, or that an
integration manages, is refused (``awdiscord.roles``). Names come from the
server's config, and ``action:layout`` prints them as a ``guildspec`` layout
(validated by ``guildspec.validate_spec`` before anything is created) so the
server's layout file can carry them.

PERMISSIONS. The bot needs Manage Roles and a highest role ABOVE every role it
hands out (and Manage Channels for squad channels). ``permission_report`` says
what is missing in words; a join whose roles could not be given still gets its
number, the member is told, and the gap is in ``action:status``.

WHERE OUTPUT GOES. The welcome card goes to the server's bot channel (the same
resolution ``awdiscord.containment`` uses) and to the member's DMs. A server with
no bot channel gets the DM only. The weekly squad digest posts only where a
manager turned ``digest`` on and squad channels exist.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Callable

from awdiscord import containment, guildspec, ideals, orders
from awdiscord import roles as rolekit
from awdiscord.builtin import join_ladder
from awdiscord.plugins import Command, Option, Plugin, PluginError

log = logging.getLogger("awdiscord.join")

NAME = "join"
MANAGE_ROLES = 1 << 28
MANAGE_CHANNELS = 1 << 4
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
WEEK = 7 * 24 * 3600
DIGEST_POLL = 3600.0

SQUADS: dict[str, str] = {
    "zeroauthy": "ZeroAuthy", "salescompel": "SalesCompel", "photocraft": "PhotoCraft",
    "website": "Website", "new-idea": "New Idea",
}
TRACKS: dict[str, str] = {
    "clean-room": "Clean-Room Implementer", "self-host": "Self-Host Deployer",
    "maintainer": "Community Maintainer", "ai-ops": "AI-Ops",
}
SKILLS: dict[str, str] = {
    "python": "Python", "typescript": "TypeScript / JS", "rust": "Rust", "go": "Go",
    "frontend": "Frontend / UI", "backend": "Backend / APIs", "devops": "DevOps / infra",
    "ml": "ML / AI", "security": "Security", "data": "Data", "design": "Design",
    "docs": "Docs / writing", "community": "Community / support",
}
AVAILABILITY: dict[str, str] = {   # occupyxaitherium join/README.md, field 6
    "few-hours": "A few hours a week", "evenings": "Evenings", "weekends": "Weekends",
    "full-time": "Full time",
}
_SLUG = re.compile(r"^[a-z0-9-]{1,60}$")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "cohort_cap": 100,
    "squad_channels": False,
    "category": "Squads",
    "digest": False,
    "role_founding": "Founding Cohort",
    "role_lead": "Squad Lead",
    "role_certified": "",        # "" = the name set's ("ACAE: {track}")
    "role_track": "{track}",
    "role_squad": "Squad: {squad}",
    "squads": SQUADS,
    "tracks": TRACKS,
    "repos": {},                 # squad key -> "owner/repo" (digest + first issue)
    "name_set": "professional",  # "professional" (original names) or "themed" (opt-in)
    "umbrella": "",              # override the agent-access role name ("" = name set's)
    "academy": "",               # override the academy's display name ("" = name set's)
    "join_log": "",              # channel id: one line per join (who, squad, number)
}

#: ``action:set`` keys and their parsers. ``repo.<squad>`` sets one squad's repo.
SETTABLE = ("cohort_cap", "squad_channels", "category", "digest", "role_founding",
            "role_lead", "role_certified", "role_track", "role_squad", "repo.<squad>",
            "name_set", "umbrella", "academy", "join_log")

_GITHUB = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
_CSV_UNSAFE = ("=", "+", "-", "@", "\t", "\r")

_lock = threading.Lock()                 # only for a store without update()
_digest_started = threading.Event()


# ── config ────────────────────────────────────────────────────────────────────

def config(store) -> dict:
    """The server's join config, over the defaults. ``store`` is a ScopedStore."""
    conf = dict(DEFAULTS)
    saved = store.get("config") or {}
    if isinstance(saved, dict):
        conf.update(saved)
    return conf


def _save_config(store, conf: dict) -> None:
    store.set("config", {k: v for k, v in conf.items() if DEFAULTS.get(k) != v})


def is_open(host, guild: str | None) -> bool:
    """Both switches: the plugin on in this server AND the server's config enabled."""
    if not guild or not host.enabled(guild, NAME):
        return False
    from awdiscord.plugins import ScopedStore
    return bool(config(ScopedStore(host.store, guild, NAME)).get("enabled"))


def role_name(conf: dict, kind: str, key: str = "") -> str:
    """The display name of a role: founding, lead, certified/<track>, track, squad,
    archetype/<key>, ideal/<level>, umbrella."""
    if kind == "archetype":
        return orders.archetype_name(key, conf.get("name_set"))
    if kind == "ideal":
        return orders.ideal_name(int(key), conf.get("name_set"))
    if kind == "umbrella":
        return orders.umbrella_name(conf.get("name_set"), str(conf.get("umbrella") or ""))
    if kind == "founding":
        return str(conf["role_founding"])
    if kind == "lead":
        return str(conf["role_lead"])
    if kind == "certified":
        label = conf["tracks"].get(key, key)
        own = str(conf.get("role_certified") or "")
        return own.replace("{track}", label) if own else \
            orders.certified_role(label, conf.get("name_set"))
    if kind == "track":
        label = conf["tracks"].get(key, key)
        return str(conf["role_track"]).replace("{track}", label)
    if kind == "squad":
        return str(conf["role_squad"]).replace("{squad}", conf["squads"].get(key, key))
    raise PluginError(f"unknown role kind {kind!r}")


def _protected(conf: dict) -> set[str]:
    """Role names the join path may never hand out (casefolded)."""
    names = {role_name(conf, "lead"), role_name(conf, "umbrella")}
    names |= {role_name(conf, "certified", t) for t in conf["tracks"]}
    names |= orders.earned_role_names()      # level/umbrella/partner roles, EITHER set
    return {n.strip().casefold() for n in names}


def layout(conf: dict) -> dict:
    """The roles (and squad channels) this config wants, as a guildspec layout."""
    roles = [{"name": role_name(conf, "founding"), "color": 15844367, "hoist": True,
              "mentionable": False, "permissions": "0"}]
    roles += [{"name": role_name(conf, "track", t), "color": 3447003, "hoist": False,
               "mentionable": True, "permissions": "0"} for t in conf["tracks"]]
    roles += [{"name": role_name(conf, "squad", s), "color": 10181046, "hoist": False,
               "mentionable": True, "permissions": "0"} for s in conf["squads"]]
    roles += [{"name": role_name(conf, "certified", t), "color": 3066993, "hoist": True,
               "mentionable": False, "permissions": "0"} for t in conf["tracks"]]
    roles.append({"name": role_name(conf, "lead"), "color": 15105570, "hoist": True,
                  "mentionable": True, "permissions": "0"})
    roles += [{"name": role_name(conf, "archetype", a.key), "color": a.color, "hoist": False,
               "mentionable": True, "permissions": "0"} for a in orders.ARCHETYPES]
    roles += [{"name": role_name(conf, "ideal", str(n)), "color": 15132390, "hoist": False,
               "mentionable": False, "permissions": "0"}
              for n in range(1, orders.MAX_IDEAL + 1)]
    roles.append({"name": role_name(conf, "umbrella"), "color": 6211020, "hoist": True,
                  "mentionable": False, "permissions": "0"})
    channels: list[dict] = []
    if conf.get("squad_channels"):
        cat = str(conf["category"])
        channels.append({"name": cat, "type": "category", "category": None, "topic": "",
                         "position": 0, "overwrites": []})
        channels += [{"name": _slug(s), "type": "text", "category": cat,
                      "topic": f"The {conf['squads'][s]} squad.", "position": i + 1,
                      "overwrites": []} for i, s in enumerate(conf["squads"])]
    return {"version": 1, "roles": roles, "channels": channels}


def _slug(key: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", key.lower()).strip("-")[:90] or "squad"


# ── the cohort number (atomic) ────────────────────────────────────────────────

def allocate_number(store, user: str, cap: int) -> tuple[int, bool]:
    """(member number, founding?) for ``user``; the same answer on every call.

    One atomic update of the ``cohort`` key: ``{"next": n, "members": {user: n}}``.
    A number is never reused and never shared; founding = number <= cap.
    """
    def bump(cur: Any) -> tuple[dict, tuple[int, bool]]:
        cur = cur if isinstance(cur, dict) else {}
        members = dict(cur.get("members") or {})
        n = members.get(user)
        nxt = int(cur.get("next") or 1)
        if n is None:
            n, nxt = nxt, nxt + 1
            members[user] = n
        return {"next": nxt, "members": members}, (int(n), int(n) <= int(cap))

    update = getattr(store, "update", None)
    if callable(update):
        return update("cohort", bump, {})
    with _lock:                          # an older store: one process, one lock
        value, out = bump(store.get("cohort", {}))
        store.set("cohort", value)
        return out


def _number(n: int) -> str:
    return f"#{n:03d}" if n else "#pending"


def join_log(host, guild: str, conf: dict, record: dict) -> bool:
    """One line to the private join log: who, which squad, which number. Never
    a free-text answer. Off unless ``join_log`` names a channel."""
    channel = str(conf.get("join_log") or "")
    rest = getattr(host, "rest", None)
    if not channel or rest is None:
        return False
    squads = ", ".join(conf["squads"].get(s, s) for s in record.get("squads") or []) or "-"
    try:
        rest.create_message(channel, {
            "content": f"<@{record.get('user')}> joined: {squads}, "
                       f"{_number(int(record.get('number') or 0))}",
            "allowed_mentions": {"parse": []}})
        return True
    except Exception as exc:                                  # noqa: BLE001
        log.warning("join: join-log post failed in %s: %s", guild, exc)
        return False


# ── Discord reads ─────────────────────────────────────────────────────────────

def _rest(host):
    rest = getattr(host, "rest", None)
    if rest is None:
        raise PluginError("this bot cannot reach Discord right now")
    return rest


def bot_standing(rest, guild: str) -> dict:
    """{"roles": live roles, "bits": bot permission bits, "top": highest position,
    "bot_id"}. Raises PluginError when Discord cannot be read."""
    live = rest.request("GET", f"/guilds/{guild}/roles")
    if not isinstance(live, list):
        raise PluginError("I could not read this server's roles")
    me = rest.request("GET", "/users/@me") or {}
    bot_id = str((me if isinstance(me, dict) else {}).get("id") or "")
    if not bot_id:
        raise PluginError("I could not learn my own account")
    member = rest.request("GET", f"/guilds/{guild}/members/{bot_id}") or {}
    mine = [str(r) for r in ((member if isinstance(member, dict) else {}).get("roles") or [])]
    by_id = {str(r.get("id")): r for r in live if isinstance(r, dict)}
    bits = 0
    for rid in [str(guild), *mine]:
        value = rolekit.bits(by_id.get(rid, {}).get("permissions", "0"))
        if value is None:
            raise PluginError("I could not read my own permissions in this server")
        bits |= value
    if bits & ADMINISTRATOR:
        bits |= MANAGE_ROLES | MANAGE_CHANNELS
    top = max([int(by_id.get(r, {}).get("position") or 0) for r in mine] or [0])
    return {"roles": live, "bits": bits, "top": top, "bot_id": bot_id}


_channels_by_host: dict[int, containment.BotChannels] = {}


def bot_channel(host, guild: str) -> str:
    """The server's bot channel id (containment rules); "" when none or unknown."""
    resolver = _channels_by_host.get(id(host))
    if resolver is None:
        resolver = _channels_by_host[id(host)] = containment.BotChannels(
            lambda g: _rest(host).request("GET", f"/guilds/{g}/channels"),
            setting=lambda g: str(host.store.get(g, "core", "bot_channel", "") or ""))
    return resolver.for_guild(guild) or ""


def permission_report(host, guild: str, conf: dict | None = None) -> list[str]:
    """What stops the join flow from working fully in this server, in words.

    Empty = nothing missing. Never raises: an unreadable server is itself a line.
    """
    from awdiscord.plugins import ScopedStore
    conf = conf or config(ScopedStore(host.store, guild, NAME))
    gaps: list[str] = []
    try:
        rest = _rest(host)
        standing = bot_standing(rest, guild)
    except PluginError as exc:
        return [str(exc)]
    except Exception as exc:                                  # noqa: BLE001
        return [f"I could not read this server: {type(exc).__name__}"]
    bits, top = standing["bits"], standing["top"]
    if not bits & MANAGE_ROLES:
        gaps.append("missing **Manage Roles**: no role can be given")
    if conf.get("squad_channels") and not bits & MANAGE_CHANNELS:
        gaps.append("missing **Manage Channels**: squad channels cannot be created")
    wanted = {r["name"] for r in layout(conf)["roles"]}
    for want in sorted(wanted):
        role = rolekit.resolve_role(standing["roles"], want)
        if role is None:
            continue                     # created on demand, below the bot
        if int(role.get("position") or 0) >= top:
            gaps.append(f"role **{want}** sits at or above my highest role: move my "
                        "role above it in Server Settings > Roles")
        bad = rolekit.privileged_names(role.get("permissions"))
        if bad:
            gaps.append(f"role **{want}** carries {', '.join(bad)}: I will not hand it out")
    try:
        if not bot_channel(host, guild):
            gaps.append("no bot channel: welcome cards go by DM only "
                        "(create #bot or set core/bot_channel)")
    except Exception as exc:                                  # noqa: BLE001
        gaps.append(f"bot channel unreadable: {type(exc).__name__}")
    return gaps


# ── roles ─────────────────────────────────────────────────────────────────────

def ensure_role(rest, guild: str, standing: dict, name: str, *, hoist: bool = False) -> dict:
    """The live role called ``name``, created (permissions 0) when missing.

    Refuses (PluginError) an ambiguous name, an integration role, or a role that
    carries a privileged bit: an existing role is only reused when it is harmless.
    """
    hits = [r for r in standing["roles"] if isinstance(r, dict)
            and str(r.get("name") or "").strip().casefold() == name.strip().casefold()]
    if len(hits) > 1:
        raise PluginError(f"two roles are called {name!r}; rename one")
    if hits:
        role = hits[0]
        if rolekit._managed(role):                       # noqa: SLF001
            raise PluginError(f"{name!r} belongs to an integration")
        bad = rolekit.privileged_names(role.get("permissions"))
        if bad:
            raise PluginError(f"{name!r} carries {', '.join(bad)}")
        return role
    if not standing["bits"] & MANAGE_ROLES:
        raise PluginError("I need **Manage Roles** to create roles")
    made = rest.request("POST", f"/guilds/{guild}/roles",
                        {"name": name[:100], "permissions": "0", "hoist": hoist,
                         "mentionable": False})
    if not isinstance(made, dict) or not made.get("id"):
        raise PluginError(f"Discord did not create {name!r}")
    standing["roles"].append(made)
    return made


def _give(rest, guild: str, user: str, standing: dict, name: str, *,
          hoist: bool = False) -> str:
    role = ensure_role(rest, guild, standing, name, hoist=hoist)
    problem = rolekit.bot_ceiling_problem(role, standing["top"], standing["bits"],
                                          bot_role_id=standing["bot_id"])
    if problem:
        raise PluginError(problem)
    try:
        rolekit.assign(rest, guild, user, str(role["id"]))
    except rolekit.RoleError as exc:
        raise PluginError(str(exc)) from exc
    return str(role["id"])


def join_role_names(conf: dict, record: dict) -> list[tuple[str, bool]]:
    """(role name, hoist) the join path wants for this record, protected ones dropped."""
    want: list[tuple[str, bool]] = []
    if record.get("founding"):
        want.append((role_name(conf, "founding"), True))
    if record.get("track"):
        want.append((role_name(conf, "track", record["track"]), False))
    want += [(role_name(conf, "squad", s), False) for s in record.get("squads") or []]
    blocked = _protected(conf)
    return [(n, h) for n, h in want if n.strip().casefold() not in blocked]


def assign_join_roles(host, guild: str, user: str, conf: dict, record: dict,
                      protected_ids: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Give the founding/track/squad roles. (given role names, errors in words).

    NEVER a certified or lead role: those names are dropped by ``join_role_names``
    and a role id an operator granted through /certify or /lead is refused here.
    """
    wanted = join_role_names(conf, record)
    try:
        rest = _rest(host)
        standing = bot_standing(rest, guild)
    except PluginError as exc:
        return [], [str(exc)]
    given, errors = [], []
    blocked_ids = {str(x) for x in (protected_ids or ())}
    for name, hoist in wanted:
        try:
            role = ensure_role(rest, guild, standing, name, hoist=hoist)
            if str(role.get("id")) in blocked_ids:
                raise PluginError("that role is operator-granted only")
            _give(rest, guild, user, standing, name, hoist=hoist)
            given.append(name)
        except PluginError as exc:
            errors.append(f"{name}: {exc}")
    return given, errors


# ── audit ─────────────────────────────────────────────────────────────────────

def audit(store, by: str, action: str, target: str = "", detail: str = "") -> dict:
    entry = {"at": int(time.time()), "by": str(by), "action": action,
             "target": str(target), "detail": detail[:300]}
    store.set(f"audit:{entry['at']:012d}:{secrets.token_hex(3)}", entry)
    log.info("join audit %s", json.dumps(entry))
    return entry


def audit_log(store, limit: int = 20) -> list[dict]:
    keys = store.keys("audit:")[-limit:]
    return [e for e in (store.get(k) for k in keys) if isinstance(e, dict)]


# ── the form ──────────────────────────────────────────────────────────────────

def _draft(ctx) -> dict:
    d = ctx.store.get(f"draft:{ctx.user}")
    return d if isinstance(d, dict) else {}


def _select(field: str, placeholder: str, choices: dict, chosen: list[str],
            many: bool) -> dict:
    return {"type": 1, "components": [{
        "type": 3, "custom_id": PLUGIN.button_id(f"sel:{field}"),
        "placeholder": placeholder[:150], "min_values": 1,
        "max_values": min(len(choices), 25) if many else 1,
        "options": [{"label": label[:100], "value": key, "default": key in chosen}
                    for key, label in list(choices.items())[:25]]}]}


def form_view(ctx, conf: dict) -> tuple[str, list]:
    d = _draft(ctx)
    rows = [
        _select("skills", "Your skills (pick any)", SKILLS, d.get("skills") or [], True),
        _select("squads", "Project squad(s)", conf["squads"], d.get("squads") or [], True),
        _select("track", "Track you are interested in", conf["tracks"],
                [d["track"]] if d.get("track") else [], False),
        _select("availability", "Availability", AVAILABILITY,
                [d["availability"]] if d.get("availability") else [], False),
        {"type": 1, "components": [
            {"type": 2, "style": 3 if d.get("coc") else 2,
             "label": "Code of conduct: agreed" if d.get("coc") else
             "I agree to the code of conduct",
             "custom_id": PLUGIN.button_id("coc")},
            {"type": 2, "style": 1, "label": "Continue",
             "custom_id": PLUGIN.button_id("next")}]},
    ]
    missing = [f for f in ("squads", "track") if not d.get(f)]
    if not d.get("coc"):
        missing.append("code of conduct")
    text = ("**Join the collective** - step 1 of 2. Pick below, then press **Continue**."
            + (f"\nStill needed: {', '.join(missing)}." if missing else
               "\nReady: press **Continue**."))
    return text, rows


def _start(ctx):
    conf = config(ctx.store)
    if not is_open(ctx.host, ctx.guild):
        return ctx.reply("Joining is not open in this server.", private=True)
    # Re-joining edits the record and keeps the number (allocate_number).
    text, rows = form_view(ctx, conf)
    return ctx.reply(text, private=True, components=rows)


def _on_button(ctx, payload: str):
    if not is_open(ctx.host, ctx.guild):
        return ctx.reply("Joining is not open in this server.", private=True)
    conf = config(ctx.store)
    if payload == "assess" or payload.startswith(("aq:", "apage:", "afinish")):
        return join_ladder.on_button(ctx, payload)
    if payload == "start":
        text, rows = form_view(ctx, conf)
        return ctx.reply(text, private=True, components=rows)
    if payload.startswith("project:"):
        # A project card's "+ Join as contributor" (awp:join:project:<slug>).
        slug = payload[len("project:"):]
        note = ""
        if _SLUG.match(slug) and slug in conf["squads"]:
            d = _draft(ctx)
            d["squads"] = sorted(set(d.get("squads") or []) | {slug})
            ctx.store.set(f"draft:{ctx.user}", d)
        else:
            note = "\n(That project is not a squad here yet; pick one below.)"
        text, rows = form_view(ctx, conf)
        return ctx.reply(text + note, private=True, components=rows)
    if payload == "coc":
        d = _draft(ctx)
        d["coc"] = not d.get("coc")
        ctx.store.set(f"draft:{ctx.user}", d)
        text, rows = form_view(ctx, conf)
        return ctx.update(text, components=rows)
    if payload.startswith("sel:"):
        field = payload[4:]
        allowed = {"skills": SKILLS, "squads": conf["squads"], "track": conf["tracks"],
                   "availability": AVAILABILITY}.get(field)
        if allowed is None:
            return ctx.reply("Unknown picker.", private=True)
        values = [str(v) for v in ((ctx.interaction.get("data") or {}).get("values") or [])
                  if str(v) in allowed]
        d = _draft(ctx)
        d[field] = values if field in ("skills", "squads") else (values[0] if values else "")
        ctx.store.set(f"draft:{ctx.user}", d)
        text, rows = form_view(ctx, conf)
        return ctx.update(text, components=rows)
    if payload == "next":
        d = _draft(ctx)
        missing = [f for f in ("squads", "track") if not d.get(f)]
        if missing:
            return ctx.reply(f"Pick your {' and '.join(missing)} first.", private=True)
        if not d.get("coc"):
            return ctx.reply("Agree to the code of conduct first.", private=True)
        prev = ctx.store.get(f"member:{ctx.user}") or {}
        return ctx.modal("form", "Join - step 2 of 2", [
            {"id": "handle", "label": "What should we call you?", "max_length": 32,
             "placeholder": prev.get("handle") or "your handle"},
            {"id": "github", "label": "GitHub username (optional)", "max_length": 39,
             "required": False, "placeholder": prev.get("github") or "octocat"},
            {"id": "build", "label": "What do you want to build?", "paragraph": True,
             "max_length": 500, "required": False},
            {"id": "timezone", "label": "Time zone (optional)", "max_length": 40,
             "required": False, "placeholder": prev.get("timezone") or "e.g. UTC-5"}])
    return ctx.reply("Unknown button.", private=True)


def validate(values: dict, draft: dict, conf: dict) -> tuple[dict, list[str]]:
    """The cleaned submission and the problems with it (empty = accept)."""
    problems: list[str] = []
    handle = re.sub(r"\s+", " ", str(values.get("handle") or "")).strip()
    handle = handle.replace("@", "")
    if not 2 <= len(handle) <= 32:
        problems.append("handle must be 2-32 characters")
    github = str(values.get("github") or "").strip().lstrip("@")
    if github and not _GITHUB.match(github):
        problems.append("that is not a GitHub username")
    squads = [s for s in draft.get("squads") or [] if s in conf["squads"]]
    track = draft.get("track") if draft.get("track") in conf["tracks"] else ""
    if not squads:
        problems.append("pick at least one squad")
    if not track:
        problems.append("pick a track")
    if not draft.get("coc"):
        problems.append("agree to the code of conduct")
    tz = re.sub(r"[^A-Za-z0-9 +:/_-]", "", str(values.get("timezone") or "")).strip()[:40]
    clean = {"handle": handle, "github": github,
             "skills": [s for s in draft.get("skills") or [] if s in SKILLS],
             "squads": squads, "track": track,
             "availability": draft.get("availability") if draft.get("availability")
             in AVAILABILITY else "",
             "build": str(values.get("build") or "").strip()[:500],
             "timezone": tz, "coc": bool(draft.get("coc"))}
    return clean, problems


def _on_modal(ctx, payload: str, values: dict):
    if payload != "form":
        return ctx.reply("Unknown form.", private=True)
    if not is_open(ctx.host, ctx.guild):
        return ctx.reply("Joining is not open in this server.", private=True)
    conf = config(ctx.store)
    clean, problems = validate(values, _draft(ctx), conf)
    if problems:
        return ctx.reply("Not saved: " + "; ".join(problems) + ". Run `/enlist` again.",
                         private=True)
    member = ctx.interaction.get("member") or {}
    username = str(((member.get("user") or {}).get("username")) or "")
    return ctx.defer(lambda: complete_join(ctx.host, str(ctx.guild), ctx.user, clean,
                                           username=username), private=True)


def complete_join(host, guild: str, user: str, clean: dict, username: str = "") -> dict:
    """Number, store, join roles. Returns the private reply payload.

    The welcome card waits for the assessment: the 1st ideal needs both, and the
    card names the archetype. A re-submission by someone already assessed only
    updates the record.
    """
    from awdiscord.plugins import ScopedStore
    store = ScopedStore(host.store, guild, NAME)
    conf = config(store)
    prev = store.get(f"member:{user}") or {}
    try:
        number, founding = allocate_number(store, user, int(conf.get("cohort_cap") or 0))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("join: number allocation failed for %s: %s", user, exc)
        number, founding = int(prev.get("number") or 0), bool(prev.get("founding"))
    now = int(time.time())
    record = {**prev, **clean, "user": user, "username": username, "number": number,
              "founding": founding, "at": prev.get("at") or now, "updated": now,
              "last_active": now}
    given, errors = assign_join_roles(host, guild, user, conf, record,
                                      set(store.get("protected_role_ids") or []))
    record["roles"] = sorted(set(prev.get("roles") or []) | set(given))
    record["role_errors"] = errors
    store.set(f"member:{user}", record)
    store.delete(f"draft:{user}")
    if not prev:
        ideals.log_event(store, user, "joined", user, f"member {_number(number)}")
        join_log(host, guild, conf, record)
    if errors:
        audit(store, "system", "role_gap", user, "; ".join(errors))
    lines = [(f"You are member **{_number(number)}**" if number else
              "Your member number is **pending** (it will be issued; nothing else waits)")
             + (f" - **Founding Cohort, {academy(conf)}**." if founding else ".")]
    if given:
        lines.append("Roles: " + ", ".join(given) + ".")
    if errors:
        lines.append("Some roles could not be given yet (a server manager can see why "
                     "in `/enlist action:status`); your number is kept.")
    if record.get("assessment"):
        lines.append("Profile updated.")
        return {"content": "\n".join(lines)}
    lines.append("Last step: a 2-minute assessment (10 scenarios) finds your archetype "
                 "and earns your first ideal.")
    return {"content": "\n".join(lines),
            "components": [{"type": 1, "components": [{
                "type": 2, "style": 1, "label": "Take the assessment",
                "custom_id": PLUGIN.button_id("assess")}]}]}


def give_role(host, guild: str, user: str, name: str, *, hoist: bool = False) -> str:
    """Give one role by name (created if missing); the error in words, or ""."""
    try:
        rest = _rest(host)
        standing = bot_standing(rest, guild)
        _give(rest, guild, user, standing, name, hoist=hoist)
        return ""
    except PluginError as exc:
        return f"{name}: {exc}"


def drop_role(host, guild: str, user: str, name: str) -> str:
    """Remove one role by name when it exists; the error in words, or ""."""
    try:
        rest = _rest(host)
        standing = bot_standing(rest, guild)
        role = rolekit.resolve_role(standing["roles"], name)
        if role is not None:
            rolekit.remove(rest, guild, user, str(role["id"]))
        return ""
    except (PluginError, rolekit.RoleError) as exc:
        return f"{name}: {exc}"


def promote(host, guild: str, user: str, now: float | None = None) -> list[int]:
    """Advance the ladder from the Oath Log; give the earned roles; announce.

    The ONLY path that gives an ideal role or the umbrella role. Returns the
    levels gained (often none).
    """
    from awdiscord.plugins import ScopedStore
    store = ScopedStore(host.store, guild, NAME)
    conf = config(store)
    gained = ideals.advance(store, user, now)
    for lvl in gained:
        errors = [give_role(host, guild, user, role_name(conf, "ideal", str(lvl)))]
        if lvl == 3:
            track = ideals.certified_track(store, user)
            if track in conf["tracks"]:
                errors.append(give_role(host, guild, user,
                                        role_name(conf, "certified", track), hoist=True))
        errors = [e for e in errors if e]
        if errors:
            audit(store, "system", "role_gap", user, "; ".join(errors))
        audit(store, "system", "promote", user, f"ideal {lvl}")
        announce(host, guild, user, lvl)
    return gained


def ideals_store(host, guild: str):
    from awdiscord.plugins import ScopedStore
    return ScopedStore(host.store, guild, NAME)


def academy(conf: dict) -> str:
    return orders.academy_name(conf.get("name_set"), str(conf.get("academy") or ""))


def ceremony_card(host, guild: str, conf: dict, record: dict, lvl: int) -> dict:
    """The card for reaching ideal ``lvl``; level 1 is the welcome card."""
    if lvl == 1:
        return welcome_card(host, guild, conf, record)
    arche = (record.get("assessment") or {}).get("archetype", "")
    words = orders.oath_line(lvl, arche, conf.get("name_set"))
    embed = {"title": f"{record.get('handle') or 'A member'} speaks the "
                      f"{role_name(conf, 'ideal', str(lvl))}",
             "description": f"*{words}*",
             "color": 15844367,
             "fields": [{"name": "Earned",
                         "value": orders.level_display(lvl, conf.get("name_set")),
                         "inline": True},
                        {"name": "Member", "value": _number(int(record.get("number") or 0)),
                         "inline": True}]}
    if lvl == 3:
        track = ideals.certified_track(ideals_store(host, guild), str(record.get("user")))
        if track in conf["tracks"]:
            embed["fields"].append({"name": "Certified", "value": orders.certified_display(
                conf["tracks"][track], conf.get("name_set")), "inline": True})
    return {"embeds": [embed]}


def announce(host, guild: str, user: str, lvl: int) -> list[str]:
    """Post the ceremony card: the bot channel, the first squad channel, and (level
    1) the member's DMs. Where it went."""
    from awdiscord.plugins import ScopedStore
    store = ScopedStore(host.store, guild, NAME)
    conf = config(store)
    record = ideals.record(store, user)
    card = ceremony_card(host, guild, conf, record, lvl)
    public = public_card(conf, record) if lvl == 1 else card
    rest = getattr(host, "rest", None)
    posted: list[str] = []
    if rest is None:
        return posted
    targets: list[str] = []
    try:
        channel = bot_channel(host, guild)
        if channel:
            targets.append(channel)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("join: bot channel unreadable in %s: %s", guild, exc)
    squad = next(iter(record.get("squads") or []), "")
    squad_ch = store.get(f"channel:{squad}") if squad and lvl >= 2 else ""
    if squad_ch and str(squad_ch) not in targets:
        targets.append(str(squad_ch))
    for target in targets:
        try:
            rest.create_message(target, {"content": f"<@{user}>", **public,
                                         "allowed_mentions": {"users": [user]}})
            posted.append(target)
        except Exception as exc:                              # noqa: BLE001
            log.warning("join: ceremony card failed in %s: %s", target, exc)
    if lvl == 1:
        try:
            dm = rest.dm_channel(user) or {}
            if dm.get("id"):
                rest.create_message(str(dm["id"]), card)
                posted.append("DM")
        except Exception as exc:                              # noqa: BLE001
            log.info("join: DM closed for %s: %s", user, exc)
    return posted


# ── the welcome card and the next step ────────────────────────────────────────

def first_issue(repo: str) -> dict | None:
    """The oldest open good-first-issue in ``repo`` (owner/name), or None."""
    if not repo or not _REPO.match(repo):
        return None
    from awdiscord.builtin import github
    q = f'repo:{repo} is:issue is:open label:"good first issue" sort:created-asc'
    query = ("query($q: String!) { search(query: $q, type: ISSUE, first: 1) "
             "{ nodes { ... on Issue { number title url } } } }")
    try:
        data = github.graphql(query, {"q": q}, opener=github.OPENER[0])
    except PluginError as exc:
        log.info("join: first issue lookup failed for %s: %s", repo, exc)
        return None
    nodes = [n for n in ((data.get("search") or {}).get("nodes") or []) if n]
    return nodes[0] if nodes else None


def next_step(host, guild: str, conf: dict, record: dict) -> str:
    from awdiscord.plugins import ScopedStore
    store = ScopedStore(host.store, guild, NAME)
    parts = []
    squads = record.get("squads") or []
    for s in squads[:3]:
        cid = store.get(f"channel:{s}")
        label = conf["squads"].get(s, s)
        parts.append(f"Say hi in <#{cid}> ({label})." if cid else f"Squad: {label}.")
    repo = next((conf["repos"].get(s) for s in squads if conf["repos"].get(s)), "")
    issue = first_issue(repo) if repo else None
    if issue:
        parts.append(f"First issue: [{repo}#{issue['number']}]({issue['url']}) "
                     f"{issue['title'][:80]} - comment on it to take it.")
    else:
        parts.append("Find a first issue with `/gh untracked` or `/gh board`, "
                     "then comment on it to take it.")
    return "\n".join(parts)


_academy_seen: dict[str, tuple[bool, float]] = {}


def academy_live(now: float | None = None) -> bool:
    """Does the learn site answer? Probed at most hourly; off with
    ``AWDISCORD_ACADEMY_PROBE=0`` (then the card says "coming soon")."""
    import os
    if (os.getenv("AWDISCORD_ACADEMY_PROBE") or "1").strip() in ("0", "false", "off"):
        return False
    now = time.time() if now is None else now
    hit = _academy_seen.get(orders.LEARN_URL)
    if hit and hit[1] > now:
        return hit[0]
    try:
        join_ladder._fetch(orders.LEARN_URL)
        ok = True
    except Exception:                                         # noqa: BLE001
        ok = False
    _academy_seen[orders.LEARN_URL] = (ok, now + 3600)
    return ok


def learn_line() -> str:
    return orders.LEARN_URL + ("" if academy_live() else " (coming soon)")


def archetype_fields(conf: dict, record: dict) -> list[dict]:
    key = (record.get("assessment") or {}).get("archetype", "")
    if key not in orders.BY_KEY:
        return []
    a = orders.BY_KEY[key]
    return [{"name": "Archetype",
             "value": f"**{role_name(conf, 'archetype', key)}** - {a.identity}"},
            {"name": "Strengths", "value": ", ".join(a.strengths), "inline": True},
            {"name": "In a squad", "value": a.squad_role, "inline": True}]


def public_card(conf: dict, record: dict) -> dict:
    """The bot-channel card: name, number, archetype. None of the member's answers
    (squad, track, skills, what they want to build) -- those go to their DM."""
    num = _number(int(record.get("number") or 0))
    badge = (f"Founding Cohort, {academy(conf)}, {num}" if record.get("founding")
             else f"{academy(conf)}, member {num}")
    key = (record.get("assessment") or {}).get("archetype", "")
    fields = []
    if key in orders.BY_KEY:
        fields.append({"name": "Archetype", "value": role_name(conf, "archetype", key)})
    return {"embeds": [{"title": f"Welcome, {record.get('handle') or 'friend'}",
                        "description": f"**{badge}**.",
                        "color": 15844367 if record.get("founding") else 3447003,
                        "fields": fields}]}


def welcome_card(host, guild: str, conf: dict, record: dict) -> dict:
    track = conf["tracks"].get(record.get("track") or "", record.get("track") or "-")
    squads = ", ".join(conf["squads"].get(s, s) for s in record.get("squads") or []) or "-"
    num = _number(int(record.get("number") or 0))
    badge = (f"Founding Cohort, {academy(conf)}, {num}" if record.get("founding")
             else f"{academy(conf)}, member {num}")
    embed = {"title": f"Welcome, {record.get('handle') or 'friend'}",
             "description": f"**{badge}**. A number nobody else will ever hold.\n"
                            f"*{orders.oath_line(1, '', conf.get('name_set'))}*",
             "color": 15844367 if record.get("founding") else 3447003,
             "fields": archetype_fields(conf, record) + [
                 {"name": "Squad", "value": squads[:1024], "inline": True},
                 {"name": "AAA track", "value": track[:1024], "inline": True},
                 {"name": "Next step",
                  "value": next_step(host, guild, conf, record)[:1024]},
                 {"name": "Learn", "value": learn_line()}]}
    return {"embeds": [embed]}


# ── operator actions ──────────────────────────────────────────────────────────

def _needs_admin(ctx) -> None:
    if not ctx.is_admin:
        raise PluginError("Only server managers can do that.")


def _status(ctx):
    conf = config(ctx.store)

    def work() -> str:
        cohort = ctx.store.get("cohort") or {}
        joined = len(ctx.store.keys("member:"))
        n = int(cohort.get("next") or 1) - 1
        gaps = permission_report(ctx.host, str(ctx.guild), conf)
        lines = [f"Join flow: plugin {'on' if ctx.host.enabled(ctx.guild, NAME) else 'off'}, "
                 f"config {'enabled' if conf.get('enabled') else 'DISABLED'}.",
                 f"Members: {joined} submitted, {n} numbers issued, founding cap "
                 f"{conf['cohort_cap']} ({max(0, int(conf['cohort_cap']) - n)} left).",
                 f"Squad channels: {'on' if conf.get('squad_channels') else 'off'}; "
                 f"weekly digest: {'on' if conf.get('digest') else 'off'}."]
        lines.append("Permission gaps:\n" + "\n".join(f"- {g}" for g in gaps)
                     if gaps else "Permission gaps: none.")
        return "\n".join(lines)
    return ctx.defer(work, private=True)


def _setup(ctx):
    conf = config(ctx.store)
    spec = layout(conf)
    problems = guildspec.validate_spec(spec)
    if problems:
        return ctx.reply("Refused, the layout is unsafe: " + "; ".join(problems),
                         private=True)
    guild = str(ctx.guild)

    def work() -> str:
        rest = _rest(ctx.host)
        standing = bot_standing(rest, guild)
        made, errors = [], []
        for r in spec["roles"]:
            before = len(standing["roles"])
            try:
                ensure_role(rest, guild, standing, r["name"], hoist=bool(r["hoist"]))
                if len(standing["roles"]) > before:
                    made.append(r["name"])
            except PluginError as exc:
                errors.append(f"{r['name']}: {exc}")
        if conf.get("squad_channels"):
            made += _ensure_squad_channels(ctx.store, rest, guild, conf, standing, errors)
        audit(ctx.store, ctx.user, "setup", "", f"created {len(made)}")
        out = [f"Created: {', '.join(made) if made else 'nothing (all present)'}."]
        if errors:
            out.append("Could not:\n" + "\n".join(f"- {e}" for e in errors))
        return "\n".join(out)
    return ctx.defer(work, private=True)


def _ensure_squad_channels(store, rest, guild: str, conf: dict, standing: dict,
                           errors: list[str]) -> list[str]:
    if not standing["bits"] & MANAGE_CHANNELS:
        errors.append("squad channels: I need **Manage Channels**")
        return []
    live = rest.request("GET", f"/guilds/{guild}/channels") or []
    made: list[str] = []
    cat_name = str(conf["category"])
    cat = next((c for c in live if isinstance(c, dict) and int(c.get("type") or 0) == 4
                and str(c.get("name", "")).casefold() == cat_name.casefold()), None)
    if cat is None:
        cat = rest.request("POST", f"/guilds/{guild}/channels",
                           {"name": cat_name[:100], "type": 4})
        made.append(cat_name)
    led = set()
    for key in store.keys("member:"):
        r = store.get(key)
        if isinstance(r, dict):
            led |= set(r.get("leads") or [])
    for s in conf["squads"]:
        slug = _slug(s)
        if s not in led:
            errors.append(f"#{slug}: no Squad Lead yet (made once `/lead` names one)")
            continue
        have = next((c for c in live if isinstance(c, dict)
                     and str(c.get("parent_id") or "") == str(cat.get("id"))
                     and c.get("name") == slug), None)
        if have is None:
            try:
                have = rest.request("POST", f"/guilds/{guild}/channels", {
                    "name": slug, "type": 0, "parent_id": str(cat.get("id")),
                    "topic": f"The {conf['squads'][s]} squad."})
                made.append(f"#{slug}")
            except Exception as exc:                          # noqa: BLE001
                errors.append(f"#{slug}: {type(exc).__name__}")
                continue
        if isinstance(have, dict) and have.get("id"):
            store.set(f"channel:{s}", str(have["id"]))
    return made


def join_card() -> dict:
    return {"embeds": [{"title": "Join the collective",
                        "description": "Get your member number, a squad and a track. "
                                       "The first members are the **Founding Cohort**.",
                        "color": 15844367}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Join",
                 "custom_id": PLUGIN.button_id("start")}]}]}


def _post(ctx):
    channel = str(ctx.opt("channel") or "")
    if not channel:
        return ctx.reply("Name the channel: `/enlist action:post channel:#where`.",
                         private=True)
    rest = _rest(ctx.host)
    rest.create_message(channel, join_card())
    audit(ctx.store, ctx.user, "post_card", channel)
    return ctx.reply(f"Join card posted in <#{channel}>.", private=True)


CSV_FIELDS = ("number", "founding", "user", "username", "handle", "github", "track",
              "squads", "skills", "availability", "build", "roles", "role_errors", "at",
              "archetype", "assessed_at", "ideal", "mentor", "github_verified")


def _cell(value: Any) -> str:
    text = ";".join(map(str, value)) if isinstance(value, list) else str(value)
    return "'" + text if text.startswith(_CSV_UNSAFE) else text


def export_csv(store) -> str:
    rows = [r for r in (store.get(k) for k in store.keys("member:")) if isinstance(r, dict)]
    rows.sort(key=lambda r: int(r.get("number") or 0))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS)
    for r in rows:
        done = r.get("assessment") or {}
        r = {**r, "archetype": done.get("archetype", ""), "assessed_at": done.get("at", ""),
             "ideal": r.get("ideal", 0)}
        w.writerow([_cell(r.get(f, "")) for f in CSV_FIELDS])
    return buf.getvalue()


def _export(ctx):
    data = export_csv(ctx.store)
    count = data.count("\n") - 1
    user = ctx.user

    def work() -> str:
        rest = _rest(ctx.host)
        dm = rest.dm_channel(user) or {}
        if not dm.get("id"):
            return "I could not open a DM with you; nothing was sent."
        rest.upload(str(dm["id"]), f"join-{ctx.guild}.csv", data.encode("utf-8"),
                    "text/csv", f"Join submissions: {count} row(s).")
        audit(ctx.store, user, "export", "", f"{count} rows")
        return f"Sent {count} row(s) to your DMs as CSV."
    return ctx.defer(work, private=True)


def _audit(ctx):
    rows = audit_log(ctx.store)
    if not rows:
        return ctx.reply("No audit entries yet.", private=True)
    lines = [f"<t:{e['at']}:f> <@{e['by']}> {e['action']} "
             f"{('<@' + e['target'] + '>') if e['target'].isdigit() and len(e['target']) > 15 else e['target']}"
             f" {e['detail']}".rstrip() for e in rows]
    return ctx.reply("\n".join(lines)[:1900], private=True)


def _set(ctx):
    key = str(ctx.opt("key") or "").strip()
    raw = str(ctx.opt("value") or "").strip()
    conf = config(ctx.store)
    if key in ("cohort_cap",):
        try:
            value: Any = max(0, min(int(raw), 100000))
        except ValueError:
            return ctx.reply("cohort_cap must be a number.", private=True)
        conf[key] = value
    elif key in ("squad_channels", "digest"):
        conf[key] = raw.lower() in ("1", "true", "yes", "on")
    elif key in ("category", "role_founding", "role_lead", "role_certified",
                 "role_track", "role_squad"):
        if not 1 <= len(raw) <= 90:
            return ctx.reply("A name is 1-90 characters.", private=True)
        conf[key] = raw
    elif key == "name_set":
        if orders.name_set(raw) != orders.ALIASES.get(raw, raw):
            return ctx.reply("name_set is one of: " + ", ".join(orders.NAME_SETS), private=True)
        conf[key] = orders.name_set(raw)
    elif key in ("umbrella", "academy"):
        if len(raw) > 90:
            return ctx.reply("A name is at most 90 characters.", private=True)
        conf[key] = raw
    elif key.startswith("repo."):
        squad = key[5:]
        if squad not in conf["squads"]:
            return ctx.reply(f"No squad {squad!r}.", private=True)
        if raw and not _REPO.match(raw):
            return ctx.reply("A repo is owner/name.", private=True)
        conf["repos"] = {**conf["repos"], squad: raw}
        if not raw:
            conf["repos"].pop(squad, None)
    else:
        return ctx.reply("Settable: " + ", ".join(SETTABLE), private=True)
    _save_config(ctx.store, conf)
    audit(ctx.store, ctx.user, "set", key, raw)
    return ctx.reply(f"Set **{key}** = `{raw}`.", private=True)


def _toggle(on: bool):
    def handler(ctx):
        conf = config(ctx.store)
        conf["enabled"] = on
        _save_config(ctx.store, conf)
        audit(ctx.store, ctx.user, "enable" if on else "disable")
        tail = "" if ctx.host.enabled(ctx.guild, NAME) else \
            " The plugin itself is off here: `/plugins enable join` too."
        return ctx.reply(f"Join flow {'enabled' if on else 'disabled'} in this server.{tail}",
                         private=True)
    return handler


def _layout(ctx):
    spec = layout(config(ctx.store))
    text = json.dumps(spec, indent=1)
    return ctx.reply(f"```json\n{text[:1850]}\n```", private=True)


def _me(ctx):
    record = ctx.store.get(f"member:{ctx.user}")
    if not isinstance(record, dict):
        return ctx.reply("You have not joined yet: run `/enlist`.", private=True)
    conf = config(ctx.store)
    return ctx.defer(lambda: {"content": "", **welcome_card(ctx.host, str(ctx.guild),
                                                             conf, record)}, private=True)


def _digest_now(ctx):
    guild = str(ctx.guild)
    return ctx.defer(lambda: post_digest(ctx.host, guild, by=ctx.user), private=True)


_ACTIONS: dict[str, tuple[Callable, bool]] = {   # action -> (handler, admin?)
    "start": (_start, False), "me": (_me, False),
    "assess": (lambda ctx: join_ladder.start_assessment(ctx), False),
    "post": (_post, True), "status": (_status, True), "setup": (_setup, True),
    "export": (_export, True), "audit": (_audit, True), "digest": (_digest_now, True),
    "layout": (_layout, True), "set": (_set, True),
    "enable": (_toggle(True), True), "disable": (_toggle(False), True),
}


def _enlist(ctx):
    action = str(ctx.opt("action") or "start")
    handler, admin = _ACTIONS.get(action, (None, False))
    if handler is None:
        return ctx.reply("Unknown action.", private=True)
    if admin:
        _needs_admin(ctx)
    return handler(ctx)


# ── /certify and /lead: operator-only, audited ────────────────────────────────

def _grant(kind: str):
    def handler(ctx):
        _needs_admin(ctx)
        conf = config(ctx.store)
        target = str(ctx.opt("user") or "")
        key = str(ctx.opt("track" if kind == "certified" else "squad") or "")
        pool = conf["tracks"] if kind == "certified" else conf["squads"]
        if key not in pool:
            return ctx.reply(f"Pick one of: {', '.join(pool)}.", private=True)
        if not target:
            return ctx.reply("Name the member.", private=True)
        if target == ctx.user:
            return ctx.reply("Nobody grants themselves an earned role.", private=True)
        revoke = bool(ctx.opt("revoke", False))
        name = role_name(conf, kind, key) if kind == "certified" else role_name(conf, "lead")
        guild, by = str(ctx.guild), ctx.user

        def work() -> str:
            rest = _rest(ctx.host)
            standing = bot_standing(rest, guild)
            role = ensure_role(rest, guild, standing, name, hoist=True)
            problem = rolekit.bot_ceiling_problem(role, standing["top"], standing["bits"],
                                                  bot_role_id=standing["bot_id"])
            if problem:
                raise PluginError(problem)
            ids = set(ctx.store.get("protected_role_ids") or [])
            ids.add(str(role["id"]))
            ctx.store.set("protected_role_ids", sorted(ids))
            try:
                (rolekit.remove if revoke else rolekit.assign)(
                    rest, guild, target, str(role["id"]))
            except rolekit.RoleError as exc:
                raise PluginError(str(exc)) from exc
            record = ctx.store.get(f"member:{target}") or {}
            field = "certified" if kind == "certified" else "leads"
            have = set(record.get(field) or [])
            (have.discard if revoke else have.add)(key)
            if record:
                record[field] = sorted(have)
                ctx.store.set(f"member:{target}", record)
            audit(ctx.store, by, ("revoke_" if revoke else "grant_") + kind, target,
                  f"{name} ({key})")
            tail = ""
            if kind == "certified" and not revoke and record:
                # An operator's certification is the practical's approval (3rd ideal).
                ideals.log_event(ctx.store, target, "review_approve", by, "", track=key,
                                 qualified=True, operator=True)
                gained = promote(ctx.host, guild, target)
                tail = f" They reached ideal {gained[-1]}." if gained else ""
            return f"{'Removed' if revoke else 'Gave'} **{name}** {'from' if revoke else 'to'} " \
                   f"<@{target}>. Logged.{tail}"
        return ctx.defer(work, private=True)
    return handler


# ── the weekly squad digest ───────────────────────────────────────────────────

def merged_leaderboard(repo: str, since: int) -> list[tuple[str, int]]:
    """(GitHub login, merged PRs) in ``repo`` since ``since`` (epoch), most first."""
    from awdiscord.builtin import github
    day = time.strftime("%Y-%m-%d", time.gmtime(since))
    query = ("query($q: String!, $after: String) { search(query: $q, type: ISSUE, "
             "first: 100, after: $after) { pageInfo { hasNextPage endCursor } "
             "nodes { ... on PullRequest { author { login } } } } }")
    counts: dict[str, int] = {}
    after = None
    for _ in range(5):
        data = github.graphql(query, {"q": f"repo:{repo} is:pr is:merged merged:>={day}",
                                      "after": after}, opener=github.OPENER[0])
        page = data.get("search") or {}
        for n in page.get("nodes") or []:
            login = str(((n or {}).get("author") or {}).get("login") or "")
            if login and not login.endswith("[bot]"):
                counts[login] = counts.get(login, 0) + 1
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def post_digest(host, guild: str, by: str = "system", now: float | None = None) -> str:
    """Post each squad's merged-PR leaderboard in its squad channel. A summary line."""
    from awdiscord.plugins import ScopedStore
    store = ScopedStore(host.store, guild, NAME)
    conf = config(store)
    now = time.time() if now is None else now
    rest = _rest(host)
    posted, skipped = [], []
    for s, label in conf["squads"].items():
        cid, repo = store.get(f"channel:{s}"), conf["repos"].get(s)
        if not cid or not repo:
            skipped.append(f"{label} ({'no channel' if not cid else 'no repo'})")
            continue
        try:
            board = merged_leaderboard(repo, int(now - WEEK))
        except PluginError as exc:
            skipped.append(f"{label} ({exc})")
            continue
        rows = "\n".join(f"`{i + 1:>2}.` **{login}** - {n} merged"
                         for i, (login, n) in enumerate(board[:10])) or "No merged PRs this week."
        embed: dict = {"title": f"{label} - weekly merged PRs", "description": rows[:4000],
                       "footer": {"text": repo}}
        close = join_ladder.close_to_next(store, s, conf, now)
        if close:
            embed["fields"] = [{"name": "Close to their next ideal",
                                "value": "\n".join(close)[:1024]}]
        rest.create_message(str(cid), {"embeds": [embed]})
        posted.append(label)
    store.set("digest:last", int(now))
    audit(store, by, "digest", "", f"posted {len(posted)}")
    out = f"Digest posted for: {', '.join(posted) or 'none'}."
    return out + (f" Skipped: {', '.join(skipped)}." if skipped else "")


def digest_due(host, guild: str, now: float) -> bool:
    from awdiscord.plugins import ScopedStore
    if not is_open(host, guild):
        return False
    store = ScopedStore(host.store, guild, NAME)
    conf = config(store)
    if not conf.get("digest") or not conf.get("squad_channels"):
        return False
    return now - float(store.get("digest:last") or 0) >= WEEK


def _on_ready(host) -> None:
    if _digest_started.is_set():
        return
    _digest_started.set()

    def loop() -> None:
        while True:
            try:
                now = time.time()
                for guild in host.store.guilds():
                    if is_open(host, guild):
                        join_ladder.sweep(host, guild, now)
                    if digest_due(host, guild, now):
                        log.info("join digest: %s", post_digest(host, guild, now=now))
            except Exception as exc:                          # noqa: BLE001
                log.warning("join digest sweep failed: %s", exc)
            time.sleep(DIGEST_POLL)

    threading.Thread(target=loop, name="join-digest", daemon=True).start()


# ── the plugin ────────────────────────────────────────────────────────────────

PLUGIN = Plugin(
    name=NAME,
    version="0.1.0",
    description="Join form, archetype assessment and the five-ideal ladder",
    default_enabled=False,
    on_button=_on_button,
    on_modal=_on_modal,
    on_ready=[_on_ready],
    persona=("This server has a join flow: point newcomers at /enlist to get their "
             "member number, squad, track and archetype, and members at /progress to see "
             "what their next ideal needs."),
    commands=[
        Command("enlist", "Join: get your member number, squad and track", _enlist,
                options=[
                    Option("action", "What to do (managers: post, status, setup, export...)",
                           choices=list(_ACTIONS)),
                    Option("channel", "Where to post the Join card (post)", type="channel"),
                    Option("key", "Setting to change (set)", max_length=60),
                    Option("value", "New value (set)", max_length=100)]),
        Command("certify", "Give a member the Certified <track> role (audited)",
                _grant("certified"), admin=True, options=[
                    Option("user", "The member", type="user", required=True),
                    Option("track", "Track", required=True, choices=list(TRACKS)),
                    Option("revoke", "Remove it instead", type="boolean")]),
        Command("lead", "Make a member a Squad Lead (audited)", _grant("lead"), admin=True,
                options=[
                    Option("user", "The member", type="user", required=True),
                    Option("squad", "Squad", required=True, choices=list(SQUADS)),
                    Option("revoke", "Remove it instead", type="boolean")]),
        Command("progress", "Your ideal, archetype, evidence, and what the next ideal needs",
                join_ladder.progress, options=[
                    Option("user", "Someone else's card", type="user")]),
        Command("verify-github", "Prove the GitHub account in your join record",
                join_ladder.verify_github),
        Command("prove-deploy", "Prove a self-host deploy (health check + your token)",
                join_ladder.prove_deploy, options=[
                    Option("url", "Your deploy's base URL (https://...)", required=True,
                           max_length=300)]),
        Command("review", "Peer review of a track practical", subcommands=[
            Command("approve", "Approve a member's track practical (3rd ideal+ or operator)",
                    join_ladder.review_approve, options=[
                        Option("user", "The member", type="user", required=True),
                        Option("track", "Track", required=True, choices=list(TRACKS)),
                        Option("evidence", "Link to the practical", max_length=300)])]),
        Command("nominate", "Nominate a squad-mate who led a milestone to release",
                join_ladder.nominate, options=[
                    Option("user", "The member", type="user", required=True),
                    Option("milestone", "Release link (https://...)", required=True,
                           max_length=300)]),
        Command("council", "The stewards' council", subcommands=[
            Command("vote", "Vote a member to the 5th ideal (5th ideal or operator)",
                    join_ladder.council_vote, options=[
                        Option("user", "The member", type="user", required=True),
                        Option("evidence", "Adoption / core-contribution link",
                               required=True, max_length=300)])]),
        Command("mentor", "Name who mentors you (once)", join_ladder.mentor, options=[
            Option("mentor", "Your mentor", type="user", required=True)]),
        Command("ideal", "Ideals ladder (operators)", admin=True, subcommands=[
            Command("evidence", "Record evidence for a member", join_ladder.ideal_evidence,
                    options=[Option("user", "The member", type="user", required=True),
                             Option("kind", "What it proves", required=True,
                                    choices=list(join_ladder.EVIDENCE_KINDS)),
                             Option("url", "Evidence link (https://...)", required=True,
                                    max_length=300)]),
            Command("confirm", "Confirm a nominated member's 4th ideal",
                    join_ladder.ideal_confirm,
                    options=[Option("user", "The member", type="user", required=True)]),
            Command("umbrella", "Grant the agent-access role early (partners)",
                    join_ladder.ideal_umbrella,
                    options=[Option("user", "The member", type="user", required=True),
                             Option("revoke", "Remove it instead", type="boolean")])]),
    ],
)
