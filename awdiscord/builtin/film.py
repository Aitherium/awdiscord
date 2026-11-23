"""Film: a customer steers their own construction film from their Discord channel.

On by default, and it offers NOTHING until a channel is mapped: the tools exist
only in a channel named in ``AWDISCORD_FILM_MAP`` and only for the Discord users
that entry lists as the customer's members. Everyone else, and every other
channel, never sees them (``Tool.offered``).

The map (a JSON file) binds one channel to one customer instance::

    {"refuse_hosts": ["owner-forge.internal"],
     "channels": {"<channel id>": {
         "tenant": "tnt_x", "instance": "x-mediaforge",
         "url": "http://x-mediaforge:8200", "project": "x-film",
         "members": ["<discord user id>", ...],
         "token_file": "/run/secrets/film-tnt_x",
         "desk_url": "https://...",
         "name": "the customer's name, as their team reads it (optional)"}}}

The film's state lives ONLY in that instance; this module is glue over its ops
(``POST <url>/api/op/<name>``), with the platform's names: ``film_steer_status``,
``film_comment_step``, ``film_steer``, ``film_preview``. An entry whose host is in
``refuse_hosts`` is dropped, so a customer can never be pointed at an instance
that is not theirs. The call carries the tenant's bearer from ``token_file`` and
``X-Aither-Origin: agent-tool``: the instance judges every verb itself, and an
agent tool never approves -- approve/reject come back refused with the film
desk's link, exactly as the film procedure gates them.

QUESTION CARDS (2026-11-22). A script proposal carries open questions, each with a
prompt and two or three options. They used to reach the customer as pasted text he
had to answer by typing "5: B". ``film_questions`` posts them as CARDS instead: one
message per question, one button per option, an "Other" button that opens a form.
A press records the answer on the customer's own instance through the same
``film_steer`` op (action ``answer``) the text path uses, as the person who
pressed, and the card is edited to show it.

Nothing about a card lives in this process: a button's ``custom_id`` names the
binding (a digest of tenant, instance and project), the proposal, the question and
the option, and the press re-reads the questions from the instance before anything
is recorded. So a card still works after the bot restarts, a card for an earlier
proposal or a changed question records nothing, and an answer is only ever what
the person pressed or typed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awdiscord.plugins import Plugin, ScopedStore, Tool
from awdiscord.rest import RestError

log = logging.getLogger("awdiscord.film")

MAP_ENV = "AWDISCORD_FILM_MAP"
TIMEOUT_S = 45.0
ORIGIN_HEADER = "X-Aither-Origin"
ACTIONS = ("approve", "reject", "revise", "answer")
REQUIRED = ("tenant", "instance", "url", "project", "members")

#: Cards posted by one ``film_questions`` call: a channel is never flooded.
MAX_CARDS = 5
#: Discord's ceiling for a button label.
LABEL_MAX = 80
#: Option buttons on one card (Discord takes 25 components; one is "Other").
MAX_OPTION_BUTTONS = 20
#: The longest answer a person may type (the instance keeps 500 characters).
ANSWER_MAX = 500
BLURPLE, GREEN = 0x5865F2, 0x57F287
DEFERRED_UPDATE = 6     # interaction response: ack a press, edit its message later
EPHEMERAL = 64
NO_MENTIONS = {"parse": []}
_HOST: dict = {}


class FilmError(RuntimeError):
    """The instance refused or did not answer. Never turned into an empty read.

    ``detail`` is the instance's own reason when it gave one (plain words a card
    may show a customer); the message itself names the instance and the op.
    """

    def __init__(self, message: str, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Binding:
    channel: str
    tenant: str
    instance: str
    url: str
    project: str
    members: frozenset
    token_file: str = ""
    desk_url: str = ""
    name: str = ""


# --------------------------------------------------------------------------- the map

def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def load_map(path: str | None = None) -> dict[str, Binding]:
    """Channel id -> Binding. A missing, unreadable or invalid map binds nothing."""
    raw_path = (path if path is not None else os.getenv(MAP_ENV, "")).strip()
    if not raw_path:
        return {}
    try:
        doc = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("film map %s unreadable (%s): no channel is bound", raw_path, exc)
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("channels"), dict):
        log.warning("film map %s has no channels object: no channel is bound", raw_path)
        return {}
    refused = {str(h).strip().lower() for h in doc.get("refuse_hosts") or () if str(h).strip()}
    out: dict[str, Binding] = {}
    for channel, entry in doc["channels"].items():
        if not isinstance(entry, dict) or any(not entry.get(k) for k in REQUIRED):
            log.warning("film map: channel %s lacks one of %s; skipped", channel, REQUIRED)
            continue
        url = str(entry["url"]).strip().rstrip("/")
        if urllib.parse.urlsplit(url).scheme not in ("http", "https") or not _host(url):
            log.warning("film map: channel %s url %r is not http(s); skipped", channel, url)
            continue
        if _host(url) in refused:
            log.warning("film map: channel %s points at refused host %s; skipped",
                        channel, _host(url))
            continue
        members = entry["members"]
        if not isinstance(members, list) or not all(str(m).isdigit() for m in members):
            log.warning("film map: channel %s members must be Discord user ids; skipped",
                        channel)
            continue
        out[str(channel)] = Binding(
            channel=str(channel), tenant=str(entry["tenant"]),
            instance=str(entry["instance"]), url=url, project=str(entry["project"]),
            members=frozenset(str(m) for m in members),
            token_file=str(entry.get("token_file") or ""),
            desk_url=str(entry.get("desk_url") or ""),
            name=" ".join(str(entry.get("name") or "").split())[:80])
    return out


def binding_for(caller: dict | None) -> Binding | None:
    """The binding for this turn, or None unless the channel is mapped AND the asker
    is one of that customer's members. Reads the map each time (it is tiny), so a
    map edit needs no restart."""
    c = caller or {}
    b = load_map().get(str(c.get("channel") or ""))
    if b is None or str(c.get("user") or "") not in b.members:
        return None
    return b


def offered(caller: dict) -> bool:
    return binding_for(caller) is not None


# --------------------------------------------------------------------------- the client

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a refusal: following it would carry the tenant's bearer elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _opener() -> urllib.request.OpenerDirector:
    ca = (os.getenv("AWDISCORD_CA_BUNDLE") or "").strip() or None
    ctx = ssl.create_default_context(cafile=ca)
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))


def _token(b: Binding) -> str:
    if not b.token_file:
        raise FilmError(f"no credential is configured for {b.tenant}'s film instance "
                        f"({b.instance}), so I could not reach it", 401)
    try:
        tok = Path(b.token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FilmError(f"the credential for {b.tenant}'s film instance is unreadable "
                        f"({type(exc).__name__})", 401) from exc
    if not tok:
        raise FilmError(f"the credential for {b.tenant}'s film instance is empty", 401)
    return tok


def _headers(b: Binding) -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {_token(b)}", ORIGIN_HEADER: "agent-tool"}


def _send(b: Binding, req: urllib.request.Request, what: str) -> dict:
    """One request to the customer's OWN instance; its result dict, or FilmError."""
    try:
        with _opener().open(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read(4096).decode("utf-8", "replace"))
            detail = str(parsed.get("error") or parsed.get("detail") or "")
        except (OSError, ValueError, AttributeError):
            detail = ""
        raise FilmError(f"{b.instance} refused {what} (HTTP {exc.code})"
                        + (f": {detail[:300]}" if detail else ""), exc.code,
                        detail[:300]) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FilmError(f"{b.instance} did not answer {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise FilmError(f"{b.instance} answered {what} with {type(data).__name__}")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if data.get("ok") is False:
        why = str(data.get("error") or data.get("detail") or "no reason given")
        # 400, not 0: the instance ANSWERED and said no. 0 means it never answered.
        raise FilmError(f"{b.instance} refused {what}: {why[:300]}",
                        int(meta.get("status") or 0) or 400, why[:300])
    result = meta.get("result")
    return result if isinstance(result, dict) else data


def call(b: Binding, op: str, payload: dict | None = None, by: str = "") -> dict:
    """POST one film op on the customer's OWN instance; return its result dict.

    The project always comes from the binding, never from the model.
    """
    body = {k: v for k, v in (payload or {}).items() if v not in (None, "")}
    body["project"] = b.project
    if by:
        body["by"] = by
    req = urllib.request.Request(
        f"{b.url}/api/op/{op}", data=json.dumps(body).encode("utf-8"), method="POST",
        headers=_headers(b))
    return _send(b, req, op)


def read(b: Binding, path: str, what: str) -> dict:
    """GET one of the film desk's READ routes on the customer's own instance."""
    req = urllib.request.Request(f"{b.url}{path}", method="GET", headers=_headers(b))
    return _send(b, req, what)


# --------------------------------------------------------------------------- formatting

def summarize(state: dict) -> str:
    acc = state.get("accepted") if isinstance(state.get("accepted"), dict) else {}
    lines = [f"Film: {state.get('project') or '?'}"]
    if acc.get("version"):
        lines.append(f"Accepted script: v{str(acc['version']).lstrip('vV')} "
                     f"({acc.get('filename') or '?'})")
    else:
        lines.append("Accepted script: none recorded on this instance")
    pend = state.get("pending")
    if isinstance(pend, dict) and pend:
        changed = ", ".join(map(str, pend.get("changed_steps") or [])) or "none listed"
        status = pend.get("status") or "pending"
        lines.append(f"Pending proposal {pend.get('id') or '?'} ({status}), "
                     f"changed steps: {changed}")
    else:
        lines.append("Pending proposal: none")
    qs = state.get("open_questions")
    n = len(qs) if isinstance(qs, list) else int(qs) if str(qs or "").isdigit() else 0
    if n:
        lines.append(f"Open questions: {n} (film_questions posts them as cards with a "
                     "button per option; never paste them as text)")
    oc = state.get("open_comments") if isinstance(state.get("open_comments"), dict) else {}
    if oc:
        lines.append("Open comments: " + "; ".join(
            f"step {s}: {len(v) if isinstance(v, list) else v}" for s, v in list(oc.items())[:20]))
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    if steps:
        by_state: dict[str, list] = {}
        for s, row in steps.items():
            st = str((row or {}).get("state") or "unknown") if isinstance(row, dict) else str(row)
            by_state.setdefault(st, []).append(str(s))
        lines.append("Steps: " + "; ".join(f"{k}: {', '.join(v[:15])}"
                                           for k, v in sorted(by_state.items())))
    job = state.get("job")
    if isinstance(job, dict) and job:
        lines.append(f"Render: {job.get('state') or job.get('status') or 'running'}")
    return "\n".join(lines)


def _who(caller: dict | None) -> str:
    return f"discord:{(caller or {}).get('user') or '?'}"


def _refused(caller: dict | None) -> str:
    return ("I can only do that for this film's own team in its own channel, so I "
            "changed nothing.")


def _desk(b: Binding) -> str:
    return f" Open the film desk to do it: {b.desk_url}" if b.desk_url else \
        " A signed-in admin does that from the film desk."


# --------------------------------------------------------------------------- questions
# What the instance returns for one question (media-forge ``scene_questions``):
# ``id``, ``step`` (or ``steps``), ``kind``, ``quote`` (the script's own wording),
# ``question`` (the prompt), ``options`` (a list of strings) and, once answered,
# ``answer`` (``{"choice", "by", "at"}``). Other spellings of the prompt are accepted.

def _line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _answer_text(answer: Any) -> str:
    if isinstance(answer, dict):
        return _line(answer.get("choice") or answer.get("text")) or "answered"
    return _line(answer)


def _steps(q: dict) -> list[int]:
    raw = q.get("steps") if isinstance(q.get("steps"), list) else [q.get("step")]
    return [int(s) for s in raw if str(s or "").isdigit() and int(s) > 0]


def questions_of(raw: Any) -> list[dict]:
    """The instance's questions in ITS order, each ``{id, steps, prompt, quote, options,
    answer}``. An entry with no id or no prompt is dropped: it cannot be asked."""
    out = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        prompt = _line(q.get("question") or q.get("q") or q.get("prompt") or q.get("text"))
        if not qid or not prompt:
            continue
        options = q.get("options") if isinstance(q.get("options"), list) else []
        out.append({"id": qid, "steps": _steps(q), "prompt": prompt,
                    "quote": _line(q.get("quote")),
                    "options": [o for o in map(_line, options) if o],
                    "answer": _answer_text(q.get("answer"))})
    return out


def _question_list(doc: Any) -> Any:
    """Where a status, a plan or a saved proposal keeps its questions."""
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("questions"), list):
        return doc["questions"]
    plan = doc.get("plan") if isinstance(doc.get("plan"), dict) else {}
    return plan.get("questions")


