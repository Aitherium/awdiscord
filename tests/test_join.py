"""The /enlist join flow: form -> number -> roles, scarce roles, permissions, opt-in."""
from __future__ import annotations

import csv
import io
import sys
import threading
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord.builtin import github, join  # noqa: E402
from awdiscord.plugins import MANAGE_GUILD, PluginHost, ScopedStore  # noqa: E402
from awdiscord.rest import RestError  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

G = "100000000000000001"
BOT = "900000000000000009"
ADMIN = str(MANAGE_GUILD)
MANAGE_ROLES = str(1 << 28)


class FakeRest:
    """Just enough of Discord's REST API, recording every write."""

    def __init__(self, bot_perms: str = MANAGE_ROLES, bot_position: int = 50,
                 channels: list | None = None, dm_ok: bool = True):
        self.roles = [{"id": G, "name": "@everyone", "position": 0, "permissions": "0"},
                      {"id": "botrole", "name": "Aither", "position": bot_position,
                       "permissions": bot_perms, "managed": True, "tags": {"bot_id": BOT}}]
        self.member_roles: dict[str, set] = {}
        self.channels = channels if channels is not None else [
            {"id": "c-bot", "name": "bot", "type": 0}]
        self.messages: list[tuple[str, dict]] = []
        self.uploads: list[tuple] = []
        self.dm_ok = dm_ok
        self.lock = threading.Lock()
        self._seq = 0

    def request(self, method, path, body=None, auth=True):
        with self.lock:
            parts = path.strip("/").split("/")
            if path == "/users/@me":
                return {"id": BOT, "bot": True}
            if method == "GET" and path == f"/guilds/{G}/roles":
                return [dict(r) for r in self.roles]
            if method == "GET" and path == f"/guilds/{G}/members/{BOT}":
                return {"roles": ["botrole"]}
            if method == "GET" and path == f"/guilds/{G}/channels":
                return list(self.channels)
            if method == "POST" and path == f"/guilds/{G}/roles":
                self._seq += 1
                role = {"id": f"r{self._seq}", "name": body["name"], "position": 1,
                        "permissions": body["permissions"]}
                self.roles.append(role)
                return dict(role)
            if method == "POST" and path == f"/guilds/{G}/channels":
                self._seq += 1
                ch = {"id": f"ch{self._seq}", "name": body["name"], "type": body["type"],
                      "parent_id": body.get("parent_id")}
                self.channels.append(ch)
                return dict(ch)
            if parts[:1] == ["guilds"] and "members" in parts and "roles" in parts:
                user, role = parts[3], parts[5]
                perms = int(next(r for r in self.roles if r["id"] == "botrole")["permissions"])
                if not perms & (1 << 28):
                    raise RestError(403, "Missing Permissions")
                s = self.member_roles.setdefault(user, set())
                (s.add if method == "PUT" else s.discard)(role)
                return None
            raise AssertionError(f"unexpected {method} {path}")

    def create_message(self, channel_id, payload):
        self.messages.append((channel_id, payload))
        return {"id": f"m{len(self.messages)}"}

    def dm_channel(self, user_id):
        if not self.dm_ok:
            raise RestError(403, "Cannot send messages to this user")
        return {"id": f"dm-{user_id}"}

    def upload(self, channel_id, filename, data, content_type="", content=""):
        self.uploads.append((channel_id, filename, data, content_type, content))
        return {"id": "u1"}

    def names_of(self, user):
        ids = self.member_roles.get(user, set())
        return sorted(r["name"] for r in self.roles if r["id"] in ids)


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    monkeypatch.delenv("AWDISCORD_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    join._channels_by_host.clear()


def _host(rest=None, enable_plugin=True, enable_config=True, **conf):
    host = PluginHost(GuildStore(), plugins=[join.PLUGIN])
    host.rest = rest if rest is not None else FakeRest()
    if enable_plugin:
        host.set_enabled(G, "join", True)
    store = ScopedStore(host.store, G, "join")
    if enable_config or conf:
        c = join.config(store)
        c["enabled"] = enable_config
        c.update(conf)
        join._save_config(store, c)
    return host


def _member(user, perms="0"):
    return {"user": {"id": user, "username": f"u{user}"}, "permissions": perms,
            "roles": []}


def _cmd(name, user="7", perms="0", **opts):
    return {"type": 2, "id": "1", "token": "t", "guild_id": G, "channel_id": "c-gen",
            "member": _member(user, perms),
            "data": {"name": name, "options": [{"name": k, "value": v}
                                               for k, v in opts.items()]}}


def _press(payload, user="7", values=None):
    data = {"custom_id": join.PLUGIN.button_id(payload)}
    if values is not None:
        data["values"] = values
    return {"type": 3, "id": "2", "token": "t", "guild_id": G, "member": _member(user),
            "data": data}


def _submit(user="7", handle="ada", github_name="ada-l", build="a parser"):
    comps = [{"type": 1, "components": [{"type": 4, "custom_id": k, "value": v}]}
             for k, v in (("handle", handle), ("github", github_name), ("build", build))]
    return {"type": 5, "id": "3", "token": "t", "guild_id": G, "member": _member(user),
            "data": {"custom_id": join.PLUGIN.button_id("form"), "components": comps}}


def _fill(host, user="7", squads=("zeroauthy",), track="clean-room"):
    host.handle_button(_press("sel:squads", user, list(squads)))
    host.handle_button(_press("sel:track", user, [track]))
    host.handle_button(_press("sel:skills", user, ["python", "rust"]))
    host.handle_button(_press("sel:availability", user, ["evenings"]))
    if not (ScopedStore(host.store, G, "join").get(f"draft:{user}") or {}).get("coc"):
        host.handle_button(_press("coc", user))


def _join(host, user="7", **kw):
    _fill(host, user, **{k: v for k, v in kw.items() if k in ("squads", "track")})
    result = host.handle_modal(_submit(user))
    assert result.deferred is not None
    return result.deferred()


WARDEN = [0, 2, 2, 0, 0, 0, 0, 2, 0, 0]          # four warden answers


def _assess(host, user="7", answers=WARDEN):
    """Answer every question through the real pickers, then press Finish."""
    host.handle_button(_press("assess", user))
    for qi, idx in enumerate(answers):
        host.handle_button(_press(f"aq:{qi}", user, [str(idx)]))
    out = host.handle_button(_press("afinish", user))
    assert out.deferred is not None, out.response
    return out.deferred()


# ── opt-in ────────────────────────────────────────────────────────────────────

def test_off_by_default_everywhere():
    host = PluginHost(GuildStore(), plugins=[join.PLUGIN])
    assert join.PLUGIN.default_enabled is False
    assert join.DEFAULTS["enabled"] is False
    assert host.enabled(G, "join") is False
    # plugin on, config untouched: still closed
    host.set_enabled(G, "join", True)
    host.rest = FakeRest()
    out = host.handle(_cmd("enlist"))
    assert "not open" in out.response["data"]["content"]
    assert not join.is_open(host, G)


def test_plugin_off_refuses_even_with_config_enabled():
    host = _host(enable_plugin=False)
    out = host.handle(_cmd("enlist"))
    assert "off in this server" in out.response["data"]["content"]


# ── modal -> roles ────────────────────────────────────────────────────────────

def test_form_is_two_steps_with_pickers_then_modal():
    host = _host()
    out = host.handle(_cmd("enlist"))
    data = out.response["data"]
    assert data["flags"] == 64
    kinds = [row["components"][0]["type"] for row in data["components"]]
    assert kinds == [3, 3, 3, 3, 2]                      # four selects + CoC/Continue
    assert len(data["components"][4]["components"]) == 2
    early = host.handle_button(_press("next"))
    assert "Pick your squads and track" in early.response["data"]["content"]
    _fill(host)
    modal = host.handle_button(_press("next"))
    assert modal.response["type"] == 9
    assert len(modal.response["data"]["components"]) <= 5


def test_submission_assigns_founding_track_and_squad_roles():
    rest = FakeRest()
    host = _host(rest)
    reply = _join(host, squads=("zeroauthy", "website"))
    assert "#001" in reply["content"] and "Founding Cohort" in reply["content"]
    assert rest.names_of("7") == ["Clean-Room Implementer", "Founding Cohort",
                                  "Squad: Website", "Squad: ZeroAuthy"]
    record = ScopedStore(host.store, G, "join").get("member:7")
    assert record["number"] == 1 and record["founding"] is True
    assert record["github"] == "ada-l" and record["squads"] == ["zeroauthy", "website"]
    assert record["role_errors"] == []
    # created roles carry no permissions
    assert all(r["permissions"] == "0" for r in rest.roles if r["id"].startswith("r"))
    # no card until the assessment; then the welcome card in the bot channel + DM
    assert rest.messages == []
    assert reply["components"][0]["components"][0]["label"] == "Take the assessment"
    done = _assess(host)
    assert "First Oath" not in done["content"]
    where = [c for c, _ in rest.messages]
    assert where == ["c-bot", "dm-7"]
    card = rest.messages[1][1]["embeds"][0]            # the DM carries the full card
    assert "Founding Cohort, Aitherium Academy: Agentic Engineering, #001" in card["description"]
    assert any(f["name"] == "Archetype" and "Warden" in f["value"] for f in card["fields"])
    assert any(f["name"] == "Next step" and "/gh untracked" in f["value"]
               for f in card["fields"])


def test_resubmitting_keeps_the_number():
    host = _host()
    _join(host, "7")
    _join(host, "8")
    again = _join(host, "7", track="ai-ops")
    assert "#001" in again["content"]
    assert ScopedStore(host.store, G, "join").get("member:7")["track"] == "ai-ops"


def test_bad_input_is_refused_and_not_stored():
    host = _host()
    _fill(host)
    out = host.handle_modal(_submit(handle="x", github_name="not a user!"))
    text = out.response["data"]["content"]
    assert "handle must be" in text and "GitHub" in text
    assert ScopedStore(host.store, G, "join").get("member:7") is None


def test_no_bot_channel_means_dm_only():
    rest = FakeRest(channels=[{"id": "c-gen", "name": "general", "type": 0}])
    host = _host(rest)
    _join(host)
    _assess(host)
    assert [c for c, _ in rest.messages] == ["dm-7"]


# ── the founding cohort cap, atomic under concurrency ─────────────────────────

def test_cohort_cap_and_numbering_are_atomic_under_concurrent_joins(tmp_path):
    store = GuildStore(tmp_path / "s.sqlite")
    scoped = ScopedStore(store, G, "join")
    results: dict[str, tuple] = {}
    barrier = threading.Barrier(30)

    def worker(start):
        barrier.wait()
        for i in range(start, 150, 30):
            results[str(i)] = join.allocate_number(scoped, str(i), 100)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    numbers = sorted(n for n, _ in results.values())
    assert numbers == list(range(1, 151))                # unique, gap-free
    assert sum(1 for _, f in results.values() if f) == 100
    assert all(f == (n <= 100) for n, f in results.values())
    # idempotent per user
    assert join.allocate_number(scoped, "5", 100) == results["5"]


def test_cohort_cap_ends_founding_role_but_not_numbers():
    rest = FakeRest()
    host = _host(rest, cohort_cap=1)
    _join(host, "7")
    second = _join(host, "8")
    assert "#002" in second["content"] and "Founding" not in second["content"]
    assert "Founding Cohort" not in rest.names_of("8")


# ── never auto-assigning certified / lead ─────────────────────────────────────

def test_join_never_assigns_certified_or_lead_even_when_misconfigured():
    rest = FakeRest()
    # A track role named exactly like the lead / certified roles must be dropped.
    host = _host(rest, role_track="ACAE: {track}")
    _join(host)
    names = rest.names_of("7")
    assert "ACAE: Clean-Room Implementer" not in names
    host2 = _host(FakeRest(), role_squad="Squad Lead")
    _join(host2)
    assert "Squad Lead" not in host2.rest.names_of("7")
    conf = join.config(ScopedStore(host.store, G, "join"))
    record = {"founding": True, "track": "clean-room", "squads": ["zeroauthy"]}
    for name, _ in join.join_role_names(conf, record):
        assert name.casefold() not in join._protected(conf)


def test_operator_granted_role_id_is_refused_by_join_path():
    rest = FakeRest()
    host = _host(rest)
    it = _cmd("certify", user="1", perms=ADMIN)
    it["data"]["options"] = [{"name": "user", "value": "7"},
                             {"name": "track", "value": "clean-room"}]
    msg = host.handle(it).deferred()
    assert "Gave" in msg["content"]
    store = ScopedStore(host.store, G, "join")
    protected = set(store.get("protected_role_ids"))
    assert protected
    # Someone renames the certified role in Discord to the TRACK role's name: the
    # name check no longer sees it, the recorded id still blocks it.
    certified = next(r for r in rest.roles if r["id"] in protected)
    certified["name"] = "Clean-Room Implementer"
    conf = join.config(store)
    record = {"founding": False, "track": "clean-room", "squads": []}
    given, errors = join.assign_join_roles(host, G, "8", conf, record, protected)
    assert given == [] and "operator-granted" in errors[0]
    assert rest.names_of("8") == []


def test_certify_and_lead_are_admin_only_and_audited():
    rest = FakeRest()
    host = _host(rest)
    denied = _cmd("lead", user="5")
    denied["data"]["options"] = [{"name": "user", "value": "7"},
                                 {"name": "squad", "value": "website"}]
    assert "Only server managers" in host.handle(denied).response["data"]["content"]
    assert rest.names_of("7") == []
    ok = _cmd("lead", user="1", perms=ADMIN)
    ok["data"]["options"] = denied["data"]["options"]
    assert "Squad Lead" in host.handle(ok).deferred()["content"]
    assert rest.names_of("7") == ["Squad Lead"]
    entries = join.audit_log(ScopedStore(host.store, G, "join"))
    assert entries[-1]["action"] == "grant_lead" and entries[-1]["by"] == "1"
    assert entries[-1]["target"] == "7"
    revoke = _cmd("lead", user="1", perms=ADMIN)
    revoke["data"]["options"] = ok["data"]["options"] + [{"name": "revoke", "value": True}]
    host.handle(revoke).deferred()
    assert rest.names_of("7") == []


def test_member_cannot_run_operator_actions():
    host = _host()
    for action in ("export", "status", "post", "setup", "enable", "set"):
        out = host.handle(_cmd("enlist", action=action))
        assert "Only server managers" in out.response["data"]["content"], action


# ── missing permissions are reported, never silent ────────────────────────────

def test_missing_manage_roles_is_reported_and_join_still_numbers():
    rest = FakeRest(bot_perms="0")
    host = _host(rest)
    reply = _join(host)
    assert "#001" in reply["content"] and "could not be given" in reply["content"]
    record = ScopedStore(host.store, G, "join").get("member:7")
    assert record["role_errors"] and "Manage Roles" in record["role_errors"][0]
    gaps = join.permission_report(host, G)
    assert any("Manage Roles" in g for g in gaps)
    status = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="status")).deferred()
    assert "Manage Roles" in status["content"]


