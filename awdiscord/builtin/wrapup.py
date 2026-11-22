"""Wrap-up: turn a channel conversation into positions, resolutions and due-outs.

  /aither wrapup now [since] [post_to] [preview]   one top-level post (default #general)
  /aither wrapup status                            what is done or still open
  "Wrap up from here" (message menu)               a private preview from that message
  "@Aither wrap this up [since <link>]"            the same, from a mention

The engine is ``awdiscord.wrapup`` (fetch, ground, render). This module decides who
may post, keeps the interaction alive, tracks the due-outs and wires the buttons.

Who may post publicly: server staff (Manage Messages, Manage Server or
Administrator) or one of the bot's operators. Anyone who can read the channel may
see a private preview.

Discord's 3-second rule: every path acknowledges first (``ctx.defer``) and reads the
history, waits for the model and posts inside the deferred work, editing a progress
line in while the model queue is busy. The post itself goes to the target channel
through the bot's REST client, so it lands even after the 15-minute interaction
token has expired.

Due-outs are tracked in this plugin's per-server store (the buttons and
``status`` read it) and, when the bot is configured for the platform
(``AWDISCORD_GENESIS_URL`` + ``AITHER_INTERNAL_SECRET``), mirrored as cards on the
platform task board (``/atlas/projects``) with a link back to the post. A board
that refuses (for example a server not linked to a workspace) is reported to the
person who ran it, never hidden; the in-server tracking still works.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from awdiscord import intake, redact, wrapup
from awdiscord.plugins import Command, Option, Plugin, PluginError, ScopedStore

log = logging.getLogger("awdiscord.wrapup")

NAME = "wrapup"
MENU_NAME = "Wrap up from here"
RECORD = "wu:"
INDEX = "wu-index"
INDEX_KEEP = 20
DEFERRED_UPDATE = 6
VIEW_CHANNEL = 1 << 10
READ_HISTORY = 1 << 16
ADMINISTRATOR = 1 << 3
PATIENCE_INTERACTION_S = 780.0        # an interaction token lives 15 minutes
PATIENCE_POST_S = 1800.0              # a channel post does not need the token
PROJECT_NAME = "Conversation due-outs"
_RUNNING: dict[str, float] = {}        # guild -> started, one wrap-up per server
_LOCK = threading.Lock()
_HOST: dict = {}
_ASK_RE = re.compile(r"\bwrap\s*(?:this|it|that|the\s+(?:conversation|discussion|chat))?"
                     r"\s*-?\s*up\b|\bwrap-?up\b", re.I)

_STAFF_ONLY = ("Only server staff (Manage Messages or Manage Server) can post a wrap-up. "
               "`/aither wrapup now preview:True` shows you one privately.")


def _rest(host=None):
    host = host or _HOST.get("host")
    return getattr(host, "rest", None) if host is not None else None


def _store(host, guild: str) -> ScopedStore:
    return ScopedStore(host.store, guild, NAME)


def _agent(host, guild: str, user: str):
    """This server's brain (``Ctx.agent``'s rule): None when nothing answers here."""
    resolver = getattr(host, "resolver", None)
    if resolver is None:
        return getattr(host, "agent", None)
    try:
        return getattr(resolver.resolve(guild, user), "agent", None)
    except Exception:  # noqa: BLE001 - a resolver bug is "no model"
        return None


def is_staff(permissions: int, user: str, host) -> bool:
    return user in getattr(host, "operators", set()) or redact.is_staff(permissions)


def can_read(permissions: int) -> bool:
    p = int(permissions or 0)
    return bool(p & ADMINISTRATOR) or p & (VIEW_CHANNEL | READ_HISTORY) == (
        VIEW_CHANNEL | READ_HISTORY)


def _claim_slot(guild: str) -> bool:
    with _LOCK:
        started = _RUNNING.get(guild)
        if started and time.time() - started < PATIENCE_POST_S + 120:
            return False
        _RUNNING[guild] = time.time()
        return True


def _free_slot(guild: str) -> None:
    with _LOCK:
        _RUNNING.pop(guild, None)


def general_channel(rest, guild: str) -> str:
    """The server's #general text channel id, or ""."""
    try:
        chans = rest.request("GET", f"/guilds/{guild}/channels") or []
    except Exception:  # noqa: BLE001 - no list = the caller posts where it was asked
        return ""
    for c in chans:
        if int(c.get("type") or 0) == 0 and str(c.get("name") or "").lower() == "general":
            return str(c["id"])
    return ""


