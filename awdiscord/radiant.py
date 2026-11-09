"""Knights Radiant roles for a Discord server: the ten orders, five ideals, one umbrella.

Names and colours follow the Aitherium guild data (AitherVeil src/data/knights-radiant.ts,
oaths.ts IDEAL_NAMES; the Fourth and Fifth Ideal extend that ladder for the
Aitherium Collective). The provisioner is idempotent: it creates what is missing, sets the
colour of a role that exists with the wrong one, and NEVER deletes or renames anything.

It needs the bot to hold Manage Roles in that server. awdiscord's install link refuses that
permission on purpose (a leaked token must not take a server over), so a server owner grants
it by hand for the run and may take it back after. Without it, ``apply`` reports exactly
which roles it could not create and exits 1 -- it never claims a partial run succeeded.

    python -m awdiscord.radiant plan  --guild <id>     # what would change, no writes
    python -m awdiscord.radiant apply --guild <id>     # create / recolour
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"
MANAGE_ROLES = 1 << 28
ADMINISTRATOR = 1 << 3

# (role name, hex colour) -- singular names, matching the roles people already made by hand.
ORDERS = [
    ("Windrunner", "#38bdf8"), ("Skybreaker", "#64748b"), ("Dustbringer", "#ef4444"),
    ("Edgedancer", "#f472b6"), ("Truthwatcher", "#10b981"), ("Lightweaver", "#f59e0b"),
    ("Elsecaller", "#8b5cf6"), ("Willshaper", "#06b6d4"), ("Stoneward", "#a3a3a3"),
    ("Bondsmith", "#fbbf24"),
]
IDEALS = [("First Ideal", "#e5e7eb"), ("Second Ideal", "#93c5fd"), ("Third Ideal", "#5ec9cc"),
          ("Fourth Ideal", "#a78bfa"), ("Fifth Ideal", "#fbbf24")]
UMBRELLA = [("Knight Radiant", "#5ec9cc")]
PLAN = UMBRELLA + IDEALS + ORDERS

# -- the "radiant" name set of the join flow (awdiscord.orders) --------------------
# These names and words are someone else's fiction. They are used ONLY in a server
# whose join config opts into the radiant name set (the owner's own server); the
# default and every public surface use awdiscord.orders' original names.

#: The professional name set's archetype roles and umbrella, for packs that ask
#: radiant what a held role implies (aitherium_agents.implied_role_names). The
#: names themselves live in awdiscord.orders; these are views, never copies.
def _professional() -> tuple[list[tuple[str, str]], str]:
    from awdiscord import orders
    return ([(a.name, "#%06x" % a.color) for a in orders.ARCHETYPES],
            orders.NAMES["professional"]["umbrella"])


ARCHETYPES, PROFESSIONAL_UMBRELLA = _professional()

#: original archetype key -> the order role name above (same order, same colour).
ARCHETYPE_TO_ORDER = {
    "warden": "Windrunner", "arbiter": "Skybreaker", "breaker": "Dustbringer",
    "lamplighter": "Edgedancer", "seeker": "Truthwatcher", "weaver": "Lightweaver",
    "gatewright": "Elsecaller", "pathfinder": "Willshaper", "keystone": "Stoneward",
    "binder": "Bondsmith",
}
#: The first ideal, shared by every order (knights-radiant.ts FIRST_IDEAL).
FIRST_IDEAL_TEXT = ("Life before death. Strength before weakness. "
                    "Journey before destination.")
#: archetype key -> (second ideal, third ideal) words (knights-radiant.ts).
ORDER_IDEALS = {
    "warden": ("I will protect those who cannot protect themselves.",
               "I will protect even those I hate, so long as it is right."),
    "arbiter": ("I will put the law before all else.",
                "I will follow the will of my chosen code, even if it leads to my ruin."),
    "breaker": ("I will seek self-mastery before unleashing destruction.",
                "I will understand the consequences before I act."),
    "lamplighter": ("I will remember those who have been forgotten.",
                    "I will listen to those who have been ignored."),
    "seeker": ("I will seek truth and share it, regardless of consequence.",
               "I will not close my eyes to uncomfortable data."),
    "weaver": ("I will speak my truths fearlessly.", "I will create beauty that serves truth."),
    "gatewright": ("I will reach my potential, whatever the cost.",
                   "I will step through the boundaries others cannot see."),
    "pathfinder": ("I will seek freedom for those in bondage.",
                   "I will build the path, even if I never walk it myself."),
    "keystone": ("I will be there when I am needed.", "I will stand where others have fallen."),
    "binder": ("I will unite instead of divide.", "I will take the burden others cannot bear."),
}


def colour(hexs: str) -> int:
    return int(hexs.lstrip("#"), 16)


def diff(existing: list[dict], plan=PLAN) -> list[dict]:
    """Actions to reach the plan. Matching is case-insensitive on the name; never deletes."""
    by_name = {str(r.get("name", "")).strip().lower(): r for r in existing}
    out = []
    for name, hexs in plan:
        want = colour(hexs)
        have = by_name.get(name.lower())
        if have is None:
            out.append({"op": "create", "name": name, "color": want})
        elif int(have.get("color") or 0) != want:
            out.append({"op": "recolour", "name": have["name"], "id": have["id"], "color": want})
    return out


class Discord:
    def __init__(self, token: str):
        self.h = {"Authorization": "Bot " + token, "User-Agent": "awdiscord-radiant",
                  "Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, headers=self.h, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None


def can_manage_roles(d: Discord, guild: str) -> bool:
    me = d.call("GET", "/users/@me")
    roles = {r["id"]: r for r in d.call("GET", f"/guilds/{guild}/roles")}
    member = d.call("GET", f"/guilds/{guild}/members/{me['id']}")
    perms = 0
    for rid in list(member.get("roles", [])) + [guild]:
        perms |= int(roles.get(rid, {}).get("permissions", "0"))
    return bool(perms & (MANAGE_ROLES | ADMINISTRATOR))


def run(argv: list[str] | None = None, client: Discord | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awdiscord.radiant")
    ap.add_argument("mode", choices=["plan", "apply"])
    ap.add_argument("--guild", required=True)
    a = ap.parse_args(argv)
    if client is None:
        tok = os.getenv("AWDISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or ""
        if not tok:
            print("no bot token in AWDISCORD_TOKEN / DISCORD_BOT_TOKEN", file=sys.stderr)
            return 2
        client = Discord(tok)
    actions = diff(client.call("GET", f"/guilds/{a.guild}/roles"))
    for x in actions:
        print(f"{x['op']:9} {x['name']}  #{x['color']:06x}")
    if not actions:
        print("all Knights Radiant roles present with the right colours")
        return 0
    if a.mode == "plan":
        return 0
    if not can_manage_roles(client, a.guild):
        print("REFUSED: the bot lacks Manage Roles in this server; nothing was changed. "
              "Grant it to the bot's role, re-run, then take it back.", file=sys.stderr)
        return 1
    failed = []
    for x in actions:
        try:
            if x["op"] == "create":
                client.call("POST", f"/guilds/{a.guild}/roles",
                            {"name": x["name"], "color": x["color"], "hoist": False,
                             "mentionable": True, "permissions": "0"})
            else:
                client.call("PATCH", f"/guilds/{a.guild}/roles/{x['id']}", {"color": x["color"]})
        except urllib.error.HTTPError as exc:
            failed.append(f"{x['name']}: HTTP {exc.code}")
    remaining = diff(client.call("GET", f"/guilds/{a.guild}/roles"))
    if failed or remaining:
        print("INCOMPLETE: " + "; ".join(failed + [r["name"] for r in remaining]), file=sys.stderr)
        return 1
    print(f"done: {len(actions)} change(s); every Knights Radiant role verified present")
    return 0


if __name__ == "__main__":
    sys.exit(run())