def test_role_above_bot_is_reported():
    rest = FakeRest(bot_position=5)
    rest.roles.append({"id": "hi", "name": "Founding Cohort", "position": 9,
                       "permissions": "0"})
    host = _host(rest)
    gaps = join.permission_report(host, G)
    assert any("Founding Cohort" in g and "above" in g for g in gaps)
    _join(host)
    assert "Founding Cohort" not in rest.names_of("7")


def test_privileged_existing_role_is_never_reused():
    rest = FakeRest()
    rest.roles.append({"id": "bad", "name": "Squad: ZeroAuthy", "position": 2,
                       "permissions": str(1 << 3)})
    host = _host(rest)
    reply = _join(host)
    assert "Squad: ZeroAuthy" not in rest.names_of("7")
    assert "could not be given" in reply["content"]


def test_no_rest_is_a_gap_not_a_crash():
    host = _host()
    host.rest = None
    assert join.permission_report(host, G) == ["this bot cannot reach Discord right now"]


# ── operator tools ────────────────────────────────────────────────────────────

def test_export_csv_goes_to_operator_dm_and_neutralises_formulas():
    rest = FakeRest()
    host = _host(rest)
    _fill(host)
    host.handle_modal(_submit(build="=HYPERLINK(evil)")).deferred()
    out = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="export")).deferred()
    assert "1 row" in out["content"]
    channel, name, data, ctype, _ = rest.uploads[0]
    assert channel == "dm-1" and name.endswith(".csv") and ctype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(data.decode())))
    assert rows[0]["number"] == "1" and rows[0]["build"].startswith("'=")


