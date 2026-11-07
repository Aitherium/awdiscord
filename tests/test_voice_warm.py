"""Joining warms the TTS path once, and the first-clause knob reaches the thinker."""

import types

from awdiscord.voice import BotThinker, DiscordVoice, VoiceConfig


def _voice():
    bot = types.SimpleNamespace(lines=[], agent=None)
    bot._log_voice = lambda **f: bot.lines.append(f)
    return DiscordVoice(bot, VoiceConfig(enabled=True, stt_url="https://x/t",
                                         tts_url="https://x/s")), bot


def test_warm_speaks_one_clip_and_logs_its_time():
    v, bot = _voice()
    said = []
    speaker = types.SimpleNamespace(speak=lambda text: said.append(text) or (b"a", "mp3"))
    v._warm("g", types.SimpleNamespace(loop=types.SimpleNamespace(speaker=speaker)))
    assert said == ["Hi."]
    assert bot.lines[-1]["event"] == "voice_warm" and "tts_ms" in bot.lines[-1]


def test_a_failed_warm_up_is_a_log_line_not_a_crash():
    v, bot = _voice()

    def boom(text):
        raise OSError("tts down")
    v._warm("g", types.SimpleNamespace(loop=types.SimpleNamespace(
        speaker=types.SimpleNamespace(speak=boom))))
    assert bot.lines[-1]["event"] == "voice_warm_failed"


def test_first_clause_chars_defaults_on_and_reaches_the_thinker(monkeypatch):
    monkeypatch.delenv("AWDISCORD_VOICE_FIRST_CLAUSE_CHARS", raising=False)
    assert VoiceConfig().first_clause_chars == 24
    assert BotThinker(None, first_clause_chars=24).first_clause_chars == 24
