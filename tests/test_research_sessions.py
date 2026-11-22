"""A research session continues across turns and ends as a document with sources.

2026-11-17: deep_research answered once and forgot; "go deeper" started from
nothing and the answer was never a file.
"""

import pytest
from awdiscord.builtin import research as r
from awdiscord.outbox import Outbox
from awdiscord.plugins import ScopedStore
from awdiscord.store import GuildStore

CALLER = {"user": "u1", "guild": "g1", "channel": "c1"}


class Rest:
    def request(self, *a, **k):
        return {}

    def request_files(self, *a, **k):
        return {}


class Host:
    def __init__(self):
        self.store = GuildStore(":memory:")
        self.rest = Rest()


@pytest.fixture()
def host(monkeypatch):
    h = Host()
    monkeypatch.setitem(r._HOST, "host", h)
    monkeypatch.setattr(r, "PLUGIN", r.PLUGIN)
    return h


@pytest.fixture()
def searches(monkeypatch):
    calls = []

    def fake(query, limit=8):
        calls.append(query)
        return [{"title": f"About {query}", "url": f"https://e.com/{len(calls)}",
                 "snippet": "snippet text"}][:limit]

    monkeypatch.setattr(r.web, "search_results", fake)
    return calls


def _store(host):
    return ScopedStore(host.store, "g1", r.PLUGIN.name)


def test_start_opens_a_session_and_keeps_the_first_rows(host, searches):
    out = r.research_start("who is winning the chip war", caller=CALLER)
    assert "Research session r-" in out and "About who is winning" in out
    sid = _store(host).get(r._active_key(CALLER))
    session = _store(host).get(r._session_key(sid))
    assert session["question"] == "who is winning the chip war"
    assert len(session["rounds"]) == 1 and len(session["sources"]) == 1


def test_go_deeper_continues_the_same_session(host, searches):
    r.research_start("chip war", caller=CALLER)
    out = r.research_go_deeper("what about Europe", caller=CALLER)
    assert "Round 2" in out and "2 source(s)" in out
    sid = _store(host).get(r._active_key(CALLER))
    session = _store(host).get(r._session_key(sid))
    assert [x["instruction"] for x in session["rounds"]] == ["", "what about Europe"]
    assert searches[-1] == "chip war what about Europe"


def test_go_deeper_without_a_session_says_so(host, searches):
    assert "no research session" in r.research_go_deeper("more", caller=CALLER).lower()
    assert not searches


def test_a_failed_search_leaves_the_session_unchanged(host, searches, monkeypatch):
    r.research_start("q", caller=CALLER)
    sid = _store(host).get(r._active_key(CALLER))
    before = len(_store(host).get(r._session_key(sid))["rounds"])

    def boom(*a, **k):
        raise TimeoutError("slow")

    monkeypatch.setattr(r.web, "search_results", boom)
    out = r.research_go_deeper("more", caller=CALLER)
    assert "unchanged" in out
    assert len(_store(host).get(r._session_key(sid))["rounds"]) == before


def test_report_attaches_markdown_with_numbered_sources(host, searches):
    r.research_start("chip war", caller=CALLER)
    r.research_go_deeper("Europe", caller=CALLER)
    box = Outbox()
    out = r.research_report("Chip report", "## Summary\nIt continues.", caller=dict(
        CALLER, outbox=box))
    assert "Attached chip-report.md" in out
    assert len(box.files) == 1
    body = box.files[0].data.decode()
    assert body.startswith("# Chip report")
    assert "**Question:** chip war" in body
    assert "It continues." in body
    assert "## Sources" in body and "https://e.com/1" in body and "https://e.com/2" in body
    assert box.files[0].content_type == "text/markdown"


def test_report_without_an_outbox_returns_the_document_inline(host, searches):
    r.research_start("q", caller=CALLER)
    out = r.research_report("T", "body", caller=CALLER)
    assert "could not attach" in out and "# T" in out


def test_sources_dedupes_across_rounds(host, monkeypatch):
    rows = [{"title": "same", "url": "https://e.com/x", "snippet": ""}]
    monkeypatch.setattr(r.web, "search_results", lambda q, limit=8: list(rows))
    r.research_start("q", caller=CALLER)
    r.research_go_deeper("more", caller=CALLER)
    out = r.research_sources(caller=CALLER)
    assert "1 source(s)" in out


def test_sessions_are_per_member_and_channel(host, searches):
    r.research_start("mine", caller=CALLER)
    other = dict(CALLER, user="u2")
    assert "no research session" in r.research_go_deeper("x", caller=other).lower()


