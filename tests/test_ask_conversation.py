"""/ask, @mentions and "Ask Aither about this" read the recent conversation - offline.

The live failure (Occupy SaaS #contributing-coordination, 2026-11-15): the owner ran
``/ask what is the potential misunderstanding here`` and the bot said it had no
access to the conversation, so he pasted it in by hand, cutting it to fit.
"""
from __future__ import annotations

import pytest
from awdiscord import gateway, turn_context
from awdiscord.dispatch import (
    ASK_MESSAGE_COMMAND,
    ASK_MODAL_PREFIX,
    DEFAULT_ASK,
    DEFERRED,
    EPHEMERAL,
    LIMIT,
    MODAL,
    MODAL_QUESTION_MAX,
    QUESTION_MAX,
    can_read_history,
    commands,
    parse_message_link,
    with_footer,
)
from awdiscord.rest import RestError

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped
READ = str((1 << 10) | (1 << 16))                    # View Channel + Read History
HERE, ELSEWHERE, GUILD = "1100", "1200", "77"

# The real argument, reduced: Tom and ArchitectIT on commit emails.
ARGUMENT = [
    {"id": "1009", "timestamp": "2026-11-15T18:09:00.000000+00:00",
     "content": "So you'd rather your commits not count? Fine by me.",
     "author": {"id": "u-tom", "global_name": "Tom"},
     "referenced_message": {"id": "1008", "content": "no",
                            "author": {"id": "u-arch", "global_name": "ArchitectIT"}}},
    {"id": "1008", "timestamp": "2026-11-15T18:08:00.000000+00:00",
     "content": "I'm not putting my personal email on a public repo. No.",
     "author": {"id": "u-arch", "global_name": "ArchitectIT"}},
    {"id": "1007", "timestamp": "2026-11-15T18:07:00.000000+00:00",
     "content": "GitHub only credits a commit whose author email is verified on "
                "your account. Set user.email or it won't show as yours.",
     "author": {"id": "u-tom", "global_name": "Tom"},
     "embeds": [{"type": "link", "title": "Setting your commit email address - GitHub Docs"}]},
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWDISCORD_CONTAIN", "0")
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0")


class _Agent:
    name = "aither"
    personas: dict = {}

    def __init__(self, answer="Tom wants credit; ArchitectIT wants privacy."):
        self.answer = answer
        self.prompts: list[str] = []

    def ask(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answer


class _Rest:
    """Serves channel histories; records every call."""

    def __init__(self, history=None, fail=False):
        self.history = dict(history or {})          # channel -> [messages]
        self.fail = fail
        self.calls: list = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path))
        if method == "POST" and body is not None:
            self.calls.append(("POST", body))
        if self.fail and "/messages" in path:
            raise RestError(403, "Missing Access")
        parts = path.split("?")[0].strip("/").split("/")
        if method == "GET" and len(parts) == 4 and parts[2] == "messages":
            return next((m for m in self.history.get(parts[1], [])
                         if m["id"] == parts[3]), {})
        if method == "GET" and len(parts) == 3 and parts[2] == "messages":
            return list(self.history.get(parts[1], []))
        return {}

    def fetched(self, channel):
        return [p for m, p in self.calls if m == "GET" and p.startswith(
            f"/channels/{channel}/messages?")]

    def interaction_callback(self, iid, token, payload):
        self.calls.append(("CB", payload))

    def edit_original(self, app, token, payload):
        self.calls.append(("EDIT", payload))
        return {"id": "orig", "channel_id": HERE}

    def followup(self, app, token, payload):
        self.calls.append(("FOLLOWUP", payload))

    def create_message(self, channel, payload):
        self.calls.append(("POST", payload))
        return {"id": "posted", "channel_id": channel}

    def edits(self):
        return [p for kind, p in self.calls if kind == "EDIT"]

    def callbacks(self):
        return [p for kind, p in self.calls if kind == "CB"]


def _bot(rest, agent=None):
    bot = gateway.Bot(TOKEN, agent or _Agent(), app_id="app", owners=("900",))
    bot.rest = rest
    return bot


