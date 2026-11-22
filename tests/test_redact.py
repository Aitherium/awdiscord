"""Redact a phrase on request: own messages edited, others deleted only after confirm.

Measured 2026-11-16: asked to purge the owner's full name from a channel, the bot
said "I don't have a tool that edits or deletes messages". These tests pin the
capability and its limits: staff only, others deleted only with Manage Messages and
only after the asker confirms, and the phrase never in a log line.
"""
import logging
import time

from awdiscord import redact
from awdiscord.agent import _args_summary
from awdiscord.builtin import community
from awdiscord.plugins import PluginHost
from awdiscord.store import GuildStore

G, C, BOT, OWNER, MOD, MEMBER, OTHER = "100", "200", "900", "1", "2", "3", "4"
NOW_MS = int(time.time() * 1000)
PHRASE = "Jane Q Example"
MANAGE_MESSAGES = 1 << 13


def sid(days_ago: float, n: int = 0) -> str:
    ms = NOW_MS - int(days_ago * 86_400_000)
    return str(((ms - redact.DISCORD_EPOCH_MS) << 22) + n)


class FakeRest:
    """Records every call; answers from a tiny in-memory guild."""

    def __init__(self, messages, bot_perms=MANAGE_MESSAGES, member_roles=None):
        self.messages = {m["id"]: dict(m) for m in messages}
        self.calls = []
        self.dms = []
        self.roles = [{"id": G, "permissions": "0"},
                      {"id": "r-bot", "permissions": str(bot_perms)},
                      {"id": "r-mod", "permissions": str(MANAGE_MESSAGES)}]
        self.member_roles = member_roles or {BOT: ["r-bot"], MOD: ["r-mod"], MEMBER: []}

    def me(self):
        return {"id": BOT, "bot": True}

    def dm_channel(self, user):
        return {"id": f"dm-{user}"}

    def create_message(self, channel, payload):
        self.dms.append((channel, payload))
        return {"id": "dm-msg"}

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        if method == "GET" and path == f"/guilds/{G}":
            return {"id": G, "owner_id": OWNER}
        if method == "GET" and path == f"/guilds/{G}/roles":
            return self.roles
        if method == "GET" and path.startswith(f"/guilds/{G}/members/"):
            return {"roles": self.member_roles.get(path.rsplit("/", 1)[1], [])}
        if method == "GET" and path == f"/channels/{C}":
            return {"id": C, "type": 0, "permission_overwrites": []}
        if method == "GET" and path.startswith(f"/channels/{C}/messages?"):
            before = path.split("before=")[1] if "before=" in path else None
            ids = sorted(self.messages, key=int, reverse=True)
            if before:
                ids = [i for i in ids if int(i) < int(before)]
            return [self.messages[i] for i in ids[:100]]
        if method == "GET" and path.startswith(f"/channels/{C}/messages/"):
            return self.messages[path.rsplit("/", 1)[1]]
        if method == "PATCH":
            self.messages[path.rsplit("/", 1)[1]]["content"] = body["content"]
            return {}
        if method == "DELETE":
            self.messages.pop(path.rsplit("/", 1)[1], None)
            return None
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self, method):
        return [p for m, p, _ in self.calls if m == method]


def _msgs():
    return [
        {"id": sid(1, 1), "content": f"Thanks, {PHRASE}!", "author": {"id": BOT}},
        {"id": sid(2, 2), "content": "my name is jane q example", "author":
            {"id": MEMBER, "username": "member"}},
        {"id": sid(3, 3), "content": "unrelated", "author": {"id": MEMBER}},
        {"id": sid(5, 4), "content": f"cc {PHRASE}", "author": {"id": BOT},
         "webhook_id": "w1"},          # a persona webhook post: not editable as ours
        {"id": sid(60, 5), "content": f"old {PHRASE}", "author": {"id": BOT}},
    ]


def _run(rest, user=MOD, **kw):
    kw.setdefault("now_ms", NOW_MS)
    return redact.run(rest, guild=G, channel=C, user=user, phrase=PHRASE,
                      sleep=lambda s: None, **kw)


def test_own_message_is_edited_and_others_are_listed_not_deleted():
    rest = FakeRest(_msgs())
    out = _run(rest)
    assert rest.messages[sid(1, 1)]["content"] == "Thanks, [redacted]!"
    assert rest.messages[sid(60, 5)]["content"] == f"old {PHRASE}"     # outside 30 days
    assert rest.writes("DELETE") == []                                  # nothing yet
    assert out.pending is not None
    assert sorted(out.pending["ids"]) == sorted([sid(2, 2), sid(5, 4)])
    assert f"https://discord.com/channels/{G}/{C}/{sid(2, 2)}" in out.text
    assert PHRASE.casefold() not in out.text.casefold()
    assert PHRASE.casefold() not in repr(out.pending).casefold()


def test_others_deleted_only_after_confirm_by_the_asker():
    rest = FakeRest(_msgs())
    out = _run(rest)
    stranger = redact.confirm(rest, out.pending, presser=MEMBER, sleep=lambda s: None)
    assert "Only the person" in stranger and rest.writes("DELETE") == []
    done = redact.confirm(rest, out.pending, presser=MOD, sleep=lambda s: None)
    assert done.startswith("Deleted 2")
    assert sorted(p.rsplit("/", 1)[1] for p in rest.writes("DELETE")) == \
        sorted([sid(2, 2), sid(5, 4)])


