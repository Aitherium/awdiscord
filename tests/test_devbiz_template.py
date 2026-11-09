"""The dev-business templates: safe files, additive plan/apply, name sets, operator-only."""
import pytest
from awdiscord import devbiz
from awdiscord import guildspec as gs
from awdiscord.builtin import template as tpl
from awdiscord.plugins import PluginHost
from awdiscord.store import GuildStore

G = "g1"
BOT = "b1"
OP = "op1"
MANAGE_CHANNELS = 1 << 4
MANAGE_ROLES = 1 << 28


class Fake:
    """guildspec's client slice over an in-memory server; records every write."""

    def __init__(self, perms=MANAGE_ROLES | MANAGE_CHANNELS, features=("COMMUNITY",)):
        self.roles = [{"id": G, "name": "@everyone", "color": 0, "permissions": "0",
                       "position": 0},
                      {"id": "botrole", "name": "awdiscord", "color": 0,
                       "permissions": str(perms), "position": 1}]
        self.channels = []
        self.writes = []
        self.features = list(features)
        self.n = 100

    def call(self, method, path, body=None):
        if method == "GET":
            if path == "/users/@me":
                return {"id": BOT}
            if path == f"/guilds/{G}/members/{BOT}":
                return {"roles": ["botrole"]}
            if path.endswith("/roles"):
                return [dict(r) for r in self.roles]
            if path.endswith("/channels"):
                return [dict(c) for c in self.channels]
            if path == f"/guilds/{G}":
                return {"id": G, "features": list(self.features)}
            if path.startswith("/channels/"):
                cid = path.split("/")[2]
                return dict(next(c for c in self.channels if c["id"] == cid))
            raise AssertionError(path)
        self.writes.append((method, path, body))
        assert method == "POST", "templates only ever create"
        self.n += 1
        if path.startswith("/channels/"):
            return {"id": f"m{self.n}"}
        if body.get("available_tags"):
            body = {**body, "available_tags": [{"id": f"t{i}", **t} for i, t in
                                               enumerate(body["available_tags"])]}
        if path.endswith("/roles"):
            self.roles.append({"id": f"r{self.n}", "position": 1, **body})
            return self.roles[-1]
        ch = {"id": f"c{self.n}", **body}
        self.channels.append(ch)
        return ch


# -- the files ------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(devbiz.TEMPLATES))
@pytest.mark.parametrize("names", sorted(devbiz.NAME_SETS))
def test_templates_validate_clean_in_every_name_set(kind, names):
    spec = devbiz.render(kind, names, "api, web")
    assert gs.validate_spec(spec) == []
    assert "guild_id" not in spec
    assert all(r["permissions"] == "0" for r in spec["roles"])
    targets = {o["target"] for c in spec["channels"] for o in c["overwrites"]}
    assert not any(t.startswith("member:") or t.startswith("role-id:") for t in targets)
    role_names = {r["name"] for r in spec["roles"]} | {"@everyone"}
    assert targets <= role_names, targets - role_names
    cats = {c["name"] for c in spec["channels"] if c["type"] == "category"}
    assert all(c["category"] in cats for c in spec["channels"] if c["category"])


def test_minimal_has_the_brief_and_nothing_private():
    spec = devbiz.render("minimal")
    names = {c["name"] for c in spec["channels"]}
    for want in ("welcome", "join", "bot", "announcements", "help", "showcase",
                 "Projects", "releases", "Squads", "first-project"):
        assert want in names, want
    for private in ("moderator-only", "operations", "awnboard", "music", "founders"):
        assert private not in names
    roles = [r["name"] for r in spec["roles"]]
    s = devbiz.NAME_SETS["professional"]
    assert {s["umbrella"], s["partner"], "Squad Lead"} <= set(roles)
    assert set(s["ideals"]) <= set(roles) and len(s["ideals"]) == 5
    assert set(s["orders"]) <= set(roles) and len(s["orders"]) == 10
    assert "Builders" not in roles


def test_full_is_minimal_plus_extras():
    small = {(c["category"], c["name"]) for c in devbiz.render("minimal")["channels"]}
    big = {(c["category"], c["name"]) for c in devbiz.render("full")["channels"]}
    assert small < big
    assert {("Work", "decisions"), ("Projects", "first-project-tasks")} <= big
    assert set(devbiz.packs_for("minimal")) < set(devbiz.packs_for("full"))


