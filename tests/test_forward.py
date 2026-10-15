"""DM forwarding (decision-card answers) and per-channel conversation memory."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from awdiscord.dispatch import Dispatcher
from awdiscord.forward import DmForwarder, looks_like_answer
from awdiscord.gateway import Bot
from awdiscord.ratelimit import RateLimiter


@pytest.fixture
def receiver():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((body, self.headers.get("Authorization")))
            text = body["text"].strip()
            reply = f"answered {body['last_sent_card']} with {text}" if text[:1].isdigit() else ""
            out = json.dumps({"reply": reply}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/decisions/chat-reply", seen
    srv.shutdown()


class Rest:
    def __init__(self):
        self.sent = []

    def request(self, *a, **k):
        return {}

    def create_message(self, channel, payload):
        self.sent.append((channel, payload))


class NoAgent:
    def ask(self, *a, **k):
        raise AssertionError("a card answer reached the chat agent")


def _bot(agent, forwarder):
    b = Bot("MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38, agent, forwarder=forwarder)
    b.rest = Rest()
    return b


def test_card_answer_goes_to_the_receiver_not_the_agent(receiver):
    url, seen = receiver
    fwd = DmForwarder(url, "tok", owners=["42"])
    fwd.note_own_message("dm1", "Rent a GPU?\nReply with a number:\n1 No\n2 Yes\n\nd-pspb")
    bot = _bot(NoAgent(), fwd)
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None,
                        "user": "42", "content": "1"})
    assert bot.rest.sent[0][1]["content"] == "answered d-pspb with 1"
    body, auth = seen[0]
    assert body["user_id"] == "42" and body["is_direct_message"] is True
    assert auth == "Bearer tok"


def test_plain_dm_falls_through_to_the_agent(receiver):
    url, _ = receiver

    class Agent:
        def ask(self, prompt, **k):
            return "hello there"
    bot = _bot(Agent(), DmForwarder(url, owners=["42"]))
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None,
                        "user": "42", "content": "how are you"})
    assert bot.rest.sent[0][1]["content"] == "hello there"


def test_dead_receiver_says_the_answer_was_not_applied():
    fwd = DmForwarder("http://127.0.0.1:9/decisions/chat-reply", owners=["42"], timeout=2)
    fwd.note_own_message("dm1", "... d-ab12")
    bot = _bot(NoAgent(), fwd)
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None,
                        "user": "42", "content": "2"})
    assert "NOT applied" in bot.rest.sent[0][1]["content"]


def test_guild_mentions_are_never_forwarded(receiver):
    url, seen = receiver

    class Agent:
        def ask(self, prompt, **k):
            return "ok"
    bot = _bot(Agent(), DmForwarder(url, owners=["42"]))
    bot.handle_message({"id": "m", "channel_id": "c", "guild_id": "g",
                        "user": "42", "content": "1"})
    assert seen == [] and bot.rest.sent[0][1]["content"] == "ok"


def test_looks_like_answer():
    assert looks_like_answer("d-ab12 1")
    assert looks_like_answer("2") and looks_like_answer("1.")
    assert looks_like_answer("1 only the MCP half", options=3)
    assert not looks_like_answer("12 monkeys is a great film", options=3)
    assert not looks_like_answer("1 thing I wanted to ask")        # no card seen
    assert looks_like_answer("cards") and looks_like_answer("steer try the cheaper GPU")
    assert not looks_like_answer("how are you", options=3)
    assert not looks_like_answer("100 dollars is too much")
    for yes in ("d-ab12 steer wait for CI", "steer use the MCP", "Cards?"):
        assert looks_like_answer(yes), yes
    # Chat stays chat: the receiver answers EVERY message it is sent, and a
    # word like "post" can match an option label.
    for no in ("hello", "post it", "what was d-ab12 about?", "123 things", ""):
        assert not looks_like_answer(no, options=3), no


def test_non_owner_never_answers_a_card(receiver):
    url, seen = receiver

    class Agent:
        def ask(self, prompt, **k):
            return "public answer"
    fwd = DmForwarder(url, "tok", owners=["42"])
    fwd.note_own_message("dm9", "... d-pspb")
    bot = _bot(Agent(), fwd)
    bot.handle_message({"id": "m", "channel_id": "dm9", "guild_id": None,
                        "user": "666", "content": "1"})
    assert seen == []
    assert bot.rest.sent[0][1]["content"] == "public answer"


def test_no_owners_forwards_nothing(receiver):
    url, seen = receiver
    assert DmForwarder(url, owners=[]).reply_for("dm1", "42", "1") == ""
    assert seen == []


def test_owner_note_and_named_card_are_forwarded_verbatim(receiver):
    url, seen = receiver
    fwd = DmForwarder(url, owners=["42"])
    fwd.note_own_message("dm1",
                         "Rent?\n\nReply with a number:\n`1` **No**\n`2` **Yes**\n\n`d-pspb`")
    assert fwd.options["dm1"] == 2                    # the daemon's backticked menu
    assert fwd.reply_for("dm1", "42", "2 only the MCP half") == \
        "answered d-pspb with 2 only the MCP half"
    fwd.reply_for("dm1", "42", "d-e3ny 1")
    assert seen[0][0]["text"] == "2 only the MCP half"
    assert seen[1][0]["text"] == "d-e3ny 1"


def test_empty_receiver_reply_to_an_answer_is_not_silent(receiver):
    url, _ = receiver
    # The fixture replies "" to anything not starting with a digit.
    assert "NOT applied" in DmForwarder(url, owners=["42"]).reply_for("dm1", "42", "cards")


def test_card_is_found_in_dm_history_after_a_restart(receiver):
    url, seen = receiver
    fwd = DmForwarder(url, owners=["42"])          # nothing noted: fresh process
    bot = _bot(NoAgent(), fwd)
    bot.state["user_id"] = "999"
    history = [{"author": {"id": "42"}, "content": "earlier d-zzzz from me"},
               {"author": {"id": "999"},
                "content": "Post the blog?\nReply with a number:\n`1` **Post**\n`d-e3ny`"},
               {"author": {"id": "999"}, "content": "older card d-old1"}]

    class HistRest(Rest):
        def request(self, method, path, *a, **k):
            return history if method == "GET" and "/messages" in path else {}
    bot.rest = HistRest()
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None,
                        "user": "42", "content": "1"})
    assert seen[0][0]["last_sent_card"] == "d-e3ny"
    assert bot.rest.sent[0][1]["content"] == "answered d-e3ny with 1"


def test_owner_chat_goes_to_the_fleet_agent_everyone_else_to_the_public_one():
    class Named:
        def __init__(self, name):
            self.name = name

        def ask(self, prompt, **k):
            return self.name
    d = Dispatcher(Named("public"), RateLimiter(per_user=(100, 60.0)),
                   owner_agent=Named("fleet"), owners=["42"])
    assert d.chat("42", None, "what is down?")["content"] == "fleet"
    assert d.chat("42", "g", "<@1> status", bot_id="1")["content"] == "fleet"
    assert d.chat("7", "g", "what is down?")["content"] == "public"
    # No fleet agent configured: owners get the public agent, never an error.
    assert Dispatcher(Named("public")).agent_for("42").name == "public"


def test_genesis_agent_runs_the_agent_loop_as_the_user():
    from awdiscord.agent import AgentError, GenesisAgent
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("X-Caller-Type"),
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            out = json.dumps({"response": "3 containers unhealthy"} if len(seen) == 1
                             else {"response": ""}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = GenesisAgent(f"http://127.0.0.1:{srv.server_port}", effort=5)
        out = a.ask("what is down?", user="42", tools=["ignored"],
                    history=[{"role": "user", "content": "hi"}, {"role": "tool", "content": "x"}])
        assert out == "3 containers unhealthy"
        path, caller, body = seen[0]
        assert path == "/chat" and caller == "platform"
        assert body["agentic"] is True and body["persona"] == "aither"
        assert body["session_id"] == "discord-42" and body["max_effort"] == 5
        assert body["context"]["recent_history"] == [{"role": "user", "content": "hi"}]
        with pytest.raises(AgentError):
            a.ask("again", user="42")                 # empty answer is an error, not ""
    finally:
        srv.shutdown()


def test_card_menu_sets_the_option_count():
    f = DmForwarder("http://x")
    f.note_own_message("dm", "Rent?\n\nReply with a number:\n1 Do not rent\n2 Rent\n\nd-pspb")
    assert f.last_card["dm"] == "d-pspb" and f.options["dm"] == 2


def test_follow_up_sees_the_earlier_turn():
    got = []

    class Agent:
        def ask(self, prompt, *, user="", history=None, **k):
            got.append(history or [])
            return f"re: {prompt}"
    d = Dispatcher(Agent(), RateLimiter(per_user=(100, 60.0)))
    d.chat("7", "g", "Did Hera post on LinkedIn?", channel="c1")
    d.chat("7", "g", "No, Hera our agent", channel="c1")
    assert got[0] == []
    assert got[1][0] == {"role": "user", "content": "Did Hera post on LinkedIn?"}
    assert got[1][1]["role"] == "assistant"
    d.chat("7", "g", "elsewhere", channel="c2")
    assert got[2] == []                                  # per channel


def test_history_is_bounded():
    class Agent:
        def ask(self, prompt, *, history=None, **k):
            return "x"
    d = Dispatcher(Agent(), RateLimiter(per_user=(100, 60.0)))
    for i in range(20):
        d.chat("7", "g", f"q{i}", channel="c")
    assert len(d._history["c"]) == 2 * Dispatcher.HISTORY_TURNS


def test_agent_names_its_teammates(monkeypatch):
    from awdiscord.agent import OpenAICompatAgent
    sent = {}

    def complete(self, payload):
        sent.update(payload)
        return {"content": "ok"}
    monkeypatch.setattr(OpenAICompatAgent, "_complete", complete)
    a = OpenAICompatAgent("http://x/v1", personas={"aither": "I am Aither.", "hera": "H"},
                          default_persona="aither")
    a.ask("hi", history=[{"role": "user", "content": "before"},
                         {"role": "tool", "content": "dropped"}])
    assert "Hera are your fellow agents" in sent["messages"][0]["content"]
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "user"]


@pytest.fixture
def echo():
    """A receiver that echoes what it was sent, so a test can read the exact text."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            out = json.dumps({"reply": f"got {body['text']}"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/decisions/chat-reply", seen
    srv.shutdown()


