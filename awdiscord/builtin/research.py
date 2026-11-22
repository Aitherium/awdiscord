"""Research sessions: keep going, then hand over a document with its sources.

A single ``deep_research`` call answers one question and forgets. A member who
wanted more had to start again from nothing, and what came back was a chat
message, never a file.

A session keeps the question, every follow-up ("go deeper on X") and every
search row it saw, so the next round builds on the last. ``research_report``
turns it into a Markdown document -- the agent's write-up plus a numbered
source list deduped across all rounds -- and attaches it with the answer
(``caller["outbox"]``). Sessions are per member, per channel, so a thread is
the natural place to iterate.
"""
from __future__ import annotations

import logging
import re
import secrets
import time

from awdiscord.builtin import web
from awdiscord.plugins import Command, Option, Plugin, ScopedStore, Tool

log = logging.getLogger("awdiscord.research")

MAX_ROUNDS = 12
MAX_SOURCES = 60
MAX_ROWS_PER_ROUND = 8
_HOST: dict = {}


def _remember_host(host) -> None:
    _HOST["host"] = host


def _store(caller: dict):
    host = _HOST.get("host")
    guild = str(caller.get("guild") or "")
    if host is None or not guild:
        return None
    return ScopedStore(host.store, guild, PLUGIN.name)


def _active_key(caller: dict) -> str:
    return f"active:{caller.get('channel') or ''}:{caller.get('user') or ''}"


def _session_key(sid: str) -> str:
    return f"s:{sid}"


def _in_a_server(caller: dict) -> bool:
    return bool(caller.get("guild")) and bool(caller.get("channel"))


# ── the knowledge base: research filed into a forum, one thread per question ──
#
# Every session can be filed as a thread in one forum channel (a manager picks
# it with /research forum). The thread is the durable artefact: the first round
# opens it, each "go deeper" appends, the report lands in it. A later member who
# asks the same thing continues that thread instead of starting over, so the
# forum gets more detailed the more people use it.

STOPWORDS = frozenset((
    "the", "and", "for", "are", "was", "were", "what", "who", "whom", "how", "why",
    "when", "where", "which", "with", "from", "about", "into", "that", "this",
    "these", "those", "has", "have", "had", "does", "did", "can", "could", "should",
    "would", "will", "there", "their", "them", "its", "our", "your", "you", "not",
    "but", "all", "any", "more", "most", "some", "than", "then", "now", "today",
))
MATCH_SCORE = 0.45          # Jaccard over keywords
MATCH_SHARED = 3            # ...and at least this many words in common


def _forum(store) -> str:
    conf = store.get("config") or {}
    return str(conf.get("forum") or "") if isinstance(conf, dict) else ""


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return sorted({w for w in words if len(w) >= 3 and w not in STOPWORDS})


def _kb_match(store, question: str):
    """The filed thread most nearly about ``question``, or None."""
    mine = set(_keywords(question))
    if len(mine) < 3:
        return None
    best, best_score = None, 0.0
    for key in store.keys("kb:"):
        entry = store.get(key)
        if not isinstance(entry, dict):
            continue
        theirs = set(entry.get("keywords") or [])
        shared = mine & theirs
        score = len(shared) / max(1, len(mine | theirs))
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < MATCH_SCORE or \
            len(mine & set(best.get("keywords") or [])) < MATCH_SHARED:
        return None
    return best


def _kb_remember(store, session: dict, thread: str) -> None:
    store.set(f"kb:{thread}", {"thread": thread, "title": session.get("question", ""),
                               "keywords": _keywords(session.get("question", "")),
                               "created": int(time.time())})


def _rest():
    host = _HOST.get("host")
    return getattr(host, "rest", None) if host is not None else None


def _chunks(text: str, size: int = 1850) -> list[str]:
    """Discord takes 2000 characters a message; cut on lines where it can."""
    out, buf = [], ""
    for line in str(text).splitlines(True):
        while len(line) > size:                      # a single monster line
            out.append(buf + line[:size - len(buf)])
            line, buf = line[size - len(buf):], ""
        if len(buf) + len(line) > size:
            out.append(buf)
            buf = ""
        buf += line
    if buf:
        out.append(buf)
    return out[:8] or [""]


def _post_message(thread: str, text: str) -> bool:
    rest = _rest()
    if rest is None or not thread:
        return False
    try:
        for part in _chunks(text):
            rest.request("POST", f"/channels/{thread}/messages",
                         {"content": part, "allowed_mentions": {"parse": []}})
        return True
    except Exception as exc:  # noqa: BLE001 - the answer must not be lost over this
        log.info("research: could not post in thread %s (%s)", thread, type(exc).__name__)
        return False


