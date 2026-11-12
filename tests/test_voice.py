"""The Discord side of the voice loop: config, downmix, the worker, and commands.

No discord.py is imported here: the transport imports it lazily, which is what
lets the pure parts run on a box that has never held a bot token.  The one live
piece, ffmpeg, is exercised only when it is on PATH.
"""
from __future__ import annotations

import io
import struct
import subprocess
import time
import types
import wave

import pytest
from awdiscord.voice import (
    BotThinker,
    DiscordVoice,
    VoiceConfig,
    decode_to_pcm48k,
    downmix_resample,
)


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=False)
        return True
    except (FileNotFoundError, OSError):
        return False


class _Bot:
    def __init__(self):
        self.lines = []
        self.agent = types.SimpleNamespace(
            ask=lambda prompt, persona=None, history=None: "reply to " + prompt)

    def _log_voice(self, **fields):
        self.lines.append(fields)


class _Rest:
    def __init__(self):
        self.answered = []

    def interaction_callback(self, iid, token, payload):
        self.answered.append(payload["data"]["content"])


def _voice() -> DiscordVoice:
    return DiscordVoice(_Bot(), VoiceConfig(enabled=True,
                                            stt_url="https://x/t",
                                            tts_url="https://x/s"))


class TestVoiceConfig:
    def test_voice_is_off_by_default(self, monkeypatch):
        for name in ("AWDISCORD_VOICE", "AWDISCORD_VOICE_STT_URL",
                     "AWDISCORD_VOICE_TTS_URL"):
            monkeypatch.delenv(name, raising=False)
        assert VoiceConfig.from_env().enabled is False

    def test_urls_thresholds_and_join_channels(self, monkeypatch):
        monkeypatch.setenv("AWDISCORD_VOICE", "1")
        monkeypatch.setenv("AWDISCORD_VOICE_STT_URL", "https://x/voice/transcribe")
        monkeypatch.setenv("AWDISCORD_VOICE_TTS_URL", "https://x/voice/synthesize")
        monkeypatch.setenv("AWDISCORD_VOICE_JOIN", "111, 222")
        monkeypatch.setenv("AWDISCORD_VOICE_SILENCE_MS", "550")
        monkeypatch.setenv("AWDISCORD_DEFAULT_PERSONA", "hera")
        cfg = VoiceConfig.from_env()
        assert cfg.enabled is True
        assert cfg.stt_url.endswith("/transcribe")
        assert cfg.join_channels == ("111", "222")
        assert cfg.loop_config().silence_ms == 550
        assert cfg.persona == "hera"

    def test_from_env_refuses_a_half_configured_voice(self, monkeypatch):
        monkeypatch.setenv("AWDISCORD_VOICE", "1")
        monkeypatch.delenv("AWDISCORD_VOICE_STT_URL", raising=False)
        monkeypatch.delenv("AWDISCORD_VOICE_TTS_URL", raising=False)
        with pytest.raises(RuntimeError, match="STT_URL"):
            DiscordVoice.from_env(_Bot())

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.delenv("AWDISCORD_VOICE", raising=False)
        assert DiscordVoice.from_env(_Bot()) is None


class TestDownmix:
    def test_48k_stereo_becomes_16k_mono(self):
        frames = 4800                       # 100 ms
        pcm = b"".join(struct.pack("<hh", 4000, 4000) for _ in range(frames))
        out = downmix_resample(pcm, 48000, 16000, 2)
        # 16 kHz mono, 100 ms, int16: 1600 samples / 3200 bytes (the rate
        # converter keeps a residual state, so allow the one-frame tail).
        assert abs(len(out) - 3200) <= 4
        assert any(sample for (sample,) in struct.iter_unpack("<h", out))

    def test_empty_is_empty(self):
        assert downmix_resample(b"") == b""


class TestThinker:
    def test_persona_and_history_reach_the_agent(self):
        seen = {}

        class Agent:
            def ask(self, prompt, persona=None, history=None):
                seen.update(prompt=prompt, persona=persona, history=history)
                return "ok"

        thinker = BotThinker(Agent(), "vera")
        assert thinker.think("question",
                             history=[{"role": "user", "content": "a"}]) == "ok"
        assert seen["persona"] == "vera"
        assert seen["history"] == [{"role": "user", "content": "a"}]


class TestCommands:
    def test_command_defs_are_join_leave_and_calls(self):
        names = [c["name"] for c in _voice().command_defs()]
        assert names == ["join", "leave", "calls"]

    def test_intercept_ignores_a_text_command(self):
        assert _voice().intercept({"data": {"name": "ask"}}) is False

    def test_intercept_answers_when_the_bot_is_not_attached(self):
        voice = _voice()
        voice.bot.rest = _Rest()
        assert voice.intercept({"id": "1", "token": "t", "data": {"name": "join"},
                                "guild_id": "1",
                                "member": {"user": {"id": "2"}}}) is True
        assert "starting" in voice.bot.rest.answered[0]

    def test_join_outside_a_guild_is_refused(self):
        voice = _voice()
        voice.bot.rest = _Rest()
        assert voice.intercept({"id": "1", "token": "t",
                                "data": {"name": "join"}}) is True
        assert "server" in voice.bot.rest.answered[0]


class TestSessionWorker:
    def test_a_submitted_utterance_is_answered_and_played(self):
        voice = _voice()
        played = []
        voice.play_turn = lambda guild_id, turn: played.append((guild_id, turn))
        voice.log = lambda **fields: None

        class Loop:
            history = []
            user = ""

            def handle_utterance(self, pcm):
                from awvoice.loop import VoiceTurn
                return VoiceTurn(transcript="hi", reply="hello", audio=b"A",
                                 audio_format="wav")

        from awdiscord.voice import _Session
        session = _Session(voice, "9", "8", Loop())
        session.submit("1", "tester", b"\x00\x00" * 320)
        for _ in range(100):
            if played:
                break
            time.sleep(0.02)
        session.stop()
        assert played and played[0][1].reply == "hello"

    def test_a_failed_turn_does_not_kill_the_worker(self):
        voice = _voice()
        voice.log = lambda **fields: None
        errors = []

        class Loop:
            history = []
            user = ""

            def handle_utterance(self, pcm):
                errors.append(1)
                raise RuntimeError("stt down")

        from awdiscord.voice import _Session
        session = _Session(voice, "9", "8", Loop())
        session.submit("1", "tester", b"\x00\x00" * 320)
        for _ in range(100):
            if errors:
                break
            time.sleep(0.02)
        session.stop()
        assert errors == [1]


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg is not installed here")
class TestDecode:
    def test_wav_decodes_to_48k_stereo(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<h", 1000) * 1600)
        pcm = decode_to_pcm48k(buf.getvalue(), "wav")
        # 100 ms at 48 kHz stereo int16
        assert abs(len(pcm) - 4800 * 2 * 2) <= 16