def test_post_card_only_by_operator_and_button_opens_form():
    rest = FakeRest()
    host = _host(rest)
    out = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="post", channel="c-join"))
    assert "posted" in out.response["data"]["content"]
    channel, card = rest.messages[0]
    assert channel == "c-join"
    cid = card["components"][0]["components"][0]["custom_id"]
    press = host.handle_button({"type": 3, "guild_id": G, "member": _member("9"),
                                "data": {"custom_id": cid}})
    assert press.response["data"]["flags"] == 64 and press.response["data"]["components"]


def _lead_everywhere(host):
    """A lead recorded for every squad (what /lead writes)."""
    store = ScopedStore(host.store, G, "join")
    store.set("member:500", {"user": "500", "number": 0,
                             "leads": list(join.config(store)["squads"])})


def test_setup_creates_roles_and_squad_channels():
    rest = FakeRest(bot_perms=str((1 << 28) | (1 << 4)))
    host = _host(rest, squad_channels=True)
    out = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="setup")).deferred()
    assert "Squad Lead" in out["content"] and "no Squad Lead yet" in out["content"]
    assert {c["name"] for c in rest.channels} & {"zeroauthy", "website"} == set()
    _lead_everywhere(host)
    host.handle(_cmd("enlist", user="1", perms=ADMIN, action="setup")).deferred()
    names = {c["name"] for c in rest.channels}
    assert {"Squads", "zeroauthy", "website", "new-idea"} <= names
    store = ScopedStore(host.store, G, "join")
    assert store.get("channel:zeroauthy")
    # the welcome card now points at the squad channel
    _join(host)
    _assess(host)
    card = next(p for c, p in rest.messages if c == "dm-7")["embeds"][0]
    step = next(f["value"] for f in card["fields"] if f["name"] == "Next step")
    assert f"<#{store.get('channel:zeroauthy')}>" in step
    assert not join.guildspec.validate_spec(join.layout(join.config(store)))


