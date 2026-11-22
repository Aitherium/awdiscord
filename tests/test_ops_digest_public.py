"""An operator's ops digest never lands in a channel everyone can read.

Measured in a community thread: the operator asked a twin bot something during a
banter session and the bot posted the verbatim ops digest into the public channel:
open decision cards (one naming a mail account), the agents relay's alerts and the
fleet roster. Who asked decides who may SEE it; the channel decides who WOULD.
"""
from __future__ import annotations

import pathlib

import pytest
from awdiscord import gateway, quiet
from awdiscord.rest import RestError

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped

DIGEST = (
    "Decision cards: 24 open, 10 urgent, oldest waiting 6.0 days.\n"
    "Most recent open: Mail bridge refused the stored password for someone@example.com\n\n"
    "Relay #agents: last 5 messages (oldest first):\n"
    "[2026-11-18T01:15:08] ops: [alert] fleet roll REFUSED for a worker\n\n"
    "Agent fleet roster (registry): 63 agents (63 registered).\n"
    "Heartbeat telemetry (scheduler, NOT the roster size): 12 live\n"
)


def test_the_digest_shapes_are_recognised():
    assert quiet.carries_ops_digest(DIGEST)
    for line in DIGEST.split("\n\n"):
        assert quiet.carries_ops_digest(line), line
    assert quiet.carries_ops_digest("ok", None, "  Relay #ops-room: last 1 message\nx")


def test_ordinary_talk_is_not_a_digest():
    for text in ("Decision cards are how the owner approves things.",
                 "the relay is down, try again", "who is on the roster for tonight?",
                 "Decision cards: none today", ""):
        assert not quiet.carries_ops_digest(text), text


class _Agent:
    name = "atlas"
    personas: dict = {}

    def __init__(self, answer):
        self.answer = answer

    def ask(self, prompt, **kw):
        return self.answer


class _Rest:
    def __init__(self, dm_closed=False):
        self.dm_closed = dm_closed
        self.posts: list = []

    def request(self, method, path, body=None, auth=True):
        return {}

    def dm_channel(self, user_id):
        if self.dm_closed:
            raise RestError(403, "Cannot send messages to this user")
        return {"id": "dm-" + user_id}

    def create_message(self, channel, payload):
        self.posts.append((channel, payload))
        return {"id": "posted", "channel_id": channel}

    def texts(self, channel):
        return [p.get("content", "") for c, p in self.posts if c == channel]


def _run(monkeypatch, *, public, privileged, answer=DIGEST, dm_closed=False):
    monkeypatch.setenv("AWDISCORD_CONTAIN", "0")
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0")
    monkeypatch.setenv("AWDISCORD_PUBLIC_REPLY_CHARS", "0")
    rest = _Rest(dm_closed=dm_closed)
    bot = gateway.Bot(TOKEN, _Agent(answer), app_id="app")
    bot.rest = rest
    monkeypatch.setattr(bot, "_quiet_privileged", lambda msg: privileged)
    bot.handle_message({"user": "op1", "guild_id": "g1", "channel_id": "c1", "id": "m1",
                        "content": "<@1> battlebots!", "bot_id": "1", "public": public})
    return rest


def test_public_channel_operator_gets_it_by_dm_and_the_room_gets_one_line(monkeypatch):
    rest = _run(monkeypatch, public=True, privileged=True)
    assert rest.texts("c1") == [quiet.OPS_TO_DM]
    sent = "".join(rest.texts("dm-op1"))
    assert "Decision cards: 24 open" in sent and "Relay #agents" in sent


def test_public_channel_anyone_else_gets_only_the_line(monkeypatch):
    rest = _run(monkeypatch, public=True, privileged=False)
    assert rest.texts("c1") == [quiet.OPS_WITHHELD]
    assert rest.texts("dm-op1") == []


def test_dms_closed_withholds_instead_of_posting_it_here(monkeypatch):
    rest = _run(monkeypatch, public=True, privileged=True, dm_closed=True)
    assert rest.texts("c1") == [quiet.OPS_WITHHELD]


def test_a_private_channel_still_gets_the_digest(monkeypatch):
    rest = _run(monkeypatch, public=False, privileged=True)
    assert any("Decision cards: 24 open" in t for t in rest.texts("c1"))


def test_ordinary_answers_in_public_are_untouched(monkeypatch):
    rest = _run(monkeypatch, public=True, privileged=True, answer="Ring's open. Gloves on.")
    assert rest.texts("c1") == ["Ring's open. Gloves on."]


_TOOLS = (pathlib.Path(__file__).resolve().parents[3]
          / "lib" / "orchestration" / "fleet_ops_tools.py")


@pytest.mark.skipif(not _TOOLS.is_file(), reason="the fleet tools ship in the monorepo only")
def test_the_guard_still_matches_what_the_fleet_tools_print():
    # A rename on the producing side must fail here, not silently re-open the leak.
    src = _TOOLS.read_text(encoding="utf-8")
    for marker in ("Decision cards: ", "Agent fleet roster (", "Heartbeat telemetry ("):
        assert marker in src, marker