def _channel_name(rest, channel: str) -> str:
    try:
        return str((rest.request("GET", f"/channels/{channel}") or {}).get("name") or "")
    except Exception:  # noqa: BLE001 - cosmetic
        return ""


# ── building ──────────────────────────────────────────────────────────────────

def make(rest, agent, *, guild: str, channel: str, user: str, since_text: str = "",
         anchor: str = "", bot_id: str = "", patience_s: float = PATIENCE_INTERACTION_S,
         on_wait=None, on_read=None) -> wrapup.Wrapup:
    """Read the channel and build a grounded wrap-up. Raises WrapupError."""
    since = wrapup.parse_since(since_text)
    if anchor:
        since = wrapup.Since(after=int(anchor), anchor=anchor, label="from that message")
    if since.channel and since.channel != channel:
        raise wrapup.WrapupError("Link a message in THIS channel (or run it in the channel "
                                 "the link points to).")
    if since.guild and guild and since.guild != guild:
        raise wrapup.WrapupError("That link is from another server.")
    try:
        messages, truncated = wrapup.fetch(rest, channel, since)
    except Exception as exc:  # noqa: BLE001 - say it, never a half wrap-up
        raise wrapup.WrapupError(f"I could not read this channel's history "
                                 f"({type(exc).__name__}).") from exc
    lines = wrapup.lines_of(messages, bot_id=bot_id)
    if on_read is not None:
        on_read(len(lines))
    ask = None
    if agent is not None:
        def ask(text: str) -> str:
            return wrapup.ask_patiently(agent, text, user=user, patience_s=patience_s,
                                        on_wait=on_wait)
    return wrapup.build(lines, ask, channel=channel, guild=guild,
                        channel_name=_channel_name(rest, channel), label=since.label,
                        truncated=truncated)


# ── tracking ──────────────────────────────────────────────────────────────────

def _statuses(record: dict) -> dict:
    return {int(i["n"]): (i.get("status", "open"), i.get("by") or "")
            for i in record.get("items") or []}


def buttons(token: str, record: dict) -> list:
    """Claim / Done per due-out (up to 10), two buttons each, five per row."""
    items = (record.get("items") or [])[:10]
    rows, row = [], []
    for i in items:
        n, state = int(i["n"]), i.get("status", "open")
        for kind, label, style in (("c", f"Claim D{n}", 2), ("d", f"Done D{n}", 3)):
            row.append({"type": 2, "style": style, "label": label,
                        "custom_id": PLUGIN.button_id(f"{kind}:{token}:{n}"),
                        "disabled": state == "done" or (kind == "c" and state != "open")})
            if len(row) == 5:
                rows.append({"type": 1, "components": row})
                row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows[:5]


def new_record(w: wrapup.Wrapup, *, by: str, post_channel: str) -> tuple[str, dict]:
    token = secrets.token_hex(4)
    items = [{"n": k, "owner": i.who, "action": i.text, "due": i.due, "cite": i.cite,
              "status": "open", "by": ""} for k, i in enumerate(w.due_outs, 1)]
    return token, {"token": token, "by": by, "at": int(time.time()),
                   "post_channel": post_channel, "post_id": "", "wrapup": w.to_dict(),
                   "items": items, "board": {}}


def save(host, guild: str, record: dict) -> None:
    store = _store(host, guild)
    store.set(RECORD + record["token"], record)

    def add(index):
        index = [t for t in (index or []) if t != record["token"]] + [record["token"]]
        return index[-INDEX_KEEP:], None
    store.update(INDEX, add, default=[])


