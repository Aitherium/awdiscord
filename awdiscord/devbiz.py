"""Turn a Discord server into a software dev and deployment business.

Three shipped layouts, all ordinary ``guildspec`` layouts (so every guard there applies:
``validate_spec`` refuses a dangerous permission, ``plan`` reads only, ``apply`` is
ADDITIVE ONLY and never deletes, renames or edits what exists):

  minimal   the least a server needs to run a dev shop with agents in it: an entrance
            (#welcome, #join, #announcements, #bot, #help, #showcase), Projects (one
            channel per project plus #releases), Squads, and the roles that gate who
            talks to the agents
  full      minimal plus the work, community and voice channels a running studio uses
  partner   ONE "Aitherium" category inside somebody else's server: #bot, #join and a
            verified-projects forum. Nothing else there is touched.

Names. ``awdiscord.orders`` owns the name sets and the server's choice lives in the join
config (``name_set``), so the join flow, the ladder and these layouts always agree:

  professional (default)  original names: Aitherium Engineer / Partner, Aitherium
                          Member..Steward, the ten archetypes (Warden..Binder)
  themed                  opt-in: Knight Radiant, First..Fifth Ideal, the ten orders
                          (``archetype`` and ``radiant`` are accepted as older spellings)

A template file names its source set and the SLOTS it uses (``"slots"``); ``render``
swaps those slots, so the sets never disagree on structure, colour or permissions. A
partner section never uses the themed names (``forbid_name_sets``).

What this module adds on top of ``guildspec``: ``render`` (name set + per-project
channels), ``fit_to_live`` (a channel the server already has elsewhere is kept, not
duplicated), ``fit_to_features`` (a forum on a non-Community server becomes its
``fallback_type``), ``missing_permissions`` (named before a single write),
``enable_packs`` (never overrides an admin's choice), and verified-project cards.
"""
from __future__ import annotations

import copy
import io
import os
import re
import urllib.error
from typing import Any, Iterable

from awdiscord import guildspec, orders

MANAGE_CHANNELS = 1 << 4
MANAGE_ROLES = 1 << 28
ADMINISTRATOR = 1 << 3

#: Template kind -> shipped layout file (``awdiscord/templates/<file>.json``).
TEMPLATES = {"minimal": "dev-business-minimal", "full": "dev-business-full",
             "partner": "partner-aitherium-section"}

DEFAULT_NAME_SET = orders.DEFAULT_SET


def _slots(set_name: str) -> dict[str, Any]:
    """One name set as slot -> name (lists are slot-aligned across sets)."""
    n = orders.NAMES[set_name]
    return {"umbrella": n["umbrella"], "partner": n["partner"], "lead": "Squad Lead",
            "steward": n["level_roles"][-1], "ideals": list(n["level_roles"]),
            "orders": [orders.archetype_name(k, set_name) for k in orders.KEYS]}


#: Slot-aligned name sets, derived from ``awdiscord.orders`` (never a second copy).
#: ``steward`` is the people who run the server (the starter layout's top role).
NAME_SETS: dict[str, dict[str, Any]] = {name: _slots(name) for name in orders.NAMES}

#: The server's choice lives in the join plugin's config (one place, one answer).
JOIN_PLUGIN = "join"
JOIN_CONFIG_KEY = "config"


def canonical(set_name: str | None) -> str:
    """A name-set spelling (``radiant`` -> ``themed``); ValueError when unknown."""
    value = orders.ALIASES.get(str(set_name or ""), str(set_name or ""))
    if value not in NAME_SETS:
        raise ValueError(f"unknown name set {set_name!r}; pick one of "
                         + ", ".join(sorted(NAME_SETS)))
    return value

MAX_PROJECTS = 10
_SLUG = re.compile(r"[^a-z0-9-]+")


def _rename_map(src: str, dst: str, slots: Iterable[str]) -> dict[str, str]:
    """``src`` name -> the same slot's name in ``dst``, for the slots a template uses."""
    a, b = NAME_SETS[canonical(src)], NAME_SETS[canonical(dst)]
    out: dict[str, str] = {}
    for slot in slots:
        if isinstance(a[slot], list):
            out.update(zip(a[slot], b[slot]))
        else:
            out[a[slot]] = b[slot]
    return out


