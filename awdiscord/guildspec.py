"""A Discord server's layout as a file: snapshot it, check drift, re-create what is missing.

The layout (roles, categories, channels, permission overwrites) is configuration, and a
configuration that exists only in Discord's UI is lost the day a server is rebuilt, moved
or vandalised. ``snapshot`` writes it to a JSON file that lives in the repo; ``check``
compares the live server to that file; ``apply`` re-creates what the file has and the
server lacks.

    python -m awdiscord.guildspec snapshot --guild <id> --out <file>
    python -m awdiscord.guildspec check    --guild <id> --spec <file>   # exit 1 on drift
    python -m awdiscord.guildspec apply    --guild <id> --spec <file>   # additive only

``apply`` is ADDITIVE ONLY: it creates missing roles, categories and channels and sets
overwrites on the ones it creates. It never deletes, renames, or edits an existing
channel's permissions -- a hand change in Discord is reported by ``check`` as drift, and
a human decides whether the file or the server is right (then re-snapshots).

``validate_spec`` is the guard in front of ``apply`` (and ``plan``): a layout file is
untrusted input, so a role with administrator/manage-server/moderation powers, an
overwrite that GRANTS administrator or manage-server, or a member overwrite that DENIES
anything is refused before a single write. ``plan`` lists what ``apply`` would create,
without writing. ``load_template`` reads a shipped starter layout by name.

Names, not ids, are the identity (ids change when a server is rebuilt). Overwrites name
roles by role name; a member overwrite keeps the user id (``member:<id>``).
Needs Manage Roles + Manage Channels for ``apply``; ``snapshot`` and ``check`` are reads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
CHANNEL_TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum"}
TYPE_IDS = {v: k for k, v in CHANNEL_TYPES.items()}
# Managed/integration roles (bots, boosters) are created by Discord, never by us.
SKIP_ROLE_TAGS = ("bot_id", "integration_id", "premium_subscriber")

#: Discord rate-limits channel/role creation per guild and answers 429 with a
#: `retry_after` in seconds. A starter layout is ~18 channel POSTs in one loop, so
#: without honouring it a single press stalls partway through. Bounded: a sleep
#: must never hang a Discord interaction.
RETRY_LIMIT = 3
MAX_RETRY_SLEEP = 5.0

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
BAN_MEMBERS = 1 << 2
KICK_MEMBERS = 1 << 1
MANAGE_ROLES = 1 << 28

#: A role must never carry these: each one reaches beyond the server layout.
DANGEROUS_ROLE_BITS = {
    "ADMINISTRATOR": ADMINISTRATOR,
    "MANAGE_GUILD": MANAGE_GUILD,
    "BAN_MEMBERS": BAN_MEMBERS,
    "KICK_MEMBERS": KICK_MEMBERS,
    "MANAGE_ROLES": MANAGE_ROLES,
}

#: An overwrite may deny these, but never allow them.
DANGEROUS_ALLOW_BITS = {"ADMINISTRATOR": ADMINISTRATOR, "MANAGE_GUILD": MANAGE_GUILD}

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = re.compile(r"^[a-z0-9-]+$")


class ApplyError(RuntimeError):
    """``apply`` created some items and refused (or could not create) others.

    A layout is ~18 creates in one loop, so any single refusal used to abort the
    loop and leave a half-built server with ONE generic message. Now every item is
    attempted, ``done`` names what exists (a retry is additive and safe),
    ``failures`` names each refused item and Discord's reason, and ``transient``
    is True when at least one failure is retryable — so a partner is never told
    "try again in a minute" about a permanent 403.
    """

    def __init__(self, done: list[str], failures: list[str], transient: bool):
        super().__init__("; ".join(failures) or "the layout could not be applied")
        self.done = list(done)
        self.failures = list(failures)
        self.transient = bool(transient)


def _retry_after(exc: urllib.error.HTTPError, default: float = 1.0) -> float:
    """Discord's own ``retry_after`` (seconds), bounded so a press cannot hang."""
    body = {}
    try:
        raw = exc.read() or b""
        body = json.loads(raw.decode("utf-8") or "{}")
    except (OSError, ValueError, UnicodeDecodeError):
        body = {}
    candidates = [body.get("retry_after") if isinstance(body, dict) else None,
                  (exc.headers or {}).get("Retry-After")]
    for value in candidates:
        try:
            return max(0.0, min(float(value), MAX_RETRY_SLEEP))
        except (TypeError, ValueError):
            continue
    return min(default, MAX_RETRY_SLEEP)