def post(rest, w: wrapup.Wrapup, record: dict) -> dict:
    """Post the wrap-up top-level in ``record['post_channel']``, with buttons and votes."""
    body = wrapup.payload(w)
    body["components"] = buttons(record["token"], record)
    msg = rest.create_message(record["post_channel"], body)
    record["post_id"] = str(msg.get("id") or "")
    for k in range(min(len(w.resolutions), len(wrapup.KEYCAPS))):
        try:
            rest.request("PUT", f"/channels/{record['post_channel']}/messages/"
                         f"{record['post_id']}/reactions/"
                         f"{urllib.parse.quote(wrapup.KEYCAPS[k])}/@me")
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001 - votes are extra; the post stands
            log.info("wrapup: vote reaction refused (%s)", type(exc).__name__)
            break
    return msg


class BoardError(Exception):
    pass


class PlatformBoard:
    """Due-outs as cards on the platform task board (Genesis ``/atlas/projects``)."""

    def __init__(self, base: str, key: str, ca: str | None, timeout: float = 15.0):
        base = base.rstrip("/")
        self.base = base[:-3] if base.endswith("/v1") else base
        self.key = key
        self.timeout = timeout
        self._ctx = ssl.create_default_context(cafile=ca) if ca else None

    @classmethod
    def from_env(cls) -> "PlatformBoard | None":
        base = (os.getenv("AWDISCORD_GENESIS_URL") or "").strip()
        key = (os.getenv("AITHER_INTERNAL_SECRET") or "").strip()
        if not base.startswith("https://") or not key:
            return None           # never an unauthenticated platform call
        ca = os.getenv("AWDISCORD_CA_BUNDLE") or (
            "/etc/aither-ca.pem" if os.path.isfile("/etc/aither-ca.pem") else None)
        return cls(base, key, ca)

    def _call(self, method: str, path: str, body: dict | None = None,
              query: dict | None = None) -> Any:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Caller-Type": "platform",
                     "X-Internal-Key": self.key, "X-Service-Name": "awdiscord"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail") or ""
            except Exception:  # noqa: BLE001 - the status alone is still the answer
                detail = ""
            raise BoardError(f"HTTP {exc.code} {str(detail)[:160]}".strip()) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BoardError(f"unreachable ({type(exc).__name__})") from exc

    def project(self, guild: str, channel: str, by: str) -> str:
        ref = f"discord:{guild}:"
        out = self._call("GET", "/atlas/projects", query={
            "source": "discord", "external_ref_prefix": ref, "name": PROJECT_NAME}) or {}
        for p in out.get("projects") or []:
            if (str(p.get("external_ref") or "").startswith(ref)
                    and str(p.get("name") or "").lower() == PROJECT_NAME.lower()):
                return str(p["project_id"])
        made = self._call("POST", "/atlas/projects", body={
            "name": PROJECT_NAME, "source": "discord",
            "description": "Due-outs from conversation wrap-ups in this server.",
            "external_ref": f"discord:{guild}:{channel}", "created_by": f"discord:{by}"})
        return str((made or {}).get("project_id") or "")

    def add(self, project: str, *, guild: str, channel: str, post_id: str, item: dict,
            owner_name: str, by: str) -> str:
        link = wrapup.jump(guild, channel, post_id)
        body = {"title": f"{owner_name}: {item['action']}"[:200],
                "description": (f"From the wrap-up {link}\nDue: {item['due']}\n"
                                f"Source messages: {', '.join(item['cite'])}")[:4000],
                "assignee": f"discord:{item['owner']}", "assignee_kind": "human",
                "source": "discord", "external_ref": f"discord:{guild}:{channel}:{post_id}",
                "created_by": f"discord:{by}"}
        out = self._call("POST", f"/atlas/projects/{urllib.parse.quote(project)}/tasks",
                         body=body)
        return str((out or {}).get("task_id") or "")

    def set_status(self, project: str, task: str, status: str, *, guild: str,
                   channel: str, by: str) -> None:
        self._call("PUT", f"/atlas/projects/{urllib.parse.quote(project)}/tasks/"
                   f"{urllib.parse.quote(task)}",
                   body={"status": status, "updated_by": f"discord:{by}", "source": "discord",
                         "external_ref": f"discord:{guild}:{channel}"})