def test_every_tool_needs_a_server_and_the_caller():
    for tool in (r.START, r.DEEPER, r.REPORT, r.SOURCES):
        assert tool.wants_caller
        assert tool.offered({"guild": "g", "channel": "c"})
        assert not tool.offered({"guild": "", "channel": ""})


def test_the_report_is_bounded_by_the_round_cap():
    assert r.MAX_ROUNDS <= 20 and r.MAX_SOURCES <= 100


# ── review findings, 2026-11-17 ──────────────────────────────────────────────

def test_a_named_session_belongs_to_its_owner(host, searches):
    r.research_start("mine", caller=CALLER)
    sid = _store(host).get(r._active_key(CALLER))
    other = dict(CALLER, user="u2")
    for call in (lambda: r.research_go_deeper("x", sid, other),
                 lambda: r.research_report("T", "b", sid, other),
                 lambda: r.research_sources(sid, other)):
        assert "belongs to someone else" in call()
    # the owner still reaches it by id after switching sessions
    r.research_start("second", caller=CALLER)
    assert "Round 2" in r.research_go_deeper("more", sid, CALLER)


def test_starting_again_says_what_it_replaced(host, searches):
    r.research_start("first", caller=CALLER)
    out = r.research_start("second", caller=CALLER)
    assert "This is now the active session" in out


# ── the forum knowledge base (2026-11-17): questions become durable threads ──

class RecRest:
    """Records every call; opens threads with increasing ids."""

    def __init__(self, fail_threads=False, fail_messages=False):
        self.calls, self.fail_threads, self.fail_messages = [], fail_threads, fail_messages
        self.n = 0

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        if path.endswith("/threads"):
            if self.fail_threads:
                raise RuntimeError("403 Missing Permissions")
            self.n += 1
            return {"id": f"t{self.n}"}
        if self.fail_messages:
            raise RuntimeError("403")
        return {"id": "m"}

    def paths(self, suffix=""):
        return [p for _, p, _ in self.calls if p.endswith(suffix)]


def _with_forum(host, forum="f1"):
    _store(host).set("config", {"forum": forum})
    rest = RecRest()
    host.rest = rest
    return rest


def test_a_session_files_its_first_round_into_a_new_thread(host, searches):
    rest = _with_forum(host)
    out = r.research_start("semiconductor export controls", caller=CALLER)
    assert rest.paths("/threads") == ["/channels/f1/threads"]
    opened = [b for _, p, b in rest.calls if p.endswith("/threads")][0]
    assert opened["name"] == "semiconductor export controls"
    assert "About semiconductor" in opened["message"]["content"]
    assert "Filed in the research forum as <#t1>" in out
    sid = _store(host).get(r._active_key(CALLER))
    assert _store(host).get(r._session_key(sid))["thread"] == "t1"
    assert _store(host).get("kb:t1")["keywords"]


def test_asking_the_same_question_again_continues_the_thread(host, searches):
    rest = _with_forum(host)
    r.research_start("semiconductor export controls to china", caller=CALLER)
    out = r.research_start("export controls to china semiconductor rules", caller=CALLER)
    assert rest.paths("/threads") == ["/channels/f1/threads"]       # only one thread
    assert "/channels/t1/messages" in rest.paths()
    assert "continues the existing thread" in out


def test_deeper_rounds_and_the_report_land_in_the_thread(host, searches):
    rest = _with_forum(host)
    r.research_start("chip export controls", caller=CALLER)
    r.research_go_deeper("what about Europe", caller=CALLER)
    assert any("Deeper: what about Europe" in (b or {}).get("content", "")
               for _, p, b in rest.calls if p == "/channels/t1/messages")
    rest.calls.clear()
    r.research_report("Chips", "## Summary\nlong text", caller=CALLER)
    posts = [b for _, p, b in rest.calls if p == "/channels/t1/messages"]
    assert posts and "# Chips" in posts[0]["content"]


def test_without_a_forum_nothing_is_posted(host, searches):
    rest = RecRest()
    host.rest = rest
    out = r.research_start("anything at all", caller=CALLER)
    assert rest.calls == [] and "Filed in the research forum" not in out


def test_a_refused_thread_does_not_break_the_answer(host, searches):
    rest = _with_forum(host)
    rest.fail_threads = True
    out = r.research_start("some question entirely", caller=CALLER)
    assert "Research session r-" in out and "Filed in the research forum" not in out


def test_messages_are_chunked_under_discords_limit():
    parts = r._chunks("x" * 5000)
    assert all(len(p) <= 1900 for p in parts) and len(parts) == 3
