"""Assessment, name sets, the five-ideal ladder and the Oath Log."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from awdiscord import ideals, orders, radiant  # noqa: E402
from awdiscord.builtin import github, join, join_ladder  # noqa: E402
from awdiscord.plugins import MANAGE_GUILD, ScopedStore  # noqa: E402
from test_join import FakeRest, G, _cmd, _host, _join, _press  # noqa: E402

ADMIN = str(MANAGE_GUILD)
DAY = ideals.DAY


def answers_for(key: str) -> list[int]:
    """Pick ``key`` where offered; elsewhere spread picks over non-neighbours."""
    k = orders.KEYS.index(key)
    near = {orders.KEYS[(k + d) % 10] for d in (-1, 0, 1)}
    used = {x: 0 for x in orders.KEYS}
    out = []
    for _, options in orders.QUESTIONS:
        keys = [o[1] for o in options]
        if key in keys:
            out.append(keys.index(key))
            continue
        far = [i for i, x in enumerate(keys) if x not in near] or list(range(len(keys)))
        i = min(far, key=lambda j: (used[keys[j]], j))
        used[keys[i]] += 1
        out.append(i)
    return out


def _assess(host, user="7", key="warden"):
    host.handle_button(_press("assess", user))
    for qi, idx in enumerate(answers_for(key)):
        host.handle_button(_press(f"aq:{qi}", user, [str(idx)]))
    out = host.handle_button(_press("afinish", user))
    assert out.deferred is not None, out.response
    return out.deferred()


def _store(host):
    return ScopedStore(host.store, G, "join")


def _member(host, user="7", key="warden"):
    _join(host, user)
    _assess(host, user, key)


# ── the assessment ────────────────────────────────────────────────────────────

def test_questions_are_balanced_and_fit_discord():
    assert 8 <= len(orders.QUESTIONS) <= 10
    counts = {k: 0 for k in orders.KEYS}
    for _, options in orders.QUESTIONS:
        assert len(options) == 4
        for _, key in options:
            counts[key] += 1
    assert set(counts.values()) == {4}
    for page in orders.PAGES:
        assert len(page) <= 4                     # 4 selects + 1 button row
    assert sorted(q for page in orders.PAGES for q in page) == list(range(len(orders.QUESTIONS)))


@pytest.mark.parametrize("key", orders.KEYS)
def test_every_archetype_is_reachable(key):
    assert orders.score(answers_for(key))[0] == key


def test_scoring_is_deterministic_and_validates():
    a = answers_for("seeker")
    assert orders.score(a) == orders.score(list(a))
    with pytest.raises(orders.AssessmentError):
        orders.score(a[:-1])
    with pytest.raises(orders.AssessmentError):
        orders.score(a[:-1] + [7])


def test_each_archetype_is_a_two_axis_pair():
    seen = set()
    for key in orders.KEYS:
        pair = orders.axes_of(key)
        assert len(set(pair)) == 2 and pair not in seen
        seen.add(pair)


# ── name sets ─────────────────────────────────────────────────────────────────

def test_professional_is_default_and_never_uses_source_names():
    host = _host()
    assert join.config(_store(host))["name_set"] == "professional"
    src = {n.lower() for n, _ in radiant.ORDERS} | {"knight radiant", "first ideal"}
    prof = orders.NAMES["professional"]
    text = " ".join([prof["academy"], prof["umbrella"], prof["partner"], *prof["level_roles"],
                     *(a.name for a in orders.ARCHETYPES),
                     *(orders.oath_line(n) for n in range(1, 6))]).lower()
    assert not any(s in text for s in src)
    assert "radiant" not in text and "journey before destination" not in text


def test_name_set_switches_per_guild():
    rest = FakeRest()
    host = _host(rest, name_set="themed")
    _member(host)
    names = rest.names_of("7")
    assert {"Windrunner", "Knight Radiant", "First Ideal"} <= set(names)
    rest2 = FakeRest()
    host2 = _host(rest2)
    _member(host2)
    names2 = rest2.names_of("7")
    assert {"Warden", "Aitherium Engineer", "Aitherium Member"} <= set(names2)
    assert not {"Windrunner", "Knight Radiant", "First Ideal"} & set(names2)
    card = next(p for c, p in rest.messages if c == "c-bot")["embeds"][0]
    assert "Order of Aitherium" in card["description"]
    assert "Journey before destination" in card["description"]


def test_set_name_set_validates():
    host = _host()
    bad = host.handle(_cmd("enlist", user="1", perms=ADMIN, action="set", key="name_set",
                           value="sanderson"))
    assert "one of" in bad.response["data"]["content"]
    host.handle(_cmd("enlist", user="1", perms=ADMIN, action="set", key="name_set",
                     value="themed"))
    assert join.config(_store(host))["name_set"] == "themed"


def test_academy_string_is_one_line_override():
    host = _host(academy="The Forge")
    reply = _join(host)
    assert "Founding Cohort, The Forge" in reply["content"]


# ── 1st ideal + umbrella at assessment ────────────────────────────────────────

def test_first_ideal_needs_form_and_assessment_and_grants_umbrella():
    rest = FakeRest()
    host = _host(rest)
    _join(host)
    assert ideals.level(_store(host), "7") == 0
    assert "Aitherium Member" not in rest.names_of("7")
    out = _assess(host)
    assert ideals.level(_store(host), "7") == 1
    assert {"Aitherium Member", "Aitherium Engineer", "Warden"} <= set(rest.names_of("7"))
    assert "agents are open" in out["content"]
    kinds = [e["kind"] for e in ideals.events(_store(host), "7")]
    assert kinds[:2] == ["joined", "assessment"] and "promoted" in kinds and "umbrella" in kinds


def test_retake_only_after_30_days_and_swaps_archetype():
    rest = FakeRest()
    host = _host(rest)
    _member(host, key="warden")
    early = host.handle_button(_press("assess", "7"))
    assert "retake" in early.response["data"]["content"]
    store = _store(host)
    r = store.get("member:7")
    r["assessment"]["at"] -= 31 * DAY
    store.set("member:7", r)
    _assess(host, key="binder")
    names = rest.names_of("7")
    assert "Binder" in names and "Warden" not in names
    assert len(store.get("member:7")["assessments"]) == 2


def test_csv_export_carries_assessment():
    host = _host()
    _member(host, key="seeker")
    csv_text = join.export_csv(_store(host))
    header, row = csv_text.splitlines()[:2]
    assert "archetype" in header and "ideal" in header
    assert "seeker" in row


# ── the 2nd ideal: only with evidence ─────────────────────────────────────────

def test_second_ideal_never_without_evidence():
    host = _host()
    _member(host)
    assert join.promote(host, G, "7") == []
    assert ideals.level(_store(host), "7") == 1


def test_unverified_github_never_counts(monkeypatch):
    host = _host(repos={"zeroauthy": "Aitherium/zeroauthy"})
    _member(host)
    calls = []
    monkeypatch.setattr(github, "graphql", lambda *a, **k: calls.append(a) or {})
    assert join_ladder.check_merged_pr(host, G, "7") == ""
    assert calls == []                       # never even asked GitHub


def test_verified_github_merged_pr_promotes(monkeypatch):
    rest = FakeRest()
    host = _host(rest, repos={"zeroauthy": "Aitherium/zeroauthy"})
    _member(host)
    out = host.handle(_cmd("verify-github"))
    token = _store(host).get("member:7")["gh_challenge"]["token"]
    assert token in out.response["data"]["content"]

    def fake(query, variables, opener=None):
        if "bio" in query:
            return {"user": {"bio": f"hi {token}"}}
        assert "author:ada-l" in variables["q"] and "is:merged" in variables["q"]
        return {"search": {"nodes": [{"url": "https://github.com/Aitherium/zeroauthy/pull/9"}]}}
    monkeypatch.setattr(github, "graphql", fake)
    msg = host.handle(_cmd("verify-github")).deferred()
    assert "verified" in msg["content"] and "pull/9" in msg["content"]
    assert ideals.level(_store(host), "7") == 2
    assert "Aitherium Contributor" in rest.names_of("7")
    ev = ideals.events(_store(host), "7", "merged_pr")[0]
    assert ev["evidence"].endswith("/pull/9")


def test_prove_deploy_needs_health_and_token():
    host = _host()
    _member(host)
    first = host.handle(_cmd("prove-deploy", url="https://me.example.com"))
    token = _store(host).get("member:7")["deploy_challenge"]["token"]
    assert token in first.response["data"]["content"]
    pages = {"https://me.example.com/health": "ok"}
    join_ladder.FETCH[0] = lambda u: pages[u] if u in pages else (_ for _ in ()).throw(
        ValueError("HTTP 404"))
    try:
        miss = host.handle(_cmd("prove-deploy", url="https://me.example.com")).deferred()
        assert "did not answer" in miss["content"] and ideals.level(_store(host), "7") == 1
        pages["https://me.example.com" + join_ladder.CHALLENGE_PATH] = "wrong"
        wrong = host.handle(_cmd("prove-deploy", url="https://me.example.com")).deferred()
        assert "does not contain" in wrong["content"] and ideals.level(_store(host), "7") == 1
        pages["https://me.example.com" + join_ladder.CHALLENGE_PATH] = token
        ok = host.handle(_cmd("prove-deploy", url="https://me.example.com")).deferred()
        assert "Deploy verified" in ok["content"] and ideals.level(_store(host), "7") == 2
    finally:
        join_ladder.FETCH[0] = None


# ── 3rd ideal: 2 qualified peers or an operator ───────────────────────────────

def _to_level(host, user, lvl):
    store = _store(host)
    r = store.get(f"member:{user}")
    r["ideal"] = lvl
    store.set(f"member:{user}", r)


def _approve(host, by, target="7", track="clean-room", perms="0"):
    it = _cmd("review", user=by, perms=perms)
    it["data"]["options"] = [{"type": 1, "name": "approve", "options": [
        {"name": "user", "value": target}, {"name": "track", "value": track}]}]
    out = host.handle(it)
    return out.deferred() if out.deferred else out.response["data"]["content"]


def test_third_ideal_needs_two_qualified_reviewers():
    rest = FakeRest()
    host = _host(rest)
    for u in ("7", "20", "21", "22"):
        _member(host, u)
    ideals.log_event(_store(host), "7", "merged_pr", "github", "https://x/pull/1",
                     qualified=True)
    join.promote(host, G, "7")
    assert ideals.level(_store(host), "7") == 2
    assert "3rd ideal" in _approve(host, "20")           # unqualified reviewer refused
    _to_level(host, "21", 3)
    _to_level(host, "22", 3)
    _approve(host, "21")
    assert ideals.level(_store(host), "7") == 2           # one is not enough
    _approve(host, "21")                                  # the same reviewer twice
    assert ideals.level(_store(host), "7") == 2
    _approve(host, "22")
    assert ideals.level(_store(host), "7") == 3
    assert "ACAE: Clean-Room Implementer" in rest.names_of("7")


def test_operator_certify_is_the_third_ideal():
    host = _host()
    _member(host)
    ideals.log_event(_store(host), "7", "deploy_verified", "7", "https://d", qualified=True)
    join.promote(host, G, "7")
    it = _cmd("certify", user="1", perms=ADMIN)
    it["data"]["options"] = [{"name": "user", "value": "7"},
                             {"name": "track", "value": "ai-ops"}]
    msg = host.handle(it).deferred()
    assert "reached ideal 3" in msg["content"] and ideals.level(_store(host), "7") == 3


# ── no self-grant ─────────────────────────────────────────────────────────────

def test_nobody_grants_themselves_anything():
    host = _host()
    _member(host)
    _to_level(host, "7", 5)
    for cmd, sub in (("review", "approve"), ("council", "vote")):
        it = _cmd(cmd, user="7", perms=ADMIN)
        it["data"]["options"] = [{"type": 1, "name": sub, "options": [
            {"name": "user", "value": "7"}, {"name": "track", "value": "ai-ops"},
            {"name": "evidence", "value": "https://x"}]}]
        assert "themselves" in host.handle(it).response["data"]["content"]
    it = _cmd("nominate", user="7")
    it["data"]["options"] = [{"name": "user", "value": "7"},
                             {"name": "milestone", "value": "https://x"}]
    assert "themselves" in host.handle(it).response["data"]["content"]
    it = _cmd("certify", user="7", perms=ADMIN)
    it["data"]["options"] = [{"name": "user", "value": "7"},
                             {"name": "track", "value": "ai-ops"}]
    assert "themselves" in host.handle(it).response["data"]["content"]
    # a self-logged event never counts
    store = _store(host)
    _to_level(host, "7", 2)
    ideals.log_event(store, "7", "review_approve", "7", "", qualified=True, operator=True,
                     track="ai-ops")
    assert not ideals.ready(store, "7", 3)


# ── 4th ideal: nomination + 3 mentees + operator ──────────────────────────────

def test_fourth_ideal_counts_mentees_and_needs_operator():
    host = _host()
    for u in ("7", "30", "31", "32", "33"):
        _member(host, u)
    store = _store(host)
    _to_level(host, "7", 3)
    for u in ("30", "31", "32"):
        r = store.get(f"member:{u}")
        r["mentor"] = "7"
        store.set(f"member:{u}", r)
    assert ideals.mentees(store, "7") == []               # mentees at the 1st ideal
    for u in ("30", "31"):
        _to_level(host, u, 2)
    nom = _cmd("nominate", user="33")
    nom["data"]["options"] = [{"name": "user", "value": "7"},
                              {"name": "milestone", "value": "https://github.com/x/releases/1"}]
    host.handle(nom).deferred()
    conf = _cmd("ideal", user="1", perms=ADMIN)
    conf["data"]["options"] = [{"type": 1, "name": "confirm", "options": [
        {"name": "user", "value": "7"}]}]
    host.handle(conf).deferred()
    assert ideals.level(store, "7") == 3                  # only 2 mentees
    _to_level(host, "32", 2)
    join.promote(host, G, "7")
    assert ideals.level(store, "7") == 4


def test_mentor_is_set_once_and_never_self():
    host = _host()
    _member(host, "7")
    _member(host, "8")
    me = _cmd("mentor", user="7", mentor="7")
    assert "other than yourself" in host.handle(me).response["data"]["content"]
    host.handle(_cmd("mentor", user="7", mentor="8"))
    again = host.handle(_cmd("mentor", user="7", mentor="8"))
    assert "already" in again.response["data"]["content"]


# ── 5th ideal: council of 3 + six months + adoption ───────────────────────────

def _vote(host, by, perms="0"):
    it = _cmd("council", user=by, perms=perms)
    it["data"]["options"] = [{"type": 1, "name": "vote", "options": [
        {"name": "user", "value": "7"}, {"name": "evidence", "value": "https://adopt"}]}]
    out = host.handle(it)
    return out.deferred() if out.deferred else out.response["data"]["content"]


def test_council_quorum_and_tenure(monkeypatch):
    host = _host()
    for u in ("7", "40", "41"):
        _member(host, u)
    store = _store(host)
    _to_level(host, "7", 4)
    assert "council" in _vote(host, "40")                  # not a steward: refused
    _to_level(host, "40", 5)
    _vote(host, "40")
    assert "already voted" in _vote(host, "40")
    _vote(host, "1", perms=ADMIN)
    assert ideals.level(store, "7") == 4                   # 2 of 3
    _to_level(host, "41", 5)
    _vote(host, "41")
    assert ideals.level(store, "7") == 4                   # 3 votes, but too new
    r = store.get("member:7")
    r["at"] -= 200 * DAY
    store.set("member:7", r)
    assert join.promote(host, G, "7") == [5]


# ── dormant, progress, digest ─────────────────────────────────────────────────

def test_dormant_flag_never_demotes():
    host = _host()
    _member(host)
    store = _store(host)
    now = store.get("member:7")["at"] + 91 * DAY
    assert ideals.dormant(store, "7", now)
    assert ideals.level(store, "7") == 1
    card = join_ladder.progress_card(host, G, "7", now)["embeds"][0]
    assert any("dormant" in f["value"] for f in card["fields"])
    assert not ideals.dormant(store, "7", now - 2 * DAY)


def test_progress_card_shows_next_requirements_and_bar():
    host = _host(name_set="themed")
    _member(host)
    card = join_ladder.progress_card(host, G, "7")["embeds"][0]
    names = {f["name"]: f["value"] for f in card["fields"]}
    assert names["Ideal"] == "Sworn to the First Ideal"
    nxt = next(k for k in names if k.startswith("Next: Sworn to the Second Ideal"))
    assert "0/1" in nxt and "/prove-deploy" in names[nxt]


def test_oath_log_is_append_only():
    store = ScopedStore(__import__("awdiscord.store", fromlist=["GuildStore"]).GuildStore(),
                        G, "join")
    a = ideals.log_event(store, "7", "joined", "7", now=1000)
    b = ideals.log_event(store, "7", "joined", "7", now=1000)
    assert len(ideals.events(store, "7")) == 2 and a is not b
    assert not hasattr(ideals, "delete_event") and not hasattr(ideals, "edit_event")


def test_digest_lists_who_is_close():
    host = _host(squad_channels=True)
    _member(host)
    store = _store(host)
    _to_level(host, "7", 3)
    ideals.log_event(store, "7", "confirm_4", "1", "", operator=True, qualified=True)
    ideals.log_event(store, "7", "nominate", "9", "https://r", qualified=True)
    close = join_ladder.close_to_next(store, "zeroauthy", join.config(store))
    assert close and "<@7>" in close[0]


def test_radiant_has_five_ideals_and_never_renames():
    assert [n for n, _ in radiant.IDEALS] == ["First Ideal", "Second Ideal", "Third Ideal",
                                             "Fourth Ideal", "Fifth Ideal"]
    have = [{"id": "1", "name": "First Ideal", "color": radiant.colour("#e5e7eb")},
            {"id": "2", "name": "second ideal", "color": 0}]
    actions = radiant.diff(have)
    assert {"op": "recolour", "name": "second ideal", "id": "2",
            "color": radiant.colour("#93c5fd")} in actions
    created = {a["name"] for a in actions if a["op"] == "create"}
    assert {"Fourth Ideal", "Fifth Ideal"} <= created and "First Ideal" not in created
    assert all(a["op"] in ("create", "recolour") for a in actions)
