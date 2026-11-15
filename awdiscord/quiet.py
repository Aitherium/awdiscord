"""How the bot behaves when someone @mentions it in a server.

Four rules, measured in the Aitherium Collective on 2026-11-13:

1. **Answer where you were asked.** A reply to a thread or channel mention goes in that
   thread or channel, as a reply to the mention, not into the bot channel with an
   "asked in" header. ``AWDISCORD_CONTAIN_MENTIONS=1`` turns the old bot-channel relay
   back on. Slash commands and the bot's own unsolicited posts are still contained.
2. **A durable mute for each channel or thread.** "remove yourself from this chat" or
   "mute" sets ``core/mute:<channel>`` in the guild store. The mute survives restarts
   and stays until the member who set it, or a server admin, says "unmute". While a
   channel is muted, a mention gets no public reply. The asker gets ONE private notice
   (a DM) at most every ``NOTICE_EVERY_S`` seconds.
3. **Warm and brief.** ``polish`` removes curt or sarcastic stock lines ("Yes, I'm here,
   and I answered. If you want me to actually do something, tell me what."). If removing
   them leaves nothing, it puts a friendly line in their place.
4. **No serving internals in public.** Sentences naming the model, the GPU or the
   serving stack are removed unless an owner or admin is asking.

``room_ask`` spots "make a room called #x where everyone can see but only I can type".
The gateway hands that ask straight to the plugin tool ``server_room`` (the collective
pack's Discord-ops tool, gated by the server's own permissions), never to a forge run.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

STORE_PLUGIN = "core"
MUTE_PREFIX = "mute:"
NOTICE_PREFIX = "mute-notice:"
NOTICE_EVERY_S = 6 * 3600
ROOM_TOOL = "server_room"

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
MANAGE_CHANNELS = 1 << 4

_MENTION = re.compile(r"<@!?&?\d+>")
_MUTE = re.compile(
    r"\b(?:remove yourself|leave (?:this|my|the) (?:new )?(?:chat|thread|channel|room),? "
    r"(?:please|now|aither)|you can'?t (?:talk|speak|post) (?:in )?here|"
    r"you (?:can ?not|cannot) (?:talk|speak|post) (?:in )?here|"
    r"stop (?:talking|replying|posting) (?:in )?here|mute yourself)", re.I)
#: Short commands that mean "mute" only when they ARE the whole message ("@Aither mute",
#: "@Aither be quiet"), never inside a longer ask ("a poem telling the rain to go away").
_MUTE_ALONE = re.compile(r"^\s*(?:please\s+)?(?:mute|be quiet|shut up|go away|stay out|"
                         r"quiet|leave (?:this|my|the) (?:new )?(?:chat|thread|channel|room))"
                         r"\s*(?:please|here|in here|now)?\s*[.!]*\s*$", re.I)
_UNMUTE = re.compile(r"^\s*(?:please\s+)?un-?mute\b|\byou can talk (?:here|again)\b", re.I)

#: Curt or sarcastic stock lines. Each one shipped in a public reply (2026-11-13).
CURT = (
    re.compile(r"\byes\s*[-\u2014\u2013,]*\s*i'?m here,?\s*and i (?:already )?answered\.?",
               re.I),
    re.compile(r"\bif you want me to actually \*?do\*? something(?: specific)?,?\s*tell me "
               r"what\.?", re.I),
    re.compile(r"\bi already (?:answered|told you)(?: that)?\b[^.!?\n]*[.!?]?", re.I),
    re.compile(r"^\s*as i (?:already )?said,\s*", re.I | re.M),
)
WARM_FALLBACK = "I'm here! What can I help you with?"

#: A sentence is about the bot's OWN serving only when it talks about itself AND names
#: the stack. "Is a 5090 good for Blender?" answered with GPU advice is left alone.
SELF = re.compile(r"\b(?:i(?:'m| am)?|my|me|this (?:response|reply|answer)|today'?s "
                  r"(?:response|reply|answer)|aither)\b", re.I)
SERVING = re.compile(
    r"\b(?:served by|powered by|deployed on|i'?m (?:running|served|hosted) on|"
    r"local model|(?:my|this) model|model (?:id|name)|bonsai\d*(?:-\w+)*|"
    r"microscheduler|vllm|llama\.?cpp|inference (?:lane|pool|server))\b", re.I)
_FENCE = re.compile(r"(```.*?```)", re.S)
_SPLIT = re.compile(r"(?<=[.!?])(\s+)")


def _text(content: str) -> str:
    return _MENTION.sub(" ", str(content or "")).strip()


def is_mute_request(content: str) -> bool:
    text = _text(content)
    if not text or _UNMUTE.search(text):
        return False
    return bool(_MUTE.search(text)) or bool(_MUTE_ALONE.match(text))


def is_unmute_request(content: str) -> bool:
    return bool(_UNMUTE.search(_text(content)))


def answer_in_place() -> bool:
    """Mentions are answered where they were asked unless the relay is turned on."""
    return (os.getenv("AWDISCORD_CONTAIN_MENTIONS") or "0").strip().lower() not in (
        "1", "true", "yes", "on")


# -- the mute ----------------------------------------------------------------------

def muted(store, guild: str, channel: str, parent: str = "") -> Optional[dict]:
    """The mute record covering this channel (or the channel a thread sits in)."""
    for where in (channel, parent):
        if not where:
            continue
        row = store.get(str(guild), STORE_PLUGIN, MUTE_PREFIX + str(where))
        if isinstance(row, dict):
            return dict(row, channel=str(where))
    return None


def set_mute(store, guild: str, channel: str, user: str) -> dict:
    row = {"by": str(user), "at": time.time()}
    store.set(str(guild), STORE_PLUGIN, MUTE_PREFIX + str(channel), row)
    return row


def clear_mute(store, guild: str, channel: str) -> None:
    store.delete(str(guild), STORE_PLUGIN, MUTE_PREFIX + str(channel))


def may_unmute(row: dict, user: str, *, admin: bool) -> bool:
    return admin or str(row.get("by") or "") == str(user)


def notice_due(store, guild: str, channel: str, user: str,
               now: Optional[float] = None) -> bool:
    """True at most once per NOTICE_EVERY_S per (channel, user); records the send."""
    now = time.time() if now is None else now
    key = NOTICE_PREFIX + "%s:%s" % (channel, user)
    last = store.get(str(guild), STORE_PLUGIN, key)
    try:
        recent = last is not None and now - float(last) < NOTICE_EVERY_S
    except (TypeError, ValueError):
        recent = False
    if recent:
        return False
    store.set(str(guild), STORE_PLUGIN, key, now)
    return True


def is_admin(rest, guild: str, user: str, operators=()) -> bool:
    """Server owner, a bot operator, or Administrator/Manage Server/Manage Channels."""
    if str(user) in {str(o) for o in operators or ()}:
        return True
    try:
        info = rest.request("GET", "/guilds/%s" % guild) or {}
        if str(info.get("owner_id") or "") == str(user):
            return True
        member = rest.request("GET", "/guilds/%s/members/%s" % (guild, user)) or {}
        roles = rest.request("GET", "/guilds/%s/roles" % guild) or []
    except Exception:  # noqa: BLE001 - unreadable standing is no standing
        return False
    held = {str(r) for r in member.get("roles") or []} | {str(guild)}
    bits = 0
    for role in roles:
        if str(role.get("id")) in held:
            try:
                bits |= int(str(role.get("permissions") or "0"))
            except ValueError:
                continue
    return bool(bits & (ADMINISTRATOR | MANAGE_GUILD | MANAGE_CHANNELS))


MUTED_REPLY = ("Okay, I'll stay quiet in here. Mention me with **unmute** whenever you "
               "want me back.")
UNMUTED_REPLY = "Thanks for having me back! I'm listening here again."
NOT_YOURS = ("I'm muted here by <@%s>. Only they or a server admin can unmute me, so "
             "I'll keep quiet.")


def notice_text(guild: str, channel: str) -> str:
    return ("I'm muted in https://discord.com/channels/%s/%s, so I won't reply there. "
            "Say **@Aither unmute** in that chat to let me talk again." % (guild, channel))


# -- tone and internals ------------------------------------------------------------

def curt_hits(text: str) -> list:
    """The curt patterns ``text`` contains (what the test and the log both report)."""
    return [p.pattern for p in CURT if p.search(str(text or ""))]


def _about_own_serving(sentence: str) -> bool:
    return bool(SELF.search(sentence)) and bool(SERVING.search(sentence))


def _polish_prose(text: str, privileged: bool) -> str:
    for pattern in CURT:
        text = pattern.sub("", text)
    if privileged:
        return text
    lines = []
    for line in text.split("\n"):
        parts = _SPLIT.split(line)             # sentences and the spaces between them
        kept = []
        for i in range(0, len(parts), 2):
            sentence = parts[i]
            gap = parts[i + 1] if i + 1 < len(parts) else ""
            if sentence and _about_own_serving(sentence):
                continue
            kept.append(sentence + gap)
        lines.append("".join(kept).rstrip() if kept else "")
    return "\n".join(lines)


def polish(text: str, *, privileged: bool = False) -> str:
    """The reply without curt stock lines and, in public, without the bot describing
    its own model or serving stack. Code fences, links and spacing are left as written.
    """
    original = str(text or "")
    pieces = _FENCE.split(original)
    out = "".join(p if p.startswith("```") else _polish_prose(p, privileged)
                  for p in pieces).strip()
    if not re.search(r"\w", out):
        return WARM_FALLBACK
    return out


# -- "make a room called #x where everyone can see but only I can type" ---------------

_ROOM = re.compile(
    r"\b(?:can you|could you|would you|will you|please|pls)?\s*(?:make|create|set ?up|"
    r"add|open)\b[^.?!\n]{0,20}?\b(?:room|channel)\b\s+(?:called|named)\s+#?"
    r"(?P<name>[\w-]{2,90})|\b(?:make|create|set ?up|add|open)\s+(?:a\s+|me\s+a\s+)?"
    r"(?:new\s+)?(?:room|channel)\s+#(?P<hash>[\w-]{2,90})", re.I)
_QUESTION = re.compile(r"^\s*(?:how|what|why|where|when|which|who|is|are|does|do)\b", re.I)
_ONLY_ME = re.compile(r"\bonly (?:i|me)\b[^.?!\n]{0,20}\b(?:can )?(?:type|write|post|talk|"
                      r"speak)\b|\bwrite access only for\b|\bread[- ]only\b", re.I)


def room_ask(content: str) -> Optional[dict]:
    """{"name", "write"} for a room-creation ask, else None."""
    text = _text(content)
    if _QUESTION.match(text) and not re.match(r"^\s*(?:can|could|would|will)\b", text, re.I):
        return None                     # "how do I create a channel?" wants an answer
    hit = _ROOM.search(text)
    if not hit:
        return None
    raw = hit.group("name") or hit.group("hash") or ""
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:90]
    if not name or name in ("called", "named", "for", "where", "that"):
        return None
    return {"name": name, "write": "requester" if _ONLY_ME.search(text) else "everyone"}


def run_room_tool(host, msg: dict) -> Optional[str]:
    """Run the ``server_room`` plugin tool for this ask; None when no tool is on here."""
    ask = room_ask(msg.get("content") or "")
    if not ask or host is None:
        return None
    tools = host.tools_for(msg.get("guild_id"), user=str(msg.get("user") or ""),
                           channel=str(msg.get("channel_id") or ""),
                           message=str(msg.get("id") or ""),
                           role_names=msg.get("role_names"),
                           question=str(msg.get("content") or ""))
    tool: Any = next((t for t in tools if t.name == ROOM_TOOL), None)
    if tool is None:
        return None
    return tool.run(ask)