def read_questions(b: Binding) -> dict:
    """``{"proposal": id, "questions": [...]}`` for the proposal whose questions hold
    the film: every question, answered or not, in the instance's order.

    The film desk's own read route is asked first (``.../steer/questions?all=true``):
    it picks the proposal that blocks the film and changes nothing. An instance that
    refuses or lacks that route is asked through the ops instead: ``film_steer_status``
    for the pending proposal, then ``film_plan_steps`` for its questions (that op
    re-plans, which is why it is second). An instance that does not answer at all is
    an error at once; it is not asked twice.
    """
    view: dict | None = None
    try:
        view = read(b, f"/api/film/show/{urllib.parse.quote(b.project, safe='')}"
                       "/steer/questions?all=true", "the questions")
    except FilmError as exc:
        if not exc.status:
            raise
        log.info("film: questions route refused (HTTP %s); using the plan op", exc.status)
    if view is None or not isinstance(view.get("questions"), list):
        pid = _pending_id(b)
        if not pid:
            return {"proposal": "", "questions": []}
        view = {"proposal": pid,
                "questions": _question_list(call(b, "film_plan_steps", {"proposal_id": pid}))}
    return {"proposal": str(view.get("proposal") or ""),
            "questions": questions_of(view.get("questions"))}


# --------------------------------------------------------------------------- the cards
# One message per question. Everything a press needs is in the button's custom_id
# (``awp:film:<kind>:<binding key>:<proposal>:<question>[:<option>:<option digest>]``):
#   a = answer with option N     o = open the "Other" form     c = change an answer
#   m = the form's submission (its text input is ``answer``)
# A proposal or question id that is long or has odd characters is carried as
# ``#<10 hex of its sha256>`` and found again by re-reading the questions.