def slug(name: str) -> str:
    """A Discord-style channel name: lowercase, a-z 0-9 and dashes, at most 90 chars."""
    s = _SLUG.sub("-", str(name).strip().lower().replace(" ", "-")).strip("-")
    return re.sub("-{2,}", "-", s)[:90]


def parse_projects(raw: str | Iterable[str] | None) -> list[str]:
    """Project names from a comma-separated option (or a list), slugged, de-duplicated."""
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for item in items:
        s = slug(item)
        if s and s not in out:
            out.append(s)
    if len(out) > MAX_PROJECTS:
        raise ValueError(f"at most {MAX_PROJECTS} projects at a time")
    return out


def render_layout(template: str, name_set: str | None = None) -> dict:
    """A shipped layout file with its role names in ``name_set``.

    ``None`` keeps the file's own set. A set the file forbids (a partner section never
    reads as themed) falls back to the default; ``spec["name_set"]`` says which ran.
    """
    spec = copy.deepcopy(guildspec.load_template(template))
    src = canonical(spec.get("name_set") or DEFAULT_NAME_SET)
    want = canonical(name_set or src)
    if want in {canonical(x) for x in spec.get("forbid_name_sets") or []}:
        want = DEFAULT_NAME_SET
    names = _rename_map(src, want, spec.get("slots") or [])
    for r in spec.get("roles") or []:
        r["name"] = names.get(r["name"], r["name"])
    for c in spec.get("channels") or []:
        for o in c.get("overwrites") or []:
            o["target"] = names.get(o["target"], o["target"])
    spec["name_set"] = want
    return spec


def render(kind: str, name_set: str = DEFAULT_NAME_SET,
           projects: Iterable[str] | str | None = None) -> dict:
    """The ``kind`` layout in ``name_set``, with channels for each project.

    A template with ``per_project`` and no project given gets one ``first-project``
    channel, so a fresh server is never left with an empty Projects category.
    Raises ValueError on an unknown kind or name set.
    """
    if kind not in TEMPLATES:
        raise ValueError(f"unknown template {kind!r}; pick one of " + ", ".join(TEMPLATES))
    spec = render_layout(TEMPLATES[kind], name_set)
    if not spec.get("per_project"):
        return spec
    projs = parse_projects(projects) or ["first-project"]
    cat = spec.get("project_category") or "Projects"
    pos = max((int(c.get("position", 0)) for c in spec["channels"]), default=0)
    have = {(c.get("category") or "", c["name"]) for c in spec["channels"]}
    for p in projs:
        for part in spec["per_project"]:
            name = slug(p + str(part.get("suffix") or ""))
            if (cat, name) in have:
                continue
            pos += 1
            have.add((cat, name))
            spec["channels"].append({
                "name": name, "type": part.get("type", "text"), "category": cat,
                "topic": str(part.get("topic") or "").replace("{project}", p),
                "position": pos, "overwrites": []})
    return spec


def starter_layout(store=None, guild: str = "", template: str = "starter-community") -> dict:
    """The starter community layout in this server's name set (professional by default)."""
    return render_layout(template, name_set(store, guild))


def name_set(store, guild: str) -> str:
    """This server's name set, read from the join config (the default until chosen)."""
    conf = {}
    if store is not None and guild:
        conf = store.get(str(guild), JOIN_PLUGIN, JOIN_CONFIG_KEY) or {}
    return orders.name_set(conf.get("name_set") if isinstance(conf, dict) else None)


def set_name_set(store, guild: str, value: str) -> str:
    """Record the server's name set in the join config; returns the canonical name."""
    value = canonical(value)
    conf = store.get(str(guild), JOIN_PLUGIN, JOIN_CONFIG_KEY) or {}
    conf = dict(conf) if isinstance(conf, dict) else {}
    conf["name_set"] = value
    store.set(str(guild), JOIN_PLUGIN, JOIN_CONFIG_KEY, conf)
    return value


