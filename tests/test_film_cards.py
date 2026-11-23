"""Film question cards: one card per open question, answered with a button press.

The instance is a fake that keeps a proposal's questions and records every request;
Discord is a fake REST client that records what was posted and followed up. Nothing
here is a real customer, channel or script: every id and sentence is made up.
"""
from __future__ import annotations

import importlib
import io
import json
import urllib.error

import pytest
from awdiscord.builtin import film
from awdiscord.plugins import MODAL, build_host
from awdiscord.rest import RestError
from awdiscord.store import GuildStore

GUILD, CHANNEL, MEMBER, STRANGER = "900", "111", "222", "333"
PROPOSAL = "p9"
LONG = ("keep the temporary walkway in the second step exactly where the drawing shows "
        "it, and move the fence line to the far side of the loading bay")


def question(n: int, options=None, **extra) -> dict:
    return {"id": f"s{n}-unmatched-{n:08x}", "step": n, "kind": "unmatched",
            "quote": f"sample element {n}",
            "question": f"Step {n} names an element the scene does not have. What should "
                        "the film show?",
            "options": options or [f"show it as item {n}", "add it, marked approximate",
                                   "narration only"], **extra}


class Forge:
    """The customer's instance: a proposal with questions, the read route and the ops."""

    def __init__(self, questions, proposal=PROPOSAL, route=True, fail=None):
        self.questions = [dict(q) for q in questions]
        self.proposal, self.route, self.fail = proposal, route, dict(fail or {})
        self.calls: list = []

    def _open(self) -> int:
        return sum(1 for q in self.questions if not q.get("answer"))

    def _steer(self, body: dict) -> dict:
        q = next(x for x in self.questions if x["id"] == body["question"])
        choice = str(body["choice"])
        if choice.isdigit() and 1 <= int(choice) <= len(q["options"]):
            choice = q["options"][int(choice) - 1]
        q["answer"] = {"choice": choice, "by": body.get("by"), "at": "now"}
        return {"id": self.proposal, "status": "pending",
                "plan": {"questions": self.questions, "open_questions": self._open()}}

    def open(self, req, timeout=None):
        name = req.full_url.split("?")[0].rsplit("/", 1)[-1]
        body = json.loads(req.data) if req.data else {}
        self.calls.append({"method": req.get_method(), "name": name, "url": req.full_url,
                           "body": body})
        code = self.fail.get(name)
        if code == 0:
            raise urllib.error.URLError("connection refused")
        if code:
            err = io.BytesIO(json.dumps({"ok": False, "error": "the plan is locked"}).encode())
            raise urllib.error.HTTPError(req.full_url, code, "refused", {}, err)
        if name == "questions":
            if not self.route:
                raise urllib.error.HTTPError(req.full_url, 404, "no route", {},
                                             io.BytesIO(b"{}"))
            out = {"ok": True, "proposal": self.proposal, "status": "pending",
                   "questions": self.questions, "open": self._open()}
        elif name == "film_steer_status":
            out = {"ok": True, "meta": {"result": {"pending": {"id": self.proposal}}}}
        elif name == "film_plan_steps":
            out = {"ok": True, "meta": {"result": {"doc_id": self.proposal,
                                                   "questions": self.questions}}}
        else:
            out = {"ok": True, "meta": {"result": self._steer(body)}}
        return io.BytesIO(json.dumps(out).encode())

    def named(self, name: str) -> list:
        return [c for c in self.calls if c["name"] == name]


class Rest:
    """The bot's REST client: what was posted, and what was said privately."""

    def __init__(self, refuse_after: int | None = None):
        self.posted: list = []
        self.followups: list = []
        self.refuse_after = refuse_after

    def create_message(self, channel, payload):
        if self.refuse_after is not None and len(self.posted) >= self.refuse_after:
            raise RestError(403, "Missing Permissions")
        self.posted.append((str(channel), payload))
        return {"id": f"m{len(self.posted)}", "channel_id": str(channel)}

    def followup(self, app, token, payload):
        self.followups.append(payload)
        return {"id": "f1"}

    def cards(self) -> list:
        return [p for _, p in self.posted if p.get("embeds")]

    def lines(self) -> list:
        return [p["content"] for _, p in self.posted if p.get("content")]