def _open_thread(forum: str, title: str, first: str) -> str:
    """A forum post (= a thread) with its first message; "" when Discord refuses."""
    rest = _rest()
    if rest is None or not forum:
        return ""
    try:
        made = rest.request("POST", f"/channels/{forum}/threads", {
            "name": (str(title).strip() or "Research")[:100],
            "message": {"content": _chunks(first)[0],
                        "allowed_mentions": {"parse": []}},
            "auto_archive_duration": 10080}) or {}
        return str(made.get("id") or "")
    except Exception as exc:  # noqa: BLE001
        log.info("research: could not open a thread in %s (%s)", forum,
                 type(exc).__name__)
        return ""


def _thread_note(tid: str, resumed: bool) -> str:
    if not tid:
        return ""
    return (f" Filed in the research forum as <#{tid}>"
            + (" — this continues the existing thread." if resumed else "."))


def _set_forum(ctx):
    """Slash: /research forum channel:<#forum> — where research becomes a KB."""
    channel = str(ctx.opt("channel") or "").strip()
    if not channel:
        return ctx.reply("Pick the forum channel.", private=True)
    store = ScopedStore(ctx.host.store, str(ctx.guild), PLUGIN.name)
    conf = store.get("config") or {}
    conf = dict(conf) if isinstance(conf, dict) else {}
    conf["forum"] = channel
    store.set("config", conf)
    return ctx.reply(f"Research threads will file into <#{channel}>. Asking the same "
                     "question again continues the existing thread.", private=True)


def _show_forum(ctx):
    store = ScopedStore(ctx.host.store, str(ctx.guild), PLUGIN.name)
    forum = _forum(store)
    return ctx.reply(f"Research forum: {('<#' + forum + '>') if forum else 'not set'}"
                     + ("" if forum else " — a manager can set it with "
                        "`/research forum channel:<forum>`."), private=True)


def _owned(session, caller: dict) -> bool:
    """A session is private to the member who opened it, in the guild it lives in."""
    if not isinstance(session, dict):
        return False
    return str(session.get("user") or "") == str(caller.get("user") or "")


def _from_id(store, research_id: str, caller: dict):
    """The named session if THIS member owns it, else (None, refusal-or-None)."""
    session = store.get(_session_key(str(research_id)))
    if session is None:
        return None, None
    if not _owned(session, caller):
        return None, "That research session belongs to someone else."
    return session, None


def _load(caller: dict):
    """(store, session) for the caller's active session, or (store, None)."""
    store = _store(caller)
    if store is None:
        return None, None
    sid = str(store.get(_active_key(caller)) or "")
    return store, (store.get(_session_key(sid)) if sid else None)


def _rows_line(rows: list[dict]) -> str:
    return "\n".join(f"- {r.get('title', '')}\n  {r.get('url', '')}\n  "
                     f"{str(r.get('snippet') or '')[:240]}" for r in rows)


def _merge_sources(session: dict, rows: list[dict]) -> None:
    have = {s.get("url") for s in session.get("sources") or []}
    for r in rows:
        url = str(r.get("url") or "")
        if url and url not in have and len(session["sources"]) < MAX_SOURCES:
            session["sources"].append({"title": str(r.get("title") or ""),
                                       "url": url})
            have.add(url)


def _round(session: dict, instruction: str, query: str, rows: list[dict]) -> None:
    session["rounds"].append({"n": len(session["rounds"]), "instruction": instruction,
                              "query": query, "rows": rows, "at": int(time.time())})
    del session["rounds"][:-MAX_ROUNDS]
    _merge_sources(session, rows)


def research_start(question: str = "", caller: dict | None = None) -> str:
    """Agent tool: open a research session on ``question`` and run the first search."""
    caller = caller or {}
    question = str(question or "").strip()[:300]
    if not question:
        return "Give me the question to research."
    store = _store(caller)
    if store is None:
        return "I can only research in a server channel, and none is known here."
    try:
        rows = web.search_results(question, MAX_ROWS_PER_ROUND)
    except Exception as exc:  # noqa: BLE001 - a dead search is the answer, not a crash
        log.warning("research_start search failed: %s", type(exc).__name__)
        return (f"The search failed ({type(exc).__name__}), so no session was opened. "
                "Try again in a moment.")
    if not rows:
        return (f"No search results for {question!r}. Try fewer or more specific words.")
    sid = "r-" + secrets.token_hex(3)
    previous = _store(caller).get(_active_key(caller))
    session = {"id": sid, "question": question, "created": int(time.time()),
               "user": str(caller.get("user") or ""),
               "channel": str(caller.get("channel") or ""),
               "rounds": [], "sources": []}
    _round(session, "", question, rows)
    # File it: continue the thread that already covers this question, else open one.
    forum, filed, resumed = _forum(store), "", False
    if forum:
        hit = _kb_match(store, question)
        if hit:
            filed = str(hit.get("thread") or "")
            resumed = _post_message(filed, f"**New question on this thread:** {question}"
                                    + _rows_line(rows))
            if not resumed:
                filed = ""
        if not filed:
            filed = _open_thread(forum, question,
                                 f"**Research:** {question}\n\n{_rows_line(rows)}")
            resumed = False
        if filed:
            session["thread"] = filed
            _kb_remember(store, session, filed)
    store.set(_session_key(sid), session)
    store.set(_active_key(caller), sid)
    return (f"Research session {sid} opened on: {question}\n"
            f"What the first round found:\n{_rows_line(rows)}\n\n"
            f"Say 'go deeper on ...' and I continue this session; say 'write it up' "
            f"and I attach a document with the sources."
            + _thread_note(filed, resumed)
            + (f" This is now the active session; {previous} can still be addressed "
               f"by its id." if previous and previous != sid else ""))


