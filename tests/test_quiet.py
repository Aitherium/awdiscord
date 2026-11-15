"""awdiscord.quiet: in-place answers, a durable mute, warm tone, no internals in public.

Each test pins a line that really shipped in a public reply on 2026-11-13, or a rule
the owner asked for that day.
"""
from __future__ import annotations

from awdiscord import quiet
from awdiscord.store import GuildStore

G, C, T = "1", "10", "11"


def test_the_curt_lines_that_shipped_are_flagged_and_removed():
    sass = ("Yes — I'm here, and I answered. If you want me to actually *do* "
            "something specific, tell me what.")
    assert len(quiet.curt_hits(sass)) == 2
    assert quiet.polish(sass) == quiet.WARM_FALLBACK
    assert quiet.curt_hits(quiet.polish(sass)) == []
    assert quiet.polish("As I said, the deploy is at 5.") == "the deploy is at 5."


def test_friendly_text_passes_untouched():
    text = "Done! The channel is ready. Want me to pin a welcome note?"
    assert quiet.curt_hits(text) == []
    assert quiet.polish(text) == text


def test_serving_internals_are_dropped_in_public_but_kept_for_admins():
    text = ("I'm Aither, the team's assistant. Today's response is served by the local "
            "model bonsai2-27b-5090 on 5090 hardware. Happy to help.")
    public = quiet.polish(text)
    assert "bonsai" not in public.lower() and "5090" not in public
    assert public.startswith("I'm Aither") and public.endswith("Happy to help.")
    assert "bonsai2-27b-5090" in quiet.polish(text, privileged=True)


def test_mute_requests_and_unmute_requests():
    assert quiet.is_mute_request("<@1> now remove yourself from my new chat you cant "
                                 "talk in here either")
    assert quiet.is_mute_request("<@1> mute")
    assert not quiet.is_mute_request("<@1> unmute")
    assert quiet.is_unmute_request("<@1> unmute")
    assert not quiet.is_mute_request("<@1> what is the roadmap?")


def test_mute_is_durable_covers_threads_and_only_setter_or_admin_unmutes():
    store = GuildStore(":memory:")
    quiet.set_mute(store, G, C, "jason")
    row = quiet.muted(store, G, T, parent=C)          # a thread in a muted channel
    assert row and row["by"] == "jason" and row["channel"] == C
    assert quiet.may_unmute(row, "jason", admin=False)
    assert not quiet.may_unmute(row, "someone", admin=False)
    assert quiet.may_unmute(row, "someone", admin=True)
    quiet.clear_mute(store, G, C)
    assert quiet.muted(store, G, T, parent=C) is None


def test_mute_survives_a_new_store_on_the_same_file(tmp_path):
    db = tmp_path / "guilds.db"
    quiet.set_mute(GuildStore(db), G, C, "jason")
    assert quiet.muted(GuildStore(db), G, C)["by"] == "jason"      # after a restart


def test_one_private_notice_per_window():
    store = GuildStore(":memory:")
    assert quiet.notice_due(store, G, C, "u", now=1000.0)
    assert not quiet.notice_due(store, G, C, "u", now=1000.0 + 60)
    assert quiet.notice_due(store, G, C, "u", now=1000.0 + quiet.NOTICE_EVERY_S)


def test_room_asks_are_recognised():
    ask = quiet.room_ask("<@1> can you make a room called #roadmap where everyone can "
                         "see but only I can type")
    assert ask == {"name": "roadmap", "write": "requester"}
    assert quiet.room_ask("create a channel named test-roadmap")["write"] == "everyone"
    assert quiet.room_ask("what is in the roadmap room?") is None


def test_answers_in_place_unless_the_relay_is_turned_on(monkeypatch):
    monkeypatch.delenv("AWDISCORD_CONTAIN_MENTIONS", raising=False)
    assert quiet.answer_in_place()
    monkeypatch.setenv("AWDISCORD_CONTAIN_MENTIONS", "1")
    assert not quiet.answer_in_place()


def test_polish_leaves_normal_answers_alone():
    code = "Try this:\n```python\ndef f():\n    return 1  # runs on a GPU\n```\nThen run it..."
    assert quiet.polish(code) == code
    nested = "Steps:\n  1. open it\n     - then this"
    assert quiet.polish(nested) == nested
    advice = "A 5090 is great for Blender. The RTX line has plenty of VRAM?!"
    assert quiet.polish(advice) == advice
    link = "See ../docs/x.md and https://example.com/a?b=1."
    assert quiet.polish(link) == link


def test_only_real_room_and_mute_asks_trigger():
    assert quiet.room_ask("how do I create a channel in Discord?") is None
    assert quiet.room_ask("can you create a channel in discord") is None
    assert quiet.room_ask("make a channel #ideas")["name"] == "ideas"
    assert not quiet.is_mute_request("write a poem telling the rain to go away")
    assert quiet.is_mute_request("<@1> mute")
    assert quiet.is_mute_request("<@1> be quiet")


def test_ordinary_sentences_are_neither_cut_nor_muting():
    for text in ("I'd pick the model with the longer context.",
                 "My guess is your app runs on port 3000."):
        assert quiet.polish(text) == text
    assert not quiet.is_mute_request("should I leave the room open?")
    assert quiet.is_mute_request("<@1> leave this chat")
