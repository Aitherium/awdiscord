"""A mention hook runs BEFORE the model, so a brief becomes a job deterministically.

Owner/partner report 2026-11-20: a 75-95 s film brief pasted into Iris came back as
prose (or a busy-lane timeout), never a job. The orchestrator pack's ``on_mention``
now answers the mention itself; this pins the brick-side contract it depends on.
"""
from __future__ import annotations

from awdiscord.plugins import MessageEvent, Plugin, PluginHost
from awdiscord.store import GuildStore

GUILD, CHANNEL, MSG = "111111111111111111", "222222222222222222", "333333333333333333"


def _host(hook, enabled=True):
    return PluginHost(GuildStore(":memory:"),
                      plugins=[Plugin("briefs", "1", "briefs", default_enabled=enabled,
                                      on_mention=[hook])])


def test_a_mention_hook_is_offered_the_message_and_its_answer_wins():
    seen: list[MessageEvent] = []

    def hook(event):
        seen.append(event)
        return True

    host = _host(hook)
    assert host.has_mention_hooks(GUILD) is True
    assert host.dispatch_mention({"id": MSG, "channel_id": CHANNEL, "guild_id": GUILD,
                                  "user": "7", "content": "@Iris a 75-95 s film"}) is True
    assert seen and seen[0].content == "@Iris a 75-95 s film"
    assert seen[0].channel == CHANNEL and seen[0].user == "7"


def test_a_hook_that_declines_keeps_the_ordinary_chat_turn():
    host = _host(lambda event: False)
    assert host.dispatch_mention({"id": MSG, "channel_id": CHANNEL, "guild_id": GUILD,
                                  "user": "7", "content": "@Iris hello"}) is False


def test_a_raising_hook_never_costs_the_reply_and_the_next_still_runs():
    ran = []

    def boom(event):
        raise RuntimeError("bad hook")

    def second(event):
        ran.append(event.content)
        return True

    host = PluginHost(GuildStore(":memory:"),
                      plugins=[Plugin("a", "1", "a", default_enabled=True,
                                      on_mention=[boom]),
                                Plugin("b", "1", "b", default_enabled=True,
                                      on_mention=[second])])
    assert host.dispatch_mention({"id": MSG, "channel_id": CHANNEL, "guild_id": GUILD,
                                  "user": "7", "content": "brief"}) is True
    assert ran == ["brief"]


def test_a_plugin_off_in_the_server_never_sees_a_mention():
    host = _host(lambda event: True, enabled=False)
    assert host.has_mention_hooks(GUILD) is False
    assert host.dispatch_mention({"id": MSG, "channel_id": CHANNEL, "guild_id": GUILD,
                                  "user": "7", "content": "x"}) is False
