"""Order Chair (Herald) seats and the Fifth Ideal: spec 2026-11-12 sections 2, 3, 9."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord import heralds, ideals, orders  # noqa: E402
from awdiscord.heralds import HeraldError  # noqa: E402

DAY = ideals.DAY
T0 = 1_800_000_000
OWNER = "900"


class Mem:
    """The four methods of ScopedStore the ladder uses."""

    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)

    def keys(self, prefix=""):
        return sorted(k for k in self.d if k.startswith(prefix))

    def update(self, k, fn, default=None):
        new, out = fn(self.d.get(k, default))
        self.d[k] = new
        return out


def member(s, uid, order="warden", level=1, at=T0 - 400 * DAY, **kw):
    s.set(f"member:{uid}", {"user": uid, "archetype": order, "ideal": level, "at": at,
                            "last_active": T0, **kw})


def all_events(s):
    rows = [s.get(k) for k in s.keys("oath:")]
    return [v for v in rows if isinstance(v, dict)]


@pytest.fixture
def s():
    s = Mem()
    member(s, "c", level=5)                                  # candidate, warden
    for u in ("n1", "n2"):
        member(s, u, level=3)
    for u in ("v1", "v2", "v3"):
        member(s, u, level=1)
    member(s, "k1", order="binder", level=5)
    member(s, "k2", order="seeker", level=5)
    member(s, "k3", order="weaver", level=5)
    return s


# ── vote weight ───────────────────────────────────────────────────────────────

def test_vote_weight_table(s):
    member(s, "a0", level=0)
    member(s, "new", level=5, at=T0 - 3 * DAY)
    member(s, "wr", level=3, well_regarded=True)
    member(s, "rev", level=4, under_review=True)
    assert heralds.vote_weight(s, "a0", T0) == 1.0
    assert heralds.vote_weight(s, "v1", T0) == 1.25
    assert heralds.vote_weight(s, "c", T0) == 2.0
    assert heralds.vote_weight(s, "new", T0) == 0.0          # under 7 days
    assert heralds.vote_weight(s, "rev", T0) == 0.0          # under review
    assert heralds.vote_weight(s, "wr", T0) == round(1.75 * 1.25, 4)
    assert heralds.vote_weight(s, "a0", T0, staff=True) == 2.0
    assert heralds.vote_weight(s, "nobody", T0) == 0.0


# ── the Fifth Ideal ──────────────────────────────────────────────────────────

def _council(s, cand, voters):
    for v, op in voters:
        ideals.log_event(s, cand, "council_vote", v, "https://x", now=T0, qualified=True,
                         operator=op)


def test_fifth_ideal_needs_all_four(s):
    member(s, "z", level=4)
    rows = heralds.fifth_ideal_rows(s, "z", T0)
    assert [r["label"][:2] for r in rows] == ["R1", "R2", "R3", "R4"]
    assert not any(r["met"] for r in rows[:2])


def test_fifth_ideal_months_and_mentees(s):
    member(s, "z", level=4, active_months=heralds._months_back(T0, 6)[:3])
    assert not heralds.fifth_ideal_rows(s, "z", T0)[0]["met"]          # 3 of 6
    member(s, "z", level=4, active_months=heralds._months_back(T0, 6)[:4])
    assert heralds.fifth_ideal_rows(s, "z", T0)[0]["met"]
    member(s, "m1", mentor="z", level=3)
    member(s, "m2", mentor="z", level=2)                                # not yet L3
    assert not heralds.fifth_ideal_rows(s, "z", T0)[1]["met"]
    member(s, "m2", mentor="z", level=3)
    assert heralds.fifth_ideal_rows(s, "z", T0)[1]["met"]


def test_council_self_vote_and_mentor_cap_and_spread(s):
    member(s, "z", level=4, mentor="k1")
    member(s, "k4", order="binder", level=5, mentor="z")
    _council(s, "z", [("z", False), ("k1", False), ("k4", False)])
    votes = ideals._distinct_qualified(ideals.events(s, "z", "council_vote"), "z")
    n, _ = heralds.council_count(s, "z", votes)
    assert n == 1                    # self dropped; mentor + mentee capped to one
    _council(s, "z", [("k2", False)])                      # one order only -> 0
    votes = ideals._distinct_qualified(ideals.events(s, "z", "council_vote"), "z")
    n, why = heralds.council_count(s, "z", votes)
    assert n == 2 and not why        # k1 (mentor) + k2; k4 (mentee) capped
    member(s, "k5", order="binder", level=5)
    member(s, "k6", order="binder", level=5)
    n, why = heralds.council_count(s, "z", {"k5": {}, "k6": {}, "k1": {}})
    assert n == 0 and "2 or more orders" in why


# ── founder path ──────────────────────────────────────────────────────────────

def test_only_the_guild_owner_seats_a_founder(s):
    with pytest.raises(HeraldError):
        heralds.seat_founder(s, "warden", "c", by="n1", owner_id=OWNER, now=T0)
    with pytest.raises(HeraldError):
        heralds.seat_founder(s, "warden", "c", by=OWNER, owner_id="", now=T0)


def test_founder_seat_writes_no_vote_and_says_not_elected(s):
    seat = heralds.seat_founder(s, "warden", OWNER, by=OWNER, owner_id=OWNER,
                                title="Founder", via="owner-instruction 2026-11-12 15:58",
                                now=T0)
    member(s, "j", order="binder", level=0)
    heralds.seat_founder(s, "binder", "j", by=OWNER, owner_id=OWNER,
                         title="Chief Humanity Officer", now=T0)
    assert seat["self"] is True and seat["source"] == "founder"
    assert not [e for e in all_events(s) if e["kind"] in heralds.VOTE_KINDS]
    line = heralds.seat_line(s, "j", now=T0)
    assert "Chief Humanity Officer" in line and "not elected" in line
    assert "Founder Chair, Binder" in line                 # below L5: never "Steward"
    assert "(self)" in heralds.seat_line(s, OWNER, now=T0)
    themed = heralds.seat_line(s, "j", "themed", now=T0)
    assert heralds.FOUNDER_LABEL in themed                  # label identical when themed


def test_founder_never_displaces_and_cap_and_one_per_person(s):
    heralds.seat_founder(s, "warden", "c", by=OWNER, owner_id=OWNER, now=T0)
    with pytest.raises(HeraldError, match="held"):
        heralds.seat_founder(s, "warden", "n1", by=OWNER, owner_id=OWNER, now=T0)
    with pytest.raises(HeraldError, match="one seat per person"):
        heralds.seat_founder(s, "binder", "c", by=OWNER, owner_id=OWNER, now=T0)
    s.set("config", {"founder_chairs": 1})
    with pytest.raises(HeraldError, match="founder seats"):
        heralds.seat_founder(s, "binder", "k1", by=OWNER, owner_id=OWNER, now=T0)


def test_title_is_model_screened_and_owner_only(s):
    with pytest.raises(HeraldError, match="owner"):
        heralds.set_title(s, "c", "X", by="n1", owner_id=OWNER, screen=lambda t: True)
    with pytest.raises(HeraldError, match="screened"):
        heralds.set_title(s, "c", "X", by=OWNER, owner_id=OWNER, screen=None)
    with pytest.raises(HeraldError, match="screened"):
        heralds.set_title(s, "c", "X", by=OWNER, owner_id=OWNER, screen=lambda t: 1 / 0)
    with pytest.raises(HeraldError, match="1 to 40"):
        heralds.set_title(s, "c", "x" * 41, by=OWNER, owner_id=OWNER, screen=lambda t: True)
    assert heralds.set_title(s, "c", " Chief  Humanity Officer ", by=OWNER, owner_id=OWNER,
                             screen=lambda t: True) == "Chief Humanity Officer"


# ── elections ─────────────────────────────────────────────────────────────────

def _to_council(s):
    with pytest.raises(HeraldError, match="Self"):
        heralds.nominate(s, "c", "c", T0)
    e = heralds.nominate(s, "c", "n1", T0)
    assert e["state"] == "nominated"
    e = heralds.nominate(s, "c", "n2", T0)
    assert e["state"] == "voting"
    return e


def test_full_ascension_path(s):
    e = _to_council(s)
    for v in ("v1", "v2", "v3", "n1", "n2"):
        heralds.vote(s, e["id"], v, True, T0 + DAY)
    st = heralds.public_status(heralds.election(s, e["id"]))
    assert "final" not in st and st["voters"] == 5          # sealed until close
    heralds.advance(s, T0 + 8 * DAY)
    e = heralds.election(s, e["id"])
    assert e["state"] == "council" and e["final"]["passed"]
    with pytest.raises(HeraldError, match="outside"):
        heralds.council(s, e["id"], "n1", operator=True, now=T0 + 9 * DAY)
    for k in ("k1", "k2", "k3"):
        heralds.council(s, e["id"], k, now=T0 + 9 * DAY)
    assert heralds.election(s, e["id"])["state"] == "confirm"
    with pytest.raises(HeraldError, match="owner"):
        heralds.confirm(s, e["id"], "n1", OWNER, T0 + 10 * DAY)
    seat = heralds.confirm(s, e["id"], OWNER, OWNER, T0 + 10 * DAY)
    assert seat["source"] == "elected" and heralds.chair_of(s, "c", T0 + 10 * DAY) == "warden"
    assert "Elected" in heralds.seat_line(s, "c", now=T0 + 10 * DAY)


def test_failed_vote_cannot_be_confirmed(s):
    e = _to_council(s)
    heralds.vote(s, e["id"], "v1", True, T0)                # 1.25 of ~9.25 active: no quorum
    heralds.advance(s, T0 + 8 * DAY)
    e = heralds.election(s, e["id"])
    assert e["state"] == "failed" and not e["final"]["passed"]
    with pytest.raises(HeraldError, match="passed"):
        heralds.confirm(s, e["id"], OWNER, OWNER, T0 + 9 * DAY)


def test_majority_threshold():
    assert heralds.passes(cast=10, yes=6, active=10)
    assert not heralds.passes(cast=10, yes=5.9, active=10)
    assert not heralds.passes(cast=2.9, yes=2.9, active=10)


def test_resign_and_recall_needs_petition_or_two_council(s):
    heralds.seat_founder(s, "warden", "c", by=OWNER, owner_id=OWNER, now=T0)
    assert heralds.recall_council(s, "warden", "k1", now=T0) is None
    r = heralds.recall_council(s, "warden", "k2", now=T0)
    assert r and r["kind"] == "recall" and r["state"] == "voting"
    for v in ("v1", "v2", "v3", "n1", "n2"):
        heralds.vote(s, r["id"], v, True, T0 + DAY)
    heralds.advance(s, T0 + 8 * DAY)
    assert heralds.chair(s, "warden", T0 + 8 * DAY) is None
    assert any(e["kind"] == "herald_unseated" for e in all_events(s))
    heralds.seat_founder(s, "warden", "n1", by=OWNER, owner_id=OWNER, now=T0 + 9 * DAY)
    assert heralds.resign(s, "n1", T0 + 9 * DAY) == "warden"


def test_term_retention_and_dormancy_card_only(s):
    e = _to_council(s)
    for v in ("v1", "v2", "v3", "n1", "n2"):
        heralds.vote(s, e["id"], v, True, T0)
    heralds.advance(s, T0 + 8 * DAY)
    for k in ("k1", "k2", "k3"):
        heralds.council(s, e["id"], k, now=T0 + 8 * DAY)
    seat = heralds.confirm(s, e["id"], OWNER, OWNER, T0 + 8 * DAY)
    near = seat["term_end"] - 20 * DAY
    notes = heralds.advance(s, near)
    ret = [x for x in heralds.elections(s, "warden") if x["kind"] == "retention"]
    assert len(ret) == 1
    assert notes and "nothing automatic" in notes[0]          # a card, raised once
    assert heralds.advance(s, near + 1) == []
    assert heralds.chair(s, "warden", near + 1) is not None      # never auto-removed


def test_names_neutral_by_default_themed_opt_in():
    assert heralds.seat_name("warden") == "Order Chair, Warden"
    assert heralds.seat_name("warden", "themed").startswith("Herald of the ")
    assert len(orders.KEYS) == 10                                # at most 10 chairs


# ── the Discord surface (join plugin) ─────────────────────────────────────────

def _sub(group, sub, caller, **opts):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_join import _cmd
    it = _cmd(group, user=caller)
    it["data"]["options"] = [{"type": 1, "name": sub, "options": [
        {"name": k, "value": v} for k, v in opts.items()]}]
    return it


def _text(out):
    if out.deferred is not None:
        return str(out.deferred())
    return out.response["data"]["content"]


def test_founder_slash_path_and_progress(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from awdiscord.builtin import join_heralds, join_ladder
    from awdiscord.plugins import ScopedStore
    from test_join import G, _host
    monkeypatch.setattr(join_heralds, "GUILD_OWNER", [lambda host, guild: OWNER])
    monkeypatch.setattr(join_heralds, "SCREEN", [lambda text: True])
    host = _host()
    store = ScopedStore(host.store, G, "join")
    member(store, "j", order="binder", level=0)
    refused = _text(host.handle(_sub("founder", "set", "8", user="j", order="binder")))
    assert "Only the server owner" in refused
    out = _text(host.handle(_sub("founder", "set", OWNER, user="j", order="binder",
                                 title="Chief Humanity Officer")))
    assert "Chief Humanity Officer" in out and "not elected" in out
    listed = _text(host.handle(_sub("founder", "list", "8")))
    assert "via slash" in listed
    view = _text(host.handle(_sub("herald", "order", "8", order="binder")))
    assert "Lead agent: Aither" in view and "Founder" in view
    card = join_ladder.progress_card(host, G, "j")
    seat = [f for f in card["embeds"][0]["fields"] if f["name"] == "Seat"]
    assert seat and "Chief Humanity Officer" in seat[0]["value"]
    assert not [e for e in all_events(store) if e["kind"] in heralds.VOTE_KINDS]


def test_cli_owner_instruction_path_is_labelled(s):
    from awdiscord.builtin import join_heralds
    seat = join_heralds.record_founder_cli(s, "warden", OWNER, OWNER, "Founder",
                                           "2026-11-12 15:58")
    assert seat["via"] == "owner-instruction 2026-11-12 15:58" and seat["self"]


def test_themed_text_never_quotes_the_books():
    from awdiscord import radiant
    banned = ("life before death", "journey before destination",
              "protect those who cannot protect themselves", "protect even those i hate",
              "put the law before all else", "unite instead of divide")
    texts = [radiant.FIRST_IDEAL_TEXT] + [t for pair in radiant.ORDER_IDEALS.values()
                                          for t in pair]
    texts += [str(v) for n in orders.NAMES.values() for v in n.values()]
    joined = " ".join(texts).lower()
    assert not [b for b in banned if b in joined]
