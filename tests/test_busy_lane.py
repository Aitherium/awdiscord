"""Busy-lane fast fail (2026-11-16): a person's Discord turn must not wait behind
background work on the 27B lane. Measured: oldest queued 12-30 s on bonsai2-27b-5090 /
aither-orchestrator, inflight held by openai_compat AGENT callers and doc_classifier.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord import agent as agent_mod  # noqa: E402
from awdiscord.agent import OpenAICompatAgent  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    agent_mod._BUSY_CACHE.clear()
    monkeypatch.setenv("AWDISCORD_BUSY_MODEL", "bonsai-4b-5090")
    yield
    agent_mod._BUSY_CACHE.clear()


def _agent(monkeypatch, oldest):
    a = OpenAICompatAgent("https://aitheros-microscheduler:8150/v1",
                          model="aither-orchestrator", tool_model="aither-orchestrator")
    monkeypatch.setattr(a, "_lane_wait", lambda lane: oldest)
    seen = []
    monkeypatch.setattr(a, "_ask", lambda *args, **k: seen.append(args[7]) or "ok")
    return a, seen


def test_a_backed_up_27b_lane_only_moves_to_the_4b_when_opted_in(monkeypatch):
    # Owner rule 2026-11-20: never fall back to a weaker model. The busy-lane hop is
    # opt-in; the default is to WAIT on the configured lane with a real queue ETA.
    monkeypatch.setenv("AWDISCORD_ALLOW_MODEL_FALLBACK", "1")
    a, seen = _agent(monkeypatch, 27.7)
    a.ask("hi", tools=[object()])
    assert seen == ["bonsai-4b-5090"]


def test_a_backed_up_lane_keeps_the_configured_model_by_default(monkeypatch):
    monkeypatch.delenv("AWDISCORD_ALLOW_MODEL_FALLBACK", raising=False)
    a, seen = _agent(monkeypatch, 27.7)
    a.ask("hi", tools=[object()])
    assert seen == ["aither-orchestrator"]


def test_a_short_queue_keeps_the_configured_model(monkeypatch):
    a, seen = _agent(monkeypatch, 1.0)
    a.ask("hi")
    assert seen == ["aither-orchestrator"]


def test_an_unreadable_queue_changes_nothing(monkeypatch):
    a, seen = _agent(monkeypatch, None)
    a.ask("hi")
    assert seen == ["aither-orchestrator"]


def test_off_unless_configured_and_never_probes_a_byo_endpoint(monkeypatch):
    monkeypatch.delenv("AWDISCORD_BUSY_MODEL")
    a, seen = _agent(monkeypatch, 99.0)
    a.ask("hi")
    assert seen == ["aither-orchestrator"]
    byo = OpenAICompatAgent("https://example.com/v1", model="m", transport=object())
    assert byo._lane_wait({"m"}) is None