def _ask(question="what is the potential misunderstanding here", perms=READ,
         channel=HERE, **options):
    opts = [{"name": "question", "value": question}] if question else []
    opts += [{"name": k, "value": v} for k, v in options.items()]
    return {"id": "i1", "token": "tok", "type": 2, "guild_id": GUILD, "channel_id": channel,
            "member": {"user": {"id": "u-owner"}, "permissions": perms},
            "data": {"name": "ask", "type": 1, "options": opts}}


# -- the block -----------------------------------------------------------------------

def test_block_has_times_authors_replies_and_link_titles_oldest_first():
    block, count = turn_context.build_counted(recent=ARGUMENT, nonce="n")
    assert count == 3
    tom = block.index("[11-15 18:07] Tom: GitHub only credits")
    arch = block.index("[11-15 18:08] ArchitectIT: I'm not putting")
    reply = block.index("[11-15 18:09] Tom (replying to ArchitectIT): So you'd rather")
    assert tom < arch < reply
    assert "Setting your commit email address - GitHub Docs" in block


def test_forty_messages_by_default_and_the_budget_drops_the_oldest():
    many = [{"id": str(5000 - i), "content": f"line {5000 - i} " + "w" * 200,
             "author": {"id": "u", "username": "ann"}} for i in range(60)]
    block, count = turn_context.build_counted(recent=many, nonce="n")
    assert count == min(turn_context.RECENT_LIMIT, 40) == 40
    assert "line 5000" in block and "line 4950" not in block      # newest kept
    tight, kept = turn_context.build_counted(recent=many, nonce="n", total_chars=2000)
    assert 0 < kept < 40 and len(tight) < 2200
    assert "line 5000" in tight                                   # the newest survive


def test_questions_about_the_talk_get_the_positions_and_crux_instruction():
    assert turn_context.guidance("what is the potential misunderstanding here")
    assert turn_context.guidance("what is going on here")
    assert not turn_context.guidance("what is the capital of France")
    prompt = turn_context.with_context("[ctx]", "what's the disagreement?")
    assert "EACH person" in prompt and "**Crux:**" in prompt


# -- /ask reads the conversation ----------------------------------------------------

def test_ask_reads_the_last_messages_and_says_so():
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_ask())
    prompt = agent.prompts[0]
    assert "Tom: GitHub only credits" in prompt and "ArchitectIT" in prompt
    assert "**Crux:**" in prompt                              # the dispute instruction
    assert prompt.rstrip().endswith("what is the potential misunderstanding here")
    assert rest.fetched(HERE) == [f"/channels/{HERE}/messages?limit="
                                  f"{turn_context.RECENT_LIMIT}"]
    assert rest.edits()[0]["content"].endswith("-# (read the last 3 messages here)")


def test_ask_context_none_reads_nothing():
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_ask(context="none"))
    assert rest.fetched(HERE) == [] and "Tom" not in agent.prompts[0]
    assert "read the last" not in rest.edits()[0]["content"]


def test_ask_never_reads_a_channel_the_asker_cannot_read_history_in():
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_ask(perms=str(1 << 10)))   # view, no history
    assert rest.fetched(HERE) == [] and "Tom" not in agent.prompts[0]
    assert "can't read its history" in rest.edits()[0]["content"]


def test_a_failed_fetch_costs_the_context_never_the_answer():
    rest = _Rest(fail=True)
    agent = _Agent()
    _bot(rest, agent).handle_raw(_ask())
    assert agent.prompts and rest.edits()[0]["content"].startswith("Tom wants credit")


def test_private_ask_stays_ephemeral_with_the_context():
    rest = _Rest(history={HERE: ARGUMENT})
    _bot(rest).handle_raw(_ask(private=True))
    assert int(rest.callbacks()[0]["data"]["flags"]) & EPHEMERAL