def _is_transient(exc: Exception) -> bool:
    """A failure worth retrying: 429 after the backoff, a 5xx, or no answer."""
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= int(getattr(exc, "code", 0) or 0) < 600 or exc.code == 429
    return isinstance(exc, (urllib.error.URLError, OSError))


class Discord:
    def __init__(self, token: str):
        self.h = {"Authorization": "Bot " + token, "User-Agent": "awdiscord-guildspec",
                  "Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, headers=self.h, method=method)
        for attempt in range(RETRY_LIMIT):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                # Honour Discord's own pacing. A 429 on the last attempt, or any
                # other status, is the caller's to judge (per item, in apply).
                if exc.code != 429 or attempt == RETRY_LIMIT - 1:
                    raise
                time.sleep(_retry_after(exc))
        raise urllib.error.URLError("retry loop exhausted")


def _managed(role: dict) -> bool:
    tags = role.get("tags") or {}
    return bool(role.get("managed")) or any(k in tags for k in SKIP_ROLE_TAGS)


def snapshot(d: Discord, guild: str) -> dict:
    roles = d.call("GET", f"/guilds/{guild}/roles")
    rname = {r["id"]: r["name"] for r in roles}
    rname[guild] = "@everyone"
    chans = d.call("GET", f"/guilds/{guild}/channels")
    cname = {c["id"]: c["name"] for c in chans}

    def overwrites(c: dict) -> list[dict]:
        out = []
        for o in c.get("permission_overwrites") or []:
            who = rname.get(o["id"]) if o.get("type") == 0 else f"member:{o['id']}"
            out.append({"target": who or f"role-id:{o['id']}", "allow": str(o["allow"]),
                        "deny": str(o["deny"])})
        return sorted(out, key=lambda x: x["target"])

    spec_roles = [
        {"name": r["name"], "color": int(r.get("color") or 0), "hoist": bool(r.get("hoist")),
         "mentionable": bool(r.get("mentionable")), "permissions": str(r.get("permissions", "0"))}
        for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
        if r["id"] != guild and not _managed(r)
    ]
    spec_chans = [
        {"name": c["name"], "type": CHANNEL_TYPES.get(c["type"], str(c["type"])),
         "category": cname.get(c.get("parent_id") or "", None), "topic": c.get("topic") or "",
         "position": int(c.get("position", 0)), "overwrites": overwrites(c)}
        for c in sorted(chans, key=lambda c: (c["type"] != 4, c.get("position", 0)))
        if c["type"] in CHANNEL_TYPES
    ]
    return {"version": 1, "guild_id": guild, "roles": spec_roles, "channels": spec_chans}


def _key(c: dict) -> tuple:
    return (c.get("category") or "", c["name"], c["type"])


def diff(spec: dict, live: dict) -> list[str]:
    """Human-readable drift lines; empty = the server matches the file."""
    out = []
    lr = {r["name"]: r for r in live["roles"]}
    for r in spec["roles"]:
        have = lr.get(r["name"])
        if have is None:
            out.append(f"role missing: {r['name']}")
        elif have["permissions"] != r["permissions"] or have["color"] != r["color"]:
            out.append(f"role changed: {r['name']}")
    for name in sorted(set(lr) - {r["name"] for r in spec["roles"]}):
        out.append(f"role not in file: {name}")
    lc = {_key(c): c for c in live["channels"]}
    sc = {_key(c): c for c in spec["channels"]}
    for k, c in sc.items():
        have = lc.get(k)
        where = f"{k[0] + '/' if k[0] else ''}#{k[1]} ({k[2]})"
        if have is None:
            out.append(f"channel missing: {where}")
        elif have["overwrites"] != c["overwrites"]:
            out.append(f"permissions changed: {where}")
    for k in sorted(set(lc) - set(sc)):
        out.append(f"channel not in file: {k[0] + '/' if k[0] else ''}#{k[1]} ({k[2]})")
    return out


def _bits(value) -> int | None:
    """A permission string as an int; None when it is not one (fail closed)."""
    try:
        return int(str(value if value is not None else "0"), 10)
    except (TypeError, ValueError):
        return None


def _where(c: dict) -> str:
    cat = c.get("category") or ""
    return f"{cat}/#{c.get('name', '')} ({c.get('type', '')})" if cat else \
        f"#{c.get('name', '')} ({c.get('type', '')})"


def validate_spec(spec: dict) -> list[str]:
    """Problems that make a layout file unsafe to apply; empty = worth applying.

    A spec is untrusted input (a template, a partner's file). Refuse a role that
    can administer or moderate the server, an overwrite that GRANTS administrator
    or manage-server, and a member overwrite that DENIES anything -- a layout
    builder has no business hiding a channel from one person.
    """
    if not isinstance(spec, dict):
        return ["spec is not an object"]
    problems: list[str] = []
    roles = spec.get("roles") or []
    channels = spec.get("channels") or []
    if not isinstance(roles, list):
        problems.append("roles is not a list")
        roles = []
    if not isinstance(channels, list):
        problems.append("channels is not a list")
        channels = []
    for r in roles:
        if not isinstance(r, dict):
            problems.append("a role entry is not an object")
            continue
        name = r.get("name", "")
        bits = _bits(r.get("permissions", "0"))
        if bits is None:
            problems.append(f"role {name!r}: permissions is not an integer")
            continue
        bad = sorted(n for n, bit in DANGEROUS_ROLE_BITS.items() if bits & bit)
        if bad:
            problems.append(f"role {name!r} grants dangerous permissions: " + ", ".join(bad))
    for c in channels:
        if not isinstance(c, dict):
            problems.append("a channel entry is not an object")
            continue
        for o in c.get("overwrites") or []:
            if not isinstance(o, dict):
                problems.append(f"{_where(c)}: an overwrite entry is not an object")
                continue
            target = str(o.get("target") or "")
            allow = _bits(o.get("allow", "0"))
            deny = _bits(o.get("deny", "0"))
            if allow is None or deny is None:
                problems.append(f"{_where(c)}: overwrite for {target!r} is not an integer")
                continue
            bad = sorted(n for n, bit in DANGEROUS_ALLOW_BITS.items() if allow & bit)
            if bad:
                problems.append(f"{_where(c)}: overwrite for {target!r} allows "
                                + ", ".join(bad))
            if target.startswith("member:") and deny:
                problems.append(f"{_where(c)}: overwrite denies {target} "
                                f"(a layout never hides a channel from one person)")
    return problems


def _guild_features(d: Discord, guild: str) -> set | None:
    """The guild's Discord features, or None when they cannot be read.

    Discord only allows GUILD_FORUM creation on a server with the COMMUNITY
    feature. A partner's ordinary server does not have it, so the starter
    template's forum is a permanent 400 there — and the layout would stop
    half-created. None means "unknown": the caller still TRIES (per-item
    isolation reports the refusal) rather than silently dropping a channel the
    file asked for.
    """
    try:
        got = d.call("GET", f"/guilds/{guild}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None
    if not isinstance(got, dict):
        return None
    raw = got.get("features")
    return {str(f) for f in raw} if isinstance(raw, list) else set()


def _forum_unavailable(c: dict, features: set | None) -> bool:
    """True when the file asks for a forum this guild cannot have."""
    return c.get("type") == "forum" and features is not None and "COMMUNITY" not in features


def apply(d: Discord, guild: str, spec: dict) -> list[str]:
    """Create what the file has and the server lacks. Never deletes or edits.

    Every item is attempted independently: one permanent refusal (a 403 on a
    channel, a forum on a non-Community server) never stops the rest. Lines
    starting ``created`` name what exists; lines starting ``skipped`` did not
    need a write. When anything was refused, ``ApplyError`` carries what was made,
    what failed and why, so the caller can tell a transient stall from a
    permission the partner will never grant.
    """
    problems = validate_spec(spec)
    if problems:
        raise ValueError("refusing to apply an unsafe layout: " + "; ".join(problems))
    live = snapshot(d, guild)
    done: list[str] = []
    failures: list[str] = []
    transient: list[bool] = []

    def post(what: str, path: str, body: dict) -> dict | None:
        """One Discord write; a refusal is recorded, never raised."""
        try:
            return d.call("POST", path, body)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            reason = (f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError)
                      else type(exc).__name__)
            failures.append(f"{what}: {reason}")
            transient.append(_is_transient(exc))
            return None

    have_roles = {r["name"] for r in live["roles"]}
    for r in reversed(spec.get("roles") or []):  # bottom-up so new roles stack in file order
        if r["name"] in have_roles:
            continue
        made = post(f"role {r['name']}", f"/guilds/{guild}/roles", {
            "name": r["name"], "color": r["color"], "hoist": r["hoist"],
            "mentionable": r["mentionable"], "permissions": r["permissions"]})
        if made is not None:
            done.append(f"created role {r['name']}")

    try:
        roles = d.call("GET", f"/guilds/{guild}/roles")
        chans = d.call("GET", f"/guilds/{guild}/channels")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        reason = (f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError)
                  else type(exc).__name__)
        raise ApplyError(done, failures + [f"read back the server: {reason}"],
                         any(transient) or _is_transient(exc)) from exc
    rid = {r["name"]: r["id"] for r in roles}
    rid["@everyone"] = guild

    def ow(c: dict) -> list[dict]:
        out = []
        for o in c.get("overwrites") or []:
            t = o["target"]
            if t.startswith("member:"):
                out.append({"id": t.split(":", 1)[1], "type": 1, "allow": o["allow"], "deny": o["deny"]})
            elif t in rid:
                out.append({"id": rid[t], "type": 0, "allow": o["allow"], "deny": o["deny"]})
        return out

    have = {(next((x["name"] for x in chans if x["id"] == c.get("parent_id")), "") or "",
             c["name"], CHANNEL_TYPES.get(c["type"], "")): c for c in chans}
    cat_id = {c["name"]: c["id"] for c in chans if c["type"] == 4}
    features = _guild_features(d, guild)
    for c in spec.get("channels") or []:  # categories come first in the file
        k = _key(c)
        if k in have:
            continue
        where = f"{c['type']} {c.get('category') or ''}/#{c['name']}"
        if _forum_unavailable(c, features):
            done.append(f"skipped {where}: this server is not a Community server")
            continue
        body = {"name": c["name"], "type": TYPE_IDS.get(c["type"], 0),
                "permission_overwrites": ow(c), "position": c["position"]}
        if c.get("topic"):
            body["topic"] = c["topic"]
        if c.get("category"):
            if c["category"] not in cat_id:
                continue  # its category failed; reported by the final check
            body["parent_id"] = cat_id[c["category"]]
        made = post(where, f"/guilds/{guild}/channels", body)
        if made is None:
            continue
        if c["type"] == "category":
            cat_id[c["name"]] = made["id"]
        done.append(f"created {where}")
    if failures:
        raise ApplyError(done, failures, any(transient))
    return done


