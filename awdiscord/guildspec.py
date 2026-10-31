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

Names, not ids, are the identity (ids change when a server is rebuilt). Overwrites name
roles by role name; a member overwrite keeps the user id (``member:<id>``).
Needs Manage Roles + Manage Channels for ``apply``; ``snapshot`` and ``check`` are reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
CHANNEL_TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum"}
TYPE_IDS = {v: k for k, v in CHANNEL_TYPES.items()}
# Managed/integration roles (bots, boosters) are created by Discord, never by us.
SKIP_ROLE_TAGS = ("bot_id", "integration_id", "premium_subscriber")


class Discord:
    def __init__(self, token: str):
        self.h = {"Authorization": "Bot " + token, "User-Agent": "awdiscord-guildspec",
                  "Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, headers=self.h, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None


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


def apply(d: Discord, guild: str, spec: dict) -> list[str]:
    """Create what the file has and the server lacks. Never deletes or edits."""
    live = snapshot(d, guild)
    done = []
    have_roles = {r["name"] for r in live["roles"]}
    for r in reversed(spec["roles"]):  # bottom-up so new roles stack in file order
        if r["name"] not in have_roles:
            d.call("POST", f"/guilds/{guild}/roles", {
                "name": r["name"], "color": r["color"], "hoist": r["hoist"],
                "mentionable": r["mentionable"], "permissions": r["permissions"]})
            done.append(f"created role {r['name']}")
    roles = d.call("GET", f"/guilds/{guild}/roles")
    rid = {r["name"]: r["id"] for r in roles}
    rid["@everyone"] = guild

    def ow(c: dict) -> list[dict]:
        out = []
        for o in c["overwrites"]:
            t = o["target"]
            if t.startswith("member:"):
                out.append({"id": t.split(":", 1)[1], "type": 1, "allow": o["allow"], "deny": o["deny"]})
            elif t in rid:
                out.append({"id": rid[t], "type": 0, "allow": o["allow"], "deny": o["deny"]})
        return out

    chans = d.call("GET", f"/guilds/{guild}/channels")
    have = {(next((x["name"] for x in chans if x["id"] == c.get("parent_id")), "") or "",
             c["name"], CHANNEL_TYPES.get(c["type"], "")): c for c in chans}
    cat_id = {c["name"]: c["id"] for c in chans if c["type"] == 4}
    for c in spec["channels"]:  # categories come first in the file
        k = _key(c)
        if k in have:
            continue
        body = {"name": c["name"], "type": TYPE_IDS.get(c["type"], 0),
                "permission_overwrites": ow(c), "position": c["position"]}
        if c.get("topic"):
            body["topic"] = c["topic"]
        if c.get("category"):
            if c["category"] not in cat_id:
                continue  # its category failed; reported by the final check
            body["parent_id"] = cat_id[c["category"]]
        made = d.call("POST", f"/guilds/{guild}/channels", body)
        if c["type"] == "category":
            cat_id[c["name"]] = made["id"]
        done.append(f"created {c['type']} {c.get('category') or ''}/#{c['name']}")
    return done


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
    except urllib.error.HTTPError as exc:
        print(f"Discord answered HTTP {exc.code} -- could not judge", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())
