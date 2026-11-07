"""Inbound voice is DAVE-decrypted before Opus decode (2026-11-01: 0 turns heard)."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from awdiscord import voice


class _Dave:
    def __init__(self, fail=False, error="not ready"):
        self.calls = []
        self.fail = fail
        self.error = error
        self.ready = True                   # an established session: no passthrough

    def can_passthrough(self, user_id):
        return False

    def decrypt(self, user_id, media_type, packet):
        self.calls.append((user_id, media_type, packet))
        if self.fail:
            raise ValueError(self.error)
        return b"plain-opus"


def _fake_davey(monkeypatch):
    mod = types.ModuleType("davey")
    mod.MediaType = SimpleNamespace(audio="AUDIO")
    monkeypatch.setitem(sys.modules, "davey", mod)


def _fake_decoder(monkeypatch, seen):
    class Decoder:
        def decode(self, data, fec=False):
            seen.append(data)
            return b"\x01\x02" * 960

    opus = types.ModuleType("discord.opus")
    opus.Decoder = Decoder
    monkeypatch.setitem(sys.modules, "discord.opus", opus)


def test_dave_frames_are_decrypted_before_decode(monkeypatch):
    _fake_davey(monkeypatch)
    seen: list = []
    _fake_decoder(monkeypatch, seen)
    dave = _Dave()
    vc = SimpleNamespace(_connection=SimpleNamespace(dave_session=dave, dave_protocol_version=1))
    pcm = voice._inbound_pcm(vc, SimpleNamespace(id=42), SimpleNamespace(opus=b"cipher"), {})
    assert dave.calls == [(42, "AUDIO", b"cipher")]
    assert seen == [b"plain-opus"]          # the decoder never sees ciphertext
    assert pcm


def test_a_failed_decrypt_drops_the_frame_without_raising(monkeypatch):
    _fake_davey(monkeypatch)
    _fake_decoder(monkeypatch, [])
    vc = SimpleNamespace(_connection=SimpleNamespace(dave_session=_Dave(fail=True),
                                                     dave_protocol_version=1))
    assert voice._inbound_pcm(vc, SimpleNamespace(id=1), SimpleNamespace(opus=b"x"), {}) == b""


def test_no_dave_decodes_directly(monkeypatch):
    seen: list = []
    _fake_decoder(monkeypatch, seen)
    vc = SimpleNamespace(_connection=SimpleNamespace(dave_session=None, dave_protocol_version=0))
    assert voice._inbound_pcm(vc, SimpleNamespace(id=1), SimpleNamespace(opus=b"op"), {})
    assert seen == [b"op"]


SILENCE = bytes([0xF8, 0xFF, 0xFE])   # the Opus silence frame


def test_plaintext_silence_frames_are_decoded_not_dropped(monkeypatch):
    """2026-11-06: Discord ends each talk spurt with UNENCRYPTED Opus silence frames.
    Dropping them meant the segmenter never heard speech end, so no turn ever closed."""
    _fake_davey(monkeypatch)
    seen: list = []
    _fake_decoder(monkeypatch, seen)
    dave = _Dave(fail=True, error="Failed to decrypt: DecryptionFailed("
                                   "UnencryptedWhenPassthroughDisabled)")
    vc = SimpleNamespace(_connection=SimpleNamespace(dave_session=dave, dave_protocol_version=1))
    assert voice._inbound_pcm(vc, SimpleNamespace(id=7), SimpleNamespace(opus=SILENCE), {})
    assert seen == [SILENCE]


def _vc(members, ssrc_map=None):
    added = {}
    vc = SimpleNamespace(
        channel=SimpleNamespace(members=members),
        guild=SimpleNamespace(get_member=lambda uid: next(
            (m for m in members if m.id == uid), None)),
        _get_id_from_ssrc=lambda ssrc: (ssrc_map or {}).get(ssrc),
        _add_ssrc=lambda uid, ssrc: added.__setitem__(ssrc, uid))
    return vc, added


def test_unmapped_speaker_resolves_to_the_channels_only_human():
    """voice_recv hands user=None until SPEAKING maps the ssrc; DAVE needs the user id."""
    human = SimpleNamespace(id=5, bot=False)
    bot = SimpleNamespace(id=9, bot=True)
    vc, added = _vc([human, bot])
    data = SimpleNamespace(packet=SimpleNamespace(ssrc=1234))
    assert voice._resolve_speaker(vc, data) is human
    assert added == {1234: 5}                       # the router learns it too


def test_unmapped_speaker_with_two_humans_stays_unknown():
    vc, _ = _vc([SimpleNamespace(id=5, bot=False), SimpleNamespace(id=6, bot=False)])
    assert voice._resolve_speaker(vc, SimpleNamespace(packet=SimpleNamespace(ssrc=1))) is None


def test_a_mapped_ssrc_wins():
    a, b = SimpleNamespace(id=5, bot=False), SimpleNamespace(id=6, bot=False)
    vc, _ = _vc([a, b], ssrc_map={77: 6})
    assert voice._resolve_speaker(vc, SimpleNamespace(packet=SimpleNamespace(ssrc=77))) is b