def track(board: "PlatformBoard | None", record: dict, w: wrapup.Wrapup, guild: str) -> str:
    """Mirror the due-outs onto the platform board; a one-line result for the invoker."""
    if not record["items"]:
        return "no due-outs to track"
    if board is None:
        return "tracked in this server (the platform board is not configured on this bot)"
    channel, by = record["post_channel"], record["by"]
    try:
        project = board.project(guild, channel, by)
        for item in record["items"]:
            item["task_id"] = board.add(project, guild=guild, channel=channel,
                                        post_id=record["post_id"], item=item,
                                        owner_name=w.people.get(item["owner"], "someone"),
                                        by=by)
        record["board"] = {"project_id": project}
        return f"{len(record['items'])} due-outs added to the platform task board"
    except BoardError as exc:
        record["board"] = {"error": str(exc)}
        return f"tracked in this server; the platform task board refused ({exc})"


# ── the flows ─────────────────────────────────────────────────────────────────

def run(host, *, guild: str, channel: str, user: str, since_text: str = "",
        anchor: str = "", post_to: str = "", preview: bool, bot_id: str = "",
        progress=None) -> dict:
    """One wrap-up, start to finish. Returns the payload for the invoker's reply."""
    from awdiscord.dispatch import NO_MENTIONS
    rest = _rest(host)
    say = progress or (lambda _t: None)
    # One post per server at a time; a private preview only blocks its own asker.
    slot = f"{guild}:{user}" if preview else guild
    if not _claim_slot(slot):
        return {"content": "A wrap-up is already being written in this server; it will "
                           "appear when it is ready.", "allowed_mentions": NO_MENTIONS}
    try:
        agent = _agent(host, guild, user)
        w = make(rest, agent, guild=guild, channel=channel, user=user,
                 since_text=since_text, anchor=anchor, bot_id=bot_id,
                 patience_s=PATIENCE_INTERACTION_S if preview else PATIENCE_POST_S,
                 on_read=lambda n: say(f"Read {n} messages. Writing the wrap-up..."),
                 on_wait=lambda: say("The model queue is busy; the wrap-up is queued and "
                                     "will appear here as soon as it is written."))
        if preview:
            body = wrapup.payload(w)
            body["content"] = ("**Preview - only you can see this.**\n"
                               + body["content"])[:2000]
            return body
        target = post_to or general_channel(rest, guild) or channel
        token, record = new_record(w, by=user, post_channel=target)
        msg = post(rest, w, record)
        tracked = track(PlatformBoard.from_env(), record, w, guild)
        save(host, guild, record)
        log.info("wrapup posted guild=%s channel=%s post=%s items=%d dropped=%s model=%s",
                 guild, target, msg.get("id"), len(record["items"]), w.dropped, w.model)
        return {"content": f"Wrap-up posted: {wrapup.jump(guild, target, record['post_id'])}"
                           f"\n-# {tracked}.", "allowed_mentions": NO_MENTIONS}
    except wrapup.WrapupError as exc:
        return {"content": str(exc), "allowed_mentions": NO_MENTIONS}
    finally:
        _free_slot(slot)


def _progress_edit(ctx):
    """A function that edits the deferred reply in place (best effort)."""
    rest = _rest(ctx.host)
    app = str(ctx.interaction.get("application_id") or "")
    token = str(ctx.interaction.get("token") or "")

    def say(text: str) -> None:
        if rest is None or not app or not token:
            return
        try:
            rest.edit_original(app, token, {"content": text,
                                            "allowed_mentions": {"parse": []}})
        except Exception as exc:  # noqa: BLE001 - progress is cosmetic
            log.info("wrapup: progress edit failed (%s)", type(exc).__name__)
    return say


def _bot_id(ctx) -> str:
    return str(ctx.interaction.get("application_id") or "")


def _now(ctx):
    """/aither wrapup now [since] [post_to] [preview]."""
    if not ctx.guild:
        return ctx.reply("Wrap-ups work in a server channel.", private=True)
    if not can_read(ctx.permissions):
        return ctx.reply("You can't read this channel's history, so I won't summarise it "
                         "for you.", private=True)
    preview = bool(ctx.opt("preview", False))
    if not preview and not is_staff(ctx.permissions, ctx.user, ctx.host):
        return ctx.reply(_STAFF_ONLY, private=True)
    post_to = str(ctx.opt("post_to", "") or "")
    host, guild, channel, user = ctx.host, str(ctx.guild), str(ctx.channel), ctx.user
    since, say, bot = str(ctx.opt("since", "") or ""), _progress_edit(ctx), _bot_id(ctx)
    return ctx.defer(lambda: run(host, guild=guild, channel=channel, user=user,
                                 since_text=since, post_to=post_to, preview=preview,
                                 bot_id=bot, progress=say), private=True)


