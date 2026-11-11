"""A busy model queue is waited out; the member never has to ask again.

Owner, 2026-11-11: "why not just let it wait until it can do it??" after /ask answered
"That is taking longer than the bot waits right now - the model queue is busy. Ask
again and I will pick it back up."
"""
from __future__ import annotations

import pathlib

from awdiscord.agent import AgentError, AgentTimeoutError, EchoAgent
from awdiscord.dispatch import COMMAND, DEFERRED, STILL_WORKING_TEXT, Dispatcher
from awdiscord.ratelimit import RateLimiter


class SlowThenAnswers(EchoAgent):
    """Times out (or gets "lane is full") ``fails`` times, then answers."""

    def __init__(self, fails=2, error=None):
        super().__init__()
        self.fails, self.calls = fails, 0
        self.error = error or AgentTimeoutError("the agent is taking longer than expected")

    def ask(self, prompt, *, persona=None, user="", context=""):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.error
        return "the real answer"


def _disp(agent):
    d = Dispatcher(agent, RateLimiter(per_user=(100, 60.0), per_guild=(100, 60.0),
                                      global_=(100, 60.0)))
    d.retry_pause_s = 0.01
    return d


def _ask_interaction():
    return {"type": COMMAND, "guild_id": "g1", "channel_id": "c1",
            "member": {"user": {"id": "7"}},
            "data": {"name": "ask", "options": [{"name": "question", "value": "why?"}]}}


def test_slow_ask_is_deferred_publicly_then_answered_after_a_still_working_edit():
    # MUTATION: give up on the first AgentTimeoutError (the old _timeout_text path).
    agent = SlowThenAnswers(fails=2)
    d = _disp(agent)
    res = d.handle(_ask_interaction())
    assert res.response["type"] == DEFERRED
    assert not (res.response.get("data") or {}).get("flags")      # public, not ephemeral
    edits = []
    res.progress["fn"] = lambda: edits.append(STILL_WORKING_TEXT)
    out = res.deferred()
    assert out["content"] == "the real answer"
    assert agent.calls == 3 and edits == [STILL_WORKING_TEXT]     # one visible notice


def test_a_full_lane_refusal_is_waited_out_on_a_mention():
    # MUTATION: treat "lane is full; retry after 30s" as a final failure.
    agent = SlowThenAnswers(fails=1, error=AgentError(
        "Request expired after 120s in queue: lane llamacpp_local is full (3 of 3); "
        "retry after 30s"))
    out = _disp(agent).chat("7", "g1", "hello", channel="c1")
    assert out["content"] == "the real answer"


def test_a_real_failure_is_not_retried():
    agent = SlowThenAnswers(fails=5, error=AgentError("model refused: bad request"))
    out = _disp(agent).chat("7", "g1", "hello", channel="c1")
    assert agent.calls == 1 and "could not answer" in out["content"]


def test_no_member_facing_text_tells_anyone_to_ask_again():
    # MUTATION: bring back the retry wording anywhere in the brick.
    src = pathlib.Path(__file__).resolve().parents[1] / "awdiscord"
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8").lower()
        assert "ask again and i will pick it back up" not in text, f
        assert "taking longer than the bot waits" not in text, f