def research_go_deeper(instruction: str = "", research_id: str = "",
                       caller: dict | None = None) -> str:
    """Agent tool: continue the active (or named) session with a targeted search."""
    caller = caller or {}
    instruction = str(instruction or "").strip()[:300]
    if not instruction:
        return "Say what to go deeper on."
    store = _store(caller)
    if store is None:
        return "I can only research in a server channel."
    if research_id:
        session, refusal = _from_id(store, research_id, caller)
        if refusal:
            return refusal
    else:
        _, session = _load(caller)
    if not isinstance(session, dict):
        return ("There is no research session here yet. Start one with the question "
                "first, then I can go deeper.")
    query = f"{session['question']} {instruction}"[:300]
    try:
        rows = web.search_results(query, MAX_ROWS_PER_ROUND)
    except Exception as exc:  # noqa: BLE001
        log.warning("research_go_deeper search failed: %s", type(exc).__name__)
        return f"The search failed ({type(exc).__name__}); the session is unchanged."
    if not rows:
        return f"No new results for {query!r}; the session is unchanged."
    _round(session, instruction, query, rows)
    store.set(_session_key(session["id"]), session)
    store.set(_active_key(caller), session["id"])
    if session.get("thread"):
        _post_message(str(session["thread"]),
                      f"**Deeper: {instruction}**\n\n{_rows_line(rows)}")
    return (f"Round {len(session['rounds'])} of session {session['id']} "
            f"({instruction}):\n{_rows_line(rows)}\n\n"
            f"{len(session['sources'])} source(s) in the session so far.")


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text or "report").lower()).strip("-")
    return (out[:50] or "report")


def build_markdown(session: dict, title: str, body: str) -> str:
    """The document: write-up first, then every round's findings, then sources."""
    title = str(title or "").strip()[:120] or session.get("question", "Research")
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(session.get("created") or 0)))
    parts = [f"# {title}", "",
             f"**Question:** {session.get('question', '')}  ",
             f"**Research session:** {session.get('id', '')} · started {when}  ",
             f"**Sources:** {len(session.get('sources') or [])}", ""]
    if str(body or "").strip():
        parts += ["## Summary", "", str(body).strip()[:12000], ""]
    parts += ["## Findings", ""]
    for rnd in session.get("rounds") or []:
        head = rnd.get("instruction") or "Initial search"
        parts += [f"### {head}", "", f"_Search:_ `{rnd.get('query', '')}`", ""]
        parts += [f"- {r.get('title', '')}\n  {r.get('url', '')}\n  "
                  f"{str(r.get('snippet') or '')[:400]}" for r in rnd.get("rows") or []]
        parts.append("")
    parts += ["## Sources", ""]
    for i, src in enumerate(session.get("sources") or [], 1):
        parts.append(f"{i}. [{src.get('title') or src.get('url')}]({src.get('url')})")
    parts += ["", "---", "_Compiled by Aither · Aitherium._"]
    return "\n".join(parts) + "\n"