_PLAIN_REF = re.compile(r"^[A-Za-z0-9._-]{1,24}$")


def _digest(text: str, n: int) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:n]


def binding_key(b: Binding) -> str:
    """Which film a card belongs to. A map later pointed at another instance or
    project changes the key, so an old card can never answer on a different film."""
    return _digest(f"{b.tenant}|{b.instance}|{b.project}", 8)


def _ref(value: str) -> str:
    value = str(value)
    return value if _PLAIN_REF.match(value) else "#" + _digest(value, 10)


def _letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else str(i + 1)


def option_label(i: int, option: str) -> str:
    """The button's label: the option's letter and words, cut to Discord's 80
    characters. The card's body lists every option in full under the same letter."""
    text = f"{_letter(i)}. {option}"
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 1].rstrip() + "…"


def card_title(n: int, total: int, q: dict) -> str:
    steps = q.get("steps") or []
    where = ""
    if steps:
        where = (" · Step " if len(steps) == 1 else " · Steps ") + ", ".join(map(str, steps))
    return f"Question {n} of {total}{where}"[:256]


def card_embed(q: dict, title: str, answered: str = "") -> dict:
    """The card's embed: the prompt, the script's own wording when given, every
    option in full, and either how to answer or what was answered."""
    lines = [q["prompt"]]
    quote = q.get("quote") or ""
    if quote and not quote.startswith("(") and quote.lower() not in q["prompt"].lower():
        lines += ["", f'In the script: "{quote}"']
    options = q.get("options") or []
    if options:
        lines.append("")
        lines += [f"{_letter(i)}. {o}" for i, o in enumerate(options)]
    lines.append("")
    if answered:
        lines.append(f"Answered: {answered}")
    elif options:
        lines.append("Pick one below, or choose Other to answer in your own words.")
    else:
        lines.append("Choose Other to answer in your own words.")
    return {"title": title[:256], "description": "\n".join(lines)[:4096],
            "color": GREEN if answered else BLURPLE}