def test_digest_posts_leaderboard_only_where_configured(monkeypatch):
    rest = FakeRest(bot_perms=str((1 << 28) | (1 << 4)))
    host = _host(rest, squad_channels=True, repos={"zeroauthy": "Aitherium/zeroauthy"})
    _lead_everywhere(host)
    host.handle(_cmd("enlist", user="1", perms=ADMIN, action="setup")).deferred()
    assert not join.digest_due(host, G, 1e10)            # digest off by default

    def fake_graphql(query, variables, opener=None):
        assert "is:merged" in variables["q"]
        return {"search": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"author": {"login": "ada"}}, {"author": {"login": "ada"}},
            {"author": {"login": "bob"}}, {"author": {"login": "dependabot[bot]"}}]}}
    monkeypatch.setattr(github, "graphql", fake_graphql)
    before = len(rest.messages)
    summary = join.post_digest(host, G, now=2e9)
    assert "ZeroAuthy" in summary and "no repo" in summary
    posted = rest.messages[before:]
    assert len(posted) == 1
    desc = posted[0][1]["embeds"][0]["description"]
    assert "**ada** - 2" in desc and "dependabot" not in desc


def test_set_validates_and_audits():
    host = _host()
    bad = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="set", key="repo.zeroauthy",
                           value="not a repo"))
    assert "owner/name" in bad.response["data"]["content"]
    ok = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="set", key="cohort_cap",
                          value="25"))
    assert "cohort_cap" in ok.response["data"]["content"]
    store = ScopedStore(host.store, G, "join")
    assert join.config(store)["cohort_cap"] == 25
    assert join.audit_log(store)[-1]["action"] == "set"


