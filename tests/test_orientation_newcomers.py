"""Orientation for newcomers: the server's own text, batched welcomes, intro replies.

Found live 2026-11-13 in a partner server: newcomers posted "I'm a software engineer,
willing to contribute" in #general and nobody answered. The rules under test:

* nothing posts in a server until ITS admin runs /orientation set (an operator who is
  not an admin there cannot), and only in the channel that admin picked;
* the text is the server's own (what it is, its repos, its links, claim a plan in the
  coordination channel) and falls back to the generic text only when nothing is set;
* a burst of joins becomes ONE welcome naming at most ten people, at most once an
  hour, each member welcomed once, and only the named members are mentionable;
* a member's intro or offer to help gets one reply; chatter does not; a model that
  says NO or fails keeps the bot silent.
"""
from __future__ import annotations

import asyncio
import datetime
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import gateway  # noqa: E402
from awdiscord.builtin import orientation as mod  # noqa: E402
from awdiscord.plugins import MANAGE_GUILD, PluginHost, ScopedStore  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

GUILD = "1561399233846972457"
CHANNEL = "1566572632546091141"
OTHER = "1566572632546091999"
CLAIM = "1566572632546090000"
ADMIN = str(MANAGE_GUILD)
OPERATOR = "4242"
NOW = 1_800_000_000.0
HOUR = 3600.0
TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped


class FakeRest:
    def __init__(self, name: str = "Occupy SaaS"):
        self.name = name
        self.posted: list[tuple[str, dict]] = []

    def request(self, method, path, body=None):
        if path.startswith("/guilds/"):
            return {"id": path.rsplit("/", 1)[-1], "name": self.name}
        if "/messages?limit=" in path:
            return []
        raise AssertionError(f"unexpected {method} {path}")

    def create_message(self, channel, payload):
        self.posted.append((channel, payload))
        return {"id": f"w{len(self.posted)}"}


class Agent:
    def __init__(self, answer="YES", raises=False):
        self.answer, self.raises, self.asked = answer, raises, []

    def ask(self, prompt, *, user="", context="", **kw):
        self.asked.append((prompt, context))
        if self.raises:
            raise RuntimeError("model down")
        return self.answer


@pytest.fixture
def host(monkeypatch):
    monkeypatch.delenv(mod.ENV_REPOS, raising=False)
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: None])
    h = PluginHost(GuildStore(":memory:"), plugins=[mod.PLUGIN], operators={OPERATOR})
    h.rest = FakeRest()
    h.store.set_plugin_state(GUILD, mod.NAME, True)
    return h


def _store(host) -> ScopedStore:
    return ScopedStore(host.store, GUILD, mod.NAME)


def _config(host) -> dict:
    return _store(host).get(mod.CONFIG_KEY) or {}


def _it(verb, perms=ADMIN, user="7", **opts):
    return {"type": 2, "id": "1", "token": "t", "guild_id": GUILD,
            "member": {"user": {"id": user}, "permissions": perms},
            "data": {"name": mod.NAME,
                     "options": [{"type": 1, "name": verb,
                                  "options": [{"name": k, "value": v}
                                              for k, v in opts.items()]}]}}


def _on(host, **extra):
    """A config as the new /orientation set writes it: an admin consented here."""
    _store(host).set(mod.CONFIG_KEY, {"channel": CHANNEL, "enabled": True,
                                      "every_hours": 24.0, "last_at": NOW,
                                      "welcome": True, "intros": True,
                                      "schema": mod.SCHEMA, "newcomers_by": "7",
                                      **extra})


def _join(host, user):
    return host.dispatch_member_join(GUILD, {"user": {"id": str(user)}}, lambda u, p: None)


def _say(host, text, user="501", channel=CHANNEL, mid="m1", kind=0, joined=0.0):
    return host.dispatch_message({"id": mid, "channel_id": channel, "guild_id": GUILD,
                                  "user": str(user), "content": text, "type": kind,
                                  "joined_at": joined})


def _new(host, *users):
    """Members the plugin saw join (no queue entry, so a test controls the queue)."""
    for user in users:
        mod.mark_joined(_store(host), str(user), time.time())


def _queue(host) -> list[str]:
    return [e["user"] for e in _store(host).get(mod.NEWCOMERS_KEY) or []]


def _content(result) -> str:
    return result.response["data"]["content"]


# ── the admin gate ───────────────────────────────────────────────────────────

