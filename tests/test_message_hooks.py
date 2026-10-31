"""Plugin message hooks: dispatched only to active plugins, only for human non-mention
server messages, and only when some active plugin registered a hook."""
from __future__ import annotations

from awdiscord.gateway import CHAT, HOOK, IGNORE, Bot, build_intents, message_route
from awdiscord.plugins import Plugin, PluginHost
from awdiscord.store import GuildStore

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped


def _host(tmp_path, *plugins):
    return PluginHost(GuildStore(tmp_path / "g.db"), plugins=list(plugins))


def _msg(content="hello", guild="g1"):
    return {"id": "m1", "channel_id": "c1", "guild_id": guild, "user": "u1",
            "content": content}


def test_dispatch_reaches_only_active_plugins(tmp_path):
    seen = []
    on = Plugin("on", "0.1", "on", default_enabled=True,
                on_message=[lambda e: seen.append(("on", e.content, e.channel)) or True])
    off = Plugin("off", "0.1", "off", default_enabled=False,
                 on_message=[lambda e: seen.append(("off", e.content)) or True])
    host = _host(tmp_path, off, on)
    assert host.has_message_hooks("g1")
    assert host.dispatch_message(_msg("hi")) is True
    assert seen == [("on", "hi", "c1")]


def test_disabled_plugin_hook_is_not_counted(tmp_path):
    off = Plugin("off", "0.1", "off", on_message=[lambda e: True])
    host = _host(tmp_path, off)
    assert host.has_message_hooks("g1") is False
    assert host.dispatch_message(_msg()) is False
    host.set_enabled("g1", "off", True)
    assert host.has_message_hooks("g1") is True


def test_no_hook_and_dm_are_not_dispatched(tmp_path):
    host = _host(tmp_path, Plugin("plain", "0.1", "p", default_enabled=True))
    assert host.has_message_hooks("g1") is False
    assert host.has_message_hooks(None) is False
    assert host.dispatch_message(_msg(guild=None)) is False


def test_raising_hook_does_not_stop_the_next(tmp_path):
    def boom(e):
        raise RuntimeError("bug")
    got = []
    a = Plugin("a", "0.1", "a", default_enabled=True, on_message=[boom])
    b = Plugin("b", "0.1", "b", default_enabled=True,
               on_message=[lambda e: got.append(e.user) or True])
    host = _host(tmp_path, a, b)
    assert host.dispatch_message(_msg()) is True
    assert got == ["u1"]


def test_false_hook_means_not_handled(tmp_path):
    host = _host(tmp_path, Plugin("a", "0.1", "a", default_enabled=True,
                                  on_message=[lambda e: False]))
    assert host.dispatch_message(_msg()) is False


def test_route_skips_non_mention_without_hooks():
    assert message_route(own=False, author_bot=False, is_dm=False, mentioned=False,
                         hooks=False) == IGNORE
    assert message_route(own=False, author_bot=False, is_dm=False, mentioned=False,
                         hooks=True) == HOOK


def test_route_keeps_mention_and_dm_behaviour():
    for hooks in (False, True):
        assert message_route(own=False, author_bot=False, is_dm=False, mentioned=True,
                             hooks=hooks) == CHAT
        assert message_route(own=False, author_bot=False, is_dm=True, mentioned=False,
                             hooks=hooks) == CHAT


def test_route_drops_bot_and_webhook_authors():
    for mentioned in (False, True):
        assert message_route(own=False, author_bot=True, is_dm=False,
                             mentioned=mentioned, hooks=True) == IGNORE
        assert message_route(own=True, author_bot=False, is_dm=False,
                             mentioned=mentioned, hooks=True) == IGNORE


class _Agent:
    name = "a"
    personas: dict = {}

    def ask(self, *a, **k):
        return "x"


def test_bot_wires_rest_into_host_and_hands_messages_over(tmp_path):
    got = []
    p = Plugin("a", "0.1", "a", default_enabled=True,
               on_message=[lambda e: got.append(e.rest) or True])
    host = _host(tmp_path, p)
    bot = Bot(TOKEN, _Agent(), host=host)
    assert host.rest is bot.rest
    assert bot.handle_guild_message(_msg()) is True
    assert got == [bot.rest]


def test_bot_without_host_handles_nothing():
    bot = Bot(TOKEN, _Agent())
    assert bot.handle_guild_message(_msg()) is False


def test_message_content_intent_is_opt_in(monkeypatch):
    class Intents:
        @staticmethod
        def none():
            return Intents()

    class D:
        pass
    D.Intents = Intents
    monkeypatch.delenv("AWDISCORD_MESSAGE_CONTENT", raising=False)
    intents, chat, _ = build_intents(D)
    assert chat and intents.message_content is False
    monkeypatch.setenv("AWDISCORD_MESSAGE_CONTENT", "1")
    intents, _, _ = build_intents(D)
    assert intents.message_content is True
