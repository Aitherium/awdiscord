"""Guild join/remove hooks: per-server plugin events, no ids hardcoded."""
from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import gateway  # noqa: E402
from awdiscord.plugins import Plugin, PluginHost  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402


def _host(*plugins):
    return PluginHost(GuildStore(), plugins=list(plugins))


def _guild(**extra) -> dict:
    return {"id": "g1", "name": "OccupySaaS", "owner_id": "9",
            "system_channel_id": "c9", **extra}


def test_join_reaches_only_plugins_enabled_in_that_guild():
    seen = []

    def hook(event):
        seen.append((event.guild_id, event.name, event.owner_id, event.system_channel_id))

    on = Plugin("greeter", "0", "d", on_guild_join=[hook])
    off = Plugin("other", "0", "d", on_guild_join=[lambda e: seen.append(("other",))])
    host = _host(on, off)
    host.set_enabled("g1", "greeter", True)

    assert host.dispatch_guild_join(_guild()) == {"greeter": "ok"}
    assert seen == [("g1", "OccupySaaS", "9", "c9")]
    # A different server has no state for "greeter", so it is off there.
    assert host.dispatch_guild_join({"id": "g2", "name": "Other", "owner_id": "",
                                     "system_channel_id": ""}) == {}


def test_a_guild_with_no_state_falls_back_to_default_enabled():
    seen = []
    host = _host(Plugin("p", "0", "d", default_enabled=True,
                        on_guild_join=[lambda e: seen.append(e.guild_id)]))
    assert host.dispatch_guild_join(_guild()) == {"p": "ok"}
    assert seen == ["g1"]


def test_event_store_is_scoped_and_rest_is_the_host_client():
    rest = object()
    seen = []

    def hook(event):
        event.store.set("joined", event.name)
        seen.append(event.rest)

    host = _host(Plugin("p", "0", "d", default_enabled=True, on_guild_join=[hook]))
    host.rest = rest
    host.dispatch_guild_join(_guild())
    assert host.store.get("g1", "p", "joined") == "OccupySaaS"
    assert seen == [rest]


def test_a_failing_hook_does_not_stop_the_next_plugin():
    seen = []

    def boom(event):
        raise RuntimeError("no channel")

    host = _host(Plugin("a", "0", "d", default_enabled=True, on_guild_join=[boom]),
                 Plugin("b", "0", "d", default_enabled=True,
                        on_guild_join=[lambda e: seen.append(e.plugin.name)]))
    out = host.dispatch_guild_join(_guild())
    assert out["a"].startswith("RuntimeError") and out["b"] == "ok"
    assert seen == ["b"]


def test_remove_runs_only_the_remove_hooks():
    seen = []
    plugin = Plugin("p", "0", "d", default_enabled=True,
                    on_guild_join=[lambda e: seen.append("joined")],
                    on_guild_remove=[lambda e: seen.append(("removed", e.guild_id))])
    host = _host(plugin)
    assert host.dispatch_guild_remove(_guild()) == {"p": "ok"}
    assert seen == [("removed", "g1")]


def test_a_guild_with_no_id_is_ignored():
    seen = []
    host = _host(Plugin("p", "0", "d", default_enabled=True,
                        on_guild_join=[lambda e: seen.append(e)]))
    assert host.dispatch_guild_join({"name": "no id"}) == {}
    assert host.dispatch_guild_join({}) == {}
    assert host.dispatch_guild_remove({"name": "no id"}) == {}
    assert seen == []


def test_bot_hands_guild_events_to_the_host():
    seen = []
    host = _host(Plugin("p", "0", "d", default_enabled=True,
                        on_guild_join=[lambda e: seen.append(("join", e.guild_id))],
                        on_guild_remove=[lambda e: seen.append(("remove", e.guild_id))]))
    bot = gateway.Bot.__new__(gateway.Bot)
    bot.host = host
    assert bot.handle_guild_join({"id": "g1", "name": "n", "owner_id": "o",
                                  "system_channel_id": "c"}) == {"p": "ok"}
    assert bot.handle_guild_remove({"id": "g1", "name": "n", "owner_id": "o",
                                    "system_channel_id": "c"}) == {"p": "ok"}
    assert seen == [("join", "g1"), ("remove", "g1")]


def test_bot_without_a_host_is_a_no_op():
    bot = gateway.Bot.__new__(gateway.Bot)
    bot.host = None
    assert bot.handle_guild_join({"id": "g1"}) == {}
    assert bot.handle_guild_remove({"id": "g1"}) == {}
