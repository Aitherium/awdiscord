"""Access tiers: one grant module, and a gated plugin is not offered without it.

Owner decision 2026-11-18: launch FREE FIRST as approve-on-request; Stripe and
Discord App Subscriptions will call the same grant()/revoke() later.

Each test names the MUTATION that must make it fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import access  # noqa: E402
from awdiscord.builtin import shadowbroker  # noqa: E402
from awdiscord.plugins import MANAGE_GUILD, PluginError, PluginHost  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

OCCUPY = "1561399233846972457"      # real partner server (config/community/network.yaml)
HOME = "1470279244113510466"       # real Aitherium home / Collective


@pytest.fixture(autouse=True)
def _no_user_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AWDISCORD_PLUGIN_DIRS", raising=False)
    monkeypatch.delenv(access.SERVED_ENV, raising=False)
    monkeypatch.delenv(access.HOME_ENV, raising=False)
    monkeypatch.delenv(access.HOME_ENV_ALT, raising=False)


def _content(result):
    return result.response["data"]["content"]


# -- grant / revoke / has ---------------------------------------------------------------

def test_grant_revoke_round_trip():
    store = GuildStore(":memory:")
    assert access.tiers_for(store, "g1") == set()
    access.grant(store, "g1", "intel", "approval", by="77")
    assert access.has(store, "g1", "intel")
    assert access.tiers_for(store, "g1") == {"intel"}
    assert access.describe(store, "g1", "intel")["by"] == "77"
    # Another server never sees it.
    assert not access.has(store, "g2", "intel")
    assert access.revoke(store, "g1", "intel") is True
    assert not access.has(store, "g1", "intel")
    assert access.revoke(store, "g1", "intel") is False


def test_an_expired_grant_is_not_held():
    store = GuildStore(":memory:")
    access.grant(store, "g1", "intel", "stripe", expires=1)      # 1970: long expired
    assert not access.has(store, "g1", "intel")
    assert access.tiers_for(store, "g1") == set()


def test_an_unknown_source_or_empty_guild_is_refused():
    store = GuildStore(":memory:")
    with pytest.raises(ValueError):
        access.grant(store, "g1", "intel", "made-up")
    with pytest.raises(ValueError):
        access.grant(store, "", "intel", "approval")


def test_a_stronger_source_is_never_downgraded():
    store = GuildStore(":memory:")
    access.grant(store, "g1", "intel", "approval", by="staff")
    access.grant(store, "g1", "intel", "grandfathered", by="sweep")
    assert access.describe(store, "g1", "intel")["source"] == "approval"
    access.grant(store, "g1", "intel", "stripe", by="stripe-webhook")
    assert access.describe(store, "g1", "intel")["source"] == "stripe"


# -- grandfathering ---------------------------------------------------------------------

def test_grandfathering_is_idempotent_and_covers_both_real_guild_ids(monkeypatch):
    store = GuildStore(":memory:")
    # Occupy SaaS already runs ShadowBroker alerts; the home guild is served.
    store.set(OCCUPY, "shadowbroker", "alerts", {"channel": "c1"})
    monkeypatch.setenv(access.SERVED_ENV, HOME)

    access.grandfather(store)
    for guild in (OCCUPY, HOME):
        assert access.has(store, guild, "intel")
        assert access.describe(store, guild, "intel")["source"] == "grandfathered"
    first = access.describe(store, HOME, "intel")["at"]

    access.grandfather(store)                    # idempotent: same record, one grant
    assert access.describe(store, HOME, "intel")["at"] == first
    assert access.tiers_for(store, HOME) == {"intel"}


def test_grandfathering_never_downgrades_an_approval(monkeypatch):
    store = GuildStore(":memory:")
    monkeypatch.setenv(access.SERVED_ENV, HOME)
    access.grant(store, HOME, "intel", "approval", by="77")
    access.grandfather(store)
    assert access.describe(store, HOME, "intel")["source"] == "approval"


def test_a_stranger_guild_is_not_grandfathered(monkeypatch):
    store = GuildStore(":memory:")
    monkeypatch.setenv(access.SERVED_ENV, HOME)
    store.set("999999999999999999", "shadowbroker", "seen", ["a"])
    access.grandfather(store)
    assert not access.has(store, "999999999999999999", "intel")


def test_build_host_grandfathers_on_load(monkeypatch):
    from awdiscord.plugins import build_host

    monkeypatch.setenv(access.SERVED_ENV, HOME)
    store = GuildStore(":memory:")
    build_host(store)
    assert access.has(store, HOME, "intel")


# -- a gated plugin is not offered without the grant ------------------------------------

def _tier_host(store=None):
    return PluginHost(store or GuildStore(":memory:"), plugins=[shadowbroker.PLUGIN])


def _shadowbroker_command(perms=str(MANAGE_GUILD)):
    return {"type": 2, "id": "1", "token": "t", "guild_id": "g1",
            "member": {"user": {"id": "7"}, "permissions": perms},
            "data": {"name": "shadowbroker", "options": [
                {"type": 1, "name": "alerts",
                 "options": [{"name": "channel", "value": "c1"}]}]}}


def test_tools_are_hidden_without_the_grant_and_offered_with_it():
    host = _tier_host()
    assert host.tools_for("g1") == []
    assert not host.enabled("g1", "shadowbroker")

    access.grant(host.store, "g1", "intel", "approval", by="77")
    assert host.enabled("g1", "shadowbroker")               # default_enabled kept
    names = {t.name for t in host.tools_for("g1")}
    assert names == {"shadowbroker_news", "shadowbroker_telemetry", "shadowbroker_status"}


def test_a_command_refuses_with_the_one_line_request_hint():
    host = _tier_host()
    out = _content(host.handle(_shadowbroker_command()))
    assert "needs the intel access" in out
    assert "/aitherium access request" in out


def test_enable_is_refused_without_the_grant_then_allowed():
    host = _tier_host()
    with pytest.raises(PluginError) as err:
        host.set_enabled("g1", "shadowbroker", True)
    assert "needs the intel access" in str(err.value)
    access.grant(host.store, "g1", "intel", "operator", by="op")
    host.set_enabled("g1", "shadowbroker", True)
    assert host.enabled("g1", "shadowbroker")


def test_a_granted_server_that_turned_it_off_stays_off():
    host = _tier_host()
    access.grant(host.store, "g1", "intel", "approval", by="77")
    host.set_enabled("g1", "shadowbroker", False)
    assert not host.enabled("g1", "shadowbroker")


# -- buttons and forms of a gated plugin answer with the request hint too ---------------
def _gated_buttons_host():
    from awdiscord.plugins import Plugin
    gated = Plugin(name="gated", version="1", description="a gated plugin with a button",
                   default_enabled=True, requires_tier="intel",
                   on_button=lambda ctx, payload: "clicked",
                   on_modal=lambda ctx, payload, values: "submitted")
    return PluginHost(GuildStore(":memory:"), plugins=[gated])


def _component(kind):
    return {"type": kind, "id": "1", "token": "t", "guild_id": "g1",
            "member": {"user": {"id": "7"}, "permissions": str(MANAGE_GUILD)},
            "data": {"custom_id": "awp:gated:x", "components": []}}


def test_a_gated_button_answers_with_the_request_hint_not_plugin_off():
    # MUTATION: check enabled() before the tier and return "plugin is off" -> fails.
    host = _gated_buttons_host()
    out = _content(host.handle_button(_component(3)))
    assert "needs the intel access" in out and "is off" not in out
    access.grant(host.store, "g1", "intel", "approval", by="77")
    assert host.handle_button(_component(3)) == "clicked"


def test_a_gated_form_answers_with_the_request_hint_not_plugin_off():
    host = _gated_buttons_host()
    out = _content(host.handle_modal(_component(5)))
    assert "needs the intel access" in out and "is off" not in out