def _from_here(ctx):
    """The message menu: a private preview starting at the clicked message."""
    if not ctx.guild:
        return ctx.reply("Wrap-ups work in a server channel.", private=True)
    if not can_read(ctx.permissions):
        return ctx.reply("You can't read this channel's history.", private=True)
    target = str((ctx.interaction.get("data") or {}).get("target_id") or "")
    host, guild, channel, user = ctx.host, str(ctx.guild), str(ctx.channel), ctx.user
    say, bot = _progress_edit(ctx), _bot_id(ctx)

    def work():
        out = run(host, guild=guild, channel=channel, user=user, anchor=target,
                  preview=True, bot_id=bot, progress=say)
        if is_staff(ctx.permissions, user, host) and target:
            out["content"] = (out.get("content", "") + "\n-# To post it: `/aither wrapup now "
                              f"since:{wrapup.jump(guild, channel, target)}`")[:2000]
        return out
    return ctx.defer(work, private=True)


def status_text(host, guild: str, limit: int = 5) -> str:
    store = _store(host, guild)
    tokens = list(reversed(store.get(INDEX, []) or []))[:limit]
    if not tokens:
        return "No wrap-ups have been posted in this server yet."
    out = ["**Wrap-up due-outs**"]
    for t in tokens:
        rec = store.get(RECORD + t)
        if not rec:
            continue
        items = rec.get("items") or []
        done = sum(1 for i in items if i.get("status") == "done")
        link = wrapup.jump(guild, rec["post_channel"], rec.get("post_id") or "")
        out.append(f"\n<t:{int(rec.get('at') or 0)}:d> {link} - {done}/{len(items)} done")
        for i in items:
            mark = wrapup.STATUS_MARK.get(i.get("status", "open"), "open")
            out.append(f"`D{i['n']}` <@{i['owner']}> → {i['action'][:120]} · "
                       f"*{i.get('due') or wrapup.DATE_MISSING}* · {mark}")
    return "\n".join(out)[:1990]


def _status(ctx):
    """/aither wrapup status: posted in the channel, for everyone."""
    if not ctx.guild:
        return ctx.reply("Wrap-ups work in a server channel.", private=True)
    return ctx.reply(status_text(ctx.host, str(ctx.guild)))


def _on_button(ctx, payload: str):
    """Claim / Done on a posted wrap-up. Close = the named owner or staff only."""
    kind, _, rest_ = payload.partition(":")
    token, _, num = rest_.partition(":")
    if kind not in ("c", "d") or not token or not num.isdigit() or not ctx.guild:
        return ctx.reply("Unknown button.", private=True)
    guild, presser = str(ctx.guild), ctx.user
    staff = is_staff(ctx.permissions, presser, ctx.host)
    store = _store(ctx.host, guild)
    verdict: dict = {}

    def change(rec):
        if not rec:
            verdict["error"] = "That wrap-up is no longer tracked."
            return rec, None
        item = next((i for i in rec["items"] if int(i["n"]) == int(num)), None)
        if item is None:
            verdict["error"] = "No such due-out."
            return rec, None
        owner = str(item.get("owner") or "")
        if kind == "d" and presser != owner and not staff:
            verdict["error"] = "Only the named owner or server staff can close this item."
            return rec, None
        if kind == "c" and presser != owner and not staff:
            verdict["error"] = ("Only the named owner (or staff) can claim this item; reply "
                                "to the post to offer help.")
            return rec, None
        item["status"] = "done" if kind == "d" else "claimed"
        item["by"] = presser
        verdict["item"] = dict(item)
        return rec, None
    store.update(RECORD + token, change)
    if verdict.get("error"):
        return ctx.reply(verdict["error"], private=True)
    rec = store.get(RECORD + token)
    w = wrapup.Wrapup.from_dict(rec["wrapup"])
    body = wrapup.payload(w, _statuses(rec))
    _sync_board(rec, verdict["item"], kind, guild, presser)
    return ctx.update(body["content"], embeds=body["embeds"],
                      components=buttons(token, rec))


