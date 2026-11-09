"""Bot output containment: the bot talks in its server's bot channel - offline."""
from __future__ import annotations

import pytest
from awdiscord import containment, gateway
from awdiscord.containment import BotChannels
from awdiscord.dispatch import EPHEMERAL
from awdiscord.rest import RestError

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped
OWNER = "900"
CHANNELS = [{"id": "c-general", "name": "general", "type": 0},
            {"id": "c-bot", "name": "bot", "type": 0},
            {"id": "c-voice", "name": "bot", "type": 2},       # a voice channel named bot
            {"id": "c-feed", "name": "aither-feed", "type": 0}]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AWDISCORD_BOT_CHANNEL", raising=False)
    monkeypatch.delenv("AWDISCORD_CONTAIN", raising=False)
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0")


class _Agent:
    name = "bonsai"
    personas: dict = {}

    def __init__(self, answer="The answer is 42."):
        self.answer = answer

    def ask(self, prompt, **kw):
        return self.answer


class _Rest:
    def __init__(self, channels=CHANNELS, dm_closed=False):
        self.channels, self.dm_closed = channels, dm_closed
        self.calls: list = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        if path.endswith("/channels") and path.startswith("/guilds/"):
            return self.channels
        return {}

    def dm_channel(self, user_id):
        if self.dm_closed:
            raise RestError(403, "Cannot send messages to this user")
        return {"id": "dm-" + user_id}

    def create_message(self, channel, payload):
        if self.dm_closed and channel.startswith("dm-"):
            raise RestError(403, "Cannot send messages to this user")
        self.calls.append(("POST", channel, payload))
        return {"id": f"posted-{len(self.calls)}", "channel_id": channel}

    def interaction_callback(self, iid, token, payload):
        self.calls.append(("CB", iid, payload))

    def edit_original(self, app, token, payload):
        self.calls.append(("EDIT", token, payload))
        return {"id": "orig", "channel_id": "x"}

    def followup(self, app, token, payload):
        self.calls.append(("FOLLOWUP", token, payload))

    def posts(self, channel):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1] == channel]

    def reactions(self):
        return [c[1] for c in self.calls if c[0] == "PUT" and "/reactions/" in c[1]]


def _bot(rest, agent=None):
    bot = gateway.Bot(TOKEN, agent or _Agent(), app_id="app", owners=(OWNER,))
    bot.rest = rest
    return bot


def _mention(channel="c-general", guild="g1", parent=""):
    return {"user": "u1", "guild_id": guild, "channel_id": channel, "id": "m1",
            "content": "<@1> what is the answer?", "bot_id": "1", "parent_id": parent}


# -- which channel is the bot channel --------------------------------------------

def _resolver(channels=CHANNELS, setting=""):
    calls = []

    def fetch(guild):
        calls.append(guild)
        return channels
    return BotChannels(fetch, setting=lambda g: setting), calls


def test_default_is_the_text_channel_named_bot():
    r, _ = _resolver()
    assert r.for_guild("g1") == "c-bot"


def test_env_name_and_hash_prefix(monkeypatch):
    monkeypatch.setenv("AWDISCORD_BOT_CHANNEL", "#Aither-Feed")
    r, _ = _resolver()
    assert r.for_guild("g1") == "c-feed"


def test_env_id_counts_only_in_its_own_server(monkeypatch):
    monkeypatch.setenv("AWDISCORD_BOT_CHANNEL", "777")
    r, _ = _resolver(CHANNELS + [{"id": "777", "name": "x", "type": 0}])
    assert r.for_guild("g1") == "777"
    r2, _ = _resolver()                    # a server without channel 777: falls to "bot"
    assert r2.for_guild("g2") == "c-bot"


def test_the_servers_own_setting_wins(monkeypatch):
    monkeypatch.setenv("AWDISCORD_BOT_CHANNEL", "general")
    r, _ = _resolver(setting="aither-feed")
    assert r.for_guild("g1") == "c-feed"