def research_report(title: str = "", body: str = "", research_id: str = "",
                    caller: dict | None = None) -> str:
    """Agent tool: attach the session as a Markdown document with its sources."""
    caller = caller or {}
    store = _store(caller)
    if store is None:
        return "I can only write up research in a server channel."
    if research_id:
        session, refusal = _from_id(store, research_id, caller)
        if refusal:
            return refusal
    else:
        _, session = _load(caller)
    if not isinstance(session, dict):
        return "There is no research session here yet; start one first."
    text = build_markdown(session, title, body)
    name = _slug(title or session.get("question")) + ".md"
    # The forum copy is what makes the knowledge durable and shared.
    thread = str(session.get("thread") or "")
    if not thread:
        forum = _forum(store)
        if forum:
            thread = _open_thread(forum, session.get("question", "Research"),
                                  f"**Research:** {session.get('question', '')}")
            if thread:
                session["thread"] = thread
                _kb_remember(store, session, thread)
                store.set(_session_key(session["id"]), session)
    if thread:
        _post_message(thread, text)
    outbox = caller.get("outbox")
    if outbox is not None:
        try:
            outbox.add(name, text.encode("utf-8"), "text/markdown")
            return (f"Attached {name} ({len(text) // 1024 + 1} KB): your write-up, "
                    f"{len(session.get('rounds') or [])} round(s) of findings and "
                    f"{len(session.get('sources') or [])} sources. Point the member at "
                    "the file; do not repeat it in the answer.")
        except Exception as exc:  # noqa: BLE001 - say why, never drop silently
            log.warning("research_report outbox failed: %s", exc)
    return ("I could not attach the file here, so here is the document:\n\n"
            + text[:1500])


def research_sources(research_id: str = "", caller: dict | None = None) -> str:
    """Agent tool: list the session's sources."""
    caller = caller or {}
    store = _store(caller)
    if store is None:
        return "I can only list sources in a server channel."
    if research_id:
        session, refusal = _from_id(store, research_id, caller)
        if refusal:
            return refusal
    else:
        _, session = _load(caller)
    if not isinstance(session, dict):
        return "There is no research session here yet."
    srcs = session.get("sources") or []
    if not srcs:
        return "This session has no sources yet."
    return (f"{len(srcs)} source(s) in session {session.get('id')}:\n"
            + "\n".join(f"{i}. {s.get('title') or s.get('url')} — {s.get('url')}"
                        for i, s in enumerate(srcs, 1)))


START = Tool(
    "research_start",
    "Open a research session and run the first web search on the question. Use when a "
    "member asks you to research, investigate or look into something properly. After "
    "this, 'go deeper' continues the SAME session; it is not a new question.",
    research_start,
    {"type": "object", "properties": {
        "question": {"type": "string", "description": "the research question"}},
     "required": ["question"]}, wants_caller=True, offered=_in_a_server)

DEEPER = Tool(
    "research_go_deeper",
    "Continue the member's active research session with a targeted follow-up search "
    "('go deeper on pricing', 'what about Europe', 'find the primary source'). Use "
    "whenever they ask to continue, expand, narrow or dig further; never start a new "
    "session for that.",
    research_go_deeper,
    {"type": "object", "properties": {
        "instruction": {"type": "string", "description": "what to dig into"},
        "research_id": {"type": "string", "description": "only to address an older "
                        "session by id; leave empty for the active one"}},
     "required": ["instruction"]}, wants_caller=True, offered=_in_a_server)

REPORT = Tool(
    "research_report",
    "Write the research session up as a Markdown DOCUMENT and attach it to the answer "
    "with a numbered source list. Put your own synthesis in `body` (the member's "
    "answer): sections, findings, caveats, what is unknown. Use when a member asks for "
    "a report, a document, a write-up, to 'save it', or to send/export the research.",
    research_report,
    {"type": "object", "properties": {
        "title": {"type": "string", "description": "document title"},
        "body": {"type": "string", "description": "your synthesis in Markdown; the tool "
                 "adds the findings and the numbered sources below it"},
        "research_id": {"type": "string", "description": "optional older session id"}}},
     wants_caller=True, offered=_in_a_server)

SOURCES = Tool(
    "research_sources",
    "List every source (title and URL) the active research session has collected.",
    research_sources,
    {"type": "object", "properties": {
        "research_id": {"type": "string", "description": "optional older session id"}}},
     wants_caller=True, offered=_in_a_server)

PLUGIN = Plugin(
    name="research",
    version="0.2.0",
    description="Research sessions that continue, file into a forum, and end as a doc",
    default_enabled=True,
    on_ready=[_remember_host],
    tools=[START, DEEPER, REPORT, SOURCES],
    commands=[
        Command("research", "How research is filed", subcommands=[
            Command("forum", "File research threads in this forum (managers)",
                    _set_forum, admin=True,
                    options=[Option("channel", "The forum channel", type="channel",
                                    required=True)]),
            Command("where", "Which forum research files into", _show_forum),
        ]),
    ],
    persona=("Research is a SESSION, not one answer. When a member asks you to research "
             "something, call research_start; when they ask to go deeper, expand or keep "
             "going, call research_go_deeper on the same session; when they want it "
             "written up, saved or sent, call research_report and attach the document. "
             "Research is filed into the server's research forum: the first round opens "
             "a thread, deeper rounds append to it, and asking the same question again "
             "continues the existing thread, so say when a thread was continued rather "
             "than started. Never tell a member you cannot keep a thread of research "
             "going."),
)