def _sync_board(rec: dict, item: dict, kind: str, guild: str, by: str) -> None:
    """Mirror a claim/close onto the platform card, off the 3-second path."""
    project = (rec.get("board") or {}).get("project_id")
    task = item.get("task_id") or next(
        (i.get("task_id") for i in rec["items"] if int(i["n"]) == int(item["n"])), "")
    board = PlatformBoard.from_env()
    if not (project and task and board):
        return

    def go():
        try:
            board.set_status(project, task, "completed" if kind == "d" else "in_progress",
                             guild=guild, channel=rec["post_channel"], by=by)
        except BoardError as exc:
            log.warning("wrapup: board status sync failed: %s", exc)
    threading.Thread(target=go, daemon=True, name="wrapup-board").start()


# ── "@Aither wrap this up" ────────────────────────────────────────────────────

def wants_wrapup(text: str) -> bool:
    return bool(_ASK_RE.search(re.sub(r"<@!?\d+>", " ", str(text or ""))))


def _intake(event) -> bool:
    """Take a mention that asks for a wrap-up; everything else goes to the brain."""
    host = _HOST.get("host")
    if host is None or event.is_dm or not wants_wrapup(event.content):
        return False
    guild, channel, user = str(event.guild_id), str(event.channel_id), str(event.user)
    if not host.enabled(guild, NAME):
        return False
    rest = event.rest or _rest(host)
    operator = user in getattr(host, "operators", set())
    perms = 0 if operator else redact._perms_or_zero(rest, guild, channel, user)
    if not (operator or redact.is_staff(perms)):
        rest.create_message(channel, {"content": _STAFF_ONLY, "allowed_mentions":
                                      {"parse": []}})
        return True
    link = wrapup._LINK_RE.search(event.content or "")
    since = link.group(0) if link else ""
    if not since:
        got = re.search(r"\b(?:last|past|since)\s+(\d+\s*[dhm]\w*)", event.content or "", re.I)
        since = got.group(1) if got else ""
    note = rest.create_message(channel, {"content": "Writing a wrap-up of this conversation; "
                                         "it will be posted in #general when it is ready.",
                                         "allowed_mentions": {"parse": []}})
    note_id = str((note or {}).get("id") or "")

    def say(text: str) -> None:
        if note_id:
            try:
                rest.request("PATCH", f"/channels/{channel}/messages/{note_id}",
                             {"content": text[:2000], "allowed_mentions": {"parse": []}})
            except Exception as exc:  # noqa: BLE001 - progress is cosmetic
                log.info("wrapup: progress edit failed (%s)", type(exc).__name__)

    def work():
        out = run(host, guild=guild, channel=channel, user=user, since_text=since,
                  preview=False, progress=say)
        say(out.get("content") or "The wrap-up could not be written.")
    threading.Thread(target=work, daemon=True, name="wrapup").start()
    return True


def _ready(host) -> None:
    _HOST["host"] = host
    intake.register(_intake)


PLUGIN = Plugin(
    name=NAME,
    version="0.1.0",
    description="Wrap up a conversation: positions, open questions, resolutions, due-outs",
    default_enabled=True,
    on_button=_on_button,
    on_ready=[_ready],
    commands=[
        Command("aither", "Aither in this server", subcommands=[
            Command("wrapup", "Wrap up a conversation", subcommands=[
                Command("now", "Summarise, resolve and list due-outs, then post it",
                        _now, options=[
                            Option("since", "A message link, or how far back (3h, 90m, "
                                   "1d). Default: 12h", max_length=200),
                            Option("post_to", "Where to post it (default #general)",
                                   type="channel"),
                            Option("preview", "Only show it to me (nothing is posted)",
                                   type="boolean")]),
                Command("status", "What is done and what is still open", _status)])]),
        Command(MENU_NAME, "", _from_here, message=True),
    ],
)

__all__ = ["PLUGIN", "PluginError", "make", "run", "status_text", "wants_wrapup"]
