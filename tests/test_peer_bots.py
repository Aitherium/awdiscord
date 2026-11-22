"""A bot the operator names as a peer is answered when it addresses us; others never.

Measured in a server: another community's bot replied to Aither and tagged her,
and Aither stayed silent every time, because every bot-authored message was
ignored. The peer path answers a NAMED bot that addressed us directly, holds a
volley between two models to a per-channel budget, and pings the peer on the
reply (a bot only reacts to a mention).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from awdiscord import gateway
from awdiscord.gateway import CHAT, IGNORE, PeerBots, message_route

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped


def _route(**kw):
    base = {"own": False, "author_bot": True, "is_dm": False, "mentioned": True,
            "hooks": False, "candidate": False, "peer": False}
    base.update(kw)
    return message_route(**base)


def test_a_bot_is_ignored_unless_it_is_an_addressing_peer():
    assert _route() == IGNORE                                   # any bot, even mentioning us
    assert _route(peer=True) == CHAT                            # a named peer that addressed us
    assert _route(peer=True, mentioned=False) == IGNORE         # ambient bot chatter
    assert _route(peer=True, is_dm=True) == IGNORE              # never a bot DM loop
    assert _route(peer=True, own=True) == IGNORE                # never ourselves
    assert _route(peer=True, mentioned=False, hooks=True, candidate=True) == IGNORE


def test_humans_route_as_before():
    assert _route(author_bot=False) == CHAT
    assert _route(author_bot=False, mentioned=False, hooks=True) == "hook"
    assert _route(author_bot=False, mentioned=False, candidate=True) == "candidate"
    assert _route(author_bot=False, mentioned=False) == IGNORE


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_budget_ends_a_volley_and_refills_after_the_window():
    clock = _Clock()
    peers = PeerBots(["777"], turns=3, window_s=60, clock=clock)
    assert [peers.admit("777", "c1") for _ in range(4)] == [True, True, True, False]
    assert peers.admit("777", "c2") is True                     # budgets are per channel
    clock.now += 61
    assert peers.admit("777", "c1") is True                     # the window slid past


def test_only_named_peers_and_zero_turns_disables():
    assert PeerBots(["777"]).admit("888", "c1") is False
    assert PeerBots([]).admit("777", "c1") is False
    assert PeerBots(["777"], turns=0).admit("777", "c1") is False


def test_from_env(monkeypatch):
    monkeypatch.setenv("AWDISCORD_PEER_BOTS", " 777, 888 ,")
    monkeypatch.setenv("AWDISCORD_PEER_TURNS", "2")
    monkeypatch.setenv("AWDISCORD_PEER_WINDOW_S", "not-a-number")
    peers = PeerBots.from_env()
    assert peers.ids == {"777", "888"} and peers.turns == 2 and peers.window_s == 900.0
    monkeypatch.delenv("AWDISCORD_PEER_BOTS")
    assert PeerBots.from_env().ids == set()


class _Agent:
    name = "aither"
    personas: dict = {}

    def ask(self, prompt, **kw):
        return "Bold of you to assume I lost that round."


class _Rest:
    def __init__(self):
        self.posts: list = []

    def request(self, method, path, body=None, auth=True):
        return {}

    def create_message(self, channel, payload):
        self.posts.append((channel, payload))
        return {"id": "posted", "channel_id": channel}


def _answer(monkeypatch, peer_bot):
    monkeypatch.setenv("AWDISCORD_CONTAIN", "0")
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0")
    rest = _Rest()
    bot = gateway.Bot(TOKEN, _Agent(), app_id="app")
    bot.rest = rest
    bot.handle_message({"user": "777", "guild_id": "g1", "channel_id": "c1", "id": "m1",
                        "content": "<@1> rematch?", "bot_id": "1", "peer_bot": peer_bot})
    replies = [p for c, p in rest.posts if c == "c1" and p.get("message_reference")]
    assert replies, rest.posts
    return replies[0]


def test_the_reply_to_a_peer_pings_it(monkeypatch):
    mentions = _answer(monkeypatch, peer_bot=True)["allowed_mentions"]
    assert mentions.get("replied_user") is True
    assert mentions.get("parse") == []                          # still no @everyone / roles


def test_the_reply_to_a_member_does_not_ping(monkeypatch):
    mentions = _answer(monkeypatch, peer_bot=False).get("allowed_mentions") or {}
    assert not mentions.get("replied_user")


# -- through the gateway: what dispatch_message does with a real-shaped message --

_ME = SimpleNamespace(id="1")


def _peer_message(*, author="777", mention=True, guild=True):
    return SimpleNamespace(
        id="m1", content="<@1> rematch?" if mention else "gg everyone",
        guild=SimpleNamespace(id="g1", name="arena") if guild else None,
        channel=SimpleNamespace(id="c1", parent_id="", type=0),
        author=SimpleNamespace(id=author, bot=True, name="marjery",
                               display_name="Marjery", roles=[]),
        mentions=[_ME] if mention else [], role_mentions=[], attachments=[],
        embeds=[], reference=None)


def _dispatched(bot, **kw):
    seen: list = []
    bot.handle_message = seen.append
    asyncio.run(bot.dispatch_message(_peer_message(**kw), _ME))
    return seen


def _peer_bot(turns=2):
    bot = gateway.Bot(TOKEN, _Agent(), app_id="app")
    bot.rest = _Rest()
    bot.peers = PeerBots(["777"], turns=turns, window_s=60)
    return bot


def test_dispatch_hands_an_addressing_peer_to_chat_with_the_ping_flag():
    bot = _peer_bot()
    [msg] = _dispatched(bot)
    assert msg["peer_bot"] is True and msg["user"] == "777"
    assert len(bot.peers._seen["c1"]) == 1                      # one turn spent


def test_dispatch_spends_no_turn_on_a_peer_line_that_does_not_address_us():
    bot = _peer_bot()
    assert _dispatched(bot, mention=False) == []
    assert bot.peers._seen == {}                                # admit() never ran


def test_dispatch_stops_at_the_budget_and_a_refused_line_spends_nothing():
    bot = _peer_bot(turns=1)
    assert len(_dispatched(bot)) == 1
    assert _dispatched(bot) == []                               # the volley ends
    assert len(bot.peers._seen["c1"]) == 1


def test_dispatch_never_answers_an_unnamed_bot_or_a_peer_dm():
    bot = _peer_bot()
    assert _dispatched(bot, author="888") == []
    assert _dispatched(bot, guild=False) == []
    assert bot.peers._seen == {}


def test_a_peer_mention_end_to_end_gets_a_reply_that_pings_it(monkeypatch):
    monkeypatch.setenv("AWDISCORD_CONTAIN", "0")
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0")
    bot = _peer_bot()
    asyncio.run(bot.dispatch_message(_peer_message(), _ME))
    replies = [p for c, p in bot.rest.posts if c == "c1" and p.get("message_reference")]
    assert replies and replies[0]["allowed_mentions"]["replied_user"] is True
