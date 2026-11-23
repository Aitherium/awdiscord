"""``/notepad``: open a notepad, type, save; share it; attach Aither's call transcripts.

    /notepad open [name]                    a form with the note's text; Submit saves it
    /notepad list                           your notes in this server
    /notepad share name [user] [channel]    post it in a channel, or DM it to a member
    /notepad attach name [call] [inline]    add a voice-call transcript to the note
    /notepad calls                          the call transcripts you can attach
    /notepad delete name                    remove a note

Notes belong to one member in one server (:mod:`awdiscord.notepad` keeps them under
the bot's state directory). Nobody but their author can open or change them; sharing
posts a copy. A share goes only where the sharer may post (Discord's own computed
permissions for them in that channel) or to a member of this server. A transcript is
offered only from THIS server, and only for a call the member spoke in or started
(server managers may attach any of this server's calls).
"""
from __future__ import annotations

from awdiscord import notepad as pad
from awdiscord.plugins import Command, Option, Plugin

NAME = "notepad"
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
ATTACH_FILES = 1 << 15
SEND_IN_THREADS = 1 << 38
ADMINISTRATOR = 1 << 3
THREAD_TYPES = (10, 11, 12)
#: A note longer than this is shared as a .md file instead of an embed.
EMBED_LIMIT = 3500
MAX_FILE_BYTES = 8 * 1024 * 1024
COLOR = 0x5865F2


def _book(ctx) -> pad.Notepad:
    if not ctx.guild:
        raise pad.NotepadError("notepads live in a server; run this there")
    return pad.Notepad(pad.state_root(), ctx.guild, ctx.user)


def _say(ctx, text: str):
    return ctx.reply(text, private=True)


def _guarded(fn):
    def run(ctx, *args):
        try:
            return fn(ctx, *args)
        except pad.NotepadError as exc:
            return _say(ctx, f"Notepad: {exc}.")
    run.__name__ = fn.__name__
    return run


# -- open / save -----------------------------------------------------------------------

def _form(ctx, name: str, text: str):
    return ctx.modal(f"save:{name}", f"Notepad: {name}"[:45], [
        {"id": "text", "label": "Your note (saved when you press Submit)",
         "paragraph": True, "required": False, "max_length": pad.MAX_TEXT,
         "placeholder": "Type anything. Share it later with /notepad share.",
         "value": text}])


@_guarded
def _open(ctx):
    name = pad.clean_name(ctx.opt("name"))
    note = _book(ctx).get(name) or {}
    return _form(ctx, name, str(note.get("text") or ""))


@_guarded
def _on_modal(ctx, payload: str, values: dict):
    kind, _, name = payload.partition(":")
    if kind != "save":
        return _say(ctx, "That form is not a notepad form.")
    name = pad.clean_name(name)
    text = str(values.get("text") or "")
    note = _book(ctx).save(name, text)
    calls = len(note.get("calls") or [])
    extra = f", {calls} call transcript(s) attached" if calls else ""
    return _say(ctx, f"Saved **{name}** ({len(text)} characters{extra}). "
                     f"Share it with `/notepad share name:{name}`.")


# -- list / delete ---------------------------------------------------------------------

@_guarded
def _list(ctx):
    notes = _book(ctx).all()
    if not notes:
        return _say(ctx, "You have no notes in this server yet. `/notepad open` starts one.")
    rows = sorted(notes.items(), key=lambda kv: -float(kv[1].get("updated") or 0))
    lines = [f"- **{n}**: {len(str(v.get('text') or ''))} chars"
             + (f", {len(v.get('calls') or [])} call(s)" if v.get("calls") else "")
             + f", saved <t:{int(float(v.get('updated') or 0))}:R>"
             for n, v in rows]
    return _say(ctx, f"Your notes here ({len(rows)}):\n" + "\n".join(lines))


@_guarded
def _delete(ctx):
    name = pad.clean_name(ctx.opt("name"))
    if _book(ctx).delete(name):
        return _say(ctx, f"Deleted **{name}**.")
    return _say(ctx, f"You have no note called **{name}** here.")


# -- calls / attach ----------------------------------------------------------------------

def _visible(ctx) -> list:
    return pad.visible_calls(pad.state_root(), ctx.guild, ctx.user, ctx.is_admin)


