"""The shipped starter layout, the safety guard, and the read-only preview."""
import io
import urllib.error

import pytest
from awdiscord import guildspec as gs

G = "g1"
VIEW_CHANNEL = 1 << 10


class Fake:
    """The slice of guildspec.Discord the planner reads, plus a write log."""

    def __init__(self):
        self.roles = [{"id": G, "name": "@everyone", "color": 0,
                       "permissions": "0", "position": 0}]
        self.channels = []
        self.writes = []
        self.features = ["COMMUNITY"]
        self.n = 100

    def call(self, method, path, body=None):
        if method == "GET" and path.endswith("/roles"):
            return [dict(r) for r in self.roles]
        if method == "GET" and path.endswith("/channels"):
            return [dict(c) for c in self.channels]
        if method == "GET" and path == f"/guilds/{G}":
            return {"id": G, "name": "g", "features": list(self.features)}
        self.writes.append((method, path, body))
        self.n += 1
        if path.endswith("/roles"):
            self.roles.append({"id": f"r{self.n}", "position": 1, **body})
            return self.roles[-1]
        ch = {"id": f"c{self.n}", **body}
        self.channels.append(ch)
        return ch


class Boom:
    def call(self, *args, **kwargs):
        raise AssertionError("an unsafe spec must be refused before any Discord call")


def test_starter_template_is_version_one_without_a_guild_id():
    spec = gs.load_template()
    assert spec["version"] == 1
    assert "guild_id" not in spec
    assert [r["name"] for r in spec["roles"]] == ["Founders", "Builders", "Friends"]
    names = [c["name"] for c in spec["channels"]]
    for want in ("welcome", "rules", "introductions", "general", "ideas", "showcase",
                 "projects", "decisions", "briefs", "agents", "github-feed", "founders",
                 "Lounge", "Standup"):
        assert want in names, want
    types = {c["name"]: c["type"] for c in spec["channels"]}
    assert types["projects"] == "forum"
    assert types["Lounge"] == "voice" and types["Standup"] == "voice"


def test_starter_template_children_sit_in_categories_that_are_in_the_file():
    spec = gs.load_template()
    cats = {c["name"] for c in spec["channels"] if c["type"] == "category"}
    assert cats == {"Start Here", "Community", "Build", "Voice"}
    for c in spec["channels"]:
        if c["type"] != "category":
            assert c["category"] in cats, c["name"]
    # Categories are created before their children, or apply() would skip them.
    order = [c["name"] for c in spec["channels"]]
    for c in spec["channels"]:
        if c["type"] != "category":
            assert order.index(c["category"]) < order.index(c["name"])


def test_starter_template_has_no_dangerous_bits_and_validates_clean():
    spec = gs.load_template()
    assert gs.validate_spec(spec) == []
    for r in spec["roles"]:
        assert int(r["permissions"]) == 0


def test_founders_channel_is_hidden_from_everyone_and_open_to_founders():
    spec = gs.load_template()
    ch = next(c for c in spec["channels"] if c["name"] == "founders")
    everyone = next(o for o in ch["overwrites"] if o["target"] == "@everyone")
    founders = next(o for o in ch["overwrites"] if o["target"] == "Founders")
    assert int(everyone["deny"]) & VIEW_CHANNEL
    assert int(founders["allow"]) & VIEW_CHANNEL
    assert "member:" not in {o["target"] for c in spec["channels"]
                             for o in c["overwrites"]}


@pytest.mark.parametrize("name", ["../secrets", "Starter Community", "", "a/b", "A",
                                  "starter-community.json"])
def test_load_template_refuses_names_that_could_climb_out(name):
    with pytest.raises(ValueError):
        gs.load_template(name)


def test_an_unknown_template_is_an_error():
    with pytest.raises(FileNotFoundError):
        gs.load_template("no-such-template")


def test_validate_refuses_a_role_that_can_administer_or_moderate():
    spec = {"version": 1, "roles": [{"name": "Owners", "permissions": "0"}], "channels": []}
    for bit in (gs.ADMINISTRATOR, gs.MANAGE_GUILD, gs.BAN_MEMBERS,
                gs.KICK_MEMBERS, gs.MANAGE_ROLES):
        spec["roles"][0]["permissions"] = str(bit)
        problems = gs.validate_spec(spec)
        assert problems and "Owners" in problems[0]
    spec["roles"][0]["permissions"] = "not-a-number"
    assert gs.validate_spec(spec)


def test_validate_refuses_an_overwrite_that_grants_admin_or_hides_a_member():
    def with_overwrites(overwrites):
        return {"version": 1, "roles": [], "channels": [
            {"name": "general", "type": "text", "category": "", "overwrites": overwrites}]}

    assert gs.validate_spec(with_overwrites(
        [{"target": "@everyone", "allow": str(gs.ADMINISTRATOR), "deny": "0"}]))
    assert gs.validate_spec(with_overwrites(
        [{"target": "Founders", "allow": str(gs.MANAGE_GUILD), "deny": "0"}]))
    assert gs.validate_spec(with_overwrites(
        [{"target": "member:7", "allow": "0", "deny": str(VIEW_CHANNEL)}]))
    # Denying a ROLE is a normal layout move; denying a member is not.
    assert gs.validate_spec(with_overwrites(
        [{"target": "@everyone", "allow": "0", "deny": str(VIEW_CHANNEL)}])) == []