def _button(label: str, payload: str, style: int) -> dict:
    return {"type": 2, "style": style, "label": label, "custom_id": PLUGIN.button_id(payload)}


def card_rows(b: Binding, proposal: str, q: dict) -> list:
    """One button per option (five to a row), then "Other"."""
    key, pref, qref = binding_key(b), _ref(proposal), _ref(q["id"])
    buttons = [_button(option_label(i, o), f"a:{key}:{pref}:{qref}:{i}:{_digest(o, 6)}", 1)
               for i, o in enumerate((q.get("options") or [])[:MAX_OPTION_BUTTONS])]
    buttons.append(_button("Other…", f"o:{key}:{pref}:{qref}", 2))
    return [{"type": 1, "components": buttons[i:i + 5]} for i in range(0, len(buttons), 5)]


def answered_rows(b: Binding, proposal: str, q: dict) -> list:
    payload = f"c:{binding_key(b)}:{_ref(proposal)}:{_ref(q['id'])}"
    return [{"type": 1, "components": [_button("Change", payload, 2)]}]


def card(b: Binding, proposal: str, q: dict, n: int, total: int) -> dict:
    """The whole message for one open question."""
    return {"embeds": [card_embed(q, card_title(n, total, q))],
            "components": card_rows(b, proposal, q), "allowed_mentions": NO_MENTIONS}


def _store(host, guild: str) -> ScopedStore | None:
    if getattr(host, "store", None) is None or not guild:
        return None
    return ScopedStore(host.store, str(guild), PLUGIN.name)


def _pick(open_qs: list, shown: list, start: int) -> tuple[list, list]:
    """``(the open questions this call may card, the ids that keep counting as carded)``.

    From ``start`` when one is given. Otherwise the ones not carded yet -- and when
    every open one already has a card, all of them again with the count started
    over (somebody asking twice wants to see what is still open).
    """
    if start > 0:
        return [(n, q) for n, q in open_qs if n >= start], shown
    fresh = [(n, q) for n, q in open_qs if q["id"] not in shown]
    return (fresh, shown) if fresh else (open_qs, [])


def _post_cards(rest, b: Binding, proposal: str, todo: list, total: int) -> tuple[list, bool]:
    """Post up to ``MAX_CARDS`` cards, then the line saying how many remain.

    ``(the (number, question) pairs that were posted, whether that line went out)``.
    A Discord refusal stops the batch; what was posted before it stays posted.
    """
    posted: list = []
    try:
        for n, q in todo[:MAX_CARDS]:
            rest.create_message(b.channel, card(b, proposal, q, n, total))
            posted.append((n, q))
        left = len(todo) - len(posted)
        if left:
            rest.create_message(b.channel, {
                "content": f"{left} more: say 'more questions'.",
                "allowed_mentions": NO_MENTIONS})
        return posted, bool(left)
    except RestError as exc:
        log.warning("film: question cards refused by Discord (HTTP %s)", exc.status)
        return posted, False


