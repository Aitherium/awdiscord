"""/notepad: open a form, save it on disk, share it, attach Aither's call transcripts.

Each test names the MUTATION that must make it fail.
"""
from __future__ import annotations

import json

import pytest
from awdiscord import callnotes, dispatch
from awdiscord import notepad as pad
from awdiscord.builtin import BUILTIN_MODULES, notepad
from awdiscord.plugins import PluginHost
from awdiscord.rest import RestError
from awdiscord.store import GuildStore

G = "100000000000000001"
OTHER_G = "100000000000000009"
ME = "200000000000000002"
YOU = "300000000000000003"
CH = "400000000000000004"
VOICE = "500000000000000005"
POST = (1 << 10) | (1 << 11) | (1 << 15)


class FakeRest:
    def __init__(self, fail_dm=False, fail_post=False):
        self.posts, self.uploads, self.dms = [], [], []
        self.fail_dm, self.fail_post = fail_dm, fail_post

    def create_message(self, channel, payload):
        if self.fail_post:
            raise RestError(403, "Missing Access")
        self.posts.append((channel, payload))
        return {"id": "m1"}

    def dm_channel(self, user):
        if self.fail_dm:
            raise RestError(403, "Cannot send messages to this user")
        self.dms.append(user)
        return {"id": "dm-" + user}

    def request_files(self, method, path, payload, files):
        if self.fail_post:
            raise RestError(403, "Missing Access")
        self.uploads.append((path, payload, files))
        return {"id": "m2"}


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def host(state):
    h = PluginHost(GuildStore(), plugins=[notepad.PLUGIN])
    h.rest = FakeRest()
    return h


def _it(sub, opts=None, user=ME, guild=G, perms=POST, resolved=None):
    data = {"name": "notepad", "options": [
        {"type": 1, "name": sub,
         "options": [{"name": k, "value": v} for k, v in (opts or {}).items()]}]}
    if resolved:
        data["resolved"] = resolved
    return {"guild_id": guild, "channel_id": CH, "type": 2, "data": data,
            "member": {"user": {"id": user, "username": "me"}, "permissions": str(perms)}}


def _submit(h, name, text, user=ME, guild=G):
    return h.handle_modal({"guild_id": guild, "channel_id": CH, "type": 5,
                           "member": {"user": {"id": user}},
                           "data": {"custom_id": notepad.PLUGIN.button_id(f"save:{name}"),
                                    "components": [{"type": 1, "components": [
                                        {"type": 4, "custom_id": "text", "value": text}]}]}})


def _text(result) -> str:
    return result.response["data"].get("content", "")


def _private(result) -> bool:
    return bool(result.response["data"].get("flags", 0) & 64)


def _journal(state, guild, channel, at, lines, uid=ME, done=True):
    j = callnotes.Journal(state, guild, channel)
    j.start(at, "Me", uid)
    for i, (name, text, who) in enumerate(lines):
        j.line(at + i, name, text, who)
    return j.close() if done else j.path


# -- registration ----------------------------------------------------------------------

def test_registered_as_a_builtin_slash_command():
    # mutation: drop the module from BUILTIN_MODULES -> the command never reaches Discord
    assert "awdiscord.builtin.notepad" in BUILTIN_MODULES
    h = PluginHost(GuildStore(), plugins=[notepad.PLUGIN])
    defs = dispatch.commands(host=h)
    body = next(d for d in defs if d["name"] == "notepad")
    subs = {o["name"] for o in body["options"]}
    assert subs == {"open", "list", "share", "attach", "calls", "delete"}
    assert notepad.PLUGIN.default_enabled


# -- open / save -----------------------------------------------------------------------

def test_open_returns_a_paragraph_modal(host):
    # mutation: reply with text instead of the modal (type 9)
    r = host.handle(_it("open", {"name": "Plan"}))
    assert r.response["type"] == 9
    comp = r.response["data"]["components"][0]["components"][0]
    assert comp["style"] == 2 and comp["max_length"] == 4000
    assert r.response["data"]["custom_id"].endswith("save:plan")


