"""The voice UDP flow is kept alive, or inbound audio dies at the NAT (2026-11-08).

The bot is silent while it listens, so its voice socket sent nothing; the podman
MASQUERADE conntrack entry (30 s) expired and every packet Discord sent was dropped
before the socket -- dave active, owner speaking, packets 0. voice_recv's own
keepalive sleeps 5000 SECONDS between sends.
"""
from __future__ import annotations

import struct
import threading
import time
import types

from awdiscord import voice
from awdiscord.voice import DiscordVoice, VoiceConfig, _Session


class _Sock:
    def __init__(self, fail: bool = False):
        self.sent: list = []
        self.fail = fail

    def sendto(self, data, addr):
        if self.fail:
            raise OSError("closed")
        self.sent.append((bytes(data), addr))


class _Conn:
    def __init__(self, sock, ip="104.29.152.140", port=19313, ssrc=4242):
        self.socket = sock
        self.endpoint_ip = ip
        self.voice_port = port
        self.ssrc = ssrc
        self.listeners: list = []

    def add_socket_listener(self, cb):
        self.listeners.append(cb)


class _VC:
    def __init__(self, conn):
        self._connection = conn

    def is_connected(self):
        return True


def test_keepalive_is_a_discovery_request_voice_recv_ignores():
    pkt = voice._keepalive_packet(4242)
    assert len(pkt) == 74
    assert struct.unpack(">HHI", pkt[:8]) == (1, 70, 4242)
    # Discord's 74-byte answer has type 2; voice_recv skips exactly that shape.
    answer = struct.pack(">HHI", 2, 70, 4242) + bytes(66)
    assert len(answer) == 74 and answer[1] == 0x02


def test_keepalive_sends_to_the_voice_endpoint_and_counts_replies_once():
    sock = _Sock()
    conn = _Conn(sock)
    vc = _VC(conn)
    before = dict(voice._VOICE_UDP)
    assert voice._udp_keepalive(vc) is True
    assert voice._udp_keepalive(vc) is True
    assert [addr for _, addr in sock.sent] == [("104.29.152.140", 19313)] * 2
    assert len(conn.listeners) == 1            # counted once, not once per send
    conn.listeners[0](b"x" * 74)
    assert voice._VOICE_UDP["udp_rx"] == before["udp_rx"] + 1
    assert voice._VOICE_UDP["udp_rx_bytes"] == before["udp_rx_bytes"] + 74
    assert voice._VOICE_UDP["keepalive_sent"] == before["keepalive_sent"] + 2


def test_keepalive_never_raises_before_connect_or_on_a_closed_socket():
    assert voice._udp_keepalive(_VC(None)) is False
    assert voice._udp_keepalive(_VC(_Conn(_Sock(), ip=None))) is False
    before = voice._VOICE_UDP["keepalive_failed"]
    assert voice._udp_keepalive(_VC(_Conn(_Sock(fail=True)))) is False
    assert voice._VOICE_UDP["keepalive_failed"] == before + 1


def test_interval_beats_the_conntrack_udp_timeout():
    assert 0 < voice._KEEPALIVE_S <= 10       # nf_conntrack_udp_timeout is 30 s


def test_the_inbound_watcher_sends_keepalives_while_connected(monkeypatch):
    monkeypatch.setattr(voice, "_KEEPALIVE_S", 0.01)
    v = DiscordVoice(types.SimpleNamespace(_log_voice=lambda **f: None),
                     VoiceConfig(enabled=True, stt_url="https://x/t", tts_url="https://x/s"))
    v.log = lambda **fields: None

    class Loop:
        history: list = []
        user = ""

    session = _Session(v, "9", "8", Loop())
    sock = _Sock()
    session.vc = _VC(_Conn(sock))
    v._watch_inbound("9", session)
    deadline = time.monotonic() + 3
    while len(sock.sent) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    session.stop()
    assert len(sock.sent) >= 3, "the watcher stopped keeping the voice flow alive"
    assert threading.active_count() >= 1