def film_questions(start: int = 0, caller: dict | None = None, **_extra: Any) -> str:
    """Post the pending proposal's open questions as cards (at most five a call)."""
    b = binding_for(caller)
    if b is None:
        return _refused(caller)
    host = _HOST.get("host")
    rest = getattr(host, "rest", None) if host is not None else None
    if rest is None:
        return ("I cannot post question cards from here (no Discord connection in this "
                "process). Nothing was posted.")
    try:
        view = read_questions(b)
    except FilmError as exc:
        return f"I could not read the film's questions: {exc}. Nothing was posted."
    every = view["questions"]
    if not view["proposal"] or not every:
        return "There is no proposal with questions on this film right now."
    open_qs = [(n, q) for n, q in enumerate(every, 1) if not q["answer"]]
    if not open_qs:
        return ("Every question on the pending proposal is answered. The plan can be "
                "approved." + _desk(b))
    try:
        first = max(0, int(start or 0))
    except (TypeError, ValueError):
        first = 0
    store = _store(host, str((caller or {}).get("guild") or ""))
    scope = f"{binding_key(b)}:{_ref(view['proposal'])}"
    shown = list(store.get(f"carded:{scope}") or []) if store is not None else []
    todo, carded = _pick(open_qs, shown, first)
    if not todo:
        return (f"There is no open question from number {first} on; {len(open_qs)} are "
                "open in all. Call film_questions without start to post them.")
    posted, told = _post_cards(rest, b, view["proposal"], todo, len(every))
    if not posted:
        return "Discord refused the question cards in this channel; nothing was posted."
    if store is not None:
        store.delete(f"done:{scope}")     # new cards: "all answered" may be said again
        ids = [q["id"] for _, q in posted]
        store.set(f"carded:{scope}", ([i for i in carded if i not in ids] + ids)[-500:])
    left = len(todo) - len(posted)
    numbers = ", ".join(str(n) for n, _ in posted)
    if not left:
        more = " That is every open question."
    elif told:
        more = (f" {left} more are waiting; the channel was told to say 'more questions' "
                "(call film_questions again then).")
    else:
        more = (f" {left} more are waiting, but Discord refused the rest of this batch: "
                "say so, and that 'more questions' posts them.")
    return (f"Posted {len(posted)} question card(s) in this channel (question {numbers} of "
            f"{len(every)}), each with a button per option.{more} Do NOT repeat the "
            "questions or their options as text; one short line is enough.")


# --------------------------------------------------------------------------- the presses

class _StaleCardError(Exception):
    """The card no longer matches the film; its message is shown to the presser."""


def _press_binding(ctx, key: str) -> tuple[Binding | None, str]:
    """The binding a press may act on, or ``(None, what to tell the presser)``.

    ``binding_for`` is the plugin's one member check: the channel must be mapped
    and the presser one of that customer's members.
    """
    channel = str(ctx.channel or "")
    b = binding_for({"channel": channel, "user": ctx.user})
    if b is None:
        known = load_map().get(channel)
        if known is None:
            return None, ("This channel is no longer linked to a film, so nothing was "
                          "recorded.")
        return None, (f"Only {known.name or 'the film'} team members can answer. "
                      "Nothing was recorded.")
    if binding_key(b) != key:
        return None, ("This card belongs to an earlier setup of this film, so nothing "
                      "was recorded. Say 'questions' to get the current cards.")
    return b, ""


def _find(view: dict, pref: str, qref: str) -> tuple[dict, int, int]:
    if not view["proposal"] or _ref(view["proposal"]) != pref:
        raise _StaleCardError("This card is from an earlier version of the plan, so I left it as "
                     "it is. Say 'questions' to get the current ones.")
    for n, q in enumerate(view["questions"], 1):
        if _ref(q["id"]) == qref:
            return q, n, len(view["questions"])
    raise _StaleCardError("This question is no longer open on the film, so I left it as it is. "
                 "Say 'questions' to see what is still open.")


def _plain(exc: FilmError, doing: str) -> str:
    """An instance failure in words a customer can read: no host, no op name."""
    if not exc.status:
        return (f"The film desk is not answering right now, so I could not {doing}. "
                "Nothing was recorded. Please try again in a few minutes.")
    why = f": {exc.detail}" if exc.detail else ""
    return f"The film desk did not let me {doing}{why}. Nothing was recorded."


def _display_name(interaction: dict) -> str:
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    name = _line(member.get("nick") or user.get("global_name") or user.get("username"))
    return name[:64] or "a team member"