def plan(d: Discord, guild: str, spec: dict) -> list[str]:
    """What ``apply`` WOULD create, without writing anything.

    Reads only (a snapshot). Raises ValueError on the same unsafe spec ``apply``
    refuses, so a preview cannot promise a layout the apply would reject. A
    channel ``apply`` must skip is reported with the same ``skipped`` wording, so
    the preview and the apply never disagree.
    """
    problems = validate_spec(spec)
    if problems:
        raise ValueError("refusing to plan an unsafe layout: " + "; ".join(problems))
    live = snapshot(d, guild)
    have = {_key(c) for c in live["channels"]}
    cats = {c["name"] for c in live["channels"] if c["type"] == "category"}
    features = _guild_features(d, guild)
    would: list[str] = []
    have_roles = {r["name"] for r in live["roles"]}
    for r in reversed(spec.get("roles") or []):  # same bottom-up order as apply
        if r["name"] not in have_roles:
            would.append(f"would create role {r['name']}")
    for c in spec.get("channels") or []:  # categories come first in the file
        if _key(c) in have:
            continue
        cat = c.get("category") or ""
        if cat and cat not in cats:
            continue  # its category is missing and not created first
        where = f"{c['type']} {cat + '/' if cat else ''}#{c['name']}"
        if _forum_unavailable(c, features):
            would.append(f"would skip {where} (this server is not a Community server)")
            continue
        would.append(f"would create {where}")
        if c["type"] == "category":
            cats.add(c["name"])
    return would


