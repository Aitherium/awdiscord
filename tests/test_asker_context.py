"""Every chat turn tells the agent WHO is asking.

Reproduces 2026-11-11: right after /link the owner asked "do you know who I am now?"
and the bot said "I have no way to tell your identity from this message". Discord
gives the bot the author on every message; the agent's context never carried it.
"""
from __future__ import annotations

from awdiscord.agent import EchoAgent
from awdiscord.dispatch import Dispatcher
from awdiscord.plugins import Plugin, PluginHost
from awdiscord.ratelimit import RateLimiter
from awdiscord.store import GuildStore


class Recorder(EchoAgent):
    def __init__(self):
        super().__init__()
        self.context = None

    def ask(self, prompt, *, persona=None, user="", context=""):
        self.context = context
        return "ok"


def _disp(*plugins):
    host = PluginHost(GuildStore(":memory:"))
    for p in plugins:
        host.add(p)
        host.set_enabled("g1", p.name, True)
    agent = Recorder()
    lim = RateLimiter(per_user=(1000, 1.0), per_guild=(1000, 1.0), global_=(1000, 1.0))
    return Dispatcher(agent, lim, None, host=host), agent


def test_mention_context_names_the_author():
    # MUTATION: drop asker_context from _agent_kwargs.
    disp, agent = _disp()
    disp.chat("178", "g1", "do you know who I am?", author_name="waizns")
    assert "<@178> (waizns)" in agent.context
    assert "never say you cannot tell who they are" in agent.context


def test_a_linked_author_gets_the_account_from_the_plugin_hook():
    # MUTATION: never call Plugin.turn_context, or pass it the model's idea of the user.
    seen = {}

    def hook(caller):
        seen.update(caller)
        return "Their Aitherium account: david - platform owner/admin."
    disp, agent = _disp(Plugin("idlink", "0.1", "test", turn_context=hook))
    disp.chat("178", "g1", "who am I?", author_name="waizns", role_names=["AI Specialist"])
    assert "david - platform owner/admin" in agent.context
    assert seen["user"] == "178" and seen["guild"] == "g1"
    assert seen["author_name"] == "waizns" and seen["role_names"] == ["AI Specialist"]


def test_a_failing_hook_never_breaks_the_turn():
    def hook(caller):
        raise RuntimeError("identity down")
    disp, agent = _disp(Plugin("idlink", "0.1", "test", turn_context=hook))
    assert disp.chat("178", "g1", "hi")["content"] == "ok"
    assert "<@178>" in agent.context
