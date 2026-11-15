"""awdiscord.mirror -- every gateway event reaches the Relay bridge, or nothing does.

The owner (2026-11-01) wants Discord to stay the live surface while the workspace
keeps the durable record, so this hook is the WHOLE supply side of that record.
What it must never do is take the bot down: a mirror that raises inside
`on_message` stops every reply in the guild.
"""
from __future__ import annotations

from awdiscord import mirror


def test_a_message_is_normalised_for_the_bridge():
    payload = mirror.to_payload("message", {
        "id": 42, "channel_id": 7, "guild_id": 1, "content": "hello",
        "author": {"id": 9, "name": "bob", "global_name": "Bob", "bot": False},
        "attachments": [{"id": 3, "filename": "a.png", "url": "https://x/a.png"}]})
    assert payload["id"] == "42" and payload["guild_id"] == "1"
    assert payload["author"]["username"] == "bob"
    assert payload["attachments"][0]["filename"] == "a.png"
    assert payload["_normalized"] is True


def test_a_delete_carries_only_the_ids():
    payload = mirror.to_payload("bulk_message_delete",
                                {"message_ids": [1, 2], "channel_id": 5, "guild_id": 1})
    assert payload["ids"] == [1, 2] and payload["channel_id"] == "5"


def test_a_thread_keeps_its_parent():
    payload = mirror.to_payload("thread_create", {
        "id": 11, "name": "brief", "parent_id": 5, "guild": {"id": 1}, "type": 11})
    assert payload["parent_id"] == "5" and payload["guild_id"] == "1"


def test_an_unknown_kind_is_refused_rather_than_posted():
    assert mirror.feed("nonsense", {"id": 1}) is False


def test_a_payload_without_an_id_is_refused(monkeypatch):
    monkeypatch.setenv("AWDISCORD_MIRROR_GUILDS", "1")
    assert mirror.feed("message", {"content": "no id", "guild_id": 1}) is False


def test_stats_names_the_target_without_the_key(monkeypatch):
    monkeypatch.setenv("AITHER_INTERNAL_SECRET", "sekrit-value")
    stats = mirror.stats()
    assert stats["internal_key"] is True
    assert "sekrit-value" not in repr(stats)


# -- one surface #16333: the gateway hands the mirror every message of a mirrored guild


def test_only_the_named_guilds_are_mirrored(monkeypatch):
    monkeypatch.setenv("AWDISCORD_MIRROR_GUILDS", "111, 222")
    assert mirror.mirror_guilds() == frozenset({"111", "222"})
    sent = []
    monkeypatch.setattr(mirror, "feed", lambda kind, obj: sent.append(kind) or True)
    assert mirror.feed_guild("message", {"id": 1}, 111) is True
    assert mirror.feed_guild("message", {"id": 2}, "333") is False   # a partner server
    assert mirror.feed_guild("message", {"id": 3}, None) is False    # a DM never crosses
    assert sent == ["message"]


def test_the_home_guild_is_the_default_and_off_turns_it_off(monkeypatch):
    monkeypatch.delenv("AWDISCORD_MIRROR_GUILDS", raising=False)
    monkeypatch.setenv("AITHER_DISCORD_HOME_GUILD_ID", "147")
    assert mirror.mirror_guilds() == frozenset({"147"})
    monkeypatch.setenv("AWDISCORD_MIRROR_GUILDS", "off")
    assert mirror.mirror_guilds() == frozenset()
    monkeypatch.delenv("AITHER_DISCORD_HOME_GUILD_ID", raising=False)
    monkeypatch.delenv("AWDISCORD_MIRROR_GUILDS", raising=False)
    assert mirror.mirror_guilds() == frozenset()


def test_the_gateway_feeds_every_message_before_routing_it(monkeypatch):
    """A human message that no hook and no mention would ever see still crosses."""
    import asyncio
    from types import SimpleNamespace

    from awdiscord.gateway import Bot

    fed = []
    monkeypatch.setattr(mirror, "feed_guild",
                        lambda kind, obj, gid: fed.append((kind, gid)) or True)

    class NoHooks:
        def has_message_hooks(self, guild):
            return False

    class NoWatch:
        def is_candidate(self, channel):
            return False

    bot = Bot.__new__(Bot)
    bot.host, bot.watch, bot.brains, bot.served = NoHooks(), NoWatch(), None, None
    bot.forwarder, bot.triage, bot.state = None, None, {}
    author = SimpleNamespace(id="u1", bot=False, roles=[], display_name="jason")
    msg = SimpleNamespace(id="m1", guild=SimpleNamespace(id="147", name="home"),
                          channel=SimpleNamespace(id="c1"), author=author,
                          content="hi from discord", mentions=[], attachments=[],
                          reference=None)
    me = SimpleNamespace(id="999")
    asyncio.run(bot.dispatch_message(msg, me))
    assert fed == [("message", "147")]
    own = SimpleNamespace(**{**msg.__dict__, "author": SimpleNamespace(id="999", bot=True)})
    asyncio.run(bot.dispatch_message(own, me))
    assert fed == [("message", "147")]          # the bot's own post is never fed


def test_feed_itself_refuses_an_unmirrored_guild(monkeypatch):
    """An older gateway calls feed() directly for every event: it must still be held
    to the allowlist (2026-11-13: partner-server messages reached the bridge)."""
    monkeypatch.setenv("AWDISCORD_MIRROR_GUILDS", "147")
    queued = []
    monkeypatch.setattr(mirror, "_ensure_worker", lambda: None)
    monkeypatch.setattr(mirror._queue, "put_nowait", queued.append)
    msg = {"id": 1, "channel_id": 2, "content": "x", "author": {"id": 3}}
    assert mirror.feed("message", {**msg, "guild_id": 999}) is False      # partner
    assert mirror.feed("message", {**msg, "guild_id": None}) is False     # a DM
    assert mirror.feed("message", {**msg, "guild_id": 147}) is True       # home
    assert [q["payload"]["guild_id"] for q in queued] == ["147"]
    assert mirror.stats()["refused_guilds"].get("999", 0) >= 1
    edit = {"data": {"id": 5, "channel_id": 2}, "message_id": 5, "channel_id": 2,
            "guild_id": 999}
    assert mirror.feed("message_edit", edit) is False                     # raw edit too

