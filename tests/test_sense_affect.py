"""The bot's system prompt carries the fleet mood when AitherSense answers, and is
byte-identical to before when it does not."""
from __future__ import annotations

import awdiscord.sense as sense


def _reset():
    sense._cache.update({"at": 0.0, "text": "", "url": ""})
    sense.FETCHER[0] = None


def test_no_url_means_no_prefix(monkeypatch):
    _reset()
    monkeypatch.delenv("AWDISCORD_SENSE_AFFECT_URL", raising=False)
    assert sense.affect_prefix() == ""


def test_the_modifier_is_returned_and_cached(monkeypatch):
    _reset()
    monkeypatch.setenv("AWDISCORD_SENSE_AFFECT_URL", "https://sense.test/affect")
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return {"valence": 0.5, "prompt_modifier": "You are warm and a little playful."}

    sense.FETCHER[0] = fetch
    assert sense.affect_prefix(now=1000.0) == "You are warm and a little playful."
    assert sense.affect_prefix(now=1010.0) == "You are warm and a little playful."
    assert len(calls) == 1, "a mood must not be re-fetched per message"


def test_a_failure_is_silent_and_not_cached_as_a_mood(monkeypatch):
    _reset()
    monkeypatch.setenv("AWDISCORD_SENSE_AFFECT_URL", "https://sense.test/affect")

    def boom(url, timeout):
        raise OSError("down")

    sense.FETCHER[0] = boom
    assert sense.affect_prefix(now=2000.0) == ""
    sense.FETCHER[0] = lambda url, timeout: {"prompt_modifier": "steady"}
    assert sense.affect_prefix(now=2001.0) == "", "the failure is cached for one window"
    assert sense.affect_prefix(now=2000.0 + sense.CACHE_S + 1) == "steady"


def test_an_empty_modifier_is_empty_string(monkeypatch):
    _reset()
    monkeypatch.setenv("AWDISCORD_SENSE_AFFECT_URL", "https://sense.test/affect")
    sense.FETCHER[0] = lambda url, timeout: {"valence": 0.1}
    assert sense.affect_prefix(now=3000.0) == ""
