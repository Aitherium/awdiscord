"""awdiscord.guildspec: snapshot -> check round-trips; apply only adds; drift is named."""
import copy
import json

from awdiscord import guildspec as gs

G = "g1"


class Fake:
    def __init__(self):
        self.roles = [
            {"id": G, "name": "@everyone", "color": 0, "permissions": "1024", "position": 0},
            {"id": "r1", "name": "Windrunner", "color": 3718648, "permissions": "0", "position": 2},
            {"id": "r2", "name": "AitherOS", "color": 0, "permissions": "8", "position": 3,
             "managed": True, "tags": {"bot_id": "b"}},
        ]
        self.channels = [
            {"id": "c1", "name": "Knights Radiant", "type": 4, "position": 0,
             "permission_overwrites": [{"id": G, "type": 0, "allow": "0", "deny": "1024"}]},
            {"id": "c2", "name": "admin-comms", "type": 0, "position": 1, "parent_id": "c1",
             "topic": "ops", "permission_overwrites": [
                 {"id": G, "type": 0, "allow": "0", "deny": "1024"},
                 {"id": "u9", "type": 1, "allow": "1024", "deny": "0"}]},
        ]
        self.writes = []
        self.n = 100

    def call(self, method, path, body=None):
        if method == "GET" and path.endswith("/roles"):
            return copy.deepcopy(self.roles)
        if method == "GET" and path.endswith("/channels"):
            return copy.deepcopy(self.channels)
        if method == "GET" and path == f"/guilds/{G}":
            return {"id": G, "name": "g", "features": ["COMMUNITY"]}
        self.writes.append((method, path, body))
        self.n += 1
        if path.endswith("/roles"):
            self.roles.append({"id": f"r{self.n}", "position": 1, **body})
            return self.roles[-1]
        ch = {"id": f"c{self.n}", **body}
        self.channels.append(ch)
        return ch


def test_snapshot_skips_managed_roles_and_names_targets():
    s = gs.snapshot(Fake(), G)
    assert [r["name"] for r in s["roles"]] == ["Windrunner"]
    admin = next(c for c in s["channels"] if c["name"] == "admin-comms")
    assert admin["category"] == "Knights Radiant"
    assert [o["target"] for o in admin["overwrites"]] == ["@everyone", "member:u9"]


def test_check_is_clean_on_its_own_snapshot(tmp_path):
    f = Fake()
    spec = tmp_path / "s.json"
    assert gs.run(["snapshot", "--guild", G, "--out", str(spec)], client=f) == 0
    assert gs.run(["check", "--guild", G, "--spec", str(spec)], client=f) == 0
    assert f.writes == []


def test_drift_is_named_both_ways(tmp_path):
    f = Fake()
    spec = gs.snapshot(f, G)
    f.channels[1]["permission_overwrites"][0]["deny"] = "0"      # someone opened admin-comms
    f.channels.append({"id": "c9", "name": "random", "type": 0, "position": 5})
    spec["roles"].append({"name": "Skybreaker", "color": 1, "hoist": False,
                          "mentionable": True, "permissions": "0"})
    lines = gs.diff(spec, gs.snapshot(f, G))
    assert "permissions changed: Knights Radiant/#admin-comms (text)" in lines
    assert "channel not in file: #random (text)" in lines
    assert "role missing: Skybreaker" in lines


def test_apply_recreates_missing_and_never_deletes_or_edits(tmp_path):
    f = Fake()
    spec = gs.snapshot(f, G)
    spec["roles"].append({"name": "Skybreaker", "color": 1, "hoist": False,
                          "mentionable": True, "permissions": "0"})
    spec["channels"].append({"name": "briefs", "type": "text", "category": "Knights Radiant",
                             "topic": "", "position": 3, "overwrites": [
                                 {"target": "@everyone", "allow": "0", "deny": "1024"}]})
    p = tmp_path / "s.json"
    p.write_text(json.dumps(spec))
    f.channels.append({"id": "c9", "name": "extra", "type": 0, "position": 9})
    rc = gs.run(["apply", "--guild", G, "--spec", str(p)], client=f)
    methods = {w[0] for w in f.writes}
    assert methods == {"POST"}                                    # no PATCH, no DELETE
    made = [w[2] for w in f.writes if w[1].endswith("/channels")][0]
    assert made["parent_id"] == "c1" and made["permission_overwrites"][0]["id"] == G
    assert rc == 1                                                # #extra is still drift


def test_no_token_cannot_judge(monkeypatch):
    monkeypatch.delenv("AWDISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert gs.run(["snapshot", "--guild", G]) == 2