def _posted(interaction: dict) -> dict:
    message = interaction.get("message")
    return message if isinstance(message, dict) else {}


def _posted_title(interaction: dict) -> str:
    embeds = _posted(interaction).get("embeds") or []
    first = embeds[0] if embeds and isinstance(embeds[0], dict) else {}
    return str(first.get("title") or "")


_BUTTON_KEYS = ("type", "style", "label", "custom_id", "disabled")


def _tell(ctx, text: str) -> None:
    """One private line to the presser, after the press was acknowledged."""
    rest = getattr(ctx.host, "rest", None)
    app = str(ctx.interaction.get("application_id") or "")
    token = str(ctx.interaction.get("token") or "")
    if rest is None or not app or not token:
        log.warning("film: could not tell the presser (no interaction to follow up)")
        return
    try:
        rest.followup(app, token, {"content": text[:1900], "flags": EPHEMERAL,
                                   "allowed_mentions": NO_MENTIONS})
    except RestError as exc:
        log.warning("film: private follow-up refused by Discord (HTTP %s)", exc.status)


def _unchanged(ctx, text: str) -> dict:
    """Tell the presser privately; the card keeps exactly the buttons it had."""
    _tell(ctx, text)
    rows = [{"type": 1, "components": [{k: c[k] for k in _BUTTON_KEYS if k in c}
                                        for c in row.get("components") or []]}
            for row in _posted(ctx.interaction).get("components") or []
            if isinstance(row, dict)]
    out: dict = {"allowed_mentions": NO_MENTIONS}
    if rows:
        out["components"] = rows
    return out


def _chosen(q: dict, pick: tuple) -> tuple[str, str]:
    """``(choice sent to the instance, what the card says was answered)``.

    A pressed option is sent as its 1-based number, and only when the option at
    that place is still the one the button was made for. Typed words are sent as
    typed; the instance reads a bare option number as that option, so the card
    says so too.
    """
    options = q.get("options") or []
    if pick[0] == "option":
        _, idx, made_for = pick
        if not 0 <= idx < len(options) or _digest(options[idx], 6) != made_for:
            raise _StaleCardError("This question changed after the card was posted, so nothing "
                         "was recorded. Say 'questions' to get the current card.")
        return str(idx + 1), f"{_letter(idx)}. {options[idx]}"
    words = pick[1]
    if words.isdigit() and 1 <= int(words) <= len(options):
        return words, f"{_letter(int(words) - 1)}. {options[int(words) - 1]}"
    return words, words


def _open_left(result: Any, view: dict, answered_id: str) -> int:
    """Open questions after an answer: from the proposal the instance returned when
    it carries its questions, else from what was read just before the answer."""
    after = questions_of(_question_list(result))
    if after:
        return sum(1 for q in after if not q["answer"])
    return sum(1 for q in view["questions"] if not q["answer"] and q["id"] != answered_id)


def _announce_done(ctx, b: Binding, proposal: str) -> None:
    """ONE line when the last question is answered; never a second for the same cards."""
    store = _store(ctx.host, str(ctx.guild or ""))
    rest = getattr(ctx.host, "rest", None)
    if store is None or rest is None:
        return
    key = f"done:{binding_key(b)}:{_ref(proposal)}"
    if not store.update(key, lambda said: (True, not said), default=False):
        return
    desk = f" on the film desk: <{b.desk_url}>" if b.desk_url else " from the film desk"
    try:
        rest.create_message(b.channel, {
            "content": ("All questions are answered. Next: the plan can be approved; a "
                        f"signed-in admin does that{desk}."),
            "allowed_mentions": NO_MENTIONS})
    except RestError as exc:
        store.delete(key)
        log.warning("film: the all-answered line was refused by Discord (HTTP %s)",
                    exc.status)


def _answer_work(ctx, b: Binding, pref: str, qref: str, pick: tuple):
    """The deferred half of an answer: re-read, check, record, then edit the card."""
    def work() -> dict:
        try:
            view = read_questions(b)
            q, n, total = _find(view, pref, qref)
            choice, said = _chosen(q, pick)
            result = call(b, "film_steer", {
                "proposal_id": view["proposal"], "action": "answer",
                "question": q["id"], "choice": choice}, by=_who({"user": ctx.user}))
        except _StaleCardError as exc:
            return _unchanged(ctx, str(exc))
        except FilmError as exc:
            log.warning("film: answer not recorded (%s)", exc)
            return _unchanged(ctx, _plain(exc, "record your answer"))
        title = _posted_title(ctx.interaction) or card_title(n, total, q)
        by = _display_name(ctx.interaction)
        if _open_left(result, view, q["id"]) == 0:
            _announce_done(ctx, b, view["proposal"])
        return {"embeds": [card_embed(q, title, answered=f"{said} by {by}")],
                "components": answered_rows(b, view["proposal"], q),
                "allowed_mentions": NO_MENTIONS}
    return work


