"""Conversation wrap-ups: grounding, the floor, the 3-second ack, buttons and status."""
from __future__ import annotations

import json
import time

import pytest
from awdiscord import wrapup
from awdiscord.builtin import wrapup as plug
from awdiscord.dispatch import DEFERRED, EPHEMERAL, MESSAGE
from awdiscord.plugins import PluginHost
from awdiscord.store import GuildStore

GUILD, CHAN, GENERAL, BOT = "100", "200", "300", "999"
ANA, BEN, CAL = "11", "22", "33"
NAMES = {ANA: "Ana", BEN: "Ben", CAL: "Cal Smith"}
BASE = wrapup.snowflake_at(time.time() - 3600)


def msg(k: int, who: str, text: str, **extra) -> dict:
    return {"id": str(BASE + k * (1 << 22) * 1000), "type": 0, "content": text,
            "author": {"id": who, "global_name": NAMES.get(who, who), "username": who},
            **extra}


CONVO = [
    msg(1, ANA, "I think the shared calendar should be free for small teams."),
    msg(2, BEN, "who pays for hosting it though?"),
    msg(3, CAL, "I'll draft a hosting budget by Friday."),
    msg(4, ANA, "I should ask the printers co-op if they want in."),
    msg(5, BEN, "here is the old doc https://example.org/plan"),
    msg(6, BOT, "The application did not respond", author={"id": BOT, "bot": True}),
]


def model_answer() -> str:
    return json.dumps({
        "positions": [
            {"who": "Ana", "text": "Ana says the shared calendar should be free for small "
                                   "teams.", "cite": [1]},
            {"who": "Ben", "text": "We agree this is clearly the right call.", "cite": [2]},
            {"who": "Dana", "text": "Dana says it is a great plan.", "cite": [1]},
            {"who": "Ben", "text": "Ben says hosting costs money.", "cite": [99]},
        ],
        "agreement": [],
        "open_questions": [{"who": "Ben", "text": "Who pays for hosting?", "cite": [2]}],
        "resolutions": [{"text": "Draft a hosting budget before deciding on pricing.",
                         "cite": [3]}],
        "due_outs": [
            {"owner": "Cal", "action": "Draft a hosting budget", "due": "Friday",
             "cite": [3]},
            {"owner": "Ana", "action": "Ask the printers co-op to join", "due": "Monday",
             "cite": [4]},
            {"owner": "Ben", "action": "Write the pricing page", "due": "", "cite": [1]},
        ],
    })


class Rest:
    def __init__(self, history=None):
        self.history = history if history is not None else CONVO
        self.calls: list = []
        self.posted: list = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path))
        if method == "GET" and "/messages?" in path:
            after = int(path.split("after=")[1].split("&")[0])
            return [m for m in reversed(self.history) if int(m["id"]) > after]
        if method == "GET" and path.endswith("/channels"):
            return [{"id": GENERAL, "type": 0, "name": "general"}]
        if method == "GET" and path.startswith("/channels/"):
            return {"id": CHAN, "name": "planning"}
        return {}

    def create_message(self, channel, payload):
        self.posted.append((channel, payload))
        return {"id": "5000", "channel_id": channel}

    def edit_original(self, app, token, payload):
        self.calls.append(("EDIT", payload.get("content", "")))


class Agent:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.prompts = answer, exc, []

    def ask(self, prompt, *, user="", **_):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.answer if self.answer is not None else model_answer()


def lines():
    return wrapup.lines_of(CONVO, bot_id=BOT)


def built(agent=None):
    agent = agent or Agent()
    return wrapup.build(lines(), lambda p: agent.ask(p), channel=CHAN, guild=GUILD)


# ── grounding ─────────────────────────────────────────────────────────────────

def test_bots_are_not_participants():
    assert [ln.author_id for ln in lines()] == [ANA, BEN, CAL, ANA, BEN]


def test_invented_people_uncited_items_and_opinions_are_dropped():
    w = built()
    texts = " ".join(i.text for i in w.positions)
    assert "Dana" not in texts and "clearly" not in texts
    assert [i.who for i in w.positions] == [ANA]
    assert w.dropped["unknown person"] == 1
    assert w.dropped["uncited"] == 1
    assert w.dropped["not neutral"] == 1


def test_a_due_out_owner_must_be_the_author_of_the_cited_message():
    w = built()
    owners = {i.who: i for i in w.due_outs}
    assert BEN not in owners                         # cited Ana's message
    assert w.dropped["person not the author of the cited message"] == 1


def test_a_date_survives_only_when_the_owner_said_it():
    w = built()
    owners = {i.who: i for i in w.due_outs}
    assert owners[CAL].due == "Friday"               # "by Friday" is in Cal's message
    assert owners[ANA].due == wrapup.DATE_MISSING    # nobody said Monday


def test_the_floor_adds_commitments_the_model_missed():
    answer = json.loads(model_answer())
    answer["due_outs"] = []
    w = built(Agent(json.dumps(answer)))
    assert {i.who for i in w.due_outs} == {CAL, ANA}
    assert all(i.due == wrapup.DATE_MISSING for i in w.due_outs)
    assert all(i.source == "floor" for i in w.due_outs)


def test_no_model_still_gives_quoted_grounded_wrapup():
    w = built(Agent(exc=RuntimeError("down")))
    assert w.model.startswith("unavailable")
    assert {i.who for i in w.positions} == {ANA, BEN, CAL}
    assert any(i.who == BEN for i in w.open_questions)
    assert {i.who for i in w.due_outs} == {CAL, ANA}


def test_links_come_from_the_messages():
    assert [x["url"] for x in built().links] == ["https://example.org/plan"]


