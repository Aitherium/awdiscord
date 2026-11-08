"""The film plugin: tools only in a mapped channel, only for that customer's members."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest
from awdiscord.builtin import film
from awdiscord.plugins import build_host
from awdiscord.store import GuildStore

CHANNEL, MEMBER, STRANGER = "111", "222", "333"
FILM_TOOLS = {"film_steer_status", "film_comment_step", "film_steer", "film_preview"}


@pytest.fixture
def mapped(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("tenant-bearer\n")
    doc = {"refuse_hosts": ["owner-forge"],
           "channels": {CHANNEL: {"tenant": "tnt_x", "instance": "x-forge",
                                  "url": "http://x-forge:8200/", "project": "x-film",
                                  "members": [MEMBER], "token_file": str(token),
                                  "desk_url": "https://desk.example/film"}}}
    path = tmp_path / "map.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setenv(film.MAP_ENV, str(path))
    return path


class FakeForge:
    """Stands in for the instance's /api/op/<name>; records every request."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def open(self, req, timeout=None):
        op = req.full_url.rsplit("/", 1)[-1]
        self.calls.append({"url": req.full_url, "op": op, "body": json.loads(req.data),
                           "headers": {k.lower(): v for k, v in req.header_items()}})
        ans = self.answers[op]
        if isinstance(ans, int):
            body = io.BytesIO(json.dumps({"ok": False, "error": "needs admin"}).encode())
            raise urllib.error.HTTPError(req.full_url, ans, "refused", {}, body)
        return io.BytesIO(json.dumps(ans).encode())


@pytest.fixture
def forge(monkeypatch):
    holder = {}

    def install(answers):
        holder["f"] = FakeForge(answers)
        monkeypatch.setattr(film, "_opener", lambda: holder["f"])
        return holder["f"]
    return install


def _ok(result):
    return {"ok": True, "meta": {"result": result}}


def _names(host, channel, user):
    return {t.name for t in host.tools_for("g", user, channel=channel)}


def _tool(host, name, channel=CHANNEL, user=MEMBER):
    return next(t for t in host.tools_for("g", user, channel=channel) if t.name == name)


def test_unmapped_channel_gets_no_film_tools(mapped):
    host = build_host(GuildStore(":memory:"))
    assert not FILM_TOOLS & _names(host, "999", MEMBER)
    assert not FILM_TOOLS & _names(host, "", MEMBER)


def test_no_map_at_all_offers_nothing(monkeypatch):
    monkeypatch.delenv(film.MAP_ENV, raising=False)
    host = build_host(GuildStore(":memory:"))
    assert not FILM_TOOLS & _names(host, CHANNEL, MEMBER)


def test_mapped_channel_offers_tools_to_members_only(mapped):
    host = build_host(GuildStore(":memory:"), operators={STRANGER})
    assert FILM_TOOLS <= _names(host, CHANNEL, MEMBER)
    assert not FILM_TOOLS & _names(host, CHANNEL, STRANGER)   # an operator is not the customer


def test_refused_host_and_bad_entries_bind_nothing(tmp_path):
    doc = {"refuse_hosts": ["owner-forge"], "channels": {
        "1": {"tenant": "t", "instance": "i", "url": "http://OWNER-FORGE:8200",
              "project": "p", "members": ["5"]},
        "2": {"tenant": "t", "instance": "i", "url": "ftp://x", "project": "p", "members": ["5"]},
        "3": {"tenant": "t", "instance": "i", "url": "http://x", "project": "p",
              "members": ["jason"]},
        "4": {"instance": "i", "url": "http://x", "project": "p", "members": ["5"]}}}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc))
    assert film.load_map(str(path)) == {}
    path.write_text("{not json")
    assert film.load_map(str(path)) == {}


