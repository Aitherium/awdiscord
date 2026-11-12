"""Orientation: an admin-configured welcome post, on a timer, that never spams.

The rules under test: the interval defaults to 24h and cannot go below 6h; a
sweep skips the post while our previous one is still among the channel's last
twenty messages; only server managers can configure it; and the server's name
comes from Discord rather than a hardcoded string.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord.builtin import orientation as mod  # noqa: E402
from awdiscord.plugins import MANAGE_GUILD, PluginHost, ScopedStore  # noqa: E402
from awdiscord.served import SETUP_URL  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

GUILD = "1561399233846972457"
CHANNEL = "1566572632546091141"
ADMIN = str(MANAGE_GUILD)
NOW = 1_800_000_000.0
HOUR = 3600.0


class FakeRest:
    """The two Discord reads the post needs, plus the write."""

    def __init__(self, name: str = "Occupy SaaS", history: list | None = None):
        self.name = name
        self.history = [dict(m) for m in history or []]
        self.posted: list[tuple[str, dict]] = []
        self.reads: list[str] = []
        self.history_fails = False

    def request(self, method, path, body=None):
        assert method == "GET"
        if path.startswith("/guilds/"):
            return {"id": path.rsplit("/", 1)[-1], "name": self.name}
        if "/messages?limit=" in path:
            self.reads.append(path)
            if self.history_fails:
                raise RuntimeError("Discord answered 500")
            return [dict(m) for m in self.history]
        raise AssertionError(f"unexpected read {path}")

    def create_message(self, channel, payload):
        self.posted.append((channel, payload))
        message = {"id": f"m{len(self.posted)}"}
        self.history.insert(0, {"id": message["id"], "content": payload["content"],
                                "author": {"id": "999"}})
        return message


@pytest.fixture
def host():
    h = PluginHost(GuildStore(":memory:"), plugins=[mod.PLUGIN])
    h.rest = FakeRest()
    h.store.set_plugin_state(GUILD, mod.NAME, True)   # makes guilds() see GUILD
    return h


def _store(host, guild=GUILD) -> ScopedStore:
    return ScopedStore(host.store, guild, mod.NAME)


def _config(host, guild=GUILD) -> dict:
    return _store(host, guild).get(mod.CONFIG_KEY) or {}


def _it(verb, perms="0", user="7", guild=GUILD, **opts):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}, "permissions": perms},
            "data": {"name": mod.NAME,
                     "options": [{"type": 1, "name": verb,
                                  "options": [{"name": k, "value": v}
                                              for k, v in opts.items()]}]}}


def _content(result) -> str:
    return result.response["data"]["content"]


def _enable(host, channel=CHANNEL, last_at=NOW - 25 * HOUR, last_message_id=""):
    _store(host).set(mod.CONFIG_KEY, {"channel": channel, "every_hours": 24.0,
                                      "enabled": True, "last_at": last_at,
                                      "last_message_id": last_message_id})


# ── configuration ────────────────────────────────────────────────────────────

def test_set_stores_the_channel_and_defaults_to_24h(host):
    result = host.handle(_it("set", perms=ADMIN, channel=CHANNEL))
    assert f"<#{CHANNEL}>" in _content(result)
    assert "24h" in _content(result)
    config = _config(host)
    assert config["channel"] == CHANNEL
    assert config["every_hours"] == 24.0
    assert config["enabled"] is True


def test_set_keeps_an_interval_already_chosen(host):
    host.handle(_it("set", perms=ADMIN, channel=CHANNEL, every=12))
    host.handle(_it("set", perms=ADMIN, channel=CHANNEL))
    assert _config(host)["every_hours"] == 12.0


def test_a_member_cannot_configure_or_post_or_disable_it(host):
    for verb in ("set", "post", "off"):
        result = host.handle(_it(verb, perms="0", channel=CHANNEL))
        assert "Only server managers can do that." in _content(result)
    assert _config(host) == {}
    assert host.rest.posted == []


def test_an_interval_below_six_hours_is_refused(host):
    result = host.handle(_it("set", perms=ADMIN, channel=CHANNEL, every=2))
    assert "at most once every 6 hours" in _content(result)
    assert _config(host) == {}
    assert mod.normalize_hours(6) == 6.0


def test_due_enforces_the_six_hour_floor_even_for_a_stored_value(host):
    config = {"channel": CHANNEL, "enabled": True, "every_hours": 1}
    assert mod.due({**config, "last_at": NOW - 2 * HOUR}, NOW) is False
    assert mod.due({**config, "last_at": NOW - 6 * HOUR - 1}, NOW) is True


def test_due_defaults_to_twenty_four_hours(host):
    config = {"channel": CHANNEL, "enabled": True}
    assert mod.due({**config, "last_at": NOW - 23 * HOUR}, NOW) is False
    assert mod.due({**config, "last_at": NOW - 25 * HOUR}, NOW) is True
    assert mod.due({**config, "enabled": False, "last_at": 0}, NOW) is False
    assert mod.due({"channel": "", "last_at": 0}, NOW) is False


# ── the post ─────────────────────────────────────────────────────────────────

def test_post_now_posts_a_short_text_with_the_discord_server_name(host):
    host.handle(_it("set", perms=ADMIN, channel=CHANNEL))
    result = host.handle(_it("post", perms=ADMIN))
    done = result.deferred()                      # the post happens after the ack
    assert len(host.rest.posted) == 1
    channel, payload = host.rest.posted[0]
    text = payload["content"]
    assert channel == CHANNEL
    assert "Occupy SaaS" in text                  # read from GET /guilds/<id>
    assert "@Aither" in text and "`/ask`" in text
    assert "`/selfhost`" in text and "`/model`" in text
    assert SETUP_URL in text
    assert len(text.splitlines()) <= 6            # short and plain
    assert _config(host)["last_message_id"] == "m1"
    assert f"<#{CHANNEL}>" in done["content"]


def test_the_server_name_is_not_hardcoded(host):
    host.rest.name = "Some Other Server"
    host.handle(_it("set", perms=ADMIN, channel=CHANNEL))
    host.handle(_it("post", perms=ADMIN)).deferred()
    text = host.rest.posted[0][1]["content"]
    assert "Some Other Server" in text
    assert "Occupy SaaS" not in text


def test_post_before_a_channel_is_chosen_says_so(host):
    result = host.handle(_it("post", perms=ADMIN))
    assert "Set an entrance channel first" in _content(result)
    assert host.rest.posted == []


# ── the sweep and the skip-if-recent rule ────────────────────────────────────

def test_the_sweep_posts_when_due(host):
    _enable(host, last_at=NOW - 25 * HOUR)
    assert mod.sweep(host, NOW) == {GUILD: "posted"}
    assert len(host.rest.posted) == 1
    config = _config(host)
    assert config["last_at"] == NOW
    assert config["last_message_id"] == "m1"


def test_the_sweep_skips_while_our_last_post_is_still_recent(host):
    host.rest.history = [{"id": "old", "content": "welcome"}]
    _enable(host, last_at=NOW - 25 * HOUR, last_message_id="old")
    assert mod.sweep(host, NOW) == {GUILD: "recent"}
    assert host.rest.posted == []
    # The check time is recorded so the daemon does not re-read every minute,
    # and the marker is kept so a later sweep can still find the message.
    assert _config(host)["last_at"] == NOW
    assert _config(host)["last_message_id"] == "old"


def test_the_sweep_posts_once_our_last_post_has_scrolled_past(host):
    _enable(host, last_at=NOW - 25 * HOUR, last_message_id="old")
    host.rest.history = [{"id": f"n{i}", "content": "chatter"} for i in range(20)]
    now = NOW + 24 * HOUR + 1
    assert mod.sweep(host, now) == {GUILD: "posted"}
    assert len(host.rest.posted) == 1


def test_an_unreadable_history_does_not_risk_a_second_post(host):
    _enable(host)
    host.rest.history_fails = True
    assert mod.sweep(host, NOW) == {GUILD: "history_unreadable"}
    assert host.rest.posted == []


def test_off_stops_the_sweep(host):
    host.handle(_it("set", perms=ADMIN, channel=CHANNEL))
    _enable(host, last_at=NOW - 25 * HOUR)
    assert "off" in _content(host.handle(_it("off", perms=ADMIN))).lower()
    assert _config(host)["enabled"] is False
    assert mod.sweep(host, NOW + 48 * HOUR) == {}
    assert host.rest.posted == []


def test_a_disabled_plugin_is_not_swept(host):
    _enable(host, last_at=NOW - 25 * HOUR)
    host.set_enabled(GUILD, mod.NAME, False)
    assert mod.sweep(host, NOW) == {}
    assert host.rest.posted == []


def test_the_explicit_post_ignores_the_recent_rule(host):
    host.rest.history = [{"id": "old", "content": "welcome"}]
    _enable(host, last_at=NOW - HOUR, last_message_id="old")
    result = host.handle(_it("post", perms=ADMIN))
    # Acknowledged before any Discord call: nothing is posted yet, and the reply is
    # the private "thinking" state, which is what keeps Discord from timing it out.
    assert result.response == {"type": 5, "data": {"flags": 64}}
    assert host.rest.posted == []
    done = result.deferred()
    assert len(host.rest.posted) == 1
    assert "Posted the orientation" in done["content"]
    assert _store(host).get(mod.CONFIG_KEY)["last_message_id"]


def test_the_explicit_post_reports_a_failure_in_the_deferred_reply(host):
    _enable(host)

    def _unreadable(*args, **kwargs):
        raise RuntimeError("history down")

    host.rest.request = _unreadable
    result = host.handle(_it("post", perms=ADMIN))
    assert result.response["type"] == 5
    done = result.deferred()
    assert host.rest.posted == []
    assert "Could not post the orientation" in done["content"]


def test_the_explicit_post_without_a_channel_answers_at_once(host):
    result = host.handle(_it("post", perms=ADMIN))
    assert result.response["type"] == 4, "no Discord call is needed to say this"
    assert "Set an entrance channel first" in _content(result)


class _CaptureRest:
    def __init__(self):
        self.sent = []

    def channel_messages(self, channel, limit=20):
        return []

    def get_guild(self, guild):
        return {"name": "Test Server"}

    def create_message(self, channel, body):
        self.sent.append(body)
        return {"id": "m1"}


def test_a_server_text_replaces_the_generic_post_and_suppresses_previews(monkeypatch):
    from awdiscord.builtin import orientation as o
    rest = _CaptureRest()
    monkeypatch.setattr(o, "recent_message_ids", lambda r, c, limit=20: [])

    class H:
        pass
    h = H()
    h.rest = rest
    out = o.post_orientation(h, "1", {"channel": "2", "text": "Welcome to X. Start in #general."})
    assert out["posted"] is True
    assert rest.sent[0]["content"] == "Welcome to X. Start in #general."
    assert rest.sent[0]["flags"] == o.SUPPRESS_EMBEDS
    assert rest.sent[0]["allowed_mentions"] == {"parse": []}


def test_no_server_text_falls_back_to_the_generic_post(monkeypatch):
    from awdiscord.builtin import orientation as o
    rest = _CaptureRest()
    monkeypatch.setattr(o, "recent_message_ids", lambda r, c, limit=20: [])
    monkeypatch.setattr(o, "server_name", lambda r, g: "Test Server")

    class H:
        pass
    h = H()
    h.rest = rest
    o.post_orientation(h, "1", {"channel": "2"})
    assert "Test Server" in rest.sent[0]["content"]
