"""A pinned or standing voice session that lost its socket rejoins by itself.

2026-11-23: seven persona bots in the podcast room kept their sessions but sat
``"connected": false``; every clip failed ``ClientException("Not connected to
voice.")`` and the owner heard nothing until a manual /voice/leave + /voice/join.
"""

import asyncio
import types

import pytest
from awdiscord import voice as voice_mod
from awdiscord.voice import DiscordVoice, VoiceConfig, _Session


def _voice(join_channels=()):
    bot = types.SimpleNamespace(lines=[], agent=None)
    bot._log_voice = lambda **f: bot.lines.append(f)
    v = DiscordVoice(bot, VoiceConfig(enabled=True, stt_url="https://x/t",
                                      tts_url="https://x/s",
                                      join_channels=tuple(join_channels)))
    scheduled = []
    v._schedule = lambda coro: (scheduled.append(coro), coro.close())
    return v, bot, scheduled


def _session(v, guild="g", channel="c"):
    s = types.SimpleNamespace(guild_id=guild, channel_id=channel,
                              _play_down_at=None, _down_since=None)
    v._sessions[guild] = s
    return s


def _rejoins(bot):
    return [line for line in bot.lines if line.get("event") == "voice_rejoin"]


def test_a_pinned_session_off_the_socket_rejoins_after_15s():
    v, bot, scheduled = _voice()
    v.pin("g", "c")
    s = _session(v)
    assert v._check_rejoin("g", s, connected=False, now=100.0) == ""
    assert v._check_rejoin("g", s, connected=False, now=110.0) == ""
    assert v._check_rejoin("g", s, connected=False, now=116.0) == "not_connected"
    assert len(scheduled) == 1
    line = _rejoins(bot)[0]
    assert line["channel"] == "c" and line["reason"] == "not_connected"


def test_rejoins_are_rate_limited_to_one_a_minute():
    v, bot, scheduled = _voice()
    v.pin("g", "c")
    s = _session(v)
    v._check_rejoin("g", s, False, now=0.0)
    assert v._check_rejoin("g", s, False, now=20.0) == "not_connected"
    for t in (25.0, 40.0, 79.0):
        assert v._check_rejoin("g", s, False, now=t) == ""
    assert v._check_rejoin("g", s, False, now=81.0) == "not_connected"
    assert len(scheduled) == 2 and len(_rejoins(bot)) == 2


def test_a_standing_channel_rejoins_too():
    v, bot, scheduled = _voice(join_channels=("c",))
    s = _session(v)
    v._check_rejoin("g", s, False, now=0.0)
    assert v._check_rejoin("g", s, False, now=16.0) == "not_connected"


def test_an_unpinned_ad_hoc_session_is_left_alone():
    v, bot, scheduled = _voice()
    s = _session(v)
    v._check_rejoin("g", s, False, now=0.0)
    assert v._check_rejoin("g", s, False, now=100.0) == ""
    assert not scheduled


def test_a_session_that_was_told_to_leave_never_rejoins():
    v, bot, scheduled = _voice()
    v.pin("g", "c")
    s = _session(v)
    v._check_rejoin("g", s, False, now=0.0)
    v._sessions.pop("g")                 # leave() pops the session
    v.pin("g")                           # /voice/leave releases the pin
    assert v._check_rejoin("g", s, False, now=100.0) == ""
    assert not scheduled


def test_a_short_blip_resets_the_clock():
    v, bot, scheduled = _voice()
    v.pin("g", "c")
    s = _session(v)
    v._check_rejoin("g", s, False, now=0.0)
    v._check_rejoin("g", s, True, now=10.0)
    assert v._check_rejoin("g", s, False, now=20.0) == ""
    assert v._check_rejoin("g", s, False, now=30.0) == ""
    assert v._check_rejoin("g", s, False, now=36.0) == "not_connected"