def test_since_link_in_another_channel_needs_the_asker_to_read_it():
    link = f"https://discord.com/channels/{GUILD}/{ELSEWHERE}/1007"
    rest = _Rest(history={ELSEWHERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_ask(context="since-link", link=link))
    assert rest.fetched(ELSEWHERE) == [] and "Tom" not in agent.prompts[0]
    assert "can't read it" in rest.edits()[0]["content"]

    rest = _Rest(history={ELSEWHERE: ARGUMENT})
    agent = _Agent()
    it = _ask(context="since-link", link=link)
    it["_can_read"] = lambda channel: channel == ELSEWHERE
    _bot(rest, agent).handle_raw(it)
    assert rest.fetched(ELSEWHERE) == [f"/channels/{ELSEWHERE}/messages?limit="
                                       f"{turn_context.RECENT_LIMIT}&after=1007"]
    assert "Tom: GitHub only credits" in agent.prompts[0]
    assert f"since the linked message in <#{ELSEWHERE}>" in rest.edits()[0]["content"]


def test_a_link_into_another_server_is_never_read():
    link = f"https://discord.com/channels/999/{ELSEWHERE}/1007"
    rest = _Rest(history={ELSEWHERE: ARGUMENT, HERE: ARGUMENT[:1]})
    it = _ask(context="since-link", link=link)
    it["_can_read"] = lambda channel: True
    _bot(rest).handle_raw(it)
    assert rest.fetched(ELSEWHERE) == [] and rest.fetched(HERE)   # fell back to here


def test_thread_mode_reads_the_thread_and_its_starter():
    thread, parent = "1300", HERE
    starter = {"id": thread, "content": "Contributor credit policy?",
               "author": {"id": "u-tom", "global_name": "Tom"}}
    rest = _Rest(history={parent: [starter], thread: ARGUMENT})
    agent = _Agent()
    it = _ask(channel=thread, context="thread")
    it.update(channel_parent_id=parent, channel_type=11)
    _bot(rest, agent).handle_raw(it)
    assert "started from a message by Tom" in agent.prompts[0]
    assert rest.fetched(thread) == [f"/channels/{thread}/messages?limit=100"]
    assert "in this thread" in rest.edits()[0]["content"]


# -- long pastes are never cut ---------------------------------------------------------

def test_the_question_option_takes_a_long_paste_and_is_optional():
    ask = next(c for c in commands() if c["name"] == "ask")
    question = next(o for o in ask["options"] if o["name"] == "question")
    assert question["max_length"] == QUESTION_MAX == 6000 and not question["required"]
    context = next(o for o in ask["options"] if o["name"] == "context")
    assert [c["value"] for c in context["choices"]] == ["recent", "thread", "since-link",
                                                        "none"]


def test_ask_with_no_question_opens_a_4000_character_form():
    rest = _Rest(history={HERE: ARGUMENT})
    _bot(rest).handle_raw(_ask(question="", private=True))
    cb = rest.callbacks()[0]
    assert cb["type"] == MODAL
    assert cb["data"]["custom_id"] == f"{ASK_MODAL_PREFIX}q:recent:1"
    field = cb["data"]["components"][0]["components"][0]
    assert field["style"] == 2 and field["max_length"] == MODAL_QUESTION_MAX == 4000


def test_the_submitted_form_answers_with_the_whole_paste_and_the_conversation():
    paste = "Tom: ... " + "x" * 3500
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_modal(f"{ASK_MODAL_PREFIX}q:recent:1", paste))
    assert rest.callbacks()[0]["type"] == DEFERRED
    assert int(rest.callbacks()[0]["data"]["flags"]) & EPHEMERAL
    assert paste in agent.prompts[0] and "ArchitectIT" in agent.prompts[0]


def test_a_mention_is_never_cut_at_1500():
    rest = _Rest(history={HERE: []})
    agent = _Agent()
    text = "<@1> " + "y" * 3000 + " END"
    _bot(rest, agent).handle_message({"user": "u1", "guild_id": GUILD, "channel_id": HERE,
                                      "id": "2000", "content": text, "bot_id": "1"})
    assert "y" * 3000 + " END" in agent.prompts[0]


# -- the context-menu command ------------------------------------------------------------