def _change_work(ctx, b: Binding, pref: str, qref: str):
    """The deferred half of "Change": the choices come back; nothing is recorded."""
    def work() -> dict:
        try:
            view = read_questions(b)
            q, n, total = _find(view, pref, qref)
        except _StaleCardError as exc:
            return _unchanged(ctx, str(exc))
        except FilmError as exc:
            log.warning("film: choices not re-opened (%s)", exc)
            return _unchanged(ctx, _plain(exc, "bring the choices back"))
        title = _posted_title(ctx.interaction) or card_title(n, total, q)
        return {"embeds": [card_embed(q, title)],
                "components": card_rows(b, view["proposal"], q),
                "allowed_mentions": NO_MENTIONS}
    return work


def _later(ctx, work, what: str):
    """Acknowledge the press now; ``work`` then edits the card it sits on."""
    from awdiscord.dispatch import Result
    return Result({"type": DEFERRED_UPDATE}, deferred=work,
                  log={"plugin": PLUGIN.name, "user": ctx.user, "film": what})


def _on_button(ctx, payload: str):
    """A press on a question card: an option, "Other", or "Change"."""
    parts = payload.split(":")
    kind = parts[0]
    if (kind, len(parts)) not in (("a", 6), ("o", 4), ("c", 4)):
        return ctx.reply("That button is not one of this film's.", private=True)
    b, refusal = _press_binding(ctx, parts[1])
    if b is None:
        return ctx.reply(refusal, private=True)
    pref, qref = parts[2], parts[3]
    if kind == "o":
        return ctx.modal(f"m:{parts[1]}:{pref}:{qref}",
                         _posted_title(ctx.interaction) or "Your answer",
                         [{"id": "answer", "label": "Your answer, in your own words",
                           "paragraph": True, "max_length": ANSWER_MAX}])
    if kind == "c":
        return _later(ctx, _change_work(ctx, b, pref, qref), "change")
    if not parts[4].isdigit():
        return ctx.reply("That button is not one of this film's.", private=True)
    return _later(ctx, _answer_work(ctx, b, pref, qref, ("option", int(parts[4]), parts[5])),
                  "answer")


def _on_modal(ctx, payload: str, values: dict):
    """The "Other" form was submitted: the typed words are the answer."""
    parts = payload.split(":")
    if parts[0] != "m" or len(parts) != 4:
        return ctx.reply("That form is not one of this film's.", private=True)
    b, refusal = _press_binding(ctx, parts[1])
    if b is None:
        return ctx.reply(refusal, private=True)
    words = _line(values.get("answer"))[:ANSWER_MAX]
    if not words:
        return ctx.reply("Type your answer in the box. Nothing was recorded.", private=True)
    return _later(ctx, _answer_work(ctx, b, parts[2], parts[3], ("words", words)), "other")


def _ready(host) -> None:
    """Remember the host, so ``film_questions`` reaches the bot's REST client."""
    _HOST["host"] = host


# --------------------------------------------------------------------------- the tools
# Each tool takes **_extra: an argument the model invents (a "project") is ignored,
# never an error and never a choice of film -- the binding names the project.

def film_steer_status(caller: dict | None = None, **_extra: Any) -> str:
    b = binding_for(caller)
    if b is None:
        return _refused(caller)
    try:
        return summarize(call(b, "film_steer_status"))
    except FilmError as exc:
        return f"I could not read the film: {exc}."


def film_comment_step(step: int = 0, text: str = "", caller: dict | None = None,
                      **_extra: Any) -> str:
    b = binding_for(caller)
    if b is None:
        return _refused(caller)
    try:
        n = int(step)
    except (TypeError, ValueError):
        n = 0
    if n < 1 or not str(text or "").strip():
        return "Which step, and what should change? Give me the step number and the comment."
    try:
        call(b, "film_comment_step", {"step": n, "text": str(text).strip()[:2000]}, by=_who(caller))
    except FilmError as exc:
        return f"I could not record that comment: {exc}. Nothing changed."
    return (f"Comment recorded on step {n}; that step is marked for re-render. "
            "It goes into the next proposal, which still needs approval.")


def _pending_id(b: Binding) -> str:
    state = call(b, "film_steer_status")
    pend = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    return str(pend.get("id") or state.get("question_proposal") or "")