def test_save_persists_and_reopen_prefills(host, state):
    # mutation: drop the write, or drop the prefill value
    r = _submit(host, "plan", "line one\nline two")
    assert _private(r) and "Saved **plan**" in _text(r)
    stored = json.loads((state / "notepad" / G / f"{ME}.json").read_text("utf-8"))
    assert stored["notes"]["plan"]["text"] == "line one\nline two"
    again = PluginHost(GuildStore(), plugins=[notepad.PLUGIN])   # "after a restart"
    r = again.handle(_it("open", {"name": "plan"}))
    assert r.response["data"]["components"][0]["components"][0]["value"] == "line one\nline two"


def test_save_is_atomic_no_temp_left(host, state):
    # mutation: write in place / leave the temp file behind
    _submit(host, "a", "x")
    _submit(host, "a", "y")
    folder = state / "notepad" / G
    assert [p.name for p in folder.iterdir()] == [f"{ME}.json"]


def test_too_long_is_refused_not_cut(host):
    # mutation: silently truncate to 4000
    r = _submit(host, "big", "x" * 4001)
    assert _private(r) and "4000" in _text(r)
    assert pad.Notepad(pad.state_root(), G, ME).get("big") is None


def test_damaged_file_is_said_and_kept(host, state):
    # mutation: treat a damaged file as empty and overwrite it
    path = state / "notepad" / G / f"{ME}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", "utf-8")
    r = _submit(host, "a", "x")
    assert "damaged" in _text(r)
    assert path.read_text("utf-8") == "{not json"


def test_notes_are_per_member_and_per_server(host):
    # mutation: key notes by name only (another member opens mine)
    _submit(host, "plan", "mine")
    r = host.handle(_it("open", {"name": "plan"}, user=YOU))
    assert "value" not in r.response["data"]["components"][0]["components"][0]
    r = host.handle(_it("open", {"name": "plan"}, guild=OTHER_G))
    assert "value" not in r.response["data"]["components"][0]["components"][0]


# -- list / delete ---------------------------------------------------------------------

def test_list_and_delete(host):
    # mutation: list shows nothing / delete does not remove
    assert "no notes" in _text(host.handle(_it("list")))
    _submit(host, "one", "abc")
    out = _text(host.handle(_it("list")))
    assert "**one**" in out and "3 chars" in out
    assert "Deleted" in _text(host.handle(_it("delete", {"name": "one"})))
    assert "no note called" in _text(host.handle(_it("delete", {"name": "one"})))


# -- share -----------------------------------------------------------------------------

def test_share_here_posts_an_embed(host):
    # mutation: post nothing, or post publicly with mentions on
    _submit(host, "plan", "hello team")
    r = host.handle(_it("share", {"name": "plan"}))
    out = r.deferred()
    assert r.response["data"]["flags"] == 64
    assert "Shared **plan**" in out["content"]
    channel, payload = host.rest.posts[0]
    assert channel == CH and payload["embeds"][0]["description"] == "hello team"
    assert payload["allowed_mentions"] == {"parse": []}


def test_share_needs_the_sharers_own_send_permission(host):
    # mutation: skip the permission check (post where the member cannot)
    _submit(host, "plan", "x")
    r = host.handle(_it("share", {"name": "plan", "channel": "999"}, resolved={
        "channels": {"999": {"id": "999", "type": 0, "permissions": str(1 << 10)}}}))
    assert "cannot post" in _text(r) and _private(r)
    assert not host.rest.posts


def test_share_to_member_dms_them(host):
    # mutation: DM someone who is not in the server
    _submit(host, "plan", "for you")
    ok = {"members": {YOU: {}}, "users": {YOU: {"id": YOU}}}
    out = host.handle(_it("share", {"name": "plan", "user": YOU}, resolved=ok)).deferred()
    assert host.rest.dms == [YOU] and host.rest.posts[0][0] == "dm-" + YOU
    assert "Shared" in out["content"]
    r = host.handle(_it("share", {"name": "plan", "user": "777"},
                        resolved={"users": {"777": {"id": "777"}}}))
    assert "not a member" in _text(r)


def test_closed_dms_are_said(host):
    # mutation: swallow the RestError and claim it was shared
    host.rest = FakeRest(fail_dm=True)
    _submit(host, "plan", "x")
    ok = {"members": {YOU: {}}, "users": {YOU: {"id": YOU}}}
    out = host.handle(_it("share", {"name": "plan", "user": YOU}, resolved=ok)).deferred()
    assert out["content"].startswith("Not shared") and "403" in out["content"]