@pytest.fixture
def mapped(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("tenant-bearer\n")
    doc = {"channels": {CHANNEL: {
        "tenant": "tnt_x", "instance": "x-forge", "url": "http://x-forge:8200",
        "project": "x-film", "members": [MEMBER], "token_file": str(token),
        "desk_url": "https://desk.example/film", "name": "Example Builders"}}}
    path = tmp_path / "map.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setenv(film.MAP_ENV, str(path))
    return path, doc


@pytest.fixture
def world(mapped, monkeypatch):
    """``make(questions, ...)`` -> (host, forge, rest), wired the way the bot wires them."""
    def make(questions, store=None, rest=None, **forge_kw):
        forge = Forge(questions, **forge_kw)
        monkeypatch.setattr(film, "_opener", lambda: forge)
        host = build_host(store or GuildStore(":memory:"))
        host.rest = rest or Rest()
        film._ready(host)
        return host, forge, host.rest
    yield make
    film._HOST.clear()


def ask(host, **args) -> str:
    tool = next(t for t in host.tools_for(GUILD, MEMBER, channel=CHANNEL)
                if t.name == "film_questions")
    return tool.run(args)


def buttons(card: dict) -> list:
    return [b for row in card["components"] for b in row["components"]]


def press(host, card: dict, label_starts: str, user: str = MEMBER, name: str = "Dana"):
    """Press the button whose label starts with ``label_starts`` on a posted card."""
    button = next(b for b in buttons(card) if b["label"].startswith(label_starts))
    return host.handle_button({
        "type": 3, "id": "i1", "token": "tok", "application_id": "app",
        "guild_id": GUILD, "channel_id": CHANNEL,
        "member": {"user": {"id": user, "global_name": name}, "permissions": "0"},
        "data": {"custom_id": button["custom_id"]},
        "message": {"id": "m1", "embeds": card["embeds"], "components": card["components"]}})


def submit(host, card: dict, modal: dict, words: str, user: str = MEMBER):
    return host.handle_modal({
        "type": 5, "id": "i2", "token": "tok", "application_id": "app",
        "guild_id": GUILD, "channel_id": CHANNEL,
        "member": {"user": {"id": user, "global_name": "Dana"}, "permissions": "0"},
        "data": {"custom_id": modal["custom_id"], "components": [
            {"type": 1, "components": [{"custom_id": "answer", "value": words}]}]},
        "message": {"id": "m1", "embeds": card["embeds"], "components": card["components"]}})


def private(result) -> str:
    data = result.response["data"]
    assert data.get("flags") == film.EPHEMERAL
    return data["content"]


# -- the cards ---------------------------------------------------------------------------


def test_a_card_has_one_button_per_option_and_an_other_button(world):
    host, _, rest = world([question(4), question(7)])
    out = ask(host)
    first, second = rest.cards()
    assert first["embeds"][0]["title"] == "Question 1 of 2 · Step 4"
    assert second["embeds"][0]["title"] == "Question 2 of 2 · Step 7"
    assert [b["label"] for b in buttons(first)] == [
        "A. show it as item 4", "B. add it, marked approximate", "C. narration only",
        "Other…"]
    body = first["embeds"][0]["description"]
    assert body.startswith("Step 4 names an element the scene does not have.")
    assert 'In the script: "sample element 4"' in body
    assert "A. show it as item 4\nB. add it, marked approximate\nC. narration only" in body
    assert first["allowed_mentions"] == {"parse": []} and "content" not in first
    assert all(channel == CHANNEL for channel, _ in rest.posted)
    assert "Do NOT repeat the questions" in out and "2 question card(s)" in out


def test_a_question_spanning_steps_names_them_all(world):
    host, _, rest = world([question(3, steps=[3, 8])])
    ask(host)
    assert rest.cards()[0]["embeds"][0]["title"] == "Question 1 of 1 · Steps 3, 8"


def test_a_long_option_is_cut_on_the_button_and_whole_in_the_body(world):
    host, _, rest = world([question(2, options=[LONG, "leave it out"])])
    ask(host)
    card = rest.cards()[0]
    label = buttons(card)[0]["label"]
    assert len(f"A. {LONG}") > film.LABEL_MAX
    assert len(label) == film.LABEL_MAX and label.endswith("…")
    assert label.startswith("A. keep the temporary walkway")
    assert f"A. {LONG}" in card["embeds"][0]["description"]     # nothing is lost


def test_every_custom_id_fits_discord_and_long_ids_are_hashed(world):
    long_q = question(5)
    long_q["id"] = "s5-" + "a-very-long-question-id-" * 6          # 147 characters
    host, forge, rest = world([long_q], proposal="proposal/2026 11 22 with spaces")
    ask(host)
    card = rest.cards()[0]
    for b in buttons(card):
        assert len(b["custom_id"]) <= 100 and b["custom_id"].startswith("awp:film:")
        assert long_q["id"] not in b["custom_id"]
    press(host, card, "B.").deferred()
    steer = forge.named("film_steer")[0]["body"]
    # The hashed ids were found again by re-reading the questions.
    assert steer["question"] == long_q["id"]
    assert steer["proposal_id"] == "proposal/2026 11 22 with spaces"
    assert steer["choice"] == "2"


def test_at_most_five_cards_a_call_and_the_rest_are_counted(world):
    host, _, rest = world([question(n) for n in range(1, 22)])
    out = ask(host)
    assert len(rest.cards()) == 5
    assert rest.lines() == ["16 more: say 'more questions'."]
    assert "16 more are waiting" in out
    assert [c["embeds"][0]["title"].split(" · ")[0] for c in rest.cards()] == [
        f"Question {n} of 21" for n in range(1, 6)]

    ask(host)                                   # "more questions": the next five
    assert [c["embeds"][0]["title"].split(" · ")[0] for c in rest.cards()[5:]] == [
        f"Question {n} of 21" for n in range(6, 11)]
    assert rest.lines()[-1] == "11 more: say 'more questions'."


def test_start_continues_from_a_question_number(world):
    host, _, rest = world([question(n) for n in range(1, 9)])
    ask(host, start=7)
    assert [c["embeds"][0]["title"].split(" · ")[0] for c in rest.cards()] == [
        "Question 7 of 8", "Question 8 of 8"]
    assert rest.lines() == []                   # nothing remains after number 8


def test_the_last_batch_has_no_remainder_line_and_asking_again_reposts_open_ones(world):
    host, _, rest = world([question(n) for n in range(1, 4)])
    assert "That is every open question" in ask(host)
    assert len(rest.cards()) == 3 and rest.lines() == []
    ask(host)                                   # every open one already has a card
    assert len(rest.cards()) == 6


def test_answered_questions_get_no_card(world):
    done = question(1, answer={"choice": "narration only", "by": "discord:1", "at": "t"})
    host, _, rest = world([done, question(2)])
    ask(host)
    (card,) = rest.cards()
    assert card["embeds"][0]["title"] == "Question 2 of 2 · Step 2"


def test_no_open_question_posts_nothing(world):
    done = question(1, answer={"choice": "narration only"})
    host, _, rest = world([done])
    assert "Every question on the pending proposal is answered" in ask(host)
    host2, _, rest2 = world([])
    assert "no proposal with questions" in ask(host2)
    assert rest.posted == [] and rest2.posted == []


def test_a_discord_refusal_midway_is_reported_honestly(world):
    host, _, rest = world([question(n) for n in range(1, 8)], rest=Rest(refuse_after=2))
    out = ask(host)
    assert len(rest.cards()) == 2 and rest.lines() == []
    assert "Posted 2 question card(s)" in out and "Discord refused the rest" in out


def test_the_questions_come_from_the_plan_op_when_the_read_route_is_missing(world):
    host, forge, rest = world([question(1)], route=False)
    ask(host)
    assert len(rest.cards()) == 1
    assert [c["name"] for c in forge.calls] == ["questions", "film_steer_status",
                                                "film_plan_steps"]
    assert forge.named("film_plan_steps")[0]["body"]["proposal_id"] == PROPOSAL


def test_an_instance_that_is_down_is_asked_once_and_nothing_is_posted(world):
    host, forge, rest = world([question(1)], fail={"questions": 0})
    out = ask(host)
    assert "could not read the film's questions" in out and rest.posted == []
    assert [c["name"] for c in forge.calls] == ["questions"]


def test_only_a_member_in_the_mapped_channel_is_offered_the_tool(world):
    host, _, _ = world([question(1)])
    names = {t.name for t in host.tools_for(GUILD, STRANGER, channel=CHANNEL)}
    elsewhere = {t.name for t in host.tools_for(GUILD, MEMBER, channel="999")}
    assert "film_questions" not in names | elsewhere
    assert "changed nothing" in film.film_questions(caller={"user": STRANGER,
                                                            "channel": CHANNEL})


def test_the_descriptions_send_the_model_to_the_cards():
    by_name = {t.name: t.description for t in film.TOOLS}
    assert "what do you need from me" in by_name["film_questions"]
    assert "NEVER list the questions" in by_name["film_questions"]
    assert "film_questions" in by_name["film_steer_status"]
    assert "film_questions" in film.summarize({"open_questions": 21})


# -- a press -----------------------------------------------------------------------------


def test_a_member_press_records_the_answer_and_edits_the_card(world):
    host, forge, rest = world([question(4), question(7)])
    ask(host)
    card = rest.cards()[0]
    result = press(host, card, "B.")
    # Acknowledged at once; the instance is only called in the deferred half.
    assert result.response == {"type": film.DEFERRED_UPDATE}
    assert forge.named("film_steer") == []
    edited = result.deferred()

    (steer,) = forge.named("film_steer")
    assert steer["body"] == {"proposal_id": PROPOSAL, "action": "answer",
                             "question": "s4-unmatched-00000004", "choice": "2",
                             "project": "x-film", "by": f"discord:{MEMBER}"}
    embed = edited["embeds"][0]
    assert embed["title"] == "Question 1 of 2 · Step 4"
    assert embed["description"].endswith("Answered: B. add it, marked approximate by Dana")
    assert "Pick one below" not in embed["description"]
    assert [b["label"] for b in buttons(edited)] == ["Change"]      # the options are gone
    assert rest.followups == [] and rest.lines() == []              # one still open


def test_a_press_by_someone_else_records_nothing(world):
    host, forge, rest = world([question(4)])
    ask(host)
    forge.calls.clear()
    result = press(host, rest.cards()[0], "A.", user=STRANGER, name="Sam")
    assert private(result) == ("Only Example Builders team members can answer. "
                               "Nothing was recorded.")
    assert result.deferred is None and forge.calls == []
    # "Other" opens no form for them either.
    assert "Only Example Builders team members" in private(
        press(host, rest.cards()[0], "Other", user=STRANGER))


def test_an_instance_refusal_leaves_the_card_as_it_was(world):
    host, forge, rest = world([question(4)])
    ask(host)
    card = rest.cards()[0]
    forge.fail["film_steer"] = 409
    edited = press(host, card, "A.").deferred()
    assert "embeds" not in edited and "content" not in edited
    assert edited["components"] == card["components"]              # same buttons
    (said,) = rest.followups
    assert said["flags"] == film.EPHEMERAL
    assert said["content"] == ("The film desk did not let me record your answer: the plan "
                               "is locked. Nothing was recorded.")
    assert "x-forge" not in said["content"] and "film_steer" not in said["content"]


def test_an_instance_outage_leaves_the_card_as_it_was(world):
    host, forge, rest = world([question(4)])
    ask(host)
    card = rest.cards()[0]
    forge.calls.clear()
    forge.fail["questions"] = 0
    edited = press(host, card, "A.").deferred()
    assert edited["components"] == card["components"] and "embeds" not in edited
    assert "not answering right now" in rest.followups[0]["content"]
    assert forge.named("film_steer") == []


def test_a_card_works_after_a_restart_from_its_custom_id_alone(world, monkeypatch):
    host, _, rest = world([question(4), question(7)])
    ask(host)
    card = json.loads(json.dumps(rest.cards()[1]))      # only what Discord holds
    film._HOST.clear()
    importlib.reload(film)                              # a fresh plugin object
    host2, forge2, rest2 = world([question(4), question(7)])
    assert host2 is not host and host2.plugins["film"] is film.PLUGIN

    edited = press(host2, card, "C.").deferred()
    (steer,) = forge2.named("film_steer")
    assert steer["body"]["question"] == "s7-unmatched-00000007"
    assert steer["body"]["choice"] == "3"
    assert "Answered: C. narration only by Dana" in edited["embeds"][0]["description"]
    assert rest2.cards() == []                          # nothing had to be re-posted


def test_the_all_answered_line_is_said_once(world):
    host, forge, rest = world([question(1), question(2)])
    ask(host)
    one, two = rest.cards()
    press(host, one, "A.").deferred()
    assert rest.lines() == []                           # one question is still open
    answered = press(host, two, "A.").deferred()
    assert rest.lines() == [
        "All questions are answered. Next: the plan can be approved; a signed-in admin "
        "does that on the film desk: <https://desk.example/film>."]

    # Changing an answer and answering again does not say it twice.
    reopened = press(host, answered, "Change").deferred()
    press(host, reopened, "B.").deferred()
    assert len(rest.lines()) == 1
    assert len(forge.named("film_steer")) == 3


def test_change_brings_the_choices_back_without_recording(world):
    host, forge, rest = world([question(4)])
    ask(host)
    answered = press(host, rest.cards()[0], "A.").deferred()
    before = len(forge.named("film_steer"))
    reopened = press(host, answered, "Change").deferred()
    assert [b["label"][:2] for b in buttons(reopened)] == ["A.", "B.", "C.", "Ot"]
    assert "Answered:" not in reopened["embeds"][0]["description"]
    assert reopened["embeds"][0]["title"] == "Question 1 of 1 · Step 4"
    assert len(forge.named("film_steer")) == before


def test_other_opens_a_form_and_the_typed_words_are_the_answer(world):
    host, forge, rest = world([question(4), question(7)])
    ask(host)
    card = rest.cards()[0]
    form = press(host, card, "Other")
    assert form.response["type"] == MODAL and forge.named("film_steer") == []
    modal = form.response["data"]
    (field,) = [c for row in modal["components"] for c in row["components"]]
    assert field["custom_id"] == "answer" and field["style"] == 2
    assert field["max_length"] == film.ANSWER_MAX
    assert len(modal["custom_id"]) <= 100

    done = submit(host, card, modal, "  show the crane   on the north side ")
    assert done.response == {"type": film.DEFERRED_UPDATE}
    edited = done.deferred()
    (steer,) = forge.named("film_steer")
    assert steer["body"]["choice"] == "show the crane on the north side"
    assert steer["body"]["question"] == "s4-unmatched-00000004"
    assert "Answered: show the crane on the north side by Dana" in (
        edited["embeds"][0]["description"])

    empty = submit(host, card, modal, "   ")
    assert "Nothing was recorded" in private(empty)
    stranger = submit(host, card, modal, "anything", user=STRANGER)
    assert "Only Example Builders team members" in private(stranger)
    assert len(forge.named("film_steer")) == 1


def test_a_question_that_changed_after_the_card_records_nothing(world):
    host, forge, rest = world([question(4)])
    ask(host)
    card = rest.cards()[0]
    forge.questions[0]["options"] = ["something else entirely", "narration only"]
    edited = press(host, card, "A.").deferred()
    assert forge.named("film_steer") == []
    assert "changed after the card was posted" in rest.followups[0]["content"]
    assert edited["components"] == card["components"]


def test_a_card_from_an_earlier_proposal_records_nothing(world):
    host, forge, rest = world([question(4)])
    ask(host)
    forge.proposal = "p10"
    press(host, rest.cards()[0], "A.").deferred()
    assert forge.named("film_steer") == []
    assert "earlier version of the plan" in rest.followups[0]["content"]


def test_a_card_from_another_film_setup_records_nothing(world, mapped):
    host, forge, rest = world([question(4)])
    ask(host)
    path, doc = mapped
    doc["channels"][CHANNEL]["project"] = "another-film"       # the map was re-pointed
    path.write_text(json.dumps(doc))
    forge.calls.clear()
    result = press(host, rest.cards()[0], "A.")
    assert "earlier setup of this film" in private(result)
    assert forge.calls == []


def test_a_button_that_is_not_a_card_button_is_refused(world):
    host, forge, _ = world([question(4)])
    forge.calls.clear()
    for cid in ("awp:film:z:1:2:3", "awp:film:a:1:2:3", "awp:film:a:k:p:q:x:d"):
        result = host.handle_button({
            "type": 3, "guild_id": GUILD, "channel_id": CHANNEL,
            "member": {"user": {"id": MEMBER}, "permissions": "0"},
            "data": {"custom_id": cid}})
        assert result.deferred is None and result.response["data"]["flags"] == film.EPHEMERAL
    assert forge.calls == []