def fit_to_live(spec: dict, live: dict) -> tuple[dict, list[str]]:
    """Keep a channel the server already has (same name and type) where it already is.

    ``guildspec`` keys a channel by (category, name, type), so a server with a
    top-level #announcements would get a second one under the template's category.
    For "set up THIS server" that is noise: the existing channel counts, and the note
    says so. Returns (a copy of the spec, notes).
    """
    out = copy.deepcopy(spec)
    by_name: dict[tuple, list[str]] = {}
    for c in live.get("channels") or []:
        by_name.setdefault((c["name"], c["type"]), []).append(c.get("category") or "")
    notes: list[str] = []
    for c in out.get("channels") or []:
        if c["type"] == "category":
            continue
        cats = by_name.get((c["name"], c["type"]))
        want = c.get("category") or ""
        if cats and want not in cats:
            c["category"] = cats[0] or None
            where = f"in {cats[0]}" if cats[0] else "at the top level"
            notes.append(f"kept existing #{c['name']} {where}")
    return out, notes


def fit_to_features(spec: dict, features: set | None) -> tuple[dict, list[str]]:
    """A forum with a ``fallback_type`` becomes that type on a non-Community server.

    Discord allows forums only with the COMMUNITY feature. A forum without a fallback
    is left alone (``guildspec`` skips it and says so). Unknown features = no change.
    """
    out = copy.deepcopy(spec)
    notes: list[str] = []
    if features is None or "COMMUNITY" in features:
        return out, notes
    for c in out.get("channels") or []:
        if c["type"] == "forum" and c.get("fallback_type"):
            c["type"] = c["fallback_type"]
            c.pop("tags", None)
            notes.append(f"#{c['name']} will be a {c['type']} channel: forums need a "
                         "Community server")
    return out, notes


class RestDiscord:
    """``guildspec``'s client interface over the bot's ``awdiscord.rest.Rest``.

    ``guildspec`` isolates each write by catching ``urllib.error.HTTPError``; ``Rest``
    raises its own ``RestError``. Translating here keeps that per-item isolation (one
    403 never stops the rest of the layout).
    """

    def __init__(self, rest):
        self.rest = rest

    def call(self, method: str, path: str, body: dict | None = None):
        from awdiscord.rest import RestError
        try:
            return self.rest.request(method, path, body)
        except RestError as exc:
            raise urllib.error.HTTPError(guildspec.API + path, int(exc.status), str(exc),
                                         {}, io.BytesIO(b"")) from exc

    def call_files(self, method: str, path: str, body: dict, files: list) -> dict:
        """``call`` with attachments (multipart); same error translation."""
        from awdiscord.rest import RestError
        try:
            return self.rest.request_files(method, path, body, files)
        except RestError as exc:
            raise urllib.error.HTTPError(guildspec.API + path, int(exc.status), str(exc),
                                         {}, io.BytesIO(b"")) from exc


