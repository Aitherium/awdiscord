"""A reply the model stopped at max_tokens is continued, then marked -- never cut silently.

Discord, 2026-11-08: two long answers went out ending "**wait" and "(fredis". The agent
sent max_tokens=900 and ignored finish_reason "length".
"""
from __future__ import annotations

import json

from awdiscord import agent as ag


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _script(monkeypatch, replies):
    """Each completion pops (content, finish_reason); returns the sent payloads."""
    sent = []

    def fake(req, timeout=None, context=None):
        sent.append(json.loads(req.data))
        content, finish = replies.pop(0)
        return _Resp({"choices": [{"message": {"content": content},
                                   "finish_reason": finish}]})
    monkeypatch.setattr(ag.urllib.request, "urlopen", fake)
    return sent


def test_a_length_stop_is_continued_and_joined(monkeypatch):
    sent = _script(monkeypatch, [("Each column: 2r mod 6 takes 0,2,4 -- wa", "length"),
                                 ("it, that repeats, so L is not Latin.", "stop")])
    out = ag.OpenAICompatAgent("http://x/v1").ask("q")
    assert out == "Each column: 2r mod 6 takes 0,2,4 -- wait, that repeats, so L is not Latin."
    assert len(sent) == 2
    tail = sent[1]["messages"][-2:]
    assert tail[0] == {"role": "assistant",
                       "content": "Each column: 2r mod 6 takes 0,2,4 -- wa"}
    assert tail[1]["content"] == ag.CONTINUE_PROMPT
    assert "tools" not in sent[1]


def test_still_cut_after_the_budget_says_so(monkeypatch):
    _script(monkeypatch, [("a", "length")] * (ag.MAX_CONTINUES + 1))
    out = ag.OpenAICompatAgent("http://x/v1").ask("q")
    assert out.endswith(ag.CUT_OFF_NOTE.strip())


def test_a_normal_stop_makes_one_call(monkeypatch):
    sent = _script(monkeypatch, [("done.", "stop")])
    assert ag.OpenAICompatAgent("http://x/v1").ask("q") == "done."
    assert len(sent) == 1


def test_max_tokens_env_override(monkeypatch):
    monkeypatch.setenv("AWDISCORD_AGENT_URL", "http://x/v1")
    monkeypatch.setenv("AWDISCORD_MAX_TOKENS", "2400")
    sent = _script(monkeypatch, [("ok", "stop")])
    ag.OpenAICompatAgent.from_env().ask("q")
    assert sent[0]["max_tokens"] == 2400
    for junk in ("", "x", "0", "-5"):
        monkeypatch.setenv("AWDISCORD_MAX_TOKENS", junk)
        assert ag.env_max_tokens("AWDISCORD_MAX_TOKENS", 900) == 900