OLD_CARD = ("Rent a GPU?\n\nReply with a number:\n`1` **Rent now** ⭐ — $2/h\n"
            "`2` **Rent later**\n`3` **Hold**\n\n`d-old1`")
NEW_CARD = "Post the blog?\n\nReply with a number:\n`1` **Post**\n`2` **Hold off**\n\n`d-new2`"


def test_parse_card_reads_labels_from_the_rendered_menu():
    from awdiscord.forward import parse_card
    card, count, labels = parse_card(OLD_CARD)
    assert card == "d-old1" and count == 3
    assert labels == {"rent now": 1, "rent later": 2, "hold": 3}
    # Plain menus too, consequence dropped.
    _, _, plain = parse_card("Rent?\nReply with a number:\n1 No - stay local\n2 Yes\nd-pspb")
    assert plain == {"no": 1, "yes": 2}
    # A label two options share answers neither.
    _, _, dup = parse_card("x\nReply with a number:\n`1` **Go**\n`2` **Go**\n`d-dup1`")
    assert dup == {}


def test_reply_to_an_older_card_answers_that_card_not_the_newest(echo):
    url, seen = echo
    fwd = DmForwarder(url, owners=["42"])
    fwd.note_own_message("dm1", OLD_CARD)
    fwd.note_own_message("dm1", NEW_CARD)            # newest card on screen is d-new2
    assert fwd.reply_for("dm1", "42", "2", reference_text=OLD_CARD) == "got d-old1 2"
    assert fwd.reply_for("dm1", "42", "rent later", reference_text=OLD_CARD) == "got d-old1 2"
    # The label is translated to its NUMBER: the receiver matches labels by first
    # word, so "rent later" sent verbatim would answer "Rent now".
    assert seen[1]["text"] == "d-old1 2"
    # last_sent_card stays the card on screen; the id in the text picks the target.
    assert seen[0]["last_sent_card"] == "d-new2"


