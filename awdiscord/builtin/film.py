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
         "desk_url": "https://..."}}}

The film's state lives ONLY in that instance; this module is glue over its ops
(``POST <url>/api/op/<name>``), with the platform's names: ``film_steer_status``,
``film_comment_step``, ``film_steer``, ``film_preview``. An entry whose host is in
``refuse_hosts`` is dropped, so a customer can never be pointed at an instance
that is not theirs. The call carries the tenant's bearer from ``token_file`` and
``X-Aither-Origin: agent-tool``: the instance judges every verb itself, and an
agent tool never approves -- approve/reject come back refused with the film
desk's link, exactly as the film procedure gates them.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awdiscord.plugins import Plugin, Tool

log = logging.getLogger("awdiscord.film")

MAP_ENV = "AWDISCORD_FILM_MAP"
TIMEOUT_S = 45.0
ORIGIN_HEADER = "X-Aither-Origin"
ACTIONS = ("approve", "reject", "revise", "answer")
REQUIRED = ("tenant", "instance", "url", "project", "members")


class FilmError(RuntimeError):
    """The instance refused or did not answer. Never turned into an empty read."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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
            desk_url=str(entry.get("desk_url") or ""))
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
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {_token(b)}", ORIGIN_HEADER: "agent-tool"})
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
        raise FilmError(f"{b.instance} refused {op} (HTTP {exc.code})"
                        + (f": {detail[:300]}" if detail else ""), exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FilmError(f"{b.instance} did not answer {op}: {exc}") from exc
    if not isinstance(data, dict):
        raise FilmError(f"{b.instance} answered {op} with {type(data).__name__}")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if data.get("ok") is False:
        why = str(data.get("error") or data.get("detail") or "no reason given")
        raise FilmError(f"{b.instance} refused {op}: {why[:300]}", int(meta.get("status") or 0))
    result = meta.get("result")
    return result if isinstance(result, dict) else data


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
        lines.append(f"Open questions: {n}")
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
          "its changed steps, open questions and comments, each step's render state. Call it "
          "whenever someone asks how the film or a step stands; never guess.",
          film_steer_status, {"type": "object", "properties": {}}),
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
          "instance decides who may approve; if it refuses, say so and give the desk link.",
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
    version="0.1.0",
    description="A customer steers their own construction film from its mapped channel",
    default_enabled=True,
    # No persona text: it would reach every channel. The tools exist only where a
    # channel is mapped, and their descriptions carry the guidance.
    tools=TOOLS,
)