def test_nothing_posts_or_queues_before_an_admin_turns_it_on(host):
    _join(host, 501)
    assert _say(host, "I'm a software engineer, willing to contribute") is False
    assert mod.sweep(host, NOW + 2 * HOUR) == {}
    assert host.rest.posted == []
    assert _queue(host) == []


def test_off_stops_welcomes_and_intro_replies(host):
    _on(host)
    _join(host, 501)
    host.handle(_it("off"))
    assert _say(host, "I'm a software engineer, willing to contribute") is False
    assert mod.sweep(host, NOW + 2 * HOUR) == {}
    assert host.rest.posted == []


def test_an_operator_who_is_not_a_server_admin_cannot_turn_it_on(host):
    result = host.handle(_it("set", perms="0", user=OPERATOR, channel=CHANNEL))
    assert "this server's admins" in _content(result)
    assert _config(host) == {}
    ok = host.handle(_it("set", perms=ADMIN, user=OPERATOR, channel=CHANNEL))
    assert f"<#{CHANNEL}>" in _content(ok)
    assert _config(host)["channel"] == CHANNEL


def test_a_member_cannot_preview_or_write_the_text(host):
    for verb in ("preview", "text"):
        assert "Only server managers" in _content(host.handle(_it(verb, perms="0")))


# ── the text ─────────────────────────────────────────────────────────────────

def test_the_server_text_names_its_repos_contributing_and_the_claim_channel(host):
    result = host.handle(_it("set", channel=CHANNEL, claim=CLAIM,
                             repos="occupysaas/salescompel, "
                                   "https://github.com/occupysaas/zeroauthy"))
    assert "generic" not in _content(result)
    assert _config(host)["repos"] == ["occupysaas/salescompel", "occupysaas/zeroauthy"]
    text = mod.welcome_text(_config(host), GUILD, "Occupy SaaS", ["501"])
    assert "<https://github.com/occupysaas/salescompel>" in text
    assert "<https://github.com/occupysaas/zeroauthy>" in text
    assert "CONTRIBUTING.md" in text and "roadmap" in text
    assert f"claim it in <#{CLAIM}> before you start" in text
    assert "/selfhost" not in text                      # not the generic Aither text


def test_a_bad_repo_name_is_refused_whole(host):
    result = host.handle(_it("set", channel=CHANNEL, repos="occupysaas/ok, not a repo"))
    assert "not a GitHub repo name" in _content(result)
    assert _config(host) == {}


def test_nothing_configured_falls_back_to_the_generic_text(host):
    assert mod.server_orientation({}, GUILD) == ""
    assert "/selfhost" in mod.post_text({}, GUILD, "Occupy SaaS")
    result = host.handle(_it("set", channel=CHANNEL))
    assert "generic Aither text" in _content(result)


def test_the_coordinators_repo_map_is_used_when_no_repos_are_set(host, monkeypatch):
    monkeypatch.setenv(mod.ENV_REPOS, f"{GUILD}=occupysaas/salescompel,"
                                      "123456789=someone/else")
    body = mod.server_orientation({}, GUILD)
    assert "occupysaas/salescompel" in body and "someone/else" not in body
    monkeypatch.setenv(mod.ENV_REPOS, f"{GUILD}=occupysaas/salescompel,garbage")
    assert mod.server_orientation({}, GUILD) == ""      # fail closed, like the coordinator


def test_the_text_form_saves_about_and_links_and_prefills_them(host):
    opened = host.handle(_it("text"))
    assert opened.response["type"] == 9
    submit = {"type": 5, "id": "2", "token": "t", "guild_id": GUILD,
              "member": {"user": {"id": "7"}, "permissions": ADMIN},
              "data": {"custom_id": "awp:orientation:text", "components": [
                  {"type": 1, "components": [{"type": 4, "custom_id": "about",
                                              "value": "We build SaaS in the open."}]},
                  {"type": 1, "components": [{"type": 4, "custom_id": "links",
                                              "value": "Roadmap https://example.org/r\n"
                                                       "https://example.org/plain"}]}]}}
    saved = host.handle_modal(submit)
    assert "Nothing posts until you run `/orientation set`" in _content(saved)
    assert _config(host)["about"] == "We build SaaS in the open."
    assert _config(host)["links"] == [["Roadmap", "https://example.org/r"],
                                      ["", "https://example.org/plain"]]
    body = mod.server_orientation(_config(host), GUILD)
    assert body.startswith("We build SaaS in the open.")
    assert "- Roadmap: <https://example.org/r>" in body
    reopened = host.handle(_it("text")).response["data"]["components"]
    assert reopened[0]["components"][0]["value"] == "We build SaaS in the open."
    assert "Roadmap https://example.org/r" in reopened[1]["components"][0]["value"]
    bad = dict(submit)
    bad["data"] = {"custom_id": "awp:orientation:text", "components": [
        {"type": 1, "components": [{"type": 4, "custom_id": "links",
                                    "value": "Roadmap http://insecure"}]}]}
    assert "https://" in _content(host.handle_modal(bad))