def test_free_text_reply_to_a_card_steers_it(echo):
    url, _ = echo
    fwd = DmForwarder(url, owners=["42"])
    out = fwd.reply_for("dm1", "42", "only if it's under $1/h", reference_text=OLD_CARD)
    assert out == "got d-old1 steer only if it's under $1/h"
    out = fwd.reply_for("dm1", "42", "12 monkeys", reference_text=OLD_CARD)
    assert out == "got d-old1 steer 12 monkeys"          # 12 is not one of 3 options
    out = fwd.reply_for("dm1", "42", "steer wait for CI", reference_text=OLD_CARD)
    assert out == "got d-old1 steer wait for CI"
    assert fwd.reply_for("dm1", "42", "cards", reference_text=OLD_CARD) == "got cards"
    out = fwd.reply_for("dm1", "42", "2 but cap it at 4h", reference_text=OLD_CARD)
    assert out == "got d-old1 2 but cap it at 4h"


def test_reply_to_bot_chat_that_names_a_card_stays_chat(echo):
    url, seen = echo

    class Agent:
        def ask(self, prompt, **k):
            return "chat answer"
    bot = _bot(Agent(), DmForwarder(url, owners=["42"]))
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None, "user": "42",
                        "content": "why is that?",
                        "reference_text": "d-ab12 is about the GPU rental, it waits on you."})
    assert seen == [] and bot.rest.sent[0][1]["content"] == "chat answer"