def test_store_update_rolls_back_on_error():
    store = GuildStore()
    store.set(G, "join", "k", 1)

    def boom(cur):
        raise ValueError("no")
    with pytest.raises(ValueError):
        store.update(G, "join", "k", boom)
    assert store.get(G, "join", "k") == 1
    assert store.update(G, "join", "k", lambda c: (c + 1, c)) == 1
    assert store.get(G, "join", "k") == 2


# ── the occupyxaitherium /join contract ──────────────────────────────────────

def test_contract_dedupe_and_idempotent_resubmit():
    rest = FakeRest()
    host = _host(rest)
    _join(host)
    _assess(host)
    before = (len(rest.messages), sorted(rest.names_of("7")))
    _join(host)                                   # same answers again
    after = (len(rest.messages), sorted(rest.names_of("7")))
    assert before == after                        # no new posts, no new roles
    store = ScopedStore(host.store, G, "join")
    assert len(store.keys("member:")) == 1 and store.get("member:7")["number"] == 1


def test_contract_public_card_has_no_answers_and_dm_once():
    rest = FakeRest()
    host = _host(rest)
    _fill(host)
    host.handle_modal(_submit(build="secret plan")).deferred()
    _assess(host)
    public = [p for c, p in rest.messages if c == "c-bot"]
    assert len(public) == 1
    blob = str(public[0])
    for answer in ("secret plan", "ZeroAuthy", "Clean-Room", "ada-l", "Python"):
        assert answer not in blob, answer
    assert len([1 for c, _ in rest.messages if c == "dm-7"]) == 1