def test_themed_swaps_every_slot_and_keeps_structure():
    a, r = devbiz.render("full", "professional"), devbiz.render("full", "radiant")
    assert r["name_set"] == "themed"
    assert [x["color"] for x in a["roles"]] == [x["color"] for x in r["roles"]]
    rn = {x["name"] for x in r["roles"]}
    assert {"Knight Radiant", "Ally of the Order", "Fifth Ideal", "Bondsmith"} <= rn
    assert not rn & {"Aitherium Engineer", "Aitherium Steward", "Binder"}
    bot = next(c for c in r["channels"] if c["name"] == "bot")
    assert {o["target"] for o in bot["overwrites"]} == {"@everyone", "Knight Radiant",
                                                         "Ally of the Order"}


def test_unknown_kind_or_name_set_is_refused():
    with pytest.raises(ValueError):
        devbiz.render("huge")
    with pytest.raises(ValueError):
        devbiz.render("minimal", "nope")


def test_projects_are_slugged_deduped_and_capped():
    assert devbiz.parse_projects("My API, my api ,Web!!") == ["my-api", "web"]
    with pytest.raises(ValueError):
        devbiz.parse_projects(",".join(f"p{i}" for i in range(11)))


# -- plan / apply ----------------------------------------------------------------

def test_plan_writes_nothing_and_apply_is_additive_and_idempotent():
    d = Fake()
    pv = devbiz.preview(d, G, "minimal")
    assert pv["would"] and pv["missing"] == [] and d.writes == []
    res = devbiz.run_apply(d, G, "minimal")
    assert res["failures"] == [] and len(d.writes) == len(pv["would"])
    assert all(m == "POST" for m, _, _ in d.writes)
    before = len(d.writes)
    again = devbiz.run_apply(d, G, "minimal")
    assert again["would"] == [] and len(d.writes) == before


def test_existing_channel_elsewhere_is_kept_not_duplicated():
    d = Fake()
    d.channels.append({"id": "x1", "name": "announcements", "type": 0, "position": 0})
    d.channels.append({"id": "x2", "name": "Company", "type": 4, "position": 1})
    d.channels.append({"id": "x3", "name": "releases", "type": 0, "parent_id": "x2",
                       "position": 2})
    pv = devbiz.preview(d, G, "minimal")
    assert not any("#announcements" in x or "#releases" in x for x in pv["would"])
    assert "kept existing #releases in Company" in pv["notes"]


def test_missing_permissions_are_named_and_nothing_is_written():
    d = Fake(perms=0)
    res = devbiz.run_apply(d, G, "minimal")
    assert d.writes == []
    assert any(m.startswith("Manage Roles (needed for") for m in res["missing"])
    assert any(m.startswith("Manage Channels (needed for") for m in res["missing"])


def test_forum_on_a_non_community_server_is_skipped_not_failed():
    d = Fake(features=())
    res = devbiz.run_apply(d, G, "full", projects="api")
    assert res["failures"] == []
    assert any(x.startswith("skipped forum") for x in res["done"])


# -- packs -----------------------------------------------------------------------

def test_enable_packs_respects_an_admin_choice_and_reports_missing():
    from awdiscord.builtin import community, github
    store = GuildStore(":memory:")
    host = PluginHost(store, plugins=[community.PLUGIN, github.PLUGIN])
    store.set_plugin_state(G, "community", False)
    out = devbiz.enable_packs(host, G, "minimal")
    assert out["enabled"] == ["github"]
    assert out["kept"] == ["community (off)"]
    assert "join" in out["not_installed"] and "collab" in out["not_installed"]
    assert host.enabled(G, "community") is False


# -- the plugin: who may lay out a server -----------------------------------------

def _host():
    host = PluginHost(GuildStore(":memory:"), operators={OP}, plugins=[tpl.PLUGIN])
    return host