def bot_permissions(d, guild: str) -> int | None:
    """The bot's server-wide permission bits, or None when they cannot be read."""
    try:
        me = d.call("GET", "/users/@me")
        roles = {r["id"]: r for r in d.call("GET", f"/guilds/{guild}/roles")}
        member = d.call("GET", f"/guilds/{guild}/members/{me['id']}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, KeyError, TypeError):
        return None
    perms = 0
    for rid in list((member or {}).get("roles") or []) + [str(guild)]:
        value = guildspec._bits(roles.get(rid, {}).get("permissions", "0"))  # noqa: SLF001
        perms |= value or 0
    return perms


def missing_permissions(d, guild: str, would: list[str]) -> list[str]:
    """What the bot lacks for this plan, in words; empty = it can do all of it."""
    roles = sum(1 for line in would if line.startswith("would create role "))
    chans = sum(1 for line in would if line.startswith("would create ")
                and not line.startswith("would create role "))
    if not roles and not chans:
        return []
    perms = bot_permissions(d, guild)
    if perms is None:
        return ["could not read the bot's permissions in this server"]
    if perms & ADMINISTRATOR:
        return []
    out = []
    if roles and not perms & MANAGE_ROLES:
        out.append(f"Manage Roles (needed for {roles} role(s))")
    if chans and not perms & MANAGE_CHANNELS:
        out.append(f"Manage Channels (needed for {chans} channel(s))")
    return out


def preview(d, guild: str, kind: str, names: str = DEFAULT_NAME_SET,
            projects: Iterable[str] | str | None = None) -> dict:
    """What ``kind`` would create here, read-only. Never writes."""
    spec = render(kind, names, projects)
    fitted, notes = fit_to_live(spec, guildspec.snapshot(d, guild))
    fitted, more = fit_to_features(fitted, guildspec._guild_features(d, guild))  # noqa: SLF001
    would = guildspec.plan(d, guild, fitted)
    return {"kind": kind, "name_set": spec["name_set"], "would": would,
            "notes": notes + more, "missing": missing_permissions(d, guild, would),
            "spec": fitted}


def run_apply(d, guild: str, kind: str, names: str = DEFAULT_NAME_SET,
              projects: Iterable[str] | str | None = None) -> dict:
    """Plan, refuse on a missing permission (nothing written), else apply additively."""
    pv = preview(d, guild, kind, names, projects)
    result = {"kind": kind, "name_set": pv["name_set"], "notes": pv["notes"],
              "would": pv["would"], "missing": pv["missing"], "done": [], "failures": [],
              "transient": False}
    if pv["missing"] or not pv["would"]:
        return result
    try:
        result["done"] = guildspec.apply(d, guild, pv["spec"])
    except guildspec.ApplyError as exc:
        result.update(done=exc.done, failures=exc.failures, transient=exc.transient)
    return result


def packs_for(kind: str) -> list[str]:
    """The plugins a template turns on, from its file."""
    if kind not in TEMPLATES:
        raise ValueError(f"unknown template {kind!r}")
    return list(guildspec.load_template(TEMPLATES[kind]).get("packs") or [])


def enable_packs(host, guild: str, kind: str) -> dict[str, list[str]]:
    """Switch the template's plugins on in ``guild``, respecting per-server choices.

    An admin who already turned a plugin on or off decided; that state is kept. A
    plugin this bot does not have installed is reported, never faked.
    """
    out: dict[str, list[str]] = {"enabled": [], "kept": [], "not_installed": []}
    for name in packs_for(kind):
        if name not in host.plugins:
            out["not_installed"].append(name)
            continue
        state = host.store.plugin_state(str(guild), name)
        if state is not None:
            out["kept"].append(f"{name} ({'on' if state else 'off'})")
            continue
        host.set_enabled(str(guild), name, True)
        out["enabled"].append(name)
    return out



# -- verified projects (the partner section's forum) --------------------------------
#
# The badge is GRANTED, never self-applied: a project card is posted only for a project
# listed in the verification program's own table (``verified/projects.md``), and it
# shows exactly that table's fields -- repo, verified date, report, and whether the
# project is looking for contributors. Nothing here invents a field or a verification.

VERIFIED_TAG = "Verified"
CONTRIBUTORS_TAG = "Seeking contributors"
SQUAD_ROLE = "Squad: {squad}"
PROGRAM_NAME = "Aitherium Verified"
BADGE_ALT = "Aitherium Verified Open Source Project"
#: Where the badge links: the program's own repository (plain URL, never relative).
PROGRAM_URL = "https://github.com/Aitherium/occupyxaitherium"
#: The verified badge as a PNG URL (Discord embeds do not render SVG). Unset = no image.
BADGE_ENV = "AWDISCORD_VERIFIED_BADGE_URL"
#: ...or a PNG on disk, attached to the post itself (no public host needed).
BADGE_FILE_ENV = "AWDISCORD_VERIFIED_BADGE_FILE"
BADGE_FILENAME = "aitherium-verified.png"
MAX_BADGE_BYTES = 1024 * 1024
PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
#: Where the verified table lives: a file path or an https:// URL of the Markdown.
SOURCE_ENV = "AWDISCORD_VERIFIED_PROJECTS"
_URL = re.compile(r"^https://[^\s/]+\.[^\s/]+(/[^\s]*)?$")
_LINK = re.compile(r"\[([^\]]+)\]\((https://[^)\s]+)\)")


class VerifiedSourceError(RuntimeError):
    """The verified-projects table is not configured or cannot be read."""


def _norm_repo(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower().removesuffix(".git")


def _cell_link(cell: str) -> tuple[str, str]:
    """(display name, url) from a table cell holding ``[name](url)`` or a bare URL."""
    m = _LINK.search(cell)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    raw = cell.strip().strip("<>")
    return (raw.rstrip("/").rsplit("/", 1)[-1], raw) if _URL.match(raw) else ("", "")


def parse_verified(markdown: str) -> dict[str, dict]:
    """The verified table: normalised repo URL -> its row. Rows without a repo are skipped.

    Columns, in order: project (repo link) | verified (date) | looking for contributors
    (yes/no) | report (verification issue URL). A placeholder row ("None yet") has no
    repo link, so an empty program parses to an empty dict.
    """
    out: dict[str, dict] = {}
    for line in str(markdown or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        name, repo = _cell_link(cells[0])
        if not repo:
            continue
        _, report = _cell_link(cells[3])
        out[_norm_repo(repo)] = {
            "key": slug(name)[:60] or slug(repo.rsplit("/", 1)[-1])[:60],
            "name": name[:100], "repo": repo, "verified": cells[1][:40],
            "contributors": cells[2].strip().lower() in ("yes", "y", "true"),
            "report": report}
    return out


def load_verified(source: str | None = None, opener=None) -> dict[str, dict]:
    """Read the verified table from ``source`` (default ``$AWDISCORD_VERIFIED_PROJECTS``).

    Raises VerifiedSourceError when it is unset or unreadable: no list means nothing can
    be posted, never "everything is verified".
    """
    source = (source if source is not None else os.getenv(SOURCE_ENV, "")).strip()
    if not source:
        raise VerifiedSourceError(f"the verified-projects list is not configured "
                                  f"({SOURCE_ENV})")
    try:
        if source.startswith("https://"):
            import urllib.request
            opener = opener or urllib.request.urlopen
            with opener(urllib.request.Request(source), timeout=10) as resp:
                text = resp.read().decode("utf-8", "replace")
        else:
            with open(source, encoding="utf-8") as f:
                text = f.read()
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise VerifiedSourceError(f"could not read the verified-projects list: "
                                  f"{type(exc).__name__}") from exc
    return parse_verified(text)


def project(repo: str, verified: dict[str, dict]) -> dict:
    """The verified record for ``repo``. ValueError when it is not on the list."""
    repo = str(repo or "").strip()
    if not _URL.match(repo):
        raise ValueError("repo must be an https:// URL")
    row = verified.get(_norm_repo(repo))
    if row is None:
        raise ValueError(f"{repo} is not on the {PROGRAM_NAME} list; the badge is granted, "
                         "never self-applied")
    return dict(row)


def join_payload(key: str) -> str:
    """The join plugin's button payload: open /enlist with this project's squad."""
    return f"project:{key}"


def project_card(proj: dict, badge_url: str | None = None) -> dict:
    """The post body: the program's own fields, the badge, and (opt-in) a Join button."""
    fields = [{"name": "Repository", "value": proj["repo"][:1024], "inline": False},
              {"name": "Verified", "value": proj.get("verified") or "-", "inline": True},
              {"name": "Looking for contributors",
               "value": "Yes" if proj.get("contributors") else "No", "inline": True}]
    if proj.get("report"):
        fields.append({"name": "Verification report", "value": proj["report"][:1024],
                       "inline": False})
    embed: dict[str, Any] = {
        "title": proj["name"][:256], "url": proj["repo"], "color": 0x5ec9cc,
        "description": f"{BADGE_ALT}. Program: {PROGRAM_URL}", "fields": fields,
        "footer": {"text": PROGRAM_NAME},
    }
    if badge_url:
        embed["image"] = {"url": badge_url}        # 616x80: a wide image, not a thumbnail
    body: dict[str, Any] = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if proj.get("contributors"):
        body["components"] = [{"type": 1, "components": [{
            "type": 2, "style": 3, "label": "+ Join as contributor",
            "custom_id": f"awp:join:{join_payload(proj['key'])}"[:100]}]}]
    return body


def _project_channel(d, guild: str, kind: str = "partner") -> dict | None:
    where = guildspec.load_template(TEMPLATES[kind]).get("project_channel") or {}
    chans = d.call("GET", f"/guilds/{guild}/channels")
    cat_ids = {c["id"] for c in chans if c["type"] == 4
               and c["name"] == where.get("category")}
    for c in chans:
        if c["name"] == where.get("name") and c["type"] in (0, 15) \
                and c.get("parent_id") in cat_ids:
            return c
    return None


def plan_project(d, guild: str, proj: dict, posted: bool = False) -> dict:
    """What posting ``proj`` would do, read-only."""
    chan = _project_channel(d, guild)
    lines: list[str] = []
    missing: list[str] = []
    if chan is None:
        missing.append("the Aitherium/#projects channel (apply the partner template first)")
    if posted:
        missing.append(f"nothing: {proj['name']} is already posted")
    role = SQUAD_ROLE.format(squad=proj["name"])
    have = {r["name"] for r in d.call("GET", f"/guilds/{guild}/roles")}
    wants_role = bool(proj.get("contributors")) and role not in have
    if wants_role:
        lines.append(f"would create role {role}")
    if chan is not None:
        kind = "forum post" if chan["type"] == 15 else "message"
        tags = [VERIFIED_TAG] + ([CONTRIBUTORS_TAG] if proj.get("contributors") else [])
        tag = f", tagged {' + '.join(tags)}" if chan["type"] == 15 else ""
        lines.append(f"would post a {kind} in #{chan['name']}{tag}: {proj['name']}")
    if not missing and wants_role:
        missing += missing_permissions(d, guild, lines[:1])
    return {"would": lines, "missing": missing, "channel": chan, "role": role,
            "wants_role": wants_role}


def add_squad(store, guild: str, proj: dict) -> None:
    """Put the project in the join config's squads, so /enlist, /lead and digests see it."""
    from awdiscord.builtin import join
    from awdiscord.plugins import ScopedStore
    scoped = ScopedStore(store, str(guild), JOIN_PLUGIN)
    squads = dict(join.config(scoped).get("squads") or {})
    squads[proj["key"]] = proj["name"]
    saved = scoped.get(JOIN_CONFIG_KEY) or {}
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved["squads"] = squads
    scoped.set(JOIN_CONFIG_KEY, saved)


def read_badge(path: str | None) -> bytes | None:
    """The badge PNG's bytes, or None when unset, missing, too big or not a PNG."""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_BADGE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_BADGE_BYTES or not data.startswith(PNG_MAGIC):
        return None
    return data


def post_project(d, guild: str, proj: dict, badge_url: str | None = None,
                 store=None, badge_png: bytes | None = None) -> dict:
    """Create the squad role (contributors only, permissions 0) and post the card.

    ``badge_png`` (bytes) is attached to the post and shown as the card image, so the
    badge needs no public host; it wins over ``badge_url``. A client that cannot send
    files posts the card without the image rather than failing.
    """
    pv = plan_project(d, guild, proj)
    if pv["missing"]:
        return {"ok": False, "missing": pv["missing"], "done": []}
    done: list[str] = []
    if pv["wants_role"]:
        d.call("POST", f"/guilds/{guild}/roles", {
            "name": pv["role"], "permissions": "0", "mentionable": True, "hoist": False,
            "color": 0})
        done.append(f"created role {pv['role']}")
    if proj.get("contributors") and store is not None:
        add_squad(store, guild, proj)
        done.append(f"added squad {proj['key']} to the join form")
    attach = bool(badge_png) and hasattr(d, "call_files")
    image = f"attachment://{BADGE_FILENAME}" if attach else badge_url
    chan, body = pv["channel"], project_card(proj, image)
    files = [(BADGE_FILENAME, badge_png, "image/png")] if attach else []
    if attach:
        body["attachments"] = [{"id": 0, "filename": BADGE_FILENAME}]
    if chan["type"] == 15:
        full = d.call("GET", f"/channels/{chan['id']}") or {}
        want = {VERIFIED_TAG} | ({CONTRIBUTORS_TAG} if proj.get("contributors") else set())
        tags = [t["id"] for t in full.get("available_tags") or [] if t.get("name") in want]
        path, payload = f"/channels/{chan['id']}/threads", {
            "name": proj["name"][:100], "message": body, "applied_tags": tags[:5]}
    else:
        path, payload = f"/channels/{chan['id']}/messages", body
    if attach:
        made = d.call_files("POST", path, payload, files)
    else:
        made = d.call("POST", path, payload)
    done.append(f"posted {proj['name']} in #{chan['name']}")
    return {"ok": True, "missing": [], "done": done, "id": (made or {}).get("id")}