def test_preview_shows_the_exact_text_privately_before_anything_is_on(host):
    _store(host).set(mod.CONFIG_KEY, {"about": "We build SaaS in the open.",
                                      "claim_channel": CLAIM})
    result = host.handle(_it("preview", which="welcome"))
    assert result.response == {"type": 5, "data": {"flags": 64}}
    done = result.deferred()["content"]
    assert "OFF" in done
    assert mod.welcome_text(_config(host), GUILD, "Occupy SaaS", ["7"]) in done
    assert host.rest.posted == []
    intro = host.handle(_it("preview", which="intro")).deferred()["content"]
    assert mod.intro_text(_config(host), GUILD, "7") in intro


# ── newcomer welcomes ────────────────────────────────────────────────────────

def test_a_burst_of_joins_becomes_one_welcome_naming_them(host):
    _on(host, about="We build SaaS in the open.")
    for user in (501, 502, 503):
        _join(host, user)
    _join(host, 501)                                    # a repeat is not a second entry
    entries = _store(host).get(mod.NEWCOMERS_KEY)
    for e in entries:
        e["at"] = NOW
    _store(host).set(mod.NEWCOMERS_KEY, entries)
    assert mod.flush_welcomes(host, GUILD, _config(host), NOW + 60)["reason"] == "gathering"
    out = mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    assert out["named"] == ["501", "502", "503"]
    assert len(host.rest.posted) == 1
    channel, payload = host.rest.posted[0]
    assert channel == CHANNEL
    assert "<@501>, <@502> and <@503>" in payload["content"]
    assert "We build SaaS in the open." in payload["content"]
    assert payload["allowed_mentions"] == {"parse": [], "users": ["501", "502", "503"],
                                           "replied_user": False}
    # each member is welcomed once, ever
    _join(host, 502)
    assert _queue(host) == []
    assert mod.flush_welcomes(host, GUILD, _config(host), NOW + 3 * HOUR)["posted"] is False
    assert len(host.rest.posted) == 1


def test_at_most_ten_named_and_one_welcome_an_hour(host):
    _on(host)
    _store(host).set(mod.NEWCOMERS_KEY, [{"user": str(600 + i), "at": NOW}
                                         for i in range(12)])
    first = mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    assert len(first["named"]) == mod.MAX_NAMED
    assert len(host.rest.posted[0][1]["allowed_mentions"]["users"]) == 10
    assert _queue(host) == ["610", "611"]
    soon = mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S + 600)
    assert soon["reason"] == "gap"
    later = mod.flush_welcomes(host, GUILD, _config(host),
                               NOW + mod.GATHER_S + mod.WELCOME_GAP_S)
    assert later["named"] == ["610", "611"]
    assert len(host.rest.posted) == 2


def test_the_sweep_posts_the_welcome_in_the_admins_channel_only(host):
    _on(host)
    _store(host).set(mod.NEWCOMERS_KEY, [{"user": "501", "at": NOW}])
    assert mod.sweep(host, NOW + mod.GATHER_S) == {GUILD: "welcomed"}
    assert [c for c, _ in host.rest.posted] == [CHANNEL]


def test_a_join_line_in_the_channel_is_queued_and_replied_under(host):
    _on(host)
    assert _say(host, "", user="501", mid="join1", kind=mod.JOIN_MESSAGE_TYPE) is False
    assert _queue(host) == ["501"]
    entries = _store(host).get(mod.NEWCOMERS_KEY)
    entries[0]["at"] = NOW
    _store(host).set(mod.NEWCOMERS_KEY, entries)
    mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    payload = host.rest.posted[0][1]
    assert payload["message_reference"] == {"message_id": "join1",
                                            "fail_if_not_exists": False}