def _it(user, sub, perms=1 << 5, guild=G, **opts):
    return {"guild_id": guild, "channel_id": "c0",
            "member": {"user": {"id": user}, "permissions": str(perms)},
            "data": {"name": "template", "options": [
                {"type": 1, "name": sub,
                 "options": [{"name": k, "value": v} for k, v in opts.items()]}]}}


def _press(user, cid, perms=0, guild=G):
    return {"guild_id": guild, "channel_id": "c0", "type": 3,
            "member": {"user": {"id": user}, "permissions": str(perms)},
            "data": {"custom_id": cid}}


ADMIN = 1 << 3
MANAGE = 1 << 5


def test_a_plain_member_is_refused_everything(monkeypatch):
    d = Fake()
    monkeypatch.setattr(tpl, "CLIENT_FACTORY", lambda host: d)
    host = _host()
    for sub in ("plan", "apply", "names"):
        r = host.handle(_it("member", sub, perms=0, kind="minimal"))
        assert "managers" in r.response["data"]["content"]   # the host's admin gate
    r = host.handle_button(_press("member", tpl.PLUGIN.button_id("p:full")))
    assert "admins" in r.response["data"]["content"]
    assert d.writes == []


@pytest.mark.parametrize("perms", [ADMIN, MANAGE])
def test_a_server_admin_lays_out_their_own_server_with_confirm(monkeypatch, perms):
    """Tom's case: no operator status, but Manage Server / Administrator in THAT guild."""
    d = Fake()
    monkeypatch.setattr(tpl, "CLIENT_FACTORY", lambda host: d)
    host = _host()
    text = host.handle(_it("tom", "plan", perms=perms, kind="partner")).deferred()["content"]
    assert "role(s)" in text and d.writes == []
    payload = host.handle(_it("tom", "apply", perms=perms, kind="partner")).deferred()
    assert d.writes == []                      # apply still waits for Confirm
    confirm = payload["components"][0]["components"][0]["custom_id"]
    # a member without admin rights cannot press the admin's Confirm
    refused = host.handle_button(_press("member", confirm, perms=0))
    assert "admins" in refused.response["data"]["content"] and d.writes == []
    # the plan is scoped to the guild it was made in: another guild has no such plan
    other = host.handle_button(_press("tom", confirm, perms=ADMIN, guild="g2"))
    assert "expired" in other.response["data"]["content"] and d.writes == []
    done = host.handle_button(_press("tom", confirm, perms=perms)).deferred()
    assert "Applied dev business (partner)" in done["content"] and d.writes


def test_a_server_admin_cannot_post_a_verified_project(monkeypatch):
    monkeypatch.setattr(tpl, "CLIENT_FACTORY", lambda host: Fake())
    host = _host()
    r = host.handle(_it("tom", "project", perms=ADMIN, repo="https://github.com/a/b"))
    assert "granted" in r.response["data"]["content"]


def test_operator_apply_shows_plan_then_confirm_creates(monkeypatch):
    d = Fake()
    monkeypatch.setattr(tpl, "CLIENT_FACTORY", lambda host: d)
    host = _host()
    r = host.handle(_it(OP, "apply", perms=0, kind="minimal", projects="api"))
    payload = r.deferred()
    assert d.writes == [] and "role(s)" in payload["content"]
    confirm = payload["components"][0]["components"][0]["custom_id"]
    done = host.handle_button(_press(OP, confirm)).deferred()
    assert "Applied dev business (minimal)" in done["content"]
    assert any(b and b.get("name") == "api" for _, _, b in d.writes)
    # the nonce is single-use
    again = host.handle_button(_press(OP, confirm))
    assert "expired" in again.response["data"]["content"]

def test_names_switch_changes_what_plan_would_create(monkeypatch):
    d = Fake()
    monkeypatch.setattr(tpl, "CLIENT_FACTORY", lambda host: d)
    host = _host()
    host.handle(_it(OP, "names", set="themed"))
    assert devbiz.name_set(host.store, G) == "themed"
    # one place: the join flow reads the same choice
    assert host.store.get(G, "join", "config")["name_set"] == "themed"
    text = host.handle(_it(OP, "plan", kind="minimal")).deferred()["content"]
    assert "Knight Radiant" in text and "Aitherium Engineer" not in text
    assert "components" not in host.handle(_it(OP, "plan", kind="minimal")).deferred()