def _modal(custom_id, question="", channel=HERE):
    return {"id": "i3", "token": "tok3", "type": 5, "guild_id": GUILD,
            "channel_id": channel,
            "member": {"user": {"id": "u-owner"}, "permissions": READ},
            "data": {"custom_id": custom_id, "components": [{"type": 1, "components": [
                {"type": 4, "custom_id": "question", "value": question}]}]}}


def test_ask_about_this_is_registered_and_opens_the_form_for_any_member():
    names = [c["name"] for c in commands() if c.get("type") == 3]
    assert ASK_MESSAGE_COMMAND in names
    rest = _Rest()
    _bot(rest).handle_raw({
        "id": "i2", "token": "tok2", "type": 2, "guild_id": GUILD, "channel_id": HERE,
        "member": {"user": {"id": "u-member"}, "permissions": READ},
        "data": {"name": ASK_MESSAGE_COMMAND, "type": 3, "target_id": "1008",
                 "resolved": {"messages": {"1008": ARGUMENT[1]}}}})
    cb = rest.callbacks()[0]
    assert cb["type"] == MODAL and cb["data"]["custom_id"] == f"{ASK_MODAL_PREFIX}m:1008:0"


def test_an_empty_form_on_a_message_asks_what_is_going_on_around_it():
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_modal(f"{ASK_MODAL_PREFIX}m:1008:0"))
    assert rest.fetched(HERE) == [f"/channels/{HERE}/messages?limit="
                                  f"{turn_context.RECENT_LIMIT}&around=1008"]
    assert agent.prompts[0].rstrip().endswith(DEFAULT_ASK)
    assert "**Crux:**" in agent.prompts[0]
    assert "around that message" in rest.edits()[0]["content"]


def test_a_forged_form_id_is_refused():
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_raw(_modal(f"{ASK_MODAL_PREFIX}m:../../x:0"))
    assert not agent.prompts and rest.fetched(HERE) == []


# -- mentions --------------------------------------------------------------------------

def test_a_mention_reads_the_recent_conversation_and_says_so():
    rest = _Rest(history={HERE: ARGUMENT})
    agent = _Agent()
    _bot(rest, agent).handle_message({
        "user": "u1", "guild_id": GUILD, "channel_id": HERE, "id": "2000",
        "content": "<@1> what is the misunderstanding?", "bot_id": "1"})
    assert "Tom (replying to ArchitectIT)" in agent.prompts[0]
    posted = [p for kind, p in rest.calls if kind == "POST" and isinstance(p, dict)]
    assert posted and posted[0]["content"].endswith("-# (read the last 3 messages here)")


# -- helpers ---------------------------------------------------------------------------

def test_with_footer_never_pushes_a_part_over_the_limit():
    full = {"content": "x" * LIMIT}
    with_footer(full, "-# (read the last 3 messages here)")
    assert full["content"] == "x" * LIMIT and full["continuation"] == [
        "-# (read the last 3 messages here)"]
    short = with_footer({"content": "hi"}, "-# note")
    assert short["content"] == "hi\n-# note"


def test_permission_and_link_parsing():
    assert can_read_history({"member": {"permissions": READ}})
    assert can_read_history({"member": {"permissions": str(1 << 3)}})     # admin
    assert not can_read_history({"member": {"permissions": str(1 << 10)}})
    assert not can_read_history({})                                       # a DM
    assert parse_message_link("see https://discord.com/channels/11111/22222/33333 ok") \
        == ("11111", "22222", "33333")
    assert parse_message_link("https://evil.example/channels/11111/22222/33333") is None


def test_a_commit_email_dispute_gets_the_noreply_reference_note():
    block, _ = turn_context.build_counted(recent=ARGUMENT, nonce="n")
    prompt = turn_context.with_context(block, "what is the potential misunderstanding here")
    assert "users.noreply.github.com" in prompt
    # Only on topic, and only for a question about the conversation.
    other = turn_context.with_context("[ctx: lunch plans]", "what is going on here")
    assert "noreply" not in other
    plain = turn_context.with_context(block, "what time is the meeting")
    assert "noreply" not in plain
