"""A second join for the same guild must leave the channel listening.

2026-11-06: the auto-join and a voice-state event both called join within a
millisecond. The second stopped the first session while its sink was still
attached, listen() raised "Already receiving audio", and the new session had no
sink and no watcher -- Aither sat in the channel and heard nothing.
"""

import types

from awdiscord.voice import DiscordVoice, VoiceConfig


class _VC:
    """voice_recv's rule: listen() while already listening raises."""

    def __init__(self, refuse_listen=False):
        self.sink = None
        self.listens = 0
        self.stops = 0
        self.refuse_listen = refuse_listen

    def is_listening(self):
        return self.sink is not None

    def stop_listening(self):
        self.stops += 1
        self.sink = None

    def listen(self, sink):
        if self.refuse_listen:
            raise RuntimeError("socket gone")
        if self.sink is not None:
            raise RuntimeError("Already receiving audio.")
        self.listens += 1
        self.sink = sink

    def is_connected(self):
        return True


def _voice(monkeypatch):
    bot = types.SimpleNamespace(lines=[], agent=None)
    bot._log_voice = lambda **f: bot.lines.append(f)
    v = DiscordVoice(bot, VoiceConfig(enabled=True, stt_url="https://x/t",
                                      tts_url="https://x/s"))
    watched = []
    monkeypatch.setattr(v, "build_loop", lambda **kw: types.SimpleNamespace())
    monkeypatch.setattr(v, "_make_sink", lambda session: object())
    monkeypatch.setattr(v, "_watch_inbound",
                        lambda g, s, attached=True: watched.append(attached))
    return v, bot, watched


def test_a_second_join_reattaches_instead_of_going_deaf(monkeypatch):
    v, bot, watched = _voice(monkeypatch)
    vc = _VC()
    v._start_session("g", "c", vc)
    v._start_session("g", "c", vc)
    assert vc.sink is not None and vc.listens == 2 and vc.stops == 1
    assert not [line for line in bot.lines if line.get("event") == "voice_listen_error"]
    assert watched == [True, True]


def test_a_failed_listen_still_starts_the_watcher_so_it_retries(monkeypatch):
    v, bot, watched = _voice(monkeypatch)
    v._start_session("g", "c", _VC(refuse_listen=True))
    assert [line["event"] for line in bot.lines] == ["voice_listen_error"]
    assert watched == [False]