def test_apply_refuses_an_unsafe_spec_before_touching_discord():
    spec = gs.load_template()
    spec["roles"][0]["permissions"] = str(gs.ADMINISTRATOR)
    with pytest.raises(ValueError):
        gs.apply(Boom(), G, spec)


def test_plan_previews_every_creation_and_writes_nothing():
    f = Fake()
    spec = gs.load_template()
    would = gs.plan(f, G, spec)
    assert f.writes == []
    assert "would create role Founders" in would
    assert any("#welcome" in line for line in would)
    assert any("forum" in line and "#projects" in line for line in would)
    assert len(would) == len(spec["roles"]) + len(spec["channels"])


def test_plan_lists_only_what_is_missing():
    f = Fake()
    f.channels = [{"id": "c1", "name": "Start Here", "type": 4, "position": 0,
                   "permission_overwrites": []}]
    would = gs.plan(f, G, gs.load_template())
    assert "would create category #Start Here" not in would
    assert any("#welcome" in line for line in would)


def test_plan_refuses_the_same_unsafe_spec_apply_does():
    spec = gs.load_template()
    spec["roles"][0]["permissions"] = str(gs.BAN_MEMBERS)
    with pytest.raises(ValueError):
        gs.plan(Boom(), G, spec)


# -- a forum needs a Community server; a refusal must not end the layout ------------

def test_plan_skips_the_forum_on_a_non_community_server():
    """MUTATION: plan a forum the guild cannot have (its POST is a permanent 400)."""
    f = Fake()
    f.features = []
    would = gs.plan(f, G, gs.load_template())
    assert any("would skip forum Build/#projects" in line for line in would)
    assert not any("would create forum" in line for line in would)


def test_apply_skips_the_forum_on_a_non_community_server_and_finishes():
    """MUTATION: drop the features read and the apply dies on #projects."""
    f = Fake()
    f.features = []
    made = gs.apply(f, G, gs.load_template())
    assert any(line.startswith("skipped forum") for line in made)
    assert any("#Lounge" in line or "#general" in line for line in made)
    assert "projects" not in {c["name"] for c in f.channels}


def test_apply_continues_past_a_refused_channel_and_names_it():
    """MUTATION: let the first HTTPError abort the loop (a half-built server)."""

    class Refuse(Fake):
        def call(self, method, path, body=None):
            if (method == "POST" and path.endswith("/channels")
                    and (body or {}).get("type") == 0):   # text channels only
                self.writes.append((method, path, body))
                raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
            return super().call(method, path, body)

    f = Refuse()
    with pytest.raises(gs.ApplyError) as exc:
        gs.apply(f, G, gs.load_template())
    assert exc.value.transient is False
    assert any("#welcome" in failure for failure in exc.value.failures)
    # The loop went on: the last channel in the file was still created.
    made = [(body or {}).get("name") for method, path, body in f.writes
            if method == "POST" and path.endswith("/channels")]
    assert "Lounge" in made and "welcome" not in {c["name"] for c in f.channels}


def test_a_transient_failure_is_marked_retryable():
    """MUTATION: report a 429/5xx as permanent (or a 403 as 'try again')."""
    assert gs._is_transient(urllib.error.HTTPError("u", 429, "", {}, None))
    assert gs._is_transient(urllib.error.HTTPError("u", 503, "", {}, None))
    assert gs._is_transient(urllib.error.URLError("down"))
    assert not gs._is_transient(urllib.error.HTTPError("u", 403, "", {}, None))


def test_retry_after_reads_discords_value_and_is_bounded():
    """MUTATION: sleep Discord's unbounded retry_after and hang the interaction."""
    body = io.BytesIO(b'{"retry_after": 0.25}')
    assert gs._retry_after(urllib.error.HTTPError("u", 429, "", {}, body)) == 0.25
    body = io.BytesIO(b'{"retry_after": 99}')
    assert gs._retry_after(urllib.error.HTTPError("u", 429, "", {}, body)) \
        == gs.MAX_RETRY_SLEEP


def test_discord_call_honours_a_429_and_retries(monkeypatch):
    """MUTATION: raise on the first 429 (one stall leaves the layout unfinished)."""
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id": "ok"}'

    def fake_urlopen(req, timeout=20):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {},
                                         io.BytesIO(b'{"retry_after": 0}'))
        return Resp()

    monkeypatch.setattr(gs.urllib.request, "urlopen", fake_urlopen)
    assert gs.Discord("t").call("GET", "/x") == {"id": "ok"}
    assert len(calls) == 2
