"""What Aither hears: the per-server view, its two changes, the audit trail, and the
shared ``/aither`` group a pack's subcommands join."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import callnotes, hearing  # noqa: E402
from awdiscord.builtin import hearing as plug  # noqa: E402
from awdiscord.plugins import (  # noqa: E402
    MANAGE_GUILD,
    Command,
    Plugin,
    PluginCommandConflictError,
    PluginHost,
)
from awdiscord.store import GuildStore  # noqa: E402

G, OWNER, MANAGER, MEMBER = "111", "900", "7", "8"


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name in ("VOICE", "VOICE_AUTO_GUILDS", "VOICE_WAKE", "MESSAGE_CONTENT",
                 "WATCH_CHANNELS", "REPLY_VISIBILITY", "CONTAIN", "BOT_CHANNEL",
                 "MENTIONS", "OPERATORS", "SERVED_GUILDS"):
        monkeypatch.delenv("AWDISCORD_" + name, raising=False)
    monkeypatch.setenv("AWDISCORD_OWNERS", OWNER)
    monkeypatch.setenv("AWDISCORD_VOICE", "1")
    return GuildStore(tmp_path / "guilds.db")


def _it(sub, user=MEMBER, perms="0", group=None):
    opt = {"type": 1, "name": sub, "options": []}
    if group:
        opt = {"type": 2, "name": group, "options": [opt]}
    return {"type": 2, "id": "1", "token": "t", "guild_id": G,
            "member": {"user": {"id": user}, "permissions": perms},
            "data": {"name": "aither", "options": [opt]}}


def _press(payload, user=MEMBER, perms="0"):
    return {"type": 3, "id": "1", "token": "t", "guild_id": G,
            "member": {"user": {"id": user}, "permissions": perms},
            "data": {"custom_id": plug.PLUGIN.button_id(payload)}}


def _by(rows):
    return {r["key"]: r for r in rows}


# -- the view ---------------------------------------------------------------------------

def test_default_is_private_nothing_listens(store):
    rows = hearing.snapshot(G, store)
    assert not [r["key"] for r in rows if r["listening"]]
    assert _by(rows)["calls"]["value"] == "off"
    assert _by(rows)["messages"]["value"] == "off"
    assert "0 of" in hearing.headline(rows)


def test_view_reads_the_same_setting_voice_enforces(store):
    callnotes.set_voice_calls(store, G, True)
    row = _by(hearing.snapshot(G, store))["calls"]
    assert row["value"] == "on" and row["listening"] and row["change"] == "off"
    assert callnotes.voice_calls_on(G, store, ()) is True


def test_operator_forced_calls_offer_no_button(store, monkeypatch):
    monkeypatch.setenv("AWDISCORD_VOICE_AUTO_GUILDS", G)
    row = _by(hearing.snapshot(G, store))["calls"]
    assert row["value"] == "on" and row["change"] == ""
    ok, text = hearing.change(store, G, OWNER, "calls", "off", is_manager=True,
                              owners={OWNER})
    assert not ok and "operator" in text


def test_list_is_capped_at_five_with_the_real_total(store):
    rows = hearing.snapshot(G, store, host=PluginHost(store, plugins=[plug.PLUGIN]))
    v = hearing.view(rows)
    assert len(v["shown"]) == 5 and v["total"] == len(rows) == 8 and v["more"] == 3
    assert hearing.view(rows, expanded=True)["more"] == 0


def test_wake_mode_matches_voice():
    voice = pytest.importorskip("awdiscord.voice")
    for raw in ("", "auto", "on", "always", "0", "never", "weird"):
        assert hearing.wake_mode({"AWDISCORD_VOICE_WAKE": raw}) == \
            voice._wake_mode(raw or "auto")


# -- changes: less is the manager's, more is the owner's --------------------------------

def test_manager_cannot_turn_listening_on(store):
    ok, text = hearing.change(store, G, MANAGER, "calls", "on", is_manager=True,
                              owners={OWNER})
    assert not ok and "owner" in text
    assert callnotes.voice_calls_on(G, store, ()) is False
    assert hearing.audit_log(store, G)[1] == 0


def test_manager_turns_listening_off_and_it_is_audited(store):
    callnotes.set_voice_calls(store, G, True)
    ok, _ = hearing.change(store, G, MANAGER, "calls", "off", is_manager=True,
                           owners={OWNER})
    assert ok and callnotes.voice_calls_on(G, store, ()) is False
    entries, total = hearing.audit_log(store, G)
    assert total == 1
    assert (entries[0]["by"], entries[0]["what"], entries[0]["old"],
            entries[0]["new"]) == (MANAGER, "calls", "on", "off")
    assert entries[0]["at"] > 0


def test_member_changes_nothing(store):
    ok, text = hearing.change(store, G, MEMBER, "visibility", "private",
                              is_manager=False, owners={OWNER})
    assert not ok and "managers" in text
    assert _by(hearing.snapshot(G, store))["visibility"]["value"] == "public"


def test_operator_switches_are_not_settable_from_discord(store):
    ok, text = hearing.change(store, G, OWNER, "messages", "on", is_manager=True,
                              owners={OWNER})
    assert not ok and "operator" in text


def test_audit_keeps_the_newest_and_reports_the_total(store, monkeypatch):
    monkeypatch.setattr(hearing, "AUDIT_KEEP", 6)
    for i in range(8):
        hearing.audit(store, G, MANAGER, "visibility", str(i), str(i + 1))
    entries, total = hearing.audit_log(store, G)
    assert total == 6 and len(entries) == 5


# -- the Discord face -------------------------------------------------------------------

def test_settings_is_private_state_first_and_ends_with_one_next(store):
    host = PluginHost(store, plugins=[plug.PLUGIN])
    data = host.handle(_it("settings")).response["data"]
    assert data["flags"] == 64
    lines = data["content"].splitlines()
    assert lines[0].startswith("**What Aither hears in this server**")
    assert lines[-1].startswith("Next:") and sum(x.startswith("Next:") for x in lines) == 1
    assert "+3 more (of 8)" in data["content"]
    labels = [c["label"] for row in data["components"] for c in row["components"]]
    assert "+3 more" in labels and "History" in labels


def test_owner_only_button_is_disabled_for_a_manager(store):
    host = PluginHost(store, plugins=[plug.PLUGIN])
    data = host.handle(_it("settings", MANAGER, str(MANAGE_GUILD))).response["data"]
    on = next(c for row in data["components"] for c in row["components"]
              if c["label"] == "Calls: turn on")
    assert on["disabled"] is True
    forced = host.handle_button(_press("set:calls:on", MANAGER, str(MANAGE_GUILD)))
    assert "owner" in forced.response["data"]["content"]
    assert callnotes.voice_calls_on(G, store, ()) is False


def test_button_changes_visibility_and_history_names_who(store):
    host = PluginHost(store, plugins=[plug.PLUGIN])
    out = host.handle_button(_press("set:visibility:private", MANAGER, str(MANAGE_GUILD)))
    assert "Saved: replies are now `private`" in out.response["data"]["content"]
    hist = host.handle_button(_press("history")).response["data"]
    assert hist["flags"] == 64 and f"<@{MANAGER}>" in hist["content"]
    assert "`public` -> `private`" in hist["content"]


def test_more_button_reveals_every_row(store):
    host = PluginHost(store, plugins=[plug.PLUGIN])
    text = host.handle_button(_press("show:all")).response["data"]["content"]
    assert "8. **" in text and "more (of" not in text


# -- the shared /aither group -----------------------------------------------------------

def _pack(name="feedback-pack", sub="feedback", **kw):
    return Plugin(name=name, version="0", description="d", default_enabled=True, commands=[
        Command("aither", "Aither", subcommands=[Command(sub, "Feedback", subcommands=[
            Command("stats", "Stats", lambda ctx: ctx.reply("stats!"))])], **kw)])


def test_a_pack_joins_the_shared_group_instead_of_vanishing(store):
    host = PluginHost(store, plugins=[plug.PLUGIN, _pack()])
    defs = [d for d in host.command_defs() if d["name"] == "aither"]
    assert len(defs) == 1
    assert [o["name"] for o in defs[0]["options"]] == ["settings", "feedback"]
    out = host.handle(_it("stats", group="feedback"))
    assert out.response["data"]["content"] == "stats!"
    assert host.handle(_it("settings")).response["data"]["content"].startswith("**What")


def test_joined_subcommand_obeys_its_own_plugins_switch(store):
    host = PluginHost(store, plugins=[plug.PLUGIN, _pack()])
    host.set_enabled(G, "feedback-pack", False)
    assert "is off in this" in host.handle(
        _it("stats", group="feedback")).response["data"]["content"]
    assert host.handle(_it("settings")).response["data"]["flags"] == 64


def test_overlapping_subcommand_is_still_refused(store):
    host = PluginHost(store, plugins=[plug.PLUGIN])
    with pytest.raises(PluginCommandConflictError, match="settings"):
        host.add(_pack(name="clash", sub="settings"))


def test_an_unshared_group_is_not_joinable(store):
    host = PluginHost(store, plugins=[_pack()])
    with pytest.raises(PluginCommandConflictError):
        host.add(_pack(name="second", sub="other"))