def test_a_not_connected_play_error_arms_the_watchdog_even_if_vc_says_connected():
    v, bot, scheduled = _voice()
    v.pin("g", "c")
    s = _Session.__new__(_Session)
    s.voice, s.guild_id, s.channel_id = v, "g", "c"
    s._play_down_at = None
    s._down_since = None
    v._sessions["g"] = s
    s._play_error("play", RuntimeError("Not connected to voice."))
    assert s._play_down_at is not None
    assert any(line.get("event") == "voice_play_error" for line in bot.lines)
    first = s._play_down_at
    assert v._check_rejoin("g", s, True, now=first + 1) == ""
    assert v._check_rejoin("g", s, True, now=first + 16) == "play_not_connected"


def test_other_play_errors_do_not_arm_it():
    v, _bot, _ = _voice()
    s = _Session.__new__(_Session)
    s.voice, s.guild_id, s.channel_id = v, "g", "c"
    s._play_down_at = None
    s._play_error("play", ValueError("bad pcm"))
    assert s._play_down_at is None


def test_rejoin_leaves_then_joins_the_same_channel(monkeypatch):
    v, bot, _ = _voice()
    v.pin("g", "c")
    s = _session(v)
    calls = []

    async def leave(g):
        calls.append(("leave", g))
        v._sessions.pop(g, None)

    async def join(g, c):
        calls.append(("join", g, c))
        return True

    monkeypatch.setattr(v, "leave", leave)
    monkeypatch.setattr(v, "join_channel", join)
    assert asyncio.run(v._rejoin("g", "c", s)) is True
    assert calls == [("leave", "g"), ("join", "g", "c")]
    assert v._pinned == {"g": "c"}       # the pin survives its own rejoin


def test_rejoin_is_skipped_when_a_deliberate_leave_got_there_first(monkeypatch):
    v, bot, _ = _voice()
    v.pin("g", "c")
    s = _session(v)
    v._sessions.pop("g")
    calls = []

    async def join(g, c):
        calls.append(c)
        return True

    monkeypatch.setattr(v, "join_channel", join)
    assert asyncio.run(v._rejoin("g", "c", s)) is False
    assert not calls


def test_constants_match_the_brief():
    assert voice_mod.REJOIN_AFTER_S == 15.0 and voice_mod.REJOIN_EVERY_S == 60.0


def _retry_voice(monkeypatch, join_results):
    v, bot, _ = _voice()
    v.client = object()
    v.pin("g", "c")
    slept, joins = [], []

    async def fake_sleep(s):
        slept.append(s)

    async def join(g, c):
        joins.append(c)
        ok = join_results.pop(0)
        if ok:
            v._sessions[g] = types.SimpleNamespace(channel_id=c)
        return ok

    monkeypatch.setattr(voice_mod, "_sleep", fake_sleep)
    monkeypatch.setattr(v, "join_channel", join)
    return v, bot, slept, joins


def test_a_failed_pinned_join_is_retried_on_the_backoff(monkeypatch):
    """Lyra 2026-11-23: connect_wait timed out, join raised TimeoutError, no retry."""
    v, bot, slept, joins = _retry_voice(monkeypatch, [False, True])
    clock = [1000.0]
    monkeypatch.setattr(voice_mod.time, "monotonic", lambda: clock[0])

    async def fake_sleep(s):
        slept.append(s)
        clock[0] += s

    monkeypatch.setattr(voice_mod, "_sleep", fake_sleep)
    assert asyncio.run(v._retry_pinned_join("g", "c", "join_failed")) is True
    assert joins == ["c", "c"]
    assert slept[0] == 15.0 and sum(slept[1:]) >= 60.0 - 1e-6   # one try a minute
    reasons = [line["reason"] for line in _rejoins(bot)]
    assert reasons == ["join_failed", "join_failed"]
    assert "g" not in v._retrying


def test_the_retry_stops_when_the_pin_is_released(monkeypatch):
    v, bot, slept, joins = _retry_voice(monkeypatch, [True])
    v.pin("g")                                   # /voice/leave
    assert asyncio.run(v._retry_pinned_join("g", "c", "join_failed")) is False
    assert joins == []


