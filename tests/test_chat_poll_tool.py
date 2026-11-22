"""The chat agent can post a poll when a member asks (2026-11-17: it could not)."""

from awdiscord.builtin import community as c


class _Rest:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def request(self, method, path, body=None, auth=True):
        if self.fail:
            raise RuntimeError("403 Missing Permissions")
        self.calls.append((method, path, body))
        return {"id": "m1"}


def _with(monkeypatch, rest):
    monkeypatch.setitem(c._HOST, "host", type("H", (), {"rest": rest})())


CALLER = {"user": "u1", "guild": "g1", "channel": "ch1"}


def test_tool_is_offered_to_the_chat_agent():
    assert c.POLL_TOOL in c.PLUGIN.tools
    assert c.POLL_TOOL.wants_caller
    assert c.POLL_TOOL.offered({"guild": "g", "channel": "c"})
    assert not c.POLL_TOOL.offered({"guild": "", "channel": ""})  # not in DMs


def test_posts_native_poll_in_the_askers_channel(monkeypatch):
    rest = _Rest()
    _with(monkeypatch, rest)
    out = c.start_poll("Lunch?", ["Pizza", "Tacos", "Sushi"], hours=6, multi=True,
                       caller=CALLER)
    method, path, body = rest.calls[0]
    assert (method, path) == ("POST", "/channels/ch1/messages")
    assert [a["poll_media"]["text"] for a in body["poll"]["answers"]] == [
        "Pizza", "Tacos", "Sushi"]
    assert body["poll"]["duration"] == 6 and body["poll"]["allow_multiselect"] is True
    assert "Poll posted" in out


def test_choices_as_one_string_and_dupes(monkeypatch):
    rest = _Rest()
    _with(monkeypatch, rest)
    c.start_poll("Pick", "A | B | a", caller=CALLER)
    assert len(rest.calls[0][2]["poll"]["answers"]) == 2


def test_refuses_without_channel_or_choices(monkeypatch):
    rest = _Rest()
    _with(monkeypatch, rest)
    assert "server channel" in c.start_poll("Q", ["a", "b"], caller={})
    assert "at least 2" in c.start_poll("Q", ["a"], caller=CALLER)
    assert not rest.calls


def test_too_big_for_native_says_why(monkeypatch):
    rest = _Rest()
    _with(monkeypatch, rest)
    out = c.start_poll("Q", [f"opt {i}" for i in range(11)], caller=CALLER)
    assert "more than 10" in out and not rest.calls


def test_discord_refusal_is_reported_not_raised(monkeypatch):
    _with(monkeypatch, _Rest(fail=True))
    assert "refused" in c.start_poll("Q", ["a", "b"], caller=CALLER)


def test_model_cannot_pick_the_channel():
    # caller comes from the host; the schema exposes no channel argument
    assert "channel" not in c.POLL_TOOL.parameters["properties"]


# ── server_faq: "what are the rules?" in plain words ─────────────────────────

from awdiscord.plugins import ScopedStore  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402


def _faq_host(monkeypatch):
    host = type("H", (), {"rest": _Rest(), "store": GuildStore(":memory:")})()
    monkeypatch.setitem(c._HOST, "host", host)
    return ScopedStore(host.store, "g1", c.PLUGIN.name)


def test_faq_tool_returns_this_servers_faq_and_rules(monkeypatch):
    store = _faq_host(monkeypatch)
    store.set("faq:meetings", {"answer": "Thursdays 6pm UTC"})
    store.set("rules", "Be kind.")
    out = c.server_faq(caller=CALLER)
    assert "meetings: Thursdays 6pm UTC" in out and "Be kind." in out
    assert c.FAQ_TOOL in c.PLUGIN.tools


def test_faq_tool_narrows_to_a_topic_and_reports_empty(monkeypatch):
    store = _faq_host(monkeypatch)
    assert "no FAQ or rules" in c.server_faq(caller=CALLER)
    store.set("faq:a", {"answer": "one"})
    store.set("faq:b", {"answer": "two"})
    out = c.server_faq("a", caller=CALLER)
    assert "a: one" in out and "two" not in out


def test_faq_tool_needs_a_server():
    assert "No server" in c.server_faq(caller={})
