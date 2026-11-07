"""A guild's hooks follow the brain resolver, not the old served-guild allowlist.

A hosted server (inference bought from the platform) and a BYO server (its own
model) are served: plugin message hooks, member joins and triage candidates run
there. A server resolving to ``none`` is skipped. A resolver that raises is a
refusal, never an allow.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from awdiscord.brain import BYO, HOSTED, NONE, Brain, served_by_brain
from awdiscord.gateway import Bot
from awdiscord.served import ServedGuilds
from awdiscord.triage import Candidate

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped


class ModeResolver:
    """Stands in for BrainResolver: the contract is ``resolve(guild, user).mode``."""

    def __init__(self, modes: dict[str, str], boom: tuple[str, ...] = ()):
        self.modes = modes
        self.boom = set(boom)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, guild, user=""):
        self.calls.append((guild, user))
        if guild in self.boom:
            raise RuntimeError("resolver exploded")
        return Brain(self.modes.get(str(guild), NONE), reason="test")


class RecordingHost:
    def __init__(self):
        self.hooks = []
        self.member_joins = []
        self.guild_joins = []

    def has_message_hooks(self, guild):
        return True

    def dispatch_message(self, msg):
        self.hooks.append(msg)
        return True

    def dispatch_member_join(self, guild, member, send_dm):
        self.member_joins.append((guild, member))
        return {"orientation": "ok"}

    def dispatch_guild_join(self, guild):
        self.guild_joins.append(guild)
        return {"plugins": "ok"}


class FakeRest:
    def __init__(self):
        self.posted = []

    def create_message(self, channel_id, payload):
        self.posted.append((channel_id, payload))
        return {"id": "m1"}


class FakeWatch:
    def is_candidate(self, channel):
        return True


class ExplodingTriage:
    def submit(self, candidate):
        raise AssertionError("triage reached the model for an unserved server")


def _bot(modes, *, boom=(), host=None):
    bot = Bot.__new__(Bot)
    bot.rest = FakeRest()
    bot.host = host if host is not None else RecordingHost()
    bot.brains = ModeResolver(modes, boom)
    bot.served = None
    bot.watch = FakeWatch()
    bot.triage = None
    bot.state = {"user_id": "999"}
    bot.forwarder = None
    bot.app_id = "999"
    return bot


def _message(guild="g-hosted", channel="c1", user="u1", content="hello"):
    author = SimpleNamespace(id=user, bot=False, roles=[], display_name="member")
    guild_obj = SimpleNamespace(id=guild, name="Test Server") if guild else None
    return SimpleNamespace(id="m1", guild=guild_obj,
                           channel=SimpleNamespace(id=channel), author=author,
                           content=content, mentions=[], attachments=[],
                           reference=None)


def _client_user():
    return SimpleNamespace(id="999")


def _send(bot, message):
    asyncio.run(bot.dispatch_message(message, _client_user()))


def test_a_hosted_guild_runs_message_hooks():
    bot = _bot({"g-hosted": HOSTED})
    _send(bot, _message("g-hosted"))
    assert len(bot.host.hooks) == 1
    assert bot.host.hooks[0]["guild_id"] == "g-hosted"


def test_a_hosted_guild_runs_member_join_hooks():
    bot = _bot({"g-hosted": HOSTED})
    assert bot.handle_member_join("g-hosted", {"id": "u2"}) == {"orientation": "ok"}
    assert bot.host.member_joins == [("g-hosted", {"id": "u2"})]


def test_a_byo_guild_runs_message_hooks():
    bot = _bot({"g-byo": BYO})
    _send(bot, _message("g-byo"))
    assert len(bot.host.hooks) == 1
    assert bot.handle_member_join("g-byo", {"id": "u2"}) == {"orientation": "ok"}


def test_an_unserved_guild_still_skips_every_hook_path():
    bot = _bot({})                       # every guild resolves to NONE
    bot.triage = ExplodingTriage()
    _send(bot, _message("g-none"))
    assert bot.host.hooks == []
    assert bot.handle_member_join("g-none", {"id": "u2"}) == {}
    assert bot.handle_guild_join({"id": "g-none"}) == {"setup": "skipped"}
    assert bot.host.guild_joins == []
    assert bot.handle_candidate(
        Candidate(channel="c1", text="a bug?", guild="g-none", user="u1")) == []


def test_a_resolver_error_is_a_refusal():
    bot = _bot({}, boom=("g-broken",))
    _send(bot, _message("g-broken"))
    assert bot.host.hooks == []
    assert bot.handle_member_join("g-broken", {"id": "u2"}) == {}
    assert bot.host.member_joins == []


def test_the_helper_accepts_owner_byo_and_hosted_and_refuses_none():
    resolver = ModeResolver({"g1": "owner", "g2": BYO, "g3": HOSTED, "g4": NONE})
    for guild in ("g1", "g2", "g3"):
        assert served_by_brain(resolver, guild) is True
    assert served_by_brain(resolver, "g4") is False
    assert served_by_brain(resolver, "unknown") is False


def test_no_resolver_keeps_the_legacy_allowlist_and_dms_ungated():
    served = ServedGuilds(home="g-home")
    assert served_by_brain(None, "g-home", served) is True
    assert served_by_brain(None, "g-other", served) is False
    assert served_by_brain(None, None, served) is True   # a DM is not gated here
    assert served_by_brain(None, "g-other") is True      # library embedding