def test_resolution_is_cached():
    r, calls = _resolver()
    for _ in range(5):
        r.for_guild("g1")
    assert calls == ["g1"]


def test_an_unreadable_channel_list_is_unknown_and_moves_nothing():
    r = BotChannels(lambda g: {"message": "Missing Access"})
    assert r.for_guild("g1") is None
    assert r.is_bot_channel("g1", "c-general")


def test_a_thread_inside_the_bot_channel_counts():
    r, _ = _resolver()
    assert r.is_bot_channel("g1", "thread-9", parent="c-bot")
    assert not r.is_bot_channel("g1", "thread-9", parent="c-general")
    assert r.is_bot_channel(None, "dm-1")         # a DM is never contained


# -- slash commands ----------------------------------------------------------------

def _ask(channel="c-general", user="u1", options=None):
    opts = [{"name": "question", "value": "what is the answer?"}] + (options or [])
    return {"id": "i1", "token": "tok", "type": 2, "guild_id": "g1", "channel_id": channel,
            "member": {"user": {"id": user}, "permissions": "0"},
            "data": {"name": "ask", "type": 1, "options": opts}}


def _callback(rest):
    return next(c[2] for c in rest.calls if c[0] == "CB")


def test_a_slash_command_outside_the_bot_channel_is_ephemeral():
    rest = _Rest()
    _bot(rest).handle_raw(_ask("c-general"))
    assert _callback(rest)["data"]["flags"] & EPHEMERAL
    edit = next(c[2] for c in rest.calls if c[0] == "EDIT")
    assert "flags" not in edit                   # an edit may not set EPHEMERAL


def test_a_contained_long_slash_answer_keeps_its_followups_ephemeral():
    long = "Memory is stored in three tiers. " + "Detail sentence about recall. " * 120
    rest = _Rest()
    _bot(rest, _Agent(long)).handle_raw(_ask("c-general"))
    follow = [c[2] for c in rest.calls if c[0] == "FOLLOWUP"]
    assert follow and all(f["flags"] & EPHEMERAL for f in follow)


def test_a_slash_command_in_the_bot_channel_stays_public():
    rest = _Rest()
    _bot(rest).handle_raw(_ask("c-bot"))
    assert not (_callback(rest).get("data") or {}).get("flags", 0) & EPHEMERAL


def test_an_operator_explicitly_asking_public_keeps_it_public():
    rest = _Rest()
    _bot(rest).handle_raw(_ask("c-general", user=OWNER,
                               options=[{"name": "private", "value": False}]))
    assert not (_callback(rest).get("data") or {}).get("flags", 0) & EPHEMERAL


def test_a_member_cannot_use_the_flag_to_escape():
    rest = _Rest()
    _bot(rest).handle_raw(_ask("c-general", user="u1",
                               options=[{"name": "private", "value": False}]))
    assert _callback(rest)["data"]["flags"] & EPHEMERAL


def test_containment_off_restores_public_replies(monkeypatch):
    monkeypatch.setenv("AWDISCORD_CONTAIN", "0")
    rest = _Rest()
    _bot(rest).handle_raw(_ask("c-general"))
    assert not (_callback(rest).get("data") or {}).get("flags", 0) & EPHEMERAL


# -- mentions ----------------------------------------------------------------------

def test_a_mention_outside_goes_to_the_bot_channel_with_a_jump_link():
    rest = _Rest()
    _bot(rest).handle_message(_mention("c-general"))
    assert rest.posts("c-general") == []                      # nothing where it was asked
    [post] = rest.posts("c-bot")
    assert post["content"].startswith(
        "<@u1> asked in https://discord.com/channels/g1/c-general/m1\n")
    assert "The answer is 42." in post["content"]
    assert post["allowed_mentions"] == {"parse": [], "users": ["u1"]}
    assert "message_reference" not in post
    assert rest.reactions() == [f"/channels/c-general/messages/m1/reactions/"
                                f"{'%F0%9F%91%80'}/@me"]