def test_arrival_card_is_posted_once_with_three_buttons():
    class R:
        def __init__(self):
            self.sent = []

        def create_message(self, channel, payload):
            self.sent.append((channel, payload))

    host = _host()
    host.rest = R()
    guild = {"id": G, "name": "g", "owner_id": "o", "system_channel_id": "sys"}
    host.dispatch_guild_join(guild)
    host.dispatch_guild_join(guild)
    assert len(host.rest.sent) == 1
    channel, payload = host.rest.sent[0]
    ids = [b["custom_id"] for b in payload["components"][0]["components"]]
    assert channel == "sys" and ids == [tpl.PLUGIN.button_id("p:minimal"),
                                        tpl.PLUGIN.button_id("p:full"),
                                        tpl.PLUGIN.button_id("p:partner")]


# -- the partner section --------------------------------------------------------

def test_partner_section_is_one_category_and_never_themed():
    for names in sorted(devbiz.NAME_SETS):
        spec = devbiz.render("partner", names)
        assert gs.validate_spec(spec) == []
        assert spec["name_set"] == "professional"
        cats = [c for c in spec["channels"] if c["type"] == "category"]
        assert [c["name"] for c in cats] == ["Aitherium"]
        assert all(c["category"] == "Aitherium" for c in spec["channels"] if c is not cats[0])
        roles = {r["name"] for r in spec["roles"]}
        assert not roles & set(devbiz.NAME_SETS["themed"]["orders"] + ["Knight Radiant"])
        assert roles == {"Aitherium Engineer", "Aitherium Partner"}
        assert len(roles) == 2
    forum = next(c for c in devbiz.render("partner")["channels"] if c["name"] == "projects")
    assert forum["type"] == "forum"
    assert forum["tags"] == ["Verified", "Seeking contributors", "In progress", "Needs review"]


def test_forum_tags_are_sent_and_validated():
    d = Fake()
    devbiz.run_apply(d, G, "partner")
    body = next(b for _, _, b in d.writes if b and b.get("name") == "projects")
    assert [t["name"] for t in body["available_tags"]][0] == "Verified"
    bad = devbiz.render("partner")
    forum = next(c for c in bad["channels"] if c["name"] == "projects")
    forum["tags"] = ["Looking for contributors"]   # 24 characters: Discord refuses it
    assert any("tags" in p for p in gs.validate_spec(bad))


def test_partner_forum_falls_back_to_text_on_a_non_community_server():
    d = Fake(features=())
    res = devbiz.run_apply(d, G, "partner")
    assert res["failures"] == []
    made = next(b for _, _, b in d.writes if b and b.get("name") == "projects")
    assert made["type"] == 0 and "available_tags" not in made
    assert any("forums need a Community server" in n for n in res["notes"])


TABLE = """# Verified projects

| project | verified | looking for contributors | report |
|---|---|---|---|
| [Shop API](https://github.com/acme/shop-api) | 2026-11-08 | yes | https://x.io/r/1 |
| [Quiet Lib](https://github.com/acme/quiet-lib) | 2026-11-07 | no | https://x.io/r/2 |
"""

EMPTY_TABLE = """| project | verified | looking for contributors | report |
|---|---|---|---|
| None yet, as of 2026-11-08 | | | |
"""


def test_the_verified_table_parses_and_an_empty_program_is_empty(tmp_path):
    rows = devbiz.parse_verified(TABLE)
    shop = rows["https://github.com/acme/shop-api"]
    assert shop["name"] == "Shop API" and shop["verified"] == "2026-11-08"
    assert shop["contributors"] is True and shop["report"] == "https://x.io/r/1"
    assert rows["https://github.com/acme/quiet-lib"]["contributors"] is False
    assert devbiz.parse_verified(EMPTY_TABLE) == {}
    f = tmp_path / "projects.md"
    f.write_text(TABLE, encoding="utf-8")
    assert len(devbiz.load_verified(str(f))) == 2
    with pytest.raises(devbiz.VerifiedSourceError):
        devbiz.load_verified("")