def test_a_join_line_in_another_channel_is_ignored(host):
    _on(host)
    _say(host, "", user="501", channel=OTHER, mid="join1", kind=mod.JOIN_MESSAGE_TYPE)
    assert _queue(host) == []


def test_a_member_who_left_is_not_welcomed(host):
    _on(host)
    _join(host, 501)
    host.dispatch_member_change("on_member_remove", GUILD, "501", [])
    assert _queue(host) == []


def test_welcome_off_keeps_the_queue_empty(host):
    _on(host, welcome=False)
    _join(host, 501)
    assert _queue(host) == []


# ── intro replies ────────────────────────────────────────────────────────────

INTRO = "I'm a software engineer, willing to contribute"


def test_an_intro_gets_one_reply_with_the_server_orientation(host):
    _on(host, claim_channel=CLAIM, repos=["occupysaas/salescompel"])
    _join(host, 501)
    assert _say(host, INTRO, mid="hello1") is True
    channel, payload = host.rest.posted[0]
    assert channel == CHANNEL
    assert "<@501>" in payload["content"]
    assert "occupysaas/salescompel" in payload["content"]
    assert f"<#{CLAIM}>" in payload["content"]
    assert payload["message_reference"]["message_id"] == "hello1"
    assert payload["allowed_mentions"] == {"parse": [], "users": ["501"],
                                           "replied_user": False}
    assert _queue(host) == []                           # answered: not in a welcome too
    assert _say(host, "I have projects I'd like to add, can anyone direct me",
                mid="hello2") is False
    assert len(host.rest.posted) == 1


@pytest.mark.parametrize("text", [
    "I'm a software engineer, willing to contribute",
    "I have projects I'd like to add to the movement, can anyone direct me",
    "Hi everyone! How can I help?",
    "new here, where do I start?",
])
def test_the_prefilter_catches_the_intros_seen_live(text):
    assert mod.looks_like_intro(text)


@pytest.mark.parametrize("text", ["lol", "the build is red on main", "", "thanks!"])
def test_the_prefilter_lets_chatter_through(text):
    assert not mod.looks_like_intro(text)


def test_chatter_gets_no_reply(host):
    _on(host)
    _new(host, 501)
    assert _say(host, "the build is red on main") is False
    assert host.rest.posted == []


def test_an_intro_in_another_channel_gets_no_reply(host):
    _on(host)
    _new(host, 501)
    assert _say(host, INTRO, channel=OTHER) is False
    assert host.rest.posted == []


def test_the_model_decides_after_the_prefilter(host, monkeypatch):
    _on(host)
    _new(host, 501, 502, 503)
    no = Agent("NO")
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: no])
    assert _say(host, INTRO, user="501") is False
    assert no.asked and no.asked[0][1] == mod.INTRO_PROMPT
    assert INTRO in no.asked[0][0]
    broken = Agent(raises=True)
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: broken])
    assert _say(host, INTRO, user="502") is False
    yes = Agent("YES")
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: yes])
    assert _say(host, INTRO, user="503") is True
    assert len(host.rest.posted) == 1


def test_the_model_is_not_asked_about_chatter(host, monkeypatch):
    _on(host)
    _new(host, 501)
    agent = Agent("YES")
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: agent])
    _say(host, "the build is red on main")
    assert agent.asked == []


def test_only_a_members_first_messages_are_looked_at(host):
    _on(host)
    _new(host, 501)
    for i in range(mod.INTRO_LOOK):
        _say(host, f"chatter {i}", mid=f"c{i}")
    assert _say(host, INTRO, mid="late") is False
    assert host.rest.posted == []


def test_an_intro_after_a_welcome_points_at_it(host):
    _on(host, claim_channel=CLAIM)
    _new(host, 501)
    _store(host).set(mod.NEWCOMERS_KEY, [{"user": "501", "at": NOW}])
    mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    assert _say(host, INTRO, mid="hello1") is True
    reply = host.rest.posted[1][1]["content"]
    assert f"https://discord.com/channels/{GUILD}/{CHANNEL}/w1" in reply
    assert f"<#{CLAIM}>" in reply
    assert "**Projects**" not in reply                  # not the whole text again


def test_intros_off_answers_nothing(host):
    _on(host, intros=False)
    _new(host, 501)
    assert _say(host, INTRO) is False
    assert host.rest.posted == []


# ── the gateway hands a hook Discord's message type ──────────────────────────

