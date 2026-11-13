"""The founder seat: a VISIBLE ladder exception the server owner sets (2026-11-12).

A listed founder with the owner's ``founder`` event meets every requirement up to the
guild's ``founder_cap`` (default 4) and nothing above it; a non-owner cannot set one;
nobody else's self-grants count.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from awdiscord import ideals  # noqa: E402
from awdiscord.builtin import join, join_ladder  # noqa: E402
from awdiscord.plugins import MANAGE_GUILD, PluginError, ScopedStore  # noqa: E402
from test_join import FakeRest, G, _cmd, _host  # noqa: E402

OWNER, OTHER = "900", "901"


@pytest.fixture(autouse=True)
def _owner():
    join_ladder.GUILD_OWNER[0] = lambda host, guild: OWNER
    yield
    join_ladder.GUILD_OWNER[0] = None


def _store(host):
    return ScopedStore(host.store, G, "join")


def _founder_cmd(by, user, perms=str(MANAGE_GUILD), revoke=False):
    it = _cmd("ideal", user=by, perms=perms)
    opts = [{"name": "user", "value": user}]
    if revoke:
        opts.append({"name": "revoke", "value": True})
    it["data"]["options"] = [{"type": 1, "name": "founder", "options": opts}]
    return it


def test_founder_reaches_four_and_not_five():
    host = _host(FakeRest())
    msg = host.handle(_founder_cmd(OWNER, OWNER)).deferred()
    assert "founder seat" in msg["content"]
    store = _store(host)
    assert ideals.level(store, OWNER) == 4                    # stops at the cap
    assert not ideals.ready(store, OWNER, 5)                  # the council is untouched
    labels = [x["label"] for x in ideals.requirements(store, OWNER, 5)]
    assert any("council vote" in x for x in labels)
    card = join_ladder.progress_card(host, G, OWNER)          # visible on /progress
    fields = card["embeds"][0]["fields"]
    ideal = next(f for f in fields if f["name"] == "Ideal")
    assert "Founder seat, set by the server owner on" in ideal["value"]
    ev = next(f for f in fields if f["name"] == "Evidence")
    assert "founder: Founder seat" in ev["value"]


def test_founder_cap_is_a_guild_setting():
    host = _host(founder_cap=2)
    host.handle(_founder_cmd(OWNER, OTHER)).deferred()
    assert ideals.level(_store(host), OTHER) == 2
    assert not ideals.ready(_store(host), OTHER, 3)


def test_non_owner_cannot_set_founders_even_as_a_manager():
    host = _host()
    msg = host.handle(_founder_cmd(OTHER, OTHER)).deferred()
    assert "Only the server owner" in msg["content"]
    store = _store(host)
    assert ideals.level(store, OTHER) == 0
    assert not (store.get("config") or {}).get("founders")
    with pytest.raises(PluginError):
        join_ladder.set_founder(host, G, OTHER, OTHER)


def test_list_alone_or_event_alone_is_no_seat():
    host = _host()
    store = _store(host)
    c = join.config(store)
    c["founders"] = [OTHER]                                    # list edited by hand
    join._save_config(store, c)
    store.set(f"member:{OTHER}", {"user": OTHER, "at": 1})
    assert ideals.founder_seat(store, OTHER) is None
    ideals.log_event(store, OTHER, "founder", OTHER, "self")   # no guild_owner mark
    assert ideals.founder_seat(store, OTHER) is None
    assert ideals.advance(store, OTHER) == []


def test_review_by_a_founder_says_so():
    host = _host()
    host.handle(_founder_cmd(OWNER, OWNER)).deferred()
    store = _store(host)
    store.set("member:7", {"user": "7", "at": 1, "ideal": 2})
    it = _cmd("review", user=OWNER)
    it["data"]["options"] = [{"type": 1, "name": "approve", "options": [
        {"name": "user", "value": "7"}, {"name": "track", "value": "clean-room"}]}]
    msg = host.handle(it).deferred()
    assert "founder seat, set by the server owner" in msg["content"].lower()


def test_revoke_ends_the_seat_without_demoting():
    host = _host()
    host.handle(_founder_cmd(OWNER, OTHER)).deferred()
    host.handle(_founder_cmd(OWNER, OTHER, revoke=True)).deferred()
    store = _store(host)
    assert ideals.founder_seat(store, OTHER) is None
    assert ideals.level(store, OTHER) == 4                     # nothing is taken away