def load_template(name: str = "starter-community") -> dict:
    """Read a shipped layout template from ``awdiscord/templates/<name>.json``.

    ``name`` is matched against ``^[a-z0-9-]+$`` before it touches the
    filesystem, so a caller-supplied name cannot climb out of the directory.
    """
    name = str(name)
    if not TEMPLATE_NAME.fullmatch(name):
        raise ValueError("template name must match ^[a-z0-9-]+$")
    path = TEMPLATE_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no layout template named {name!r}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _client() -> Discord | None:
    tok = os.getenv("AWDISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or ""
    return Discord(tok) if tok else None


def run(argv: list[str] | None = None, client: Discord | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awdiscord.guildspec")
    ap.add_argument("mode", choices=["snapshot", "check", "apply"])
    ap.add_argument("--guild", required=True)
    ap.add_argument("--spec", help="layout file (check/apply)")
    ap.add_argument("--out", help="write the snapshot here (default: stdout)")
    a = ap.parse_args(argv)
    d = client or _client()
    if d is None:
        print("no bot token in AWDISCORD_TOKEN / DISCORD_BOT_TOKEN", file=sys.stderr)
        return 2
    try:
        if a.mode == "snapshot":
            text = json.dumps(snapshot(d, a.guild), indent=1, ensure_ascii=False) + "\n"
            if a.out:
                with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                print(f"wrote {a.out}")
            else:
                sys.stdout.write(text)
            return 0
        if not a.spec:
            print("--spec is required for check/apply", file=sys.stderr)
            return 2
        with open(a.spec, encoding="utf-8") as f:
            spec = json.load(f)
        if a.mode == "apply":
            for line in apply(d, a.guild, spec):
                print(line)
        drift = diff(spec, snapshot(d, a.guild))
        for line in drift:
            print(line)
        print("in sync" if not drift else f"{len(drift)} difference(s)")
        return 0 if not drift else 1
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ApplyError as exc:
        for line in exc.done:
            print(line)
        for line in exc.failures:
            print(f"refused: {line}", file=sys.stderr)
        print("transient failure(s) - a retry is safe and additive"
              if exc.transient else "permanent refusal(s) - retrying will not change them",
              file=sys.stderr)
        return 2 if exc.transient else 1
    except urllib.error.HTTPError as exc:
        print(f"Discord answered HTTP {exc.code} -- could not judge", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())