def test_model_cannot_ping_anyone():
    answer = json.loads(model_answer())
    answer["resolutions"] = [{"text": "<@11> @everyone vote now", "cite": [3]}]
    w = built(Agent(json.dumps(answer)))
    assert "<@" not in w.resolutions[0].text and "@everyone" not in w.resolutions[0].text


def test_payload_fits_discord_limits_and_invites_replies():
    w = built()
    body = wrapup.payload(w)
    assert len(body["content"]) <= 2000
    assert sum(len(e["title"]) + len(e["description"]) for e in body["embeds"]) <= 6000
    assert all(len(e["description"]) <= 4096 for e in body["embeds"])
    assert "Chime in" in body["content"]
    assert body["allowed_mentions"] == {"parse": []}


def test_parse_since():
    s = wrapup.parse_since("https://discord.com/channels/1/2/3")
    assert (s.guild, s.channel, s.anchor) == ("1", "2", "3")
    assert wrapup.parse_duration("8h30m") == 8 * 3600 + 1800
    assert wrapup.parse_since("2h", now=1_800_000_000).label == "the last 2h"
    with pytest.raises(wrapup.WrapupError):
        wrapup.parse_since("yesterday-ish")


def test_wants_wrapup():
    assert plug.wants_wrapup("<@999> wrap this up")
    assert plug.wants_wrapup("@Aither can you wrap up the discussion")
    assert not plug.wants_wrapup("<@999> what is a wrapper")


# ── the plugin ────────────────────────────────────────────────────────────────

def host_with(tmp_path, agent=None, operators=()):
    host = PluginHost(GuildStore(tmp_path / "g.db"), agent=agent or Agent(),
                      operators=set(operators), plugins=[plug.PLUGIN])
    host.rest = Rest()
    return host


def interaction(*, preview=False, perms=(1 << 10) | (1 << 16), user=ANA):
    opts = [{"type": 5, "name": "preview", "value": True}] if preview else []
    return {"id": "1", "token": "tok", "application_id": BOT, "type": 2,
            "guild_id": GUILD, "channel_id": CHAN,
            "member": {"user": {"id": user}, "permissions": str(perms)},
            "data": {"name": "aither", "options": [
                {"type": 2, "name": "wrapup", "options": [
                    {"type": 1, "name": "now", "options": opts}]}]}}


def test_the_interaction_is_acknowledged_before_any_slow_work(tmp_path):
    agent = Agent()
    host = host_with(tmp_path, agent)
    started = time.monotonic()
    res = host.handle(interaction(preview=True))
    assert time.monotonic() - started < 0.5
    assert res.response["type"] == DEFERRED
    assert res.response["data"]["flags"] == EPHEMERAL      # the preview is private
    assert host.rest.calls == [] and agent.prompts == []   # nothing read before the ack
    out = res.deferred()
    assert out["content"].startswith("**Preview - only you can see this.**")
    assert host.rest.posted == []                          # a preview posts nothing
    assert any(c[0] == "EDIT" and "Read 5 messages" in c[1] for c in host.rest.calls)


def test_a_busy_queue_edits_in_progress_then_answers(tmp_path, monkeypatch):
    calls = {"n": 0}

    class Busy(Agent):
        def ask(self, prompt, *, user="", **_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("model queue is busy (503)")
            return model_answer()
    monkeypatch.setattr(wrapup.time, "sleep", lambda s: None)
    host = host_with(tmp_path, Busy())
    out = host.handle(interaction(preview=True)).deferred()
    assert calls["n"] == 2 and "Preview" in out["content"]
    assert any(c[0] == "EDIT" and "queue is busy" in c[1] for c in host.rest.calls)


def test_posting_needs_staff(tmp_path):
    host = host_with(tmp_path)
    res = host.handle(interaction(preview=False))
    assert res.response["type"] == MESSAGE and "staff" in res.response["data"]["content"]


def test_staff_post_goes_top_level_to_general_with_buttons(tmp_path):
    host = host_with(tmp_path)
    staff = (1 << 10) | (1 << 16) | (1 << 13)
    out = host.handle(interaction(preview=False, perms=staff)).deferred()
    channel, body = host.rest.posted[0]
    assert channel == GENERAL
    assert "message_reference" not in body                 # never a reply/thread
    assert body["components"] and "Wrap-up posted" in out["content"]
    status = plug.status_text(host, GUILD)
    assert f"<@{CAL}>" in status and "Friday" in status and "0/2 done" in status


def press(host, payload, user, perms=0):
    return plug.PLUGIN.on_button(
        type("C", (), {"guild": GUILD, "user": user, "permissions": perms, "host": host,
                       "reply": lambda self, t, private=False: ("reply", t),
                       "update": lambda self, t, embeds=None, components=None:
                       ("update", t, embeds, components)})(), payload)


def test_only_the_owner_or_staff_closes_an_item(tmp_path):
    host = host_with(tmp_path)
    host.handle(interaction(preview=False, perms=(1 << 13) | (1 << 10) | (1 << 16))).deferred()
    token = plug._store(host, GUILD).get(plug.INDEX)[-1]
    rec = plug._store(host, GUILD).get(plug.RECORD + token)
    n_cal = next(i["n"] for i in rec["items"] if i["owner"] == CAL)
    refused = press(host, f"d:{token}:{n_cal}", BEN)
    assert refused[0] == "reply" and "named owner" in refused[1]
    done = press(host, f"d:{token}:{n_cal}", CAL)
    assert done[0] == "update"
    assert "1/2 done" in plug.status_text(host, GUILD)
    by_staff = press(host, f"c:{token}:{3 - n_cal}", BEN, perms=1 << 13)
    assert by_staff[0] == "update"