def test_a_mention_in_the_bot_channel_is_answered_in_place():
    rest = _Rest()
    _bot(rest).handle_message(_mention("c-bot"))
    [post] = rest.posts("c-bot")
    assert post["content"] == "The answer is 42."
    assert post["message_reference"]["message_id"] == "m1"
    assert rest.reactions() == []


def test_a_mention_in_a_thread_of_the_bot_channel_is_answered_in_place():
    rest = _Rest()
    _bot(rest).handle_message(_mention("thread-9", parent="c-bot"))
    assert [p["content"] for p in rest.posts("thread-9")] == ["The answer is 42."]


def test_no_bot_channel_means_a_dm():
    rest = _Rest(channels=[c for c in CHANNELS if c["id"] != "c-bot"])
    _bot(rest).handle_message(_mention("c-general"))
    assert rest.posts("c-general") == []
    dm = [p["content"] for p in rest.posts("dm-u1")]
    assert dm == ["You asked in https://discord.com/channels/g1/c-general/m1:",
                  "The answer is 42."]
    assert rest.reactions()                                   # the original is marked


def test_dms_closed_fall_back_to_the_original_channel():
    rest = _Rest(channels=[c for c in CHANNELS if c["id"] != "c-bot"], dm_closed=True)
    _bot(rest).handle_message(_mention("c-general"))
    [post] = rest.posts("c-general")
    assert post["content"] == "The answer is 42."
    assert post["message_reference"]["message_id"] == "m1"


def test_a_bot_channel_the_bot_cannot_post_in_falls_back_in_place():
    rest = _Rest()
    real = rest.create_message

    def create(channel, payload):
        if channel == "c-bot":
            raise RestError(403, "Missing Permissions")
        return real(channel, payload)
    rest.create_message = create
    _bot(rest).handle_message(_mention("c-general"))
    [post] = rest.posts("c-general")
    assert post["content"] == "The answer is 42."
    assert post["message_reference"]["message_id"] == "m1"


def test_a_long_answer_goes_whole_to_the_bot_channel():
    long = "Memory is stored in three tiers. " + "Detail sentence about recall. " * 120
    rest = _Rest()
    _bot(rest, _Agent(long)).handle_message(_mention("c-general"))
    posts = [p["content"] for p in rest.posts("c-bot")]
    assert posts[0].startswith("<@u1> asked in ")
    assert "Detail sentence about recall." in "".join(posts[1:])
    assert rest.posts("c-general") == [] and rest.posts("dm-u1") == []


def test_a_dm_is_never_contained():
    rest = _Rest()
    msg = {**_mention("dm-u1", guild=None)}
    _bot(rest).handle_message(msg)
    assert [p["content"] for p in rest.posts("dm-u1")] == ["The answer is 42."]


# -- the bot's own unsolicited posts ------------------------------------------------

def test_the_setup_card_goes_to_the_bot_channel_not_the_system_channel():
    from awdiscord.served import ServedGuilds
    rest = _Rest()
    bot = _bot(rest)
    bot.served = ServedGuilds(frozenset())
    assert bot._setup_card({"id": "g1", "system_channel_id": "c-general"}) == {
        "setup": "posted"}
    assert rest.posts("c-general") == [] and len(rest.posts("c-bot")) == 1


def test_the_setup_card_is_skipped_without_a_bot_channel():
    from awdiscord.served import ServedGuilds
    rest = _Rest(channels=[c for c in CHANNELS if c["id"] != "c-bot"])
    bot = _bot(rest)
    bot.served = ServedGuilds(frozenset())
    assert bot._setup_card({"id": "g1", "system_channel_id": "c-general"}) == {
        "setup": "skipped"}
    assert rest.posts("c-general") == []


def test_jump_link_shape():
    assert containment.jump_link("g", "c") == "https://discord.com/channels/g/c"
    assert containment.jump_link("g", "c", "m") == "https://discord.com/channels/g/c/m"