def test_only_a_listed_project_can_be_posted():
    rows = devbiz.parse_verified(TABLE)
    assert devbiz.project("https://github.com/acme/shop-api/", rows)["name"] == "Shop API"
    with pytest.raises(ValueError, match="never self-applied"):
        devbiz.project("https://github.com/acme/not-listed", rows)
    with pytest.raises(ValueError):
        devbiz.project("http://github.com/acme/shop-api", rows)


def test_card_shows_the_programs_fields_and_never_invents_one():
    rows = devbiz.parse_verified(TABLE)
    card = devbiz.project_card(devbiz.project("https://github.com/acme/shop-api", rows),
                               "https://example.com/badge.png")
    embed = card["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert names == ["Repository", "Verified", "Looking for contributors",
                     "Verification report"]
    assert embed["image"]["url"].endswith("badge.png")      # wide badge, not a thumbnail
    assert embed["description"].startswith("Aitherium Verified Open Source Project.")
    assert devbiz.PROGRAM_URL in embed["description"]
    text = str(card).lower()
    for word in ("secure", "audited", "certified", "stack"):
        assert word not in text
    button = card["components"][0]["components"][0]
    assert button["label"] == "+ Join as contributor"
    assert button["custom_id"] == "awp:join:project:shop-api"
    quiet = devbiz.project_card(devbiz.project("https://github.com/acme/quiet-lib", rows))
    assert "components" not in quiet and "image" not in quiet["embeds"][0]


def test_project_post_tags_creates_the_squad_and_adds_it_to_the_join_form():
    d = Fake()
    devbiz.run_apply(d, G, "partner")
    store = GuildStore(":memory:")
    proj = devbiz.project("https://github.com/acme/shop-api", devbiz.parse_verified(TABLE))
    pv = devbiz.plan_project(d, G, proj)
    assert pv["missing"] == [] and "would create role Squad: Shop API" in pv["would"]
    n = len(d.writes)
    res = devbiz.post_project(d, G, proj, None, store=store)
    assert res["ok"]
    (_, _, role), (_, path, post) = d.writes[n:]
    assert role["name"] == "Squad: Shop API" and role["permissions"] == "0"
    assert path.endswith("/threads") and post["applied_tags"] == ["t0", "t1"]
    squads = store.get(G, "join", "config")["squads"]
    assert squads["shop-api"] == "Shop API" and len(squads) > 1   # defaults kept


def test_a_project_not_seeking_contributors_gets_no_role_and_no_button():
    d = Fake()
    devbiz.run_apply(d, G, "partner")
    proj = devbiz.project("https://github.com/acme/quiet-lib", devbiz.parse_verified(TABLE))
    n = len(d.writes)
    assert devbiz.post_project(d, G, proj)["ok"]
    [(_, path, post)] = d.writes[n:]
    assert post["applied_tags"] == ["t0"] and "components" not in post["message"]


def test_project_needs_the_section():
    d = Fake()
    proj = devbiz.project("https://github.com/acme/shop-api", devbiz.parse_verified(TABLE))
    pv = devbiz.plan_project(d, G, proj)
    assert any("partner template first" in m for m in pv["missing"])


def test_project_command_is_operator_only_then_preview_then_confirm(monkeypatch):
    d = Fake()
    devbiz.run_apply(d, G, "partner")
    monkeypatch.setattr(tpl, "CLIENT_FACTORY", lambda host: d)
    monkeypatch.setattr(tpl, "VERIFIED_LOADER", lambda: devbiz.parse_verified(TABLE))
    host = _host()
    opts = {"repo": "https://github.com/acme/shop-api"}
    n = len(d.writes)
    preview = host.handle(_it(OP, "project", **opts)).deferred()
    assert len(d.writes) == n and preview["embeds"][0]["title"] == "Shop API"
    confirm = preview["components"][0]["components"][0]["custom_id"]
    refused = host.handle_button(_press("tom", confirm, perms=ADMIN))
    assert "granted" in refused.response["data"]["content"]
    preview = host.handle(_it(OP, "project", **opts)).deferred()     # the refusal used it up
    confirm = preview["components"][0]["components"][0]["custom_id"]
    done = host.handle_button(_press(OP, confirm)).deferred()
    assert done["content"].startswith("Done:") and len(d.writes) == n + 2
    again = host.handle(_it(OP, "project", **opts)).deferred()
    assert "already posted" in again["content"] and "components" not in again
    monkeypatch.setattr(tpl, "VERIFIED_LOADER", lambda: devbiz.parse_verified(EMPTY_TABLE))
    none = host.handle(_it(OP, "project", repo="https://github.com/x/y")).deferred()
    assert "not on the Aitherium Verified list" in none["content"]