@_guarded
def _calls(ctx):
    found = _visible(ctx)
    if not found:
        return _say(ctx, "There are no call transcripts you can read in this server yet "
                         "(you see the calls you spoke in; managers see every call).")
    lines = [f"`{i}` {pad.describe(c)}" for i, c in enumerate(found[:20], 1)]
    more = f"\n-# +{len(found) - 20} older" if len(found) > 20 else ""
    return _say(ctx, "Call transcripts (use the number with `/notepad attach`):\n"
                + "\n".join(lines) + more)


@_guarded
def _attach(ctx):
    name = pad.clean_name(ctx.opt("name"))
    which = str(ctx.opt("call") or "latest")
    book = _book(ctx)
    note = book.get(name) or {"text": "", "calls": []}
    if which.strip().lower() == "none":
        book.set_calls(name, [])
        return _say(ctx, f"Removed every call transcript from **{name}**.")
    chosen = pad.pick(_visible(ctx), which)
    refs = list(note.get("calls") or []) + [pad.ref(c) for c in chosen]
    if len(dict.fromkeys(refs)) > pad.MAX_CALLS:
        raise pad.NotepadError(f"a note carries at most {pad.MAX_CALLS} calls; "
                               f"`/notepad attach call:none` clears them")
    text = None
    said = ""
    if ctx.opt("inline") in (True, "true", "True"):
        pasted = "\n\n".join(pad.transcript_markdown(c) for c in chosen)
        joined = (str(note.get("text") or "").rstrip() + "\n\n" + pasted).strip()
        if len(joined) <= pad.MAX_TEXT:
            text = joined
            said = " The text is pasted into the note too."
        else:
            said = (f" Too long to paste into the note ({len(joined)} of "
                    f"{pad.MAX_TEXT} characters), so it goes out as a file when shared.")
    book.set_calls(name, refs, text=text)
    listed = "; ".join(pad.describe(c) for c in chosen)
    return _say(ctx, f"Attached to **{name}**: {listed}.{said} "
                     f"`/notepad share name:{name}` sends the transcript(s) as .md files.")


# -- share -----------------------------------------------------------------------------

def _resolved(ctx) -> dict:
    return ((ctx.interaction.get("data") or {}).get("resolved")) or {}


def _member_name(ctx) -> str:
    member = ctx.interaction.get("member") or {}
    user = member.get("user") or ctx.interaction.get("user") or {}
    return str(member.get("nick") or user.get("global_name") or user.get("username")
               or "a member")


def _may_post(perms: int, thread: bool, files: bool) -> str:
    """'' when allowed, else what is missing (the sharer's OWN rights there)."""
    if perms & ADMINISTRATOR:
        return ""
    if not perms & VIEW_CHANNEL:
        return "you cannot see that channel"
    if not perms & (SEND_IN_THREADS if thread else SEND_MESSAGES):
        return "you cannot post in that channel"
    if files and not perms & ATTACH_FILES:
        return "you cannot attach files in that channel"
    return ""


def _target(ctx, has_files: bool) -> tuple[str, str, str]:
    """(kind, id, label): ``("channel", id, "<#id>")`` or ``("user", id, "<@id>")``."""
    user, channel = ctx.opt("user"), ctx.opt("channel")
    if user and channel:
        raise pad.NotepadError("pick a member OR a channel, not both")
    resolved = _resolved(ctx)
    if user:
        uid = str(user)
        if uid not in (resolved.get("members") or {}):
            raise pad.NotepadError("that person is not a member of this server")
        if ((resolved.get("users") or {}).get(uid) or {}).get("bot"):
            raise pad.NotepadError("notes are shared with people, not bots")
        return "user", uid, f"<@{uid}>"
    if channel:
        cid = str(channel)
        info = (resolved.get("channels") or {}).get(cid)
        if not isinstance(info, dict):
            raise pad.NotepadError("Discord did not say what you may do in that channel")
        perms, thread = int(info.get("permissions") or 0), info.get("type") in THREAD_TYPES
    else:
        cid = str(ctx.channel or "")
        perms = ctx.permissions
        channel_info = ctx.interaction.get("channel") or {}
        thread = channel_info.get("type") in THREAD_TYPES
    why = _may_post(perms, thread, has_files)
    if why:
        raise pad.NotepadError(f"not shared: {why}")
    if not cid:
        raise pad.NotepadError("no channel to share into")
    return "channel", cid, f"<#{cid}>"


