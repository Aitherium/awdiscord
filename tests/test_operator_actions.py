"""Operator actions: the guild's own brain in a partner server, and message triggers.

Two behaviours share one vocabulary (``ACTIONS``): a message context-menu command
("Apps" on a right-click) and a mapped reaction from an operator or a server
admin. Both reach the SAME guild brain, with the fleet one ``ask_fleet`` call
away -- never a default route into the owner's fleet, and never code execution.
"""
from __future__ import annotations

import pytest
from awdiscord import gateway
from awdiscord.agent import AgentTimeoutError
from awdiscord.brain import BrainResolver
from awdiscord.dispatch import (
    COMMAND,
    DEFERRED,
    EPHEMERAL,
    MESSAGE_COMMAND,
    MESSAGE_COMMANDS,
    REACTION_ACTIONS,
    Dispatcher,
    commands,
)
from awdiscord.plugins import PluginHost
from awdiscord.served import ServedGuilds
from awdiscord.store import GuildStore

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped
HOME, PARTNER = "100000000000000001", "200000000000000002"
OP, MEMBER, CHANNEL, MSG = "1", "2", "20", "30"


class GuildAgent:
    """The bot's own model (what a partner server's brain resolves to)."""

    name = "guild-model"

    def __init__(self, answer="guild answer", error=None):
        self.calls: list[dict] = []
        self.answer = answer
        self.error = error

    def ask(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if self.error is not None:
            raise self.error
        return self.answer


class Fleet:
    """The owner's fleet agent: it never runs the bot's tools."""

    runs_bot_tools = False
    name = "fleet"

    def __init__(self):
        self.asked: list[str] = []

    def ask(self, prompt, **kw):
        self.asked.append(prompt)
        return "fleet says done"


def _dispatcher(tmp_path, *, loop=True, answer="guild answer", error=None):
    store = GuildStore(tmp_path / "g.db")
    served = ServedGuilds(home=HOME, guilds=[PARTNER])
    agent, fleet = GuildAgent(answer=answer, error=error), Fleet()
    brains = BrainResolver(agent, owner_agent=fleet, owners={OP},
                           owner_tool_loop=loop, served=served, store=store)
    host = PluginHost(store, operators={OP})
    dispatcher = Dispatcher(agent, host=host, owner_agent=fleet, owners={OP},
                            owner_tool_loop=loop, served=served, brains=brains,
                            store=store)
    return dispatcher, agent, fleet


def _interaction(name, *, guild=PARTNER, user=OP, permissions="0"):
    return {"type": COMMAND, "guild_id": guild, "channel_id": CHANNEL,
            "member": {"user": {"id": user}, "permissions": permissions},
            "data": {"type": MESSAGE_COMMAND, "name": name, "target_id": MSG,
                     "resolved": {"messages": {MSG: {
                         "id": MSG, "content": "the worker keeps 503ing",
                         "author": {"id": MEMBER, "username": "jason"},
                         "attachments": [{"filename": "trace.log"}]}}}}}


# -- routing: the guild's brain, with the fleet one tool call away ----------------

def test_a_partner_operator_reaches_the_guild_brain_with_ask_fleet(tmp_path):
    dispatcher, agent, fleet = _dispatcher(tmp_path)
    brain = dispatcher.brains.resolve(PARTNER, OP)
    assert brain.mode == "owner"
    assert brain.agent is agent                      # the guild's own brain
    assert brain.reason == "partner_operator"

    out = dispatcher.chat(OP, PARTNER, "what broke?", channel=CHANNEL)
    assert out["content"] == "guild answer"
    assert fleet.asked == []                         # never the default path
    turn = agent.calls[0]
    assert turn["guild_id"] == PARTNER
    assert "ask_fleet" in {t.name for t in turn["tools"]}


def test_the_home_guild_operator_is_unchanged(tmp_path):
    # Without the tool loop the operator's home turns go to the fleet agent,
    # exactly as before: partner routing changed no home behaviour.
    dispatcher, agent, fleet = _dispatcher(tmp_path, loop=False)
    out = dispatcher.chat(OP, HOME, "status?", channel=CHANNEL)
    assert out["content"] == "fleet says done"
    assert fleet.asked == ["status?"]
    assert agent.calls == []


def test_a_partner_member_never_gets_the_fleet_tool(tmp_path):
    dispatcher, agent, fleet = _dispatcher(tmp_path)
    dispatcher.chat(MEMBER, PARTNER, "hi", channel=CHANNEL)
    tools = {t.name for t in agent.calls[0].get("tools", [])}
    assert "ask_fleet" not in tools


# -- a timeout is a sentence, never a transport error -----------------------------

def test_a_timeout_becomes_a_retry_offer_for_an_operator(tmp_path):
    dispatcher, agent, _ = _dispatcher(
        tmp_path, error=AgentTimeoutError("the agent is taking longer than expected"))
    out = dispatcher.chat(OP, PARTNER, "hello", channel=CHANNEL)
    assert "TimeoutError" not in out["content"]
    assert "busy" in out["content"] and "Ask again" in out["content"]


def test_a_timeout_on_ask_is_friendly_and_offers_no_retry_to_a_member(tmp_path):
    dispatcher, agent, _ = _dispatcher(
        tmp_path, error=AgentTimeoutError("the agent is taking longer than expected"))
    res = dispatcher.handle({
        "type": COMMAND, "guild_id": PARTNER, "channel_id": CHANNEL,
        "member": {"user": {"id": MEMBER}},
        "data": {"name": "ask", "options": [{"name": "question", "value": "why"}]}})
    out = res.deferred()
    assert "TimeoutError" not in out["content"]
    assert "Try again in a minute" in out["content"]
    assert "Ask again" not in out["content"]


# -- message context-menu commands ------------------------------------------------

def test_message_commands_are_registered_beside_the_slash_commands():
    by_name = {cmd["name"]: cmd for cmd in commands()}
    for name in MESSAGE_COMMANDS:
        assert by_name[name] == {"name": name, "type": MESSAGE_COMMAND}


def test_the_context_menu_prompt_carries_the_target_message(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    res = dispatcher.handle(_interaction("Aither: fix this"))
    assert res.response["type"] == DEFERRED
    assert res.log["action"] == "fix"
    assert res.deferred()["content"] == "guild answer"
    prompt = agent.calls[0]["prompt"]
    assert "the worker keeps 503ing" in prompt
    assert "jason" in prompt
    assert "trace.log" in prompt
    assert f"https://discord.com/channels/{PARTNER}/{CHANNEL}/{MSG}" in prompt
    assert "ask_fleet" in prompt
    assert "Never write or run code yourself" in prompt


def test_a_member_cannot_spend_the_server_on_a_message(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    res = dispatcher.handle(_interaction("Aither: investigate", user=MEMBER))
    data = res.response["data"]
    assert data["flags"] == EPHEMERAL
    assert "operator" in data["content"]
    assert agent.calls == []


def test_a_server_admin_may_act_on_a_message(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    res = dispatcher.handle(_interaction("Aither: investigate", user=MEMBER,
                                        permissions=str(1 << 5)))
    assert res.response["type"] == DEFERRED
    assert res.deferred()["content"] == "guild answer"
    assert len(agent.calls) == 1


# -- reactions ---------------------------------------------------------------------

class _Rest:
    """Records REST traffic; GET of a message returns ``message``."""

    def __init__(self, message: dict):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.message = message

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        if method == "GET" and "/messages/" in path:
            return self.message
        return {}

    def create_message(self, channel, payload):
        self.calls.append(("POST", channel, payload))
        return {"id": "reply1", "channel_id": channel}


def _bot(tmp_path, *, owners=(OP,), message=None):
    agent = GuildAgent(answer="acted on it")
    bot = gateway.Bot(TOKEN, agent, app_id="bot", owners=owners,
                      host=PluginHost(GuildStore(tmp_path / "g.db")))
    bot.rest = _Rest(message if message is not None else {
        "id": MSG, "content": "the worker keeps 503ing",
        "author": {"id": MEMBER, "username": "jason"}})
    return bot, agent


def _evt(**kw):
    evt = {"guild_id": PARTNER, "channel_id": CHANNEL, "message_id": MSG,
           "user": OP, "emoji": "\U0001f50d", "added": True, "permissions": "0"}
    evt.update(kw)
    return evt


def test_an_operator_reaction_acts_once_and_replies_to_the_message(tmp_path):
    bot, agent = _bot(tmp_path)
    out = bot.handle_reaction(_evt())
    assert out["_action"]["action"] == "investigate"
    assert len(agent.calls) == 1
    assert "the worker keeps 503ing" in agent.calls[0]["prompt"]
    posts = [c for c in bot.rest.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2]["message_reference"]["message_id"] == MSG


def test_a_member_reaction_does_nothing(tmp_path):
    bot, agent = _bot(tmp_path)
    out = bot.handle_reaction(_evt(user=MEMBER))
    assert "_action" not in out
    assert agent.calls == []
    assert [c for c in bot.rest.calls if c[0] == "POST"] == []


def test_a_duplicate_reaction_is_ignored(tmp_path):
    bot, agent = _bot(tmp_path)
    assert bot.handle_reaction(_evt())["_action"]["action"] == "investigate"
    again = bot.handle_reaction(_evt())
    assert "_action" not in again
    assert len(agent.calls) == 1
    assert len([c for c in bot.rest.calls if c[0] == "POST"]) == 1


def test_the_bots_own_reaction_is_ignored(tmp_path):
    # The owner's id IS the bot's id here: only the self-reaction check can save us.
    bot, agent = _bot(tmp_path)
    bot.state["user_id"] = OP
    assert "_action" not in bot.handle_reaction(_evt(user=OP))
    assert agent.calls == []


def test_an_unmapped_emoji_does_nothing(tmp_path):
    bot, agent = _bot(tmp_path)
    assert "_action" not in bot.handle_reaction(_evt(emoji="\U0001f44d"))
    assert agent.calls == []


def test_the_variation_selector_does_not_change_the_emoji(tmp_path):
    bot, agent = _bot(tmp_path)
    assert bot.handle_reaction(_evt(emoji="\U0001f6e0️"))["_action"]["action"] == "fix"


def test_a_server_admin_reaction_acts_by_manage_guild(tmp_path):
    bot, agent = _bot(tmp_path)
    out = bot.handle_reaction(_evt(user=MEMBER, permissions=str(1 << 5)))
    assert out["_action"]["action"] == "investigate"
    assert len(agent.calls) == 1


# -- the per-server emoji map ------------------------------------------------------

def test_the_reaction_map_is_configurable_per_guild(tmp_path):
    dispatcher, _, _ = _dispatcher(tmp_path)
    assert dispatcher.reaction_map(PARTNER) == REACTION_ACTIONS
    dispatcher.set_reaction_map(PARTNER, {"\U0001f680": "fix"})
    assert dispatcher.reaction_action(PARTNER, "\U0001f680") == "fix"
    # The defaults are still there; another server is untouched.
    assert dispatcher.reaction_action(PARTNER, "\U0001f50d") == "investigate"
    assert dispatcher.reaction_action(HOME, "\U0001f680") == ""
    with pytest.raises(ValueError):
        dispatcher.set_reaction_map(PARTNER, {"\U0001f680": "deploy"})