def test_join_error_schedules_one_retry_only_when_pinned():
    v, bot, scheduled = _voice()
    v.client = object()
    assert v._retry_pinned("g", "join_failed") is False      # not pinned
    v.pin("g", "c")
    assert v._retry_pinned("g", "join_failed") is True
    assert v._retry_pinned("g", "join_failed") is False      # already waiting
    assert len(scheduled) == 1


def test_slash_leave_releases_the_pin(monkeypatch):
    v, bot, scheduled = _voice()
    v.client = object()
    v.pin("g", "c")
    monkeypatch.setattr(v, "_answer", lambda *a, **k: None)
    assert v.intercept({"data": {"name": "leave"}, "guild_id": "g",
                        "member": {"user": {"id": "1"}}}) is True
    assert v._pinned == {}


# -- the join race (Atlas 2026-11-23: two voice_joined 3 ms apart, then no client) --

def test_two_concurrent_joins_connect_once(monkeypatch):
    v, bot, _ = _voice()
    calls = []

    async def once(g, c):
        calls.append(c)
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(v, "_join_channel_once", once)

    async def go():
        return await asyncio.gather(v.join_channel("g", "c"), v.join_channel("g", "c"))

    assert asyncio.run(go()) == [True, True]
    assert calls == ["c"]
    assert not v.joining("g")


def test_a_join_for_another_channel_waits_its_turn(monkeypatch):
    v, bot, _ = _voice()
    order = []

    async def once(g, c):
        order.append(("start", c))
        await asyncio.sleep(0.02)
        order.append(("end", c))
        return True

    monkeypatch.setattr(v, "_join_channel_once", once)

    async def go():
        return await asyncio.gather(v.join_channel("g", "c1"), v.join_channel("g", "c2"))

    assert asyncio.run(go()) == [True, True]
    assert order == [("start", "c1"), ("end", "c1"), ("start", "c2"), ("end", "c2")]


def test_a_failed_join_releases_the_flight(monkeypatch):
    v, bot, _ = _voice()

    async def once(g, c):
        raise RuntimeError("boom")

    monkeypatch.setattr(v, "_join_channel_once", once)

    async def go():
        with pytest.raises(RuntimeError):
            await v.join_channel("g", "c")
        return v.joining("g")

    assert asyncio.run(go()) is False


def test_the_bots_own_disconnect_during_a_join_never_tears_down_the_call(monkeypatch):
    v, bot, _ = _voice()
    me = types.SimpleNamespace(id=1)
    v.client = types.SimpleNamespace(user=me)
    left = []

    async def leave(g):
        left.append(g)

    monkeypatch.setattr(v, "leave", leave)
    member = types.SimpleNamespace(id=1, guild=types.SimpleNamespace(id="g"))
    before = types.SimpleNamespace(channel=types.SimpleNamespace(id="c"))
    after = types.SimpleNamespace(channel=None)

    async def go():
        v._connecting["g"] = asyncio.get_running_loop().create_future()
        await v.on_voice_state_update(member, before, after)
        assert left == []                                  # join in flight: ignored
        v._connecting["g"].set_result(True)
        await v.on_voice_state_update(member, before, after)

    asyncio.run(go())
    assert left == ["g"]
    assert any(line.get("event") == "voice_left_ignored" for line in bot.lines)


def test_stats_read_the_guilds_actual_voice_client():
    v, bot, _ = _voice()
    stale = types.SimpleNamespace(is_connected=lambda: True)
    guild = types.SimpleNamespace(voice_client=None)
    v.client = types.SimpleNamespace(get_guild=lambda gid: guild)
    assert v._guild_vc("1", fallback=stale) is None        # no client -> not connected
    live = types.SimpleNamespace(is_connected=lambda: True)
    guild.voice_client = live
    assert v._guild_vc("1", fallback=stale) is live
    v.client = None
    assert v._guild_vc("1", fallback=stale) is stale       # tests / not attached


def test_no_voice_client_play_error_arms_the_watchdog():
    v, _bot, _ = _voice()
    s = _Session.__new__(_Session)
    s.voice, s.guild_id, s.channel_id = v, "g", "c"
    s._play_down_at = None
    s._play_error("play", RuntimeError("no voice client for guild 123"))
    assert s._play_down_at is not None