def _message(ctx, name: str, note: dict, attached: list, as_file: bool,
             to_user: bool) -> tuple[dict, list]:
    text = str(note.get("text") or "")
    files: list = []
    who = _member_name(ctx)
    intro = (f"<@{ctx.user}> shared a note with you from a server you are both in."
             if to_user else f"<@{ctx.user}> shared a note.")
    payload: dict = {"content": intro, "allowed_mentions": {"parse": []}}
    if text and (as_file or len(text) > EMBED_LIMIT):
        files.append((f"{pad.callnotes._safe(name)}.md",
                      f"# {name}\n\n{text}\n".encode("utf-8"), "text/markdown"))
        payload["embeds"] = [{"title": name[:256], "color": COLOR,
                              "description": f"The note is attached ({len(text)} chars).",
                              "footer": {"text": f"Notepad - shared by {who}"[:2048]}}]
    else:
        payload["embeds"] = [{"title": name[:256], "color": COLOR,
                              "description": text or "*(empty note)*",
                              "footer": {"text": f"Notepad - shared by {who}"[:2048]}}]
    for call in attached:
        data = pad.transcript_markdown(call).encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES - 64] + b"\n\n(truncated: transcript too large)\n"
        files.append((pad.transcript_filename(call), data, "text/markdown"))
    if files:
        payload["attachments"] = [{"id": i, "filename": f[0]} for i, f in enumerate(files)]
    return payload, files


@_guarded
def _share(ctx):
    name = pad.clean_name(ctx.opt("name"))
    note = _book(ctx).get(name)
    if note is None:
        raise pad.NotepadError(f"you have no note called **{name}** here "
                               f"(`/notepad list` shows yours)")
    root = pad.state_root()
    attached, gone = pad.resolve_attached(root, ctx.guild, list(note.get("calls") or []))
    as_file = ctx.opt("as_file") in (True, "true", "True")
    has_files = bool(attached) or as_file or len(str(note.get("text") or "")) > EMBED_LIMIT
    kind, target, label = _target(ctx, has_files)
    rest = getattr(ctx.host, "rest", None)
    if rest is None:
        raise pad.NotepadError("this bot cannot post right now (no Discord connection)")

    def work() -> dict:
        from awdiscord.rest import RestError
        payload, files = _message(ctx, name, note, attached, as_file, kind == "user")
        try:
            channel = target
            if kind == "user":
                channel = str((rest.dm_channel(target) or {}).get("id") or "")
                if not channel:
                    return {"content": f"Not shared: could not open a DM with {label}."}
            if files:
                rest.request_files("POST", f"/channels/{channel}/messages", payload, files)
            else:
                rest.create_message(channel, payload)
        except RestError as exc:
            if kind == "user":
                return {"content": f"Not shared: Discord refused the DM to {label} "
                                   f"(their DMs may be closed; HTTP {exc.status})."}
            return {"content": f"Not shared: Discord refused the post in {label} "
                               f"(the bot may lack access there; HTTP {exc.status})."}
        extra = f" with {len(attached)} call transcript(s)" if attached else ""
        warn = (f" {len(gone)} attached call(s) are no longer on disk and were left out."
                if gone else "")
        return {"content": f"Shared **{name}**{extra} with {label}.{warn}"}

    return ctx.defer(work, private=True)


PLUGIN = Plugin(
    name=NAME,
    version="0.1.0",
    description="A personal notepad: type, save, share, attach call transcripts",
    default_enabled=True,
    on_modal=_on_modal,
    commands=[
        Command("notepad", "Your notepad: open, save, share, attach call transcripts",
                subcommands=[
                    Command("open", "Open a note to type in (Submit saves it)", _open,
                            options=[Option("name", "Which note (default: scratch)",
                                            max_length=pad.MAX_NAME)]),
                    Command("list", "Your notes in this server", _list),
                    Command("share", "Post a note in a channel or DM it to a member", _share,
                            options=[Option("name", "The note to share", required=True,
                                            max_length=pad.MAX_NAME),
                                     Option("user", "DM it to this member", type="user"),
                                     Option("channel", "Post it in this channel (default: here)",
                                            type="channel"),
                                     Option("as_file", "Send the note as a .md file",
                                            type="boolean")]),
                    Command("attach", "Attach Aither's voice-call transcript(s) to a note",
                            _attach,
                            options=[Option("name", "The note", required=True,
                                            max_length=pad.MAX_NAME),
                                     Option("call", "latest (default), all, none, or a "
                                            "number from /notepad calls", max_length=80),
                                     Option("inline", "Also paste the text into the note",
                                            type="boolean")]),
                    Command("calls", "Aither's call transcripts you can attach", _calls),
                    Command("delete", "Delete one of your notes", _delete,
                            options=[Option("name", "The note to delete", required=True,
                                            max_length=pad.MAX_NAME)]),
                ]),
    ],
)