def test_contract_code_of_conduct_is_required():
    host = _host()
    host.handle_button(_press("sel:squads", "7", ["website"]))
    host.handle_button(_press("sel:track", "7", ["ai-ops"]))
    out = host.handle_button(_press("next"))
    assert "code of conduct" in out.response["data"]["content"]
    assert "code of conduct" in host.handle_modal(_submit()).response["data"]["content"]


def test_contract_project_button_preselects_squad():
    host = _host()
    out = host.handle_button(_press("project:photocraft", "9"))
    menu = out.response["data"]["components"][1]["components"][0]
    assert [o["value"] for o in menu["options"] if o["default"]] == ["photocraft"]
    odd = host.handle_button(_press("project:no-such", "9"))
    assert "not a squad here" in odd.response["data"]["content"]
    bad = host.handle_button(_press("project:../x", "9"))
    assert "not a squad here" in bad.response["data"]["content"]


def test_contract_join_log_is_one_line_without_answers():
    rest = FakeRest()
    host = _host(rest, join_log="c-log")
    _fill(host)
    host.handle_modal(_submit(build="secret plan")).deferred()
    _join(host)                                   # a resubmit logs nothing new
    logs = [p for c, p in rest.messages if c == "c-log"]
    assert len(logs) == 1
    assert "#001" in logs[0]["content"] and "secret plan" not in logs[0]["content"]


def test_contract_number_failure_still_joins(monkeypatch):
    rest = FakeRest()
    host = _host(rest)

    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(join, "allocate_number", boom)
    reply = _join(host)
    assert "pending" in reply["content"]
    assert "Squad: ZeroAuthy" in rest.names_of("7")


def test_contract_never_removes_a_role_it_did_not_grant():
    rest = FakeRest()
    host = _host(rest)
    _join(host)
    _assess(host)                                  # warden, granted by us
    store = ScopedStore(host.store, G, "join")
    r = store.get("member:7")
    r["granted_roles"] = []                        # pretend a human gave it
    r["assessment"]["at"] -= 31 * 86400
    store.set("member:7", r)
    from test_join_ladder import _assess as assess_as
    assess_as(host, "7", "binder")
    assert {"Warden", "Binder"} <= set(rest.names_of("7"))