def test_share_unknown_note_is_said(host):
    # mutation: share an empty note silently
    r = host.handle(_it("share", {"name": "nope"}))
    assert "no note called" in _text(r) and not host.rest.posts


def test_long_note_goes_as_md_file(host):
    # mutation: cut a long note into the embed
    _submit(host, "long", "y" * 3900)
    host.handle(_it("share", {"name": "long"})).deferred()
    path, payload, files = host.rest.uploads[0]
    assert path == f"/channels/{CH}/messages" and files[0][0] == "long.md"
    assert payload["attachments"][0]["filename"] == "long.md"


# -- transcripts -------------------------------------------------------------------------

def test_attach_latest_and_share_sends_transcript(host, state):
    # mutation: attach nothing, or leave the transcript out of the share
    _journal(state, G, VOICE, 1_700_000_000, [("Me", "first call", ME)])
    _journal(state, G, VOICE, 1_700_100_000, [("Me", "second call", ME)])
    r = host.handle(_it("attach", {"name": "plan"}))
    assert "Attached to **plan**" in _text(r) and _private(r)
    host.handle(_it("share", {"name": "plan"})).deferred()
    _, _, files = host.rest.uploads[0]
    assert len(files) == 1 and b"second call" in files[0][1]


def test_attach_all_and_inline(host, state):
    # mutation: inline ignored, or "all" picks one
    _journal(state, G, VOICE, 1_700_000_000, [("Me", "alpha", ME)])
    _journal(state, G, VOICE, 1_700_100_000, [("Me", "beta", ME)])
    host.handle(_it("attach", {"name": "n", "call": "all", "inline": True}))
    note = pad.Notepad(pad.state_root(), G, ME).get("n")
    assert len(note["calls"]) == 2 and "alpha" in note["text"] and "beta" in note["text"]
    assert "Removed" in _text(host.handle(_it("attach", {"name": "n", "call": "none"})))
    assert pad.Notepad(pad.state_root(), G, ME).get("n")["calls"] == []


def test_never_another_servers_transcript(host, state):
    # mutation: list every journal regardless of guild (name prefix or header)
    _journal(state, OTHER_G, VOICE, 1_700_000_000, [("Me", "secret", ME)])
    # a file NAMED for this guild whose header says another guild is not ours either
    forged = state / "callnotes" / "done" / f"{G}-{VOICE}-20231114-000000.jsonl"
    forged.write_text(json.dumps({"type": "start", "guild": OTHER_G, "channel": VOICE,
                                  "at": 1, "by": "x", "by_id": ME}) + "\n", "utf-8")
    r = host.handle(_it("attach", {"name": "plan", "call": "all"}, perms=POST | (1 << 3)))
    assert "no call transcripts" in _text(r)
    assert "no call transcripts" in _text(host.handle(_it("calls")))


def test_only_calls_you_were_in_unless_manager(host, state):
    # mutation: drop the participant filter
    _journal(state, G, VOICE, 1_700_000_000, [("You", "their call", YOU)], uid=YOU)
    assert "no call transcripts" in _text(host.handle(_it("calls")))
    out = _text(host.handle(_it("calls", perms=POST | (1 << 5))))
    assert "`1`" in out and "1 lines" in out


def test_live_call_attached_then_finished_is_still_found(host, state):
    # mutation: store the live file name only (the call vanishes when it ends)
    j = callnotes.Journal(state, G, VOICE)
    j.start(1_700_000_000, "Me", ME)
    j.line(1_700_000_001, "Me", "live words", ME)
    host.handle(_it("attach", {"name": "plan"}))
    j.close()
    host.handle(_it("share", {"name": "plan"})).deferred()
    assert b"live words" in host.rest.uploads[0][2][0][1]


def test_bad_call_number_is_said(host, state):
    # mutation: fall back to the latest call on a bad number
    _journal(state, G, VOICE, 1_700_000_000, [("Me", "x", ME)])
    assert "no call #5" in _text(host.handle(_it("attach", {"name": "p", "call": "5"})))