def film_steer(action: str = "", proposal_id: str = "", note: str = "", question: str = "",
               choice: str = "", step: int = 0, caller: dict | None = None,
               **_extra: Any) -> str:
    b = binding_for(caller)
    if b is None:
        return _refused(caller)
    act = str(action or "").strip().lower()
    if act not in ACTIONS:
        return "Say what to do with the proposal: approve, reject, revise (with a note) or answer."
    if act == "revise" and not str(note or "").strip():
        return "A revision needs a note saying what to change. Nothing changed."
    try:
        n = int(step or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        pid = str(proposal_id or "").strip() or (_pending_id(b) if n == 0 else "")
        if n == 0 and not pid:
            return "There is no pending proposal to act on. Nothing changed."
        result = call(b, "film_steer", {"proposal_id": pid, "action": act, "note": note,
                                        "question": question, "choice": choice, "step": n},
                      by=_who(caller))
    except FilmError as exc:
        if exc.status in (401, 403) and act in ("approve", "reject"):
            return (f"I cannot {act} it from here: the film instance only accepts an "
                    f"{act} from a signed-in admin, never from a chat tool." + _desk(b)
                    + " Nothing changed.")
        return f"I could not {act} it: {exc}. Nothing changed."
    target = f"step {n}" if n else f"proposal {pid}"
    status = result.get("status") if isinstance(result, dict) else None
    return f"Done: {act} recorded on {target}" + (f" (now {status})." if status else ".")


def film_preview(step: int = 0, caller: dict | None = None, **_extra: Any) -> str:
    b = binding_for(caller)
    if b is None:
        return _refused(caller)
    try:
        n = int(step)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        return "Which step should I preview? Give me the step number."
    try:
        out = call(b, "film_preview", {"step": n})
    except FilmError as exc:
        return f"I could not make a preview of step {n}: {exc}."
    if not out.get("mp4"):
        why = out.get("why") or out.get("state") or "not rendered yet"
        return f"Step {n} has no preview yet ({why})."
    return f"Step {n} preview is ready on the film desk." + (f" {b.desk_url}" if b.desk_url else "")


def _tool(name: str, description: str, fn, parameters: dict) -> Tool:
    t = Tool(name, description, fn, parameters, wants_caller=True)
    t.offered = offered
    return t


_int = {"type": "integer", "minimum": 1}
TOOLS = [
    _tool("film_steer_status",
          "Read this channel's construction film: accepted script, the pending proposal and "
          "its changed steps, how many questions and comments are open, each step's render "
          "state. Call it whenever someone asks how the film or a step stands; never guess. "
          "It only COUNTS the open questions: to show them, call film_questions.",
          film_steer_status, {"type": "object", "properties": {}}),
    _tool("film_questions",
          "Show the film's open questions to the customer as CARDS in this channel: one "
          "message per question with a button per option and an Other button, so they "
          "answer with a click. Call it whenever someone asks for the questions, the open "
          "questions, 'what do you need from me', 'what is blocking the film', or says "
          "'more questions'. It posts at most 5 cards a call and tells the channel how many "
          "remain; call it again (no arguments) for the next ones. NEVER list the questions "
          "or their options as text yourself, and never ask anyone to type '5: B': after "
          "this tool, one short line is enough.",
          film_questions,
          {"type": "object", "properties": {
              "start": {"type": "integer", "minimum": 0,
                        "description": "leave empty to continue with the questions not shown "
                                       "yet; a question number to start from that one"}}}),
    _tool("film_comment_step",
          "Record feedback on exactly ONE numbered step, in the customer's words. Only that "
          "step is marked for re-render; the change still goes through a proposal that needs "
          "approval.",
          film_comment_step,
          {"type": "object", "properties": {"step": _int, "text": {"type": "string"}},
           "required": ["step", "text"]}),
    _tool("film_steer",
          "Act on the PENDING proposal (or one step's preview with step=N): approve, reject, "
          "revise (needs note) or answer an open question (question id + choice). The film "
          "instance decides who may approve; if it refuses, say so and give the desk link. "
          "Use answer only for an answer the person gave in words in this chat; to ASK the "
          "questions, call film_questions (cards with buttons) instead.",
          film_steer,
          {"type": "object", "properties": {
              "action": {"type": "string", "enum": list(ACTIONS)},
              "proposal_id": {"type": "string",
                              "description": "leave empty: the pending one is used"},
              "note": {"type": "string"}, "question": {"type": "string"},
              "choice": {"type": "string"},
              "step": {"type": "integer", "description": "0 = the whole proposal"}},
           "required": ["action"]}),
    _tool("film_preview",
          "Check one step's preview cut and say where to watch it.",
          film_preview, {"type": "object", "properties": {"step": _int}, "required": ["step"]}),
]

PLUGIN = Plugin(
    name="film",
    version="0.2.0",
    description="A customer steers their own construction film from its mapped channel",
    default_enabled=True,
    # No persona text: it would reach every channel. The tools exist only where a
    # channel is mapped, and their descriptions carry the guidance.
    tools=TOOLS,
    # Question cards: a press is checked against the map again (channel + member),
    # so the buttons do nothing for anyone the binding does not list.
    on_button=_on_button,
    on_modal=_on_modal,
    on_ready=[_ready],
)