def test_message_type_reads_discord_py_enums_and_ints():
    assert gateway._message_type(SimpleNamespace(type=SimpleNamespace(value=7))) == 7
    assert gateway._message_type(SimpleNamespace(type=0)) == 0
    assert gateway._message_type(SimpleNamespace()) == 0


def test_the_gateway_puts_the_join_type_on_a_hooked_message():
    bot = gateway.Bot(TOKEN, Agent())
    bot.host = SimpleNamespace(has_message_hooks=lambda g: True)
    seen: list = []
    bot.handle_guild_message = lambda msg: seen.append(msg) or True
    author = SimpleNamespace(id=501, bot=False, roles=[], display_name="n", name="n")
    message = SimpleNamespace(
        id=9, content="", guild=SimpleNamespace(id=int(GUILD), name="g"),
        channel=SimpleNamespace(id=int(CHANNEL), name="general"), author=author,
        mentions=[], reference=None, embeds=[], attachments=[],
        type=SimpleNamespace(value=7))
    asyncio.run(bot.dispatch_message(message, SimpleNamespace(id=1)))
    assert seen and seen[0]["type"] == 7


# ── review fixes: who is judged, consent, budget, claims, routing ────────────

@pytest.mark.parametrize("text", ["I'm a bit confused, hi all", "I'm a fan of this",
                                  "hi all, the build is red again"])
def test_the_prefilter_no_longer_matches_ordinary_talk(text):
    assert not mod.looks_like_intro(text)


def test_an_established_member_is_never_answered_as_an_intro(host):
    # The day orientation is turned on, a regular's next messages are NOT "first
    # messages": no join record and no recent Discord join time = never judged.
    _on(host)
    for i, text in enumerate(["I'm a bit confused, hi all", "hi everyone",
                              "how can I help with the release?", INTRO]):
        assert _say(host, text, user="900", mid=f"r{i}") is False
    old = time.time() - 30 * 86400.0
    assert _say(host, INTRO, user="901", joined=old) is False
    assert host.rest.posted == []
    assert _store(host).get(mod.MEMBER_PREFIX + "900") is None   # not even counted


def test_discord_join_time_alone_marks_a_newcomer(host):
    _on(host)
    assert _say(host, INTRO, user="902", joined=time.time() - 60) is True
    assert len(host.rest.posted) == 1


def test_a_v010_config_posts_no_welcome_and_no_intro_reply(host):
    # {channel, enabled, every_hours}: what /orientation set wrote before this
    # change, possibly by an operator. Only its periodic post stays.
    _store(host).set(mod.CONFIG_KEY, {"channel": CHANNEL, "enabled": True,
                                      "every_hours": 24.0, "last_at": NOW})
    _join(host, 501)
    assert _queue(host) == []
    assert _say(host, INTRO, user="501", joined=time.time()) is False
    assert _say(host, "", user="502", mid="j", kind=mod.JOIN_MESSAGE_TYPE) is False
    assert _queue(host) == []
    assert mod.flush_welcomes(host, GUILD, _config(host), NOW + HOUR)["reason"] == "off"
    assert host.has_message_hooks(GUILD) is False
    assert mod.sweep(host, NOW + 25 * HOUR) == {GUILD: "posted"}   # the old post only
    assert len(host.rest.posted) == 1
    assert "Welcome to" in host.rest.posted[0][1]["content"]


def test_the_new_set_records_who_consented(host):
    host.handle(_it("set", user="77", channel=CHANNEL))
    config = _config(host)
    assert config["schema"] == mod.SCHEMA and config["newcomers_by"] == "77"
    assert mod.welcome_on(config) and mod.intros_on(config)


def test_twenty_intros_in_an_hour_get_one_reply_and_one_welcome(host):
    _on(host)
    users = [str(700 + i) for i in range(20)]
    _new(host, *users)
    answered = [_say(host, INTRO, user=u, mid=f"i{u}") for u in users]
    assert answered.count(True) == 1
    assert len(host.rest.posted) == 1
    queued = _queue(host)
    assert len(queued) == 19 and users[0] not in queued
    entries = _store(host).get(mod.NEWCOMERS_KEY)
    for e in entries:
        e["at"] = NOW
    _store(host).set(mod.NEWCOMERS_KEY, entries)
    for minute in range(5, 60, 5):
        mod.flush_welcomes(host, GUILD, _config(host), NOW + minute * 60)
    assert len(host.rest.posted) == 2                    # budget (1) + ONE welcome
    assert len(host.rest.posted[1][1]["allowed_mentions"]["users"]) == mod.MAX_NAMED
    # a queued intro is never answered later as well
    assert _say(host, INTRO, user=users[1], mid="again") is False