# -- the starter layout: on-brand in every name set -------------------------------

GENERIC = {"founders", "builders", "friends", "members", "admins"}


@pytest.mark.parametrize("names", [None] + sorted(devbiz.NAME_SETS))
def test_starter_layout_never_uses_a_generic_role_name(names):
    store = GuildStore(":memory:")
    if names:
        devbiz.set_name_set(store, G, names)
    spec = devbiz.starter_layout(store, G)
    assert gs.validate_spec(spec) == []
    assert not GENERIC & {r["name"].lower() for r in spec["roles"]}
    want = names or "professional"
    assert spec["roles"][0]["name"] == devbiz.NAME_SETS[want]["steward"]
    stewards = next(c for c in spec["channels"] if c["name"] == "stewards")
    assert spec["roles"][0]["name"] in {o["target"] for o in stewards["overwrites"]}


@pytest.mark.parametrize("name", sorted(p.stem for p in gs.TEMPLATE_DIR.glob("*.json")))
def test_no_shipped_template_has_a_generic_role_name(name):
    spec = gs.load_template(name)
    assert not GENERIC & {r["name"].lower() for r in spec["roles"]}, name


# -- the badge, attached (no public host) -----------------------------------------

PNG = devbiz.PNG_MAGIC + b"rest-of-a-png"


class FileFake(Fake):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.files = []

    def call_files(self, method, path, body, files):
        self.files.append((path, body, files))
        return self.call(method, path, body)


def test_badge_png_is_attached_and_shown_as_the_card_image():
    d = FileFake()
    devbiz.run_apply(d, G, "partner")
    proj = devbiz.project("https://github.com/acme/shop-api", devbiz.parse_verified(TABLE))
    assert devbiz.post_project(d, G, proj, None, badge_png=PNG)["ok"]
    [(path, body, files)] = d.files
    assert path.endswith("/threads") and files == [("aitherium-verified.png", PNG,
                                                     "image/png")]
    msg = body["message"]
    assert msg["embeds"][0]["image"]["url"] == "attachment://aitherium-verified.png"
    assert msg["attachments"] == [{"id": 0, "filename": "aitherium-verified.png"}]


def test_a_client_without_files_posts_without_the_image():
    d = Fake()
    devbiz.run_apply(d, G, "partner")
    proj = devbiz.project("https://github.com/acme/quiet-lib", devbiz.parse_verified(TABLE))
    assert devbiz.post_project(d, G, proj, None, badge_png=PNG)["ok"]
    post = d.writes[-1][2]
    assert "image" not in post["message"]["embeds"][0]


def test_read_badge_refuses_what_is_not_a_small_png(tmp_path):
    good = tmp_path / "b.png"
    good.write_bytes(PNG)
    assert devbiz.read_badge(str(good)) == PNG
    bad = tmp_path / "b.svg"
    bad.write_bytes(b"<svg/>")
    assert devbiz.read_badge(str(bad)) is None
    assert devbiz.read_badge(str(tmp_path / "missing.png")) is None
    assert devbiz.read_badge(None) is None


def test_rest_request_files_sends_payload_json_and_each_file():
    import json as _json

    from awdiscord.rest import Rest
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": "t1"}'

    def opener(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, req.data
        seen["ctype"] = req.headers.get("Content-type")
        return Resp()

    rest = Rest("x" * 24 + "." + "y" * 6 + "." + "z" * 27, opener=opener)
    out = rest.request_files("POST", "/channels/9/threads", {"name": "p"},
                             [("a.png", PNG, "image/png")])
    assert out == {"id": "t1"} and seen["url"].endswith("/channels/9/threads")
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    body = seen["body"]
    assert b'name="payload_json"' in body and _json.dumps({"name": "p"}).encode() in body
    assert b'name="files[0]"; filename="a.png"' in body and PNG in body
