"""Auto-create #bot: owner's guilds only, needs Manage Channels, never a duplicate."""
from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import autochannel, containment  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

OWNER = "111"
BOT = "999"
MANAGE = str(1 << 4)


class Rest:
    def __init__(self, guilds: dict, perms: str = MANAGE):
        self.g = guilds                 # id -> {"owner_id", "channels": [...]}
        self.perms = perms
        self.posts: list = []
        self.created: list = []
        self.race: dict = {}            # guild -> channel to appear on the next list

    def guilds(self):
        return [{"id": gid} for gid in self.g]

    def request(self, method, path, body=None):
        parts = path.strip("/").split("/")
        if path == "/users/@me":
            return {"id": BOT}
        gid = parts[1]
        if method == "GET" and len(parts) == 2:
            return {"id": gid, "owner_id": self.g[gid]["owner_id"],
                    "system_channel_id": "sys-" + gid}
        if method == "GET" and parts[2] == "channels":
            if gid in self.race:
                self.g[gid]["channels"].append(self.race.pop(gid))
            return list(self.g[gid]["channels"])
        if method == "GET" and parts[2] == "roles":
            return [{"id": gid, "permissions": "0"}, {"id": "br", "permissions": self.perms}]
        if method == "GET" and parts[2] == "members":
            return {"roles": ["br"]}
        if method == "POST" and parts[2] == "channels":
            ch = {"id": f"new{len(self.created)}", "type": 0, **body}
            self.g[gid]["channels"].append(ch)
            self.created.append((gid, body))
            return ch
        raise AssertionError(f"{method} {path}")

    def create_message(self, channel, payload):
        self.posts.append((channel, payload))
        return {"id": "m"}


class Host:
    def __init__(self):
        self.store = GuildStore()


class Bot:
    def __init__(self, rest, owners=(OWNER,)):
        self.rest = rest
        self.owners = list(owners)
        self.host = Host()
        self._bc = containment.BotChannels(
            lambda g: rest.request("GET", f"/guilds/{g}/channels"))

    def _channels(self):
        return self._bc


def test_creates_when_missing_in_owned_guild_under_aither_category():
    rest = Rest({"g1": {"owner_id": OWNER, "channels": [
        {"id": "c0", "name": "general", "type": 0},
        {"id": "cat", "name": "Aither Zone", "type": 4}]}})
    bot = Bot(rest)
    out = autochannel.sweep(bot)
    assert out["g1"].startswith("created:")
    gid, body = rest.created[0]
    assert body["name"] == "bot" and body["parent_id"] == "cat"
    assert body["topic"] == autochannel.TOPIC
    assert bot._channels().for_guild("g1") == out["g1"].split(":", 1)[1]


def test_top_level_when_no_matching_category():
    rest = Rest({"g1": {"owner_id": OWNER, "channels": [{"id": "x", "name": "Misc",
                                                         "type": 4}]}})
    autochannel.sweep(Bot(rest))
    assert "parent_id" not in rest.created[0][1]


def test_noop_when_present():
    rest = Rest({"g1": {"owner_id": OWNER, "channels": [{"id": "b", "name": "bot",
                                                         "type": 0}]}})
    assert autochannel.sweep(Bot(rest)) == {"g1": "present"}
    assert rest.created == [] and rest.posts == []


def test_missing_permission_is_reported_never_silent(caplog):
    rest = Rest({"g1": {"owner_id": OWNER, "channels": []}}, perms="0")
    with caplog.at_level("WARNING"):
        assert autochannel.sweep(Bot(rest)) == {"g1": "no-permission"}
    assert rest.created == []
    assert rest.posts == [("sys-g1", {"content": autochannel.NO_PERMISSION,
                                      "allowed_mentions": {"parse": []}})]
    assert any("no_permission" in r.message for r in caplog.records)


def test_partner_guild_untouched_unless_it_opts_in():
    rest = Rest({"p1": {"owner_id": "someone-else", "channels": []}})
    bot = Bot(rest)
    assert autochannel.sweep(bot) == {"p1": "skipped:not-owner"}
    assert rest.created == [] and rest.posts == []
    bot.host.store.set("p1", "core", "auto_bot_channel", True)
    assert autochannel.sweep(bot)["p1"].startswith("created:")


def test_opt_out_by_guild_config_and_env(monkeypatch):
    rest = Rest({"g1": {"owner_id": OWNER, "channels": []}})
    bot = Bot(rest)
    bot.host.store.set("g1", "core", "auto_bot_channel", False)
    assert autochannel.sweep(bot) == {"g1": "off"}
    bot.host.store.delete("g1", "core", "auto_bot_channel")
    monkeypatch.setenv("AWDISCORD_AUTO_BOT_CHANNEL", "0")
    assert autochannel.sweep(bot) == {"g1": "off"}
    assert rest.created == []


def test_no_duplicate_when_one_appears_between_check_and_create():
    rest = Rest({"g1": {"owner_id": OWNER, "channels": []}})
    rest.race["g1"] = {"id": "late", "name": "bot", "type": 0}
    assert autochannel.sweep(Bot(rest)) == {"g1": "present"}
    assert rest.created == []


def test_unknown_channel_list_creates_nothing():
    rest = Rest({"g1": {"owner_id": OWNER, "channels": []}})
    bot = Bot(rest)
    bot._bc = containment.BotChannels(lambda g: (_ for _ in ()).throw(OSError("down")))
    assert autochannel.sweep(bot) == {"g1": "unknown"}
    assert rest.created == []


def test_never_renames_moves_or_deletes():
    rest = Rest({"g1": {"owner_id": OWNER, "channels": [
        {"id": "c0", "name": "general", "type": 0}]}})
    calls = []
    orig = rest.request

    def spy(method, path, body=None):
        calls.append(method)
        return orig(method, path, body)
    rest.request = spy
    autochannel.sweep(Bot(rest))
    assert set(calls) <= {"GET", "POST"}