def test_option_label_alone_answers_the_newest_card(echo):
    url, seen = echo
    fwd = DmForwarder(url, owners=["42"])
    fwd.note_own_message("dm1", NEW_CARD)
    assert fwd.reply_for("dm1", "42", "Hold off.") == "got d-new2 2"
    assert fwd.reply_for("dm1", "42", "post") == "got d-new2 1"
    # A sentence that merely starts with a label is chat, not an answer.
    assert fwd.reply_for("dm1", "42", "post it to linkedin too") == ""
    assert fwd.reply_for("dm1", "77", "post") == ""                 # non-owner
    assert [s["text"] for s in seen] == ["d-new2 2", "d-new2 1"]


YES_NO_CARD = "Rent the GPU?\nReply with a number:\n1 No - stay local\n2 Yes\nd-pspb"


def test_bot_chat_after_a_card_makes_a_bare_label_chat_again(echo):
    # Review repro (#12852): card, then the bot chats, then the owner says "yes".
    # "yes" answers the chat; it must never approve a money-spending card.
    url, seen = echo

    class Agent:
        def ask(self, prompt, **k):
            return "opening it"
    fwd = DmForwarder(url, owners=["42"])
    fwd.note_own_message("dm1", YES_NO_CARD)
    fwd.note_own_message("dm1", "want me to open the PR?")
    assert fwd.reply_for("dm1", "42", "yes") == ""
    bot = _bot(Agent(), fwd)
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None,
                        "user": "42", "content": "yes"})
    assert seen == [] and bot.rest.sent[0][1]["content"] == "opening it"
    # Chat that merely names a card drops the labels too.
    fwd.note_own_message("dm1", YES_NO_CARD)
    fwd.note_own_message("dm1", "d-pspb is about the GPU rental.")
    assert fwd.reply_for("dm1", "42", "yes") == ""
    # A Discord Reply to the card still answers it: the target is explicit.
    assert fwd.reply_for("dm1", "42", "yes", reference_text=YES_NO_CARD) == "got d-pspb 2"
    # A new card on screen brings its labels back.
    fwd.note_own_message("dm1", YES_NO_CARD)
    assert fwd.reply_for("dm1", "42", "yes") == "got d-pspb 2"


def test_labels_are_not_seeded_from_dm_history_after_a_restart(echo):
    url, seen = echo
    fwd = DmForwarder(url, owners=["42"], lookup=lambda ch: YES_NO_CARD)
    assert fwd.reply_for("dm1", "42", "yes") == ""      # history may have chat after it
    assert fwd.reply_for("dm1", "42", "2") == "got 2"   # a bare number still answers
    assert seen[-1]["last_sent_card"] == "d-pspb"


def test_gateway_fetches_the_replied_to_card_and_checks_the_author(echo):
    url, seen = echo
    bot = _bot(NoAgent(), DmForwarder(url, owners=["42"]))
    bot.state["user_id"] = "999"
    msgs = {"c1": {"author": {"id": "999"}, "content": OLD_CARD},
            "c2": {"author": {"id": "42"}, "content": "Fake\nReply with a number:\n1 x\nd-fake"}}

    class RefRest(Rest):
        def request(self, method, path, *a, **k):
            if method == "GET" and "/messages/" in path:
                return msgs.get(path.rsplit("/", 1)[1], {})
            return [] if method == "GET" else {}
    bot.rest = RefRest()
    bot.handle_message({"id": "m", "channel_id": "dm1", "guild_id": None, "user": "42",
                        "content": "3", "reference_id": "c1"})
    assert bot.rest.sent[0][1]["content"] == "got d-old1 3"
    # A reply to the owner's OWN card-looking message is not a card reply: it
    # takes the newest-card path ("3" with no card seen is sent as is).
    bot.handle_message({"id": "m2", "channel_id": "dm1", "guild_id": None, "user": "42",
                        "content": "3", "reference_id": "c2"})
    assert seen[-1]["text"] == "3"


def test_reference_reads_a_resolved_reply_to_the_bot():
    from types import SimpleNamespace

    from awdiscord.gateway import _reference
    def msg(author: int, content: str) -> SimpleNamespace:
        return SimpleNamespace(author=SimpleNamespace(id=author), content=content, embeds=[])

    def reply_to(mid: int, resolved) -> SimpleNamespace:
        return SimpleNamespace(reference=SimpleNamespace(message_id=mid, resolved=resolved))

    assert _reference(reply_to(5, msg(999, NEW_CARD)), 999) == ("5", NEW_CARD)
    assert _reference(reply_to(6, msg(42, "hi")), 999) == ("6", "")
    assert _reference(SimpleNamespace(reference=None), 999) == ("", "")
