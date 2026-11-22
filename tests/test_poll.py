"""/poll: native Discord polls when they fit, a readable embed poll when they do not."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord.agent import EchoAgent  # noqa: E402
from awdiscord.builtin import community  # noqa: E402
from awdiscord.dispatch import MESSAGE, UPDATE, Dispatcher  # noqa: E402
from awdiscord.plugins import MODAL, build_host  # noqa: E402
from awdiscord.ratelimit import RateLimiter  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

CAN_POLL = str(community.SEND_POLLS | (1 << 11))
NO_POLL = str(1 << 11)


@pytest.fixture(autouse=True)
def _no_user_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AWDISCORD_PLUGIN_DIRS", raising=False)


@pytest.fixture
def disp():
    agent = EchoAgent()
    host = build_host(GuildStore(":memory:"), agent=agent)
    lim = RateLimiter(per_user=(1000, 1.0), per_guild=(1000, 1.0), global_=(1000, 1.0))
    return Dispatcher(agent, lim, None, host=host)


def _it(perms=CAN_POLL, **opts):
    return {"type": 2, "id": "1", "token": "tok", "application_id": "app",
            "guild_id": "g1", "channel_id": "c1", "app_permissions": perms,
            "member": {"user": {"id": "7"}, "permissions": "0"},
            "data": {"name": "poll", "options": [{"name": k, "value": v}
                                                  for k, v in opts.items()]}}


def _press(disp, cid, user, roles=(), message="m1"):
    return disp.handle({"type": 3, "guild_id": "g1", "channel_id": "c1",
                        "message": {"id": message},
                        "member": {"user": {"id": user}, "roles": list(roles)},
                        "data": {"custom_id": cid}})


def _buttons(res):
    return [b for row in res.response["data"]["components"] for b in row["components"]]


# -- input --------------------------------------------------------------------

@pytest.mark.parametrize("text,question,choices", [
    ("Lunch? | pizza | tacos", "Lunch?", ["pizza", "tacos"]),
    ("Lunch: pizza, tacos, sushi", "Lunch", ["pizza", "tacos", "sushi"]),
    ("Just a question", "Just a question", []),
    ("see https://x.y for it", "see https://x.y for it", []),
])
def test_parse_poll_text(text, question, choices):
    assert community.parse_poll_text(text) == (question, choices)


def test_parse_choices_strips_bullets_not_signs():
    text = "\n".join(["- one", "* two", "-5", "*bold*"])
    assert community.parse_choices(text) == ["one", "two", "-5", "*bold*"]


def test_split_emoji_unicode_and_custom():
    assert community.split_emoji("🍕 pizza") == ("pizza", {"name": "🍕"})
    assert community.split_emoji("<:party:123456789012345678> party") == (
        "party", {"id": "123456789012345678", "name": "party"})
    assert community.split_emoji("plain") == ("plain", None)
    assert community.split_emoji("C++ rocks") == ("C++ rocks", None)


def test_option_fields_build_a_native_poll(disp):
    res = disp.handle(_it(question="Colour?", option1="🟤 cinnamon", option2="sand",
                          hours=48, multi=True))
    assert res.response["type"] == MESSAGE
    poll = res.response["data"]["poll"]
    assert poll["question"] == {"text": "Colour?"}
    assert poll["answers"][0]["poll_media"] == {"text": "cinnamon", "emoji": {"name": "🟤"}}
    assert poll["answers"][1]["poll_media"] == {"text": "sand"}
    assert poll["duration"] == 48 and poll["allow_multiselect"] is True
    assert "embeds" not in res.response["data"] and "components" not in res.response["data"]


def test_whole_poll_in_the_question_box(disp):
    res = disp.handle(_it(question="Colour: cinnamon, sand"))
    answers = [a["poll_media"]["text"] for a in res.response["data"]["poll"]["answers"]]
    assert answers == ["cinnamon", "sand"]


def test_missing_choices_open_a_form_not_an_error(disp):
    res = disp.handle(_it(question="Colour?"))
    assert res.response["type"] == MODAL
    fields = [r["components"][0] for r in res.response["data"]["components"]]
    assert [f["custom_id"] for f in fields] == ["question", "choices", "hours"]
    assert fields[0]["value"] == "Colour?"


def test_form_submission_starts_the_poll(disp):
    form = disp.handle(_it(question="Colour?"))
    cid = form.response["data"]["custom_id"]
    sub = {"type": 5, "id": "2", "token": "t2", "application_id": "app", "guild_id": "g1",
           "channel_id": "c1", "app_permissions": CAN_POLL,
           "member": {"user": {"id": "7"}, "permissions": "0"},
           "data": {"custom_id": cid, "components": [
               {"type": 1, "components": [{"custom_id": "question", "value": "Colour?"}]},
               {"type": 1, "components": [{"custom_id": "choices",
                                           "value": "cinnamon\nsand\n\n"}]},
               {"type": 1, "components": [{"custom_id": "hours", "value": ""}]}]}}
    res = disp.host.handle_modal(sub)
    assert res.response["data"]["poll"]["duration"] == community.DEFAULT_HOURS
    one = dict(sub, data=dict(sub["data"], components=[
        {"type": 1, "components": [{"custom_id": "choices", "value": "only one"}]}]))
    refused = disp.host.handle_modal(one)
    assert refused.response["data"]["flags"] == 64


# -- native vs embed -------------------------------------------------------------

def test_no_send_polls_falls_back_to_the_embed(disp):
    res = disp.handle(_it(perms=NO_POLL, question="Colour? | cinnamon | sand"))
    assert "poll" not in res.response["data"]
    assert res.response["data"]["embeds"][0]["title"] == "📊 Colour?"
    assert res.log["why"] == "no SEND_POLLS"


@pytest.mark.parametrize("opts,why", [
    ({"anonymous": True, "choices": "a, b"}, "anonymous"),
    ({"choices": ",".join(f"c{i}" for i in range(11))}, "more than 10 answers"),
    ({"choices": "short, " + "x" * 60}, "an answer is longer than 55 characters"),
])
def test_what_native_cannot_do_uses_the_embed(disp, opts, why):
    res = disp.handle(_it(question="Q", **opts))
    assert res.log["poll"] == "embed" and res.log["why"] == why


def test_vote_weights_force_the_embed_and_weigh_votes(disp):
    store = community.ScopedStore(disp.host.store, "g1", "community")
    store.set("vote_weights", {"r-mod": 3})
    res = disp.handle(_it(question="Ship? | yes | no"))
    assert res.log["why"] == "weighted"
    yes, no = (b["custom_id"] for b in _buttons(res))
    _press(disp, yes, "1", roles=["r-mod"])
    after = _press(disp, no, "2")
    labels = [b["label"] for b in _buttons(after)]
    assert labels == ["yes · 3", "no · 1"]
    assert "weighted by role" in after.response["data"]["embeds"][0]["footer"]["text"]


# -- the embed card ----------------------------------------------------------------

def test_embed_card_bars_leader_and_footer(disp):
    res = disp.handle(_it(perms=NO_POLL, question="Colour?", option1="🟤 cinnamon",
                          option2="sand"))
    cin, sand = (b["custom_id"] for b in _buttons(res))
    _press(disp, cin, "1")
    _press(disp, cin, "2")
    view = _press(disp, sand, "3")
    assert view.response["type"] == UPDATE
    embed = view.response["data"]["embeds"][0]
    lines = embed["description"].splitlines()
    assert lines[0] == "🟤 **cinnamon**  ◀ leading"
    assert lines[1] == f"`{community.bar(2 / 3)}  67% 2`"
    assert lines[3] == f"`{community.bar(1 / 3)}  33% 1`"
    assert "Closes <t:" in embed["description"]
    assert embed["footer"]["text"].startswith("3 votes · one per person")
    buttons = _buttons(view)
    assert [b["style"] for b in buttons] == [3, 2]          # leader is green
    assert buttons[0]["emoji"] == {"name": "🟤"} and buttons[0]["label"] == "cinnamon · 2"
    assert buttons[1]["emoji"]["name"] == community._LETTERS[1]


def test_bar_is_fixed_width_and_eighth_precise():
    assert community.bar(0) == "░" * community.BAR_WIDTH
    assert community.bar(1) == "█" * community.BAR_WIDTH
    assert all(len(community.bar(f / 37)) == community.BAR_WIDTH for f in range(38))
    assert community.bar(0.5) == "█" * 7 + "░" * 7


def test_tap_again_takes_the_vote_back_and_multi_toggles(disp):
    res = disp.handle(_it(perms=NO_POLL, question="Q | a | b", multi=True))
    a, b = (x["custom_id"] for x in _buttons(res))
    _press(disp, a, "1")
    both = _press(disp, b, "1")
    assert [x["label"] for x in _buttons(both)] == ["a · 1", "b · 1"]
    back = _press(disp, a, "1")
    assert [x["label"] for x in _buttons(back)] == ["a · 0", "b · 1"]
    assert "1 vote · choose several" in back.response["data"]["embeds"][0]["footer"]["text"]


def test_more_than_ten_choices_fit_in_button_rows(disp):
    res = disp.handle(_it(question="Q", choices=",".join(f"c{i}" for i in range(20))))
    rows = res.response["data"]["components"]
    assert len(rows) == 4 and all(len(r["components"]) == 5 for r in rows)


# -- closing -------------------------------------------------------------------------

class _Rest:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"id": "m9", "channel_id": "c1"}
        return {}

    def edit_original(self, app_id, token, body):
        self.calls.append(("EDIT", f"{app_id}/{token}", body))


def test_sweep_learns_the_message_then_edits_in_the_final_result(disp):
    res = disp.handle(_it(perms=NO_POLL, question="Colour? | cinnamon | sand", hours=1))
    cin = _buttons(res)[0]["custom_id"]
    disp.host.rest = rest = _Rest()
    store = community.ScopedStore(disp.host.store, "g1", "community")
    key = store.keys("poll:")[0]
    poll = store.get(key)
    assert community.sweep(disp.host, "g1", now=poll["at"] + 60) == []
    assert store.get(key)["message"] == "m9" and store.get(key)["token"] == ""
    store.set(key, {**store.get(key), "votes": {"1": [0]}})
    closed = community.sweep(disp.host, "g1", now=poll["closes_at"] + 1)
    assert closed == [poll["id"]]
    method, path, body = rest.calls[-1]
    assert (method, path) == ("PATCH", "/channels/c1/messages/m9")
    assert body["components"] == []
    embed = body["embeds"][0]
    assert embed["description"].startswith("🏆 **Winner: ")
    assert "cinnamon** with 100%" in embed["description"]
    assert embed["color"] == community.GREEN and "final result" in embed["footer"]["text"]
    assert len([c for c in rest.calls if c[0] in ("POST",)]) == 0        # posts nothing
    late = _press(disp, cin, "5")
    assert late.response["data"]["components"] == []
    assert community.sweep(disp.host, "g1", now=poll["closes_at"] + 99) == []


def test_old_stored_polls_still_render(disp):
    store = community.ScopedStore(disp.host.store, "g1", "community")
    store.set("poll:old1", {"id": "old1", "question": "Old?", "choices": ["x", "y"],
                            "votes": {"1": 0}, "by": "7", "at": 1})
    view = _press(disp, community.PLUGIN.button_id("v:old1:1"), "2")
    assert [b["label"] for b in _buttons(view)] == ["x · 1", "y · 1"]


def test_command_definition_guides_the_user(disp):
    defs = {c["name"]: c for c in disp.host.command_defs()}
    names = [o["name"] for o in defs["poll"]["options"]]
    assert names[:12] == ["question", *[f"option{i}" for i in range(1, 11)], "choices"]
    hours = next(o for o in defs["poll"]["options"] if o["name"] == "hours")
    assert (hours["type"], hours["min_value"], hours["max_value"]) == (4, 1, 768)
    assert not any(o["required"] for o in defs["poll"]["options"])
    assert len(defs["poll"]["options"]) <= 25