def test_no_manage_messages_lists_without_deleting():
    rest = FakeRest(_msgs(), bot_perms=0)
    out = _run(rest)
    assert out.pending is None and out.no_permission
    assert "do NOT have Manage Messages" in out.text
    assert sid(2, 2) in out.text
    assert rest.writes("DELETE") == []


def test_non_staff_is_refused_and_nothing_is_read():
    rest = FakeRest(_msgs())
    out = _run(rest, user=MEMBER)
    assert out.refused and "Only server staff" in out.text
    assert rest.writes("PATCH") == [] and rest.writes("DELETE") == []
    assert not any("/messages" in p for _, p, _ in rest.calls)


def test_owner_and_operator_count_as_staff():
    assert not _run(FakeRest(_msgs()), user=OWNER).refused      # guild owner
    assert not _run(FakeRest(_msgs()), user=OTHER, operator=True).refused


def test_phrase_never_reaches_a_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    rest = FakeRest(_msgs())
    out = _run(rest)
    redact.confirm(rest, out.pending, presser=MOD, sleep=lambda s: None)
    _run(FakeRest(_msgs()), user=MEMBER)
    text = caplog.text.casefold()
    assert caplog.records and PHRASE.casefold() not in text
    assert redact.fingerprint(PHRASE).split()[0] in caplog.text


def test_agent_tool_call_record_never_holds_the_phrase():
    """The agent logs the args dict it handed Tool.run; run masks secret_args in it."""
    host = _host(FakeRest(_msgs()))
    tool = {t.name: t for t in host.tools_for(G, MOD, channel=C)}["redact_phrase"]
    args = {"phrase": PHRASE, "days": 30}
    answer = tool.run(args)
    assert "edited 1" in answer                       # the real phrase still ran
    logged = _args_summary(args)
    assert PHRASE not in logged and "sha256:" in logged


def test_short_phrase_and_day_bounds():
    out = redact.run(FakeRest([]), guild=G, channel=C, user=MOD, phrase="ab")
    assert out.refused
    assert redact.clamp_days(500) == 90 and redact.clamp_days(0) == 1
    assert redact.clamp_days("x") == 30


# -- the plugin wiring: slash command, button, agent tool -------------------------

def _host(rest):
    host = PluginHost(GuildStore(":memory:"), plugins=[community.PLUGIN], operators=set())
    host.rest = rest
    community._redact_ready(host)
    return host


def _interaction(user, perms, options=None, custom_id=None):
    it = {"id": "i1", "token": "t", "guild_id": G, "channel_id": C,
          "member": {"user": {"id": user}, "permissions": str(perms)}}
    if custom_id:
        it.update(type=3, data={"custom_id": custom_id})
    else:
        it.update(type=2, data={"name": "redact", "options": options or []})
    return it


def test_slash_refuses_non_staff_privately():
    host = _host(FakeRest(_msgs()))
    res = host.handle(_interaction(MEMBER, 0, [{"name": "phrase", "value": PHRASE}]))
    assert res.response["data"]["flags"] == 64
    assert "Only server staff" in res.response["data"]["content"]


def test_slash_then_button_deletes_with_confirm():
    rest = FakeRest(_msgs())
    host = _host(rest)
    res = host.handle(_interaction(MOD, MANAGE_MESSAGES,
                                   [{"name": "phrase", "value": PHRASE}]))
    assert res.response["data"]["flags"] == 64            # ephemeral
    payload = res.deferred()
    assert rest.writes("DELETE") == []
    button = payload["components"][0]["components"][0]["custom_id"]
    assert PHRASE not in payload["content"]
    pressed = host.handle_button(_interaction(MOD, MANAGE_MESSAGES, custom_id=button))
    assert pressed.response["type"] == community.DEFERRED_UPDATE
    assert pressed.deferred()["content"].startswith("Deleted 2")
    again = host.handle_button(_interaction(MOD, MANAGE_MESSAGES, custom_id=button))
    assert "expired" in again.deferred()["content"]        # one press, one run


def test_agent_tool_edits_own_and_dms_the_list():
    rest = FakeRest(_msgs())
    host = _host(rest)
    tools = {t.name: t for t in host.tools_for(G, MOD, channel=C)}
    tool = tools["redact_phrase"]
    assert tool.secret_args == ("phrase",)
    answer = tool.run({"phrase": PHRASE})
    assert "edited 1" in answer and PHRASE.casefold() not in answer.casefold()
    assert rest.writes("DELETE") == []
    (channel, body), = rest.dms
    assert channel == f"dm-{MOD}" and body["components"]
    refused = {t.name: t for t in host.tools_for(G, MEMBER, channel=C)}["redact_phrase"]
    assert "Only server staff" in refused.run({"phrase": PHRASE})
    assert "redact_phrase" not in {t.name for t in host.tools_for(G, MOD)}   # no channel


def test_persona_no_longer_denies_editing():
    assert "redact_phrase" in community.PLUGIN.persona
    assert "Never claim you cannot edit or delete" in community.PLUGIN.persona
