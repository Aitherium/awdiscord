"""An unconfigured bot asks the local spine for Bonsai at user priority, on both paths.

Before: ``OpenAICompatAgent.from_env`` defaulted the model to "default" (a name the
spine does not serve) and sent no priority unless AWDISCORD_AGENT_PRIORITY was set,
and the A2A path (``A2AAgent``, which ``Bot.from_env`` tries first) carried neither,
so a member's turn delegated over A2A queued as agent work.
"""
import pytest
from awdiscord.agent import (
    AGENT_SOURCE,
    DEFAULT_AGENT_MODEL,
    A2AAgent,
    OpenAICompatAgent,
    env_agent_priority,
)
from awdiscord.gateway import agent_route_summary


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("AWDISCORD_AGENT_MODEL", "AWDISCORD_AGENT_PRIORITY", "AWDISCORD_A2A_URL",
                 "AWDISCORD_A2A_TOKEN", "AWDISCORD_TOOL_MODEL", "AWDISCORD_VISION_MODELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWDISCORD_AGENT_URL", "https://127.0.0.1:8150/v1")
    return monkeypatch


def _sent_by(agent):
    sent = []
    agent._complete = lambda p: sent.append(p) or {"content": "ok"}
    agent.ask("hi")
    return sent[-1]


def test_unconfigured_openai_agent_asks_for_bonsai_at_user_priority(clean_env):
    agent = OpenAICompatAgent.from_env()
    payload = _sent_by(agent)
    assert DEFAULT_AGENT_MODEL == "bonsai2-27b"
    assert payload["model"] == "bonsai2-27b"
    assert payload["metadata"] == {"priority": "user", "source": "chat:awdiscord"}


def test_the_operator_still_overrides_model_and_priority(clean_env):
    clean_env.setenv("AWDISCORD_AGENT_MODEL", "gemma4-12b")
    clean_env.setenv("AWDISCORD_AGENT_PRIORITY", " Background ")
    payload = _sent_by(OpenAICompatAgent.from_env())
    assert payload["model"] == "gemma4-12b"
    assert payload["metadata"]["priority"] == "background"


@pytest.mark.parametrize("off", ["off", "none", "0", "false"])
def test_priority_off_sends_no_metadata_for_a_strict_backend(clean_env, off):
    clean_env.setenv("AWDISCORD_AGENT_PRIORITY", off)
    assert env_agent_priority() is None
    assert "metadata" not in _sent_by(OpenAICompatAgent.from_env())


def test_the_a2a_path_carries_priority_source_and_model(clean_env):
    clean_env.setenv("AWDISCORD_A2A_URL", "https://peer.example/a2a")
    agent = A2AAgent.from_env()
    seen = []

    def fake_rpc(message):
        seen.append(message)
        return {"status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": "pong"}]}]}

    agent._rpc = fake_rpc
    agent.ask("ping", user="u1", channel="c1")
    md = seen[-1]["metadata"]
    assert md["priority"] == "user"
    assert md["source"] == AGENT_SOURCE == "chat:awdiscord"
    assert md["model"] == "bonsai2-27b"
    assert md["user"] == "u1"


def test_a_bare_a2a_agent_sends_no_scheduler_hints():
    seen = []
    agent = A2AAgent("https://peer.example/a2a")
    agent._rpc = lambda m: seen.append(m) or {"status": {"state": "completed"},
                                              "artifacts": [{"parts": [{"kind": "text",
                                                                        "text": "ok"}]}]}
    agent.ask("ping")
    assert "metadata" not in seen[-1]


def test_route_summary_names_the_hints_and_nothing_secret(clean_env):
    clean_env.setenv("AWDISCORD_A2A_URL", "https://peer.example/a2a")
    clean_env.setenv("AWDISCORD_A2A_TOKEN", "tok-should-not-appear")
    summary = agent_route_summary(A2AAgent.from_env())
    assert summary == {"event": "agent_route", "kind": "A2AAgent", "model": "bonsai2-27b",
                       "priority": "user", "source": "chat:awdiscord"}
    assert "tok-should-not-appear" not in str(summary)
    assert "peer.example" not in str(summary)
