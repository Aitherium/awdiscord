"""The Discord origin of a turn decides which memory Genesis may use.

A partner server's public channel must never receive the owner's private project
memory. Three things on the bot side make that true: the origin rides in the
request, the session id is namespaced per server+channel+member, and a DM or the
home server keeps its private surface.
"""
import json

from awdiscord.agent import GenesisAgent


def _sent(monkeypatch):
    """Record the Genesis ``/chat`` bodies instead of making the request."""
    seen = []

    def fake(req, *, timeout, context, who, **kw):
        seen.append(json.loads(req.data.decode()))
        return {"response": "ok"}

    monkeypatch.setattr("awdiscord.agent.urlopen_json", fake)
    return seen


def test_partner_guild_turn_is_marked_partner_and_guild_scoped(monkeypatch):
    monkeypatch.setenv("AITHER_DISCORD_HOME_GUILD_ID", "111111111111111111")
    seen = _sent(monkeypatch)
    GenesisAgent("https://genesis.test").ask(
        "hi", user="178326506513235968",
        guild_id="1561399233846972457", channel_id="222222222222222222")
    body = seen[0]
    assert body["context"]["surface"] == "partner"
    assert body["context"]["guild_id"] == "1561399233846972457"
    assert body["context"]["channel_id"] == "222222222222222222"
    assert body["session_id"] == (
        "discord-1561399233846972457-222222222222222222-178326506513235968")


def test_home_guild_stays_private(monkeypatch):
    monkeypatch.setenv("AWDISCORD_HOME_GUILD_ID", "111111111111111111")
    seen = _sent(monkeypatch)
    GenesisAgent("https://genesis.test").ask(
        "hi", user="42", guild_id="111111111111111111", channel_id="c1")
    body = seen[0]
    assert body["context"]["surface"] == "home"
    assert body["session_id"] == "discord-111111111111111111-c1-42"


def test_dm_stays_private(monkeypatch):
    monkeypatch.delenv("AITHER_DISCORD_HOME_GUILD_ID", raising=False)
    monkeypatch.delenv("AWDISCORD_HOME_GUILD_ID", raising=False)
    seen = _sent(monkeypatch)
    GenesisAgent("https://genesis.test").ask("hi", user="42")
    body = seen[0]
    assert body["context"]["surface"] == "dm"
    assert body["session_id"] == "discord-dm-dm-42"


def test_unset_home_makes_every_guild_a_partner(monkeypatch):
    for name in ("AITHER_DISCORD_HOME_GUILD_ID", "AWDISCORD_HOME_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    seen = _sent(monkeypatch)
    GenesisAgent("https://genesis.test").ask("hi", user="42", guild_id="1561399233846972457")
    assert seen[0]["context"]["surface"] == "partner"


def test_dispatcher_forwards_the_turn_origin(monkeypatch):
    from awdiscord.dispatch import Dispatcher, RateLimiter

    seen = []

    class Agent:
        def ask(self, prompt, *, user="", guild_id="", channel_id="", **k):
            seen.append((guild_id, channel_id))
            return "ok"

    d = Dispatcher(Agent(), RateLimiter(per_user=(100, 60.0)))
    d.chat("7", "1561399233846972457", "hi there", channel="c1")
    assert seen == [("1561399233846972457", "c1")]


def test_voice_turn_without_a_dispatcher_still_names_its_guild():
    # The local fallback (no dispatcher, or no speaker bound yet) must not lose
    # the server it was spoken in: the session id and the memory surface both
    # depend on it, and an absent guild reads as a DM.
    from awdiscord.voice import BotThinker

    seen = []

    class Agent:
        def ask(self, prompt, **kwargs):
            seen.append(kwargs)
            return "ok"

    thinker = BotThinker(Agent(), guild_id="1561399233846972457", channel_id="c1")
    thinker.think("hi")
    assert seen[0]["guild_id"] == "1561399233846972457"
    assert seen[0]["channel_id"] == "c1"
