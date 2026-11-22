"""Eight persona bots in one voice channel: each answers to its own name, one at a time.

2026-11-18: every twin's wake word defaulted to Aither's and one human in the room
meant no wake word was needed, so all eight would have answered every sentence.
"""
from __future__ import annotations

from types import SimpleNamespace

from awdiscord import voice
from awdiscord.voice import (
    DEFAULT_WAKE_WORDS,
    VoiceConfig,
    other_names,
    persona_wake_words,
    strip_wake_word,
)


def test_a_twin_answers_to_its_own_name_and_aither_to_hers():
    assert persona_wake_words("lyra") == ("lyra",)
    assert persona_wake_words("aither") == DEFAULT_WAKE_WORDS
    assert persona_wake_words("") == DEFAULT_WAKE_WORDS
    assert strip_wake_word("Hey Lyra, what's new?", ("lyra",)) == "what's new?"
    assert strip_wake_word("Aither, what time is it", ("lyra",)) is None


def test_other_names_exclude_our_own():
    assert "lyra" not in other_names(("lyra",)) and "aither" in other_names(("lyra",))
    assert "aither" not in other_names(DEFAULT_WAKE_WORDS)


def test_config_from_env_makes_aither_the_lead_and_twins_followers(monkeypatch):
    monkeypatch.setenv("AWDISCORD_VOICE_PERSONA", "lyra")
    twin = VoiceConfig.from_env()
    assert twin.wake_words == ("lyra",) and twin.lead is False
    monkeypatch.setenv("AWDISCORD_VOICE_PERSONA", "aither")
    lead = VoiceConfig.from_env()
    assert lead.wake_words == DEFAULT_WAKE_WORDS and lead.lead is True
    assert "lyra" in lead.other_names


class _Probe:
    """The session's room methods, bound to a stand-in session."""

    def humans(self):
        return _cls().humans(self)

    def other_bots(self):
        return _cls().other_bots(self)

    def __init__(self, config, members, me="100"):
        self.voice = SimpleNamespace(config=config)
        self.vc = SimpleNamespace(user=SimpleNamespace(id=me),
                                  channel=SimpleNamespace(members=members))


def _cls():
    for obj in vars(voice).values():
        if isinstance(obj, type) and hasattr(obj, "wake_required") and hasattr(obj, "humans"):
            return obj
    raise AssertionError("no session class with wake_required")


def _member(i, bot):
    return SimpleNamespace(id=str(i), bot=bot)


def test_a_twin_among_bots_waits_for_its_name_the_lead_does_not():
    cls = _cls()
    room = [_member(1, False), _member(100, True), _member(200, True)]
    twin = _Probe(VoiceConfig(lead=False), room)
    lead = _Probe(VoiceConfig(lead=True), room)
    assert cls.wake_required(twin) is True
    assert cls.wake_required(lead) is False
    alone = _Probe(VoiceConfig(lead=False), [_member(1, False), _member(100, True)])
    assert cls.wake_required(alone) is False          # just you and this twin