class FlakyRest(FakeRest):
    def __init__(self, exc):
        super().__init__()
        self.exc, self.calls = exc, 0

    def create_message(self, channel, payload):
        self.calls += 1
        if self.calls == 1:
            raise self.exc
        return super().create_message(channel, payload)


def _queued_for_flush(host, *users):
    _store(host).set(mod.NEWCOMERS_KEY, [{"user": u, "at": NOW} for u in users])


def test_a_timeout_after_posting_never_posts_the_welcome_twice(host):
    _on(host)
    host.rest = FlakyRest(TimeoutError("read timed out"))   # Discord may have posted
    _queued_for_flush(host, "501", "502")
    with pytest.raises(TimeoutError):
        mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    for later in (60, 120, mod.WELCOME_GAP_S + 60):
        mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S + later)
    assert host.rest.posted == []
    assert _queue(host) == []
    assert _store(host).get(mod.MEMBER_PREFIX + "501")["welcomed_at"]


def test_a_definite_refusal_rolls_the_claim_back_for_a_retry(host):
    from awdiscord.rest import RestError
    _on(host)
    host.rest = FlakyRest(RestError(400, "bad"))
    _queued_for_flush(host, "501")
    with pytest.raises(RestError):
        mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    assert _queue(host) == ["501"]
    out = mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S + 60)
    assert out["named"] == ["501"] and len(host.rest.posted) == 1


def test_an_intro_while_its_welcome_is_posting_gets_no_second_message(host):
    _on(host)
    _new(host, 501)
    _store(host).update(mod.MEMBER_PREFIX + "501", lambda m: (
        {**m, "welcomed_at": NOW, "welcome_message": ""}, None), {})
    assert _say(host, INTRO, user="501") is False
    assert host.rest.posted == []


def test_a_welcome_write_keeps_a_concurrent_intro_mark(host):
    _on(host)
    _new(host, 501)
    _queued_for_flush(host, "501")
    _store(host).update(mod.MEMBER_PREFIX + "501",
                        lambda m: ({**m, "intro_at": NOW}, None), {})
    out = mod.flush_welcomes(host, GUILD, _config(host), NOW + mod.GATHER_S)
    assert out["posted"] is False                       # answered already: not named
    assert _store(host).get(mod.MEMBER_PREFIX + "501")["intro_at"] == NOW


def _message(content="", kind=0, joined=None):
    author = SimpleNamespace(id=501, bot=False, roles=[], display_name="n", name="n",
                             joined_at=joined)
    return SimpleNamespace(
        id=9, content=content, guild=SimpleNamespace(id=int(GUILD), name="g"),
        channel=SimpleNamespace(id=int(CHANNEL), name="general"), author=author,
        mentions=[], reference=None, embeds=[], attachments=[],
        type=SimpleNamespace(value=kind))


def test_an_unconfigured_guild_stays_off_the_hook_route(host):
    assert host.enabled(GUILD, mod.NAME)                # on by default, but no config
    assert host.has_message_hooks(GUILD) is False
    bot = gateway.Bot(TOKEN, Agent())
    bot.host = host
    called: list = []
    bot.handle_guild_message = lambda msg: called.append(msg) or True
    asyncio.run(bot.dispatch_message(_message(content="hello"), SimpleNamespace(id=1)))
    assert called == []                                 # IGNORE: no thread hop
    _on(host)
    assert host.has_message_hooks(GUILD) is True


class Brains:
    """The production resolver's contract: resolve(guild) -> an object with .mode."""

    def __init__(self, mode):
        self.mode, self.asked = mode, []

    def resolve(self, guild, user=""):
        self.asked.append(guild)
        return SimpleNamespace(mode=self.mode, agent=None)


def test_a_hosted_partner_guild_reaches_the_hook_through_the_real_gate(host):
    # Measured live 2026-11-13: this guild resolves through mode "hosted", and every
    # message_hook_skipped there was paired with "hosted lookup failed": the resolver
    # answered none for that moment, not a served-list mismatch.
    _on(host)
    bot = gateway.Bot(TOKEN, Agent())
    bot.host, bot.brains = host, Brains("hosted")
    joined = datetime.datetime.now(datetime.timezone.utc)
    asyncio.run(bot.dispatch_message(_message(INTRO, joined=joined),
                                     SimpleNamespace(id=1)))
    assert GUILD in bot.brains.asked
    assert len(host.rest.posted) == 1 and "<@501>" in host.rest.posted[0][1]["content"]


