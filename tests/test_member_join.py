"""Member joins, modals, and the members intent: generic plugin hooks, no ids."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import gateway  # noqa: E402
from awdiscord.dispatch import MESSAGE  # noqa: E402
from awdiscord.plugins import MODAL, MODAL_SUBMIT, Plugin, PluginHost  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402


def _host(*plugins):
    return PluginHost(GuildStore(), plugins=list(plugins))


def _recorder(seen, name):
    def hook(event):
        seen.append((name, event.guild, event.user))
    return hook


def test_dispatch_reaches_only_plugins_enabled_in_that_guild():
    seen = []
    on = Plugin("greeter", "0", "d", on_member_join=[_recorder(seen, "greeter")])
    off = Plugin("other", "0", "d", on_member_join=[_recorder(seen, "other")])
    host = _host(on, off)
    host.set_enabled("g1", "greeter", True)
    out = host.dispatch_member_join("g1", {"user": {"id": "7"}}, lambda u, p: None)
    assert out == {"greeter": "ok"}
    assert seen == [("greeter", "g1", "7")]
    assert host.dispatch_member_join("g2", {"user": {"id": "7"}}, lambda u, p: None) == {}


def test_a_failing_hook_does_not_stop_the_next_plugin():
    seen = []

    def boom(event):
        raise RuntimeError("closed inbox")
    host = _host(Plugin("a", "0", "d", on_member_join=[boom], default_enabled=True),
                 Plugin("b", "0", "d", on_member_join=[_recorder(seen, "b")],
                        default_enabled=True))
    out = host.dispatch_member_join("g1", {"user": {"id": "7"}}, lambda u, p: None)
    assert out["a"].startswith("RuntimeError") and out["b"] == "ok"
    assert seen == [("b", "g1", "7")]


def test_hook_gets_a_scoped_store_and_the_dm_sender():
    sent = []

    def hook(event):
        event.store.set("seen", event.user)
        event.send_dm(event.user, {"content": "hi"})
    host = _host(Plugin("p", "0", "d", on_member_join=[hook], default_enabled=True))
    host.dispatch_member_join("g1", {"user": {"id": "7"}}, lambda u, p: sent.append((u, p)))
    assert sent == [("7", {"content": "hi"})]
    assert host.store.get("g1", "p", "seen") == "7"


def _intents_env(monkeypatch, value):
    class Intents:
        @classmethod
        def none(cls):
            return SimpleNamespace(guilds=False, guild_messages=False, dm_messages=False,
                                   members=False)
    if value is None:
        monkeypatch.delenv("AWDISCORD_MEMBERS", raising=False)
    else:
        monkeypatch.setenv("AWDISCORD_MEMBERS", value)
    return gateway.build_intents(SimpleNamespace(Intents=Intents))


def test_members_intent_only_when_env_is_set(monkeypatch):
    intents, _, members = _intents_env(monkeypatch, None)
    assert intents.members is False and members is False
    intents, _, members = _intents_env(monkeypatch, "0")
    assert intents.members is False and members is False
    intents, chat, members = _intents_env(monkeypatch, "1")
    assert intents.members is True and members is True
    assert intents.guilds is True and chat is True


def _modal_plugin(seen):
    def on_button(ctx, payload):
        return ctx.modal("form", "Title", [{"id": "email", "label": "Email"}])

    def on_modal(ctx, payload, values):
        seen.append((payload, values))
        return ctx.reply("thanks", private=True)
    return Plugin("p", "0", "d", on_button=on_button, on_modal=on_modal,
                  default_enabled=True)


def test_modal_round_trip_reaches_the_plugin():
    seen = []
    host = _host(_modal_plugin(seen))
    opened = host.handle_button({"type": 3, "guild_id": "g1",
                                 "member": {"user": {"id": "7"}},
                                 "data": {"custom_id": "awp:p:open"}})
    assert opened.response["type"] == MODAL
    cid = opened.response["data"]["custom_id"]
    done = host.handle_modal({"type": MODAL_SUBMIT, "guild_id": "g1",
                              "member": {"user": {"id": "7"}},
                              "data": {"custom_id": cid, "components": [
                                  {"type": 1, "components": [
                                      {"type": 4, "custom_id": "email",
                                       "value": "a@b.co"}]}]}})
    assert done.response["type"] == MESSAGE
    assert seen == [("form", {"email": "a@b.co"})]
    assert host.handle_modal({"type": MODAL_SUBMIT, "data": {"custom_id": "x"}}) is None


def test_bot_routes_a_modal_submit_to_the_plugin_host():
    seen = []
    host = _host(_modal_plugin(seen))
    callbacks = []
    bot = gateway.Bot.__new__(gateway.Bot)
    bot.state = {"interactions": 0}
    bot.host = host
    bot.dispatcher = SimpleNamespace(handle=lambda it: (_ for _ in ()).throw(
        AssertionError("modal submit must not reach the dispatcher")))
    bot.rest = SimpleNamespace(interaction_callback=lambda i, t, r: callbacks.append(r))
    bot.handle_raw({"type": MODAL_SUBMIT, "id": "1", "token": "t", "guild_id": "g1",
                    "member": {"user": {"id": "7"}},
                    "data": {"custom_id": "awp:p:form", "components": []}})
    assert seen == [("form", {})] and callbacks[0]["type"] == MESSAGE


def test_bot_member_join_dms_through_rest():
    created = []
    host = _host(Plugin("p", "0", "d", default_enabled=True, on_member_join=[
        lambda e: e.send_dm(e.user, {"content": "welcome"})]))
    bot = gateway.Bot.__new__(gateway.Bot)
    bot.host = host
    bot.rest = SimpleNamespace(dm_channel=lambda u: {"id": f"dm-{u}"},
                               create_message=lambda c, p: created.append((c, p)) or {})
    assert bot.handle_member_join("g1", {"user": {"id": "7"}}) == {"p": "ok"}
    assert created[0][0] == "dm-7" and created[0][1]["content"] == "welcome"
    assert created[0][1]["allowed_mentions"] == {"parse": []}
