"""Regression: the owner's Digital Ground Game turn, 2026-11-16 14:02-14:23 PT.

The owner (an operator) forwarded his own donation-rewards email, said four times it was
legit, posted three screenshots as evidence, and asked Aither to plan with Rested Mind and
Patience, and then "with atlas and demi and lyra in the channel". The bot:

1. answered the screenshots with "I cannot see the image because no vision model is
   answering" -- every configured vision lane failed (4B busy, 27B parked, gemma4 off)
   while the turn model, served by the same multimodal Bonsai 2 lane, was never tried
   with the image; and a tool turn re-sent the images on every tool round under a 25 s cap;
2. treated the owner's word about his own affairs as unverified four times and asked
   what "Occupy SaaS" and "Tom" meant instead of planning;
3. said the second mentioned id "resolves to only one person": mentions reached the
   model as bare ids.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord import agent as agent_mod  # noqa: E402
from awdiscord import brain as brain_mod  # noqa: E402
from awdiscord.agent import (  # noqa: E402
    GROUND_RULES,
    AgentError,
    AgentTimeoutError,
    OpenAICompatAgent,  # noqa: E402
)
from awdiscord.dispatch import Dispatcher  # noqa: E402
from awdiscord.gateway import _raw_mentioned_people, name_mentions  # noqa: E402

OWNER = "178326506513235968"
JASON, PATTY, BOT = "1566092724996673670", "1566151665244184606", "1470273764708057120"
SHOTS = ["data:image/png;base64,AAAA"] * 3
# The owner's words, verbatim from the channel.
LEGIT = (f"<@{BOT}> good looking out but this is legit so lets make some plans wiht "
         f"<@{JASON}> mind and <@{PATTY}> to utlize this maybe we throw occupy saas into "
         "thee ring and run itb by tom to se ehow we use this.")


@pytest.fixture(autouse=True)
def _fresh_breaker():
    agent_mod._VISION_DOWN.clear()
    yield
    agent_mod._VISION_DOWN.clear()


class _Tool:
    name = "company_brain_search"
    description = "search"
    parameters = {"type": "object", "properties": {}}

    def schema(self):
        return {"type": "function", "function": {"name": self.name,
                                                 "description": self.description,
                                                 "parameters": self.parameters}}

    def run(self, args):
        return "nothing"


def _incident_agent(monkeypatch):
    """The live config at 14:20: turn model aither-orchestrator (stood in by the
    multimodal 4B lane), vision lanes 4B (busy), 27B (parked) and gemma4 (off)."""
    a = OpenAICompatAgent("http://x/v1", model="aither-orchestrator",
                          tool_model="aither-orchestrator", timeout=300,
                          vision_models=["bonsai-4b-5090", "bonsai2-27b", "gemma4-12b"])
    calls = []

    def fake(prompt, persona, user, context, tools, history, images, model, timeout=None):
        calls.append({"model": model, "images": bool(images), "tools": bool(tools),
                      "prompt": prompt})
        if images and model == "bonsai-4b-5090":
            exc = AgentError("the agent is restarting or busy right now")
            exc.status = 503
            raise exc
        if images and model in ("bonsai2-27b", "gemma4-12b"):
            raise AgentTimeoutError("the agent is taking longer than expected")
        if images:
            return "Custom Emote: a custom emote in the destiny.gg chat room, 1 year."
        return f"plan from {model}"
    monkeypatch.setattr(a, "_ask", fake)
    return a, calls


def test_the_screenshots_are_read_by_the_turn_models_bonsai_lane(monkeypatch):
    a, calls = _incident_agent(monkeypatch)
    out = a.ask("HERR IS YOUR EVIDENCE", images=SHOTS, tools=[_Tool()])
    assert agent_mod.VISION_UNREAD_NOTE not in out
    # one tool-free look per lane, the turn model last; then the tool turn, image-free
    looks = [c for c in calls if c["images"]]
    assert [c["model"] for c in looks] == ["bonsai-4b-5090", "bonsai2-27b", "gemma4-12b",
                                           "aither-orchestrator"]
    assert not any(c["tools"] for c in looks)
    final = calls[-1]
    assert final["tools"] and not final["images"]
    assert "destiny.gg" in final["prompt"]


def test_a_busy_lane_cools_down_briefly_not_for_five_minutes(monkeypatch):
    a, _ = _incident_agent(monkeypatch)
    a.ask("look", images=SHOTS, tools=[_Tool()])
    left = agent_mod._VISION_DOWN["bonsai-4b-5090"] - time.monotonic()
    assert 0 < left <= agent_mod.VISION_BUSY_COOLDOWN_S + 1


def test_hosted_servers_read_images_unless_told_not_to(monkeypatch):
    monkeypatch.delenv(brain_mod.HOSTED_VISION_ENV, raising=False)
    assert brain_mod.hosted_vision_models() is None
    monkeypatch.setenv(brain_mod.HOSTED_VISION_ENV, "none")
    assert brain_mod.hosted_vision_models() == []
    monkeypatch.setenv(brain_mod.HOSTED_VISION_ENV, "bonsai2-27b, bonsai-4b-5090")
    assert brain_mod.hosted_vision_models() == ["bonsai2-27b", "bonsai-4b-5090"]


class _Bare:
    name = "m"

    def ask(self, prompt, **kw):
        return "ok"


def test_the_operator_turn_ends_the_caveat_and_starts_the_work():
    d = Dispatcher(_Bare(), owners=[OWNER])
    d.known_servers = ["Aitherium Collective", "OccupySaaS"]
    ctx = d.asker_context("g", OWNER, "waizns | Windrunner", question=LEGIT)
    assert "OPERATOR" in ctx and "never ask them for evidence" in ctx
    assert "at most ONE short question" in ctx
    assert "OccupySaaS" in ctx
    member = d.asker_context("g", "999", "someone", question=LEGIT)
    assert "OPERATOR" not in member
    assert "Say that caution ONCE" in GROUND_RULES
    assert "never repeat the caveat" in GROUND_RULES
    assert "ask at most ONE question" in GROUND_RULES


def test_both_mentioned_people_reach_the_model_by_name():
    people = _raw_mentioned_people({"mentions": [
        {"id": JASON, "username": "jason.aitherium", "global_name": "Jason",
         "member": {"nick": "Rested Mind"}},
        {"id": PATTY, "username": "patience", "global_name": "Patience"},
        {"id": BOT, "username": "Aither"}]})
    text = name_mentions(LEGIT, people, skip=BOT)
    assert f"<@{JASON}> (Rested Mind, @jason.aitherium)" in text
    assert f"<@{PATTY}> (Patience, @patience)" in text
    assert f"<@{BOT}> (" not in text          # the bot's own mention is stripped later
    assert name_mentions("no mentions", people) == "no mentions"


TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38


class _RehandleRest:
    def __init__(self, author):
        self.author, self.paths = author, []

    def request(self, method, path, *a, **k):
        self.paths.append(path)
        if "/messages/" in path:
            return {"id": "m1", "author": {"id": self.author, "username": "w"},
                    "content": f"<@{BOT}> plan with atlas and lyra in the channel",
                    "mentions": [{"id": BOT, "username": "Aither"}],
                    "attachments": [{"id": "a", "filename": "s.png", "url": "https://x/s.png",
                                     "content_type": "image/png", "size": 9}]}
        return {"id": "c1", "guild_id": "g1", "type": 0}


def test_rehandle_answers_only_an_operators_message_and_fetches_nothing_else(monkeypatch):
    from awdiscord.gateway import Bot
    bot = Bot(TOKEN, _Bare(), owners=[OWNER])
    bot.rest = _RehandleRest("999")
    with pytest.raises(ValueError):
        bot.rehandle("111111111111111111", "222222222222222222")
    assert len(bot.rest.paths) == 1           # refused before the channel was read
    seen = []
    monkeypatch.setattr(bot, "handle_message", seen.append)
    bot.rest = _RehandleRest(OWNER)
    assert bot.rehandle("111111111111111111", "222222222222222222") == {
        "rehandling": "222222222222222222"}
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    msg = seen[0]
    assert msg["user"] == OWNER and msg["guild_id"] == "g1"
    assert msg["attachments"][0]["content_type"] == "image/png"
    with pytest.raises(ValueError):
        bot.rehandle("../x", "1")


def test_over_100_chat_commands_overflow_to_the_home_server_instead_of_crashing():
    from awdiscord.gateway import split_commands
    cmds = [{"name": f"c{i}", "type": 1} for i in range(102)]
    cmds[5]["default_member_permissions"] = "32"
    cmds += [{"name": "Ask Aither about this", "type": 3}]
    glob, over = split_commands(cmds)
    assert sum(1 for c in glob if c.get("type", 1) == 1) == 100
    assert [c["name"] for c in over] == ["c5", "c101"]     # restricted first, then the tail
    assert any(c["type"] == 3 for c in glob)
    assert split_commands(cmds[:10]) == (cmds[:10], [])