def test_a_guild_the_resolver_answers_none_for_gets_no_hook(host):
    _on(host)
    bot = gateway.Bot(TOKEN, Agent())
    bot.host, bot.brains = host, Brains("none")
    joined = datetime.datetime.now(datetime.timezone.utc)
    asyncio.run(bot.dispatch_message(_message(INTRO, joined=joined),
                                     SimpleNamespace(id=1)))
    assert host.rest.posted == []


def test_the_gateway_puts_the_join_time_on_a_hooked_message():
    when = datetime.datetime(2026, 11, 13, tzinfo=datetime.timezone.utc)
    assert gateway._joined_at(SimpleNamespace(joined_at=when)) == when.timestamp()
    assert gateway._joined_at(SimpleNamespace()) == 0.0
    bot = gateway.Bot(TOKEN, Agent())
    bot.host = SimpleNamespace(has_message_hooks=lambda g: True)
    seen: list = []
    bot.handle_guild_message = lambda msg: seen.append(msg) or True
    asyncio.run(bot.dispatch_message(_message(joined=when), SimpleNamespace(id=1)))
    assert seen[0]["joined_at"] == when.timestamp()


# ── review fixes (PR #16712) ─────────────────────────────────────────────────

def test_the_intro_judge_is_a_partner_turn_scoped_to_the_guild_without_tools(
        host, monkeypatch):
    """A stranger's text must never reach Genesis as the owner's private DM turn."""
    import json as _json

    from awdiscord import agent as agent_mod
    sent: list[dict] = []

    def fake_urlopen_json(req, **kw):
        sent.append(_json.loads(req.data.decode()))
        return {"response": "YES"}

    monkeypatch.setattr(agent_mod, "urlopen_json", fake_urlopen_json)
    genesis = agent_mod.GenesisAgent("https://genesis.invalid:8001")
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: genesis])
    _on(host)
    _new(host, 222)
    assert _say(host, INTRO, user="222") is True
    assert len(sent) == 1
    payload = sent[0]
    assert payload["context"]["surface"] == "partner"
    assert payload["context"]["guild_id"] == GUILD
    assert payload["context"]["channel_id"] == CHANNEL
    assert payload["agentic"] is False
    assert not payload["session_id"].startswith("discord-dm-")


def test_a_raid_of_fresh_accounts_costs_a_bounded_number_of_model_turns(
        host, monkeypatch):
    _on(host)
    users = [str(900 + i) for i in range(20)]
    _new(host, *users)
    no = Agent("NO")
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: no])
    for u in users:
        for i in range(mod.INTRO_LOOK):
            _say(host, "hi everyone", user=u, mid=f"r{u}{i}")
    assert len(no.asked) <= mod.INTRO_JUDGE_MAX
    assert host.rest.posted == []


def test_a_raid_answered_yes_asks_the_model_once_and_posts_once(host, monkeypatch):
    _on(host)
    users = [str(950 + i) for i in range(20)]
    _new(host, *users)
    yes = Agent("YES")
    monkeypatch.setattr(mod, "AGENT", [lambda h, g, u: yes])
    for u in users:
        _say(host, INTRO, user=u, mid=f"y{u}")
    assert len(yes.asked) == 1                 # the reply budget closed: no more turns
    assert len(host.rest.posted) == 1
    assert len(_queue(host)) == 19             # welcomes on: named in the next welcome


def test_intros_only_an_over_budget_member_is_answered_once_the_budget_frees(
        host, monkeypatch):
    """welcome:false intros:true - there is no batched welcome to name them in."""
    clock = [NOW]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])
    _on(host, welcome=False)
    _new(host, 801, 802)
    assert _say(host, INTRO, user="801", mid="a1") is True
    clock[0] += 60
    assert _say(host, INTRO, user="802", mid="b1") is False      # over the budget
    assert not _store(host).get(mod.MEMBER_PREFIX + "802").get("intro_queued")
    assert _queue(host) == []
    clock[0] += 11 * 60
    assert _say(host, "how can I help?", user="802", mid="b2") is True
    replies_802 = [p for _, p in host.rest.posted if "<@802>" in p["content"]]
    assert len(replies_802) == 1
    assert len(host.rest.posted) == 2