def test_status_calls_the_mapped_instance_with_its_bearer(mapped, forge):
    f = forge({"film_steer_status": _ok({
        "project": "x-film", "accepted": {"version": 8, "filename": "s.docx"},
        "pending": {"id": "p9", "status": "pending", "changed_steps": [4, 6]},
        "steps": {"4": {"state": "stale"}}})})
    out = _tool(build_host(GuildStore(":memory:")), "film_steer_status").run({})
    assert "v8" in out and "p9" in out and "4, 6" in out
    call = f.calls[0]
    assert call["url"] == "http://x-forge:8200/api/op/film_steer_status"
    assert call["body"]["project"] == "x-film"
    assert call["headers"]["authorization"] == "Bearer tenant-bearer"
    assert call["headers"]["x-aither-origin"] == "agent-tool"


def test_comment_names_the_member_and_cannot_choose_the_project(mapped, forge):
    f = forge({"film_comment_step": _ok({"step": 4, "state": "open"})})
    tool = _tool(build_host(GuildStore(":memory:")), "film_comment_step")
    out = tool.run({"step": 4, "text": "canopy is too low", "project": "someone-else",
                    "caller": {"user": MEMBER, "channel": CHANNEL}})
    assert "step 4" in out and "approval" in out
    body = f.calls[0]["body"]
    assert body == {"step": 4, "text": "canopy is too low", "project": "x-film",
                    "by": f"discord:{MEMBER}"}


def test_approve_resolves_the_pending_id_and_relays_the_instance_refusal(mapped, forge):
    f = forge({"film_steer_status": _ok({"pending": {"id": "p9"}}), "film_steer": 403})
    out = _tool(build_host(GuildStore(":memory:")), "film_steer").run({"action": "approve"})
    assert "cannot approve" in out and "https://desk.example/film" in out
    assert "Nothing changed" in out
    steer = f.calls[1]
    assert steer["body"]["proposal_id"] == "p9" and steer["body"]["action"] == "approve"


def test_revise_needs_a_note_and_answer_is_recorded(mapped, forge):
    host = build_host(GuildStore(":memory:"))
    assert "needs a note" in _tool(host, "film_steer").run({"action": "revise"})
    f = forge({"film_steer_status": _ok({"pending": {"id": "p9"}}),
               "film_steer": _ok({"status": "pending"})})
    out = _tool(host, "film_steer").run({"action": "answer", "question": "q1", "choice": "2"})
    assert out.startswith("Done: answer recorded on proposal p9")
    assert f.calls[1]["body"]["question"] == "q1"


def test_preview_reports_state_without_a_file(mapped, forge):
    forge({"film_preview": _ok({"step": 3, "mp4": None, "state": "stale", "why": "frames"})})
    out = _tool(build_host(GuildStore(":memory:")), "film_preview").run({"step": 3})
    assert "no preview yet" in out and "frames" in out


def test_missing_credential_fails_closed(mapped, forge, tmp_path):
    (tmp_path / "token").unlink()
    f = forge({})
    out = _tool(build_host(GuildStore(":memory:")), "film_steer_status").run({})
    assert "credential" in out and f.calls == []


def test_unbound_call_refuses(mapped, forge):
    f = forge({})
    assert "changed nothing" in film.film_steer_status(caller=None)
    assert "changed nothing" in film.film_comment_step(4, "x", caller={"user": STRANGER,
                                                                         "channel": CHANNEL})
    assert f.calls == []


def test_a_raising_offered_gate_withholds_the_tool(monkeypatch, mapped):
    def boom(caller):
        raise RuntimeError("map exploded")
    for t in film.TOOLS:
        monkeypatch.setattr(t, "offered", boom)
    host = build_host(GuildStore(":memory:"))
    assert not FILM_TOOLS & _names(host, CHANNEL, MEMBER)


def test_the_builtin_wins_a_same_named_pack_tool_in_the_mapped_channel(mapped):
    from awdiscord.plugins import Plugin, Tool
    host = build_host(GuildStore(":memory:"))
    legacy = Tool("film_steer_status", "legacy", lambda: "LEGACY")
    host.add(Plugin(name="legacy-film", version="0", description="d", default_enabled=True,
                    tools=[legacy]))
    assert _tool(host, "film_steer_status").fn is film.film_steer_status
