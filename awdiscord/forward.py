"""DM forwarding: decision-card answers go to the card system, not the chat agent.

A decision-card system DMs the operator "Reply with a number: 1 ... 2 ...". The
answer ("1", "2 only the MCP half", "d-ab12 2", "cards", "steer ...") is meant for
that system, not for the chat agent. With ``AWDISCORD_DM_FORWARD_URL`` set, a DM
from a card OWNER whose text is card-shaped is POSTed there:

    {"platform": "discord", "user_id": "...", "text": "...",
     "is_direct_message": true, "last_sent_card": "d-ab12"}

and the receiver's ``{"reply": "..."}`` (the recorded outcome) is sent back; the
agent never sees the message. Everything else goes to the agent.

Only card-shaped text is forwarded, never every DM. The receiver answers EVERY
message it gets ("Not authorized." to a stranger, "26 cards are open" to the
owner's "how are you", and a word like "post" can match an option label), so
forwarding ordinary chat would both swallow it and risk answering a card nobody
meant to answer.

Two more shapes are card answers, because the owner can only mean the card:

* a Discord REPLY to a card message the bot posted (``reference_text`` is the
  replied-to message). The card id comes from THAT message, not the newest
  card, so answering an older card from the phone answers the right one. A
  number or an option label is sent as ``"d-xxxx <n>"``; anything else is sent
  as ``"d-xxxx steer <text>"``: the free text reaches the session that raised
  the card and the card stays open.
* text that IS one of the newest card's option labels ("Ship", "hold off"),
  whole message, case-insensitive, and ONLY while that card is the last
  message the bot posted in the DM (any bot chat after it, or a restart,
  drops the labels). It is sent as ``"d-xxxx <n>"``, never the
  label itself: the receiver matches a label by its first word, so "Ship later"
  sent verbatim would answer "Ship now".

Non-owners are never forwarded; the receiver re-authorizes the owner anyway
(fail-closed against its own binding), and the bearer only proves the bot is the
caller. ``last_sent_card`` is the newest ``d-xxxx`` id the bot posted in that DM,
so a bare "1" resolves against the card the person is looking at.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Iterable

CARD_ID_RE = re.compile(r"\b(d-[0-9a-z]{4,12})\b", re.IGNORECASE)
_LEADING_ID_RE = re.compile(r"^d-[0-9a-z]{4,12}\b", re.IGNORECASE)
_STEER_RE = re.compile(r"^(?:d-[0-9a-z]{4,12}\s+)?steer\s+\S", re.IGNORECASE)
# The receiver's "show me what is waiting" words.
LIST_WORDS = frozenset({"cards", "decisions", "pending", "waiting"})
# One option line of a rendered card menu: "`2` **Hold** <star> -- waits for CI"
# (the adk/awask render_for_chat shape) or the plain "2 Hold" form.
_OPTION_RE = re.compile(r"(?m)^\s*`?(\d{1,2})`?\s+(.+?)\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CONSEQUENCE_RE = re.compile(r"\s+[—–-]\s+.*$")


def _norm(text: str) -> str:
    """Compare form of a label or a message: no markdown, star, case or end punctuation."""
    t = re.sub(r"[`*_⭐]", "", text or "").strip().lower()
    return re.sub(r"\s+", " ", t).strip(" .!?")


def parse_card(content: str) -> tuple[str, int, dict[str, int]]:
    """(newest card id, option count, {normalized label: option number}) of a card text."""
    ids = CARD_ID_RE.findall(content or "")
    if not ids:
        return "", 0, {}
    _, _, menu = (content or "").partition("Reply with a number")
    labels: dict[str, int] = {}
    count = 0
    for m in _OPTION_RE.finditer(menu):
        n, rest = int(m.group(1)), m.group(2)
        count = max(count, n)
        bold = _BOLD_RE.search(rest)
        label = _norm(bold.group(1) if bold else _CONSEQUENCE_RE.sub("", rest))
        # A label two options share answers neither: a guessed answer is how a
        # card gets the wrong one.
        if label:
            labels[label] = 0 if label in labels else n
    return ids[-1].lower(), count, {k: v for k, v in labels.items() if v}


def is_card(content: str) -> bool:
    """A rendered card (a number menu, or its id alone on the last line), not chat
    that merely names one ("d-ab12 is about the GPU rental")."""
    text = content or ""
    if "Reply with a number" in text and CARD_ID_RE.search(text):
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return bool(lines) and bool(CARD_ID_RE.fullmatch(lines[-1].strip("`*_ ")))


def label_choice(text: str, labels: dict[str, int]) -> int:
    """The option number whose label IS the whole message, else 0."""
    return labels.get(_norm(text), 0) if labels else 0


class DmForwarder:
    def __init__(self, url: str, token: str = "", owners: Iterable[str] = (),
                 timeout: float = 15.0,
                 lookup: Callable[[str], str] | None = None):
        self.url = url
        self._token = token
        self.owners = frozenset(str(o).strip() for o in owners if str(o).strip())
        self._timeout = timeout
        # channel id -> text of the newest card the bot posted there, read from
        # DM history. Set by the gateway: after a restart the maps below are
        # empty but the card the owner is replying to is still in the DM.
        self._lookup = lookup
        self.last_card: dict[str, str] = {}     # channel id -> newest card id
        self.options: dict[str, int] = {}       # channel id -> that card's option count
        self.labels: dict[str, dict[str, int]] = {}  # channel id -> that card's labels

    def note_own_message(self, channel_id: str, content: str) -> None:
        """Remember the newest card id (option count, labels) posted in this DM.

        The gateway calls this for EVERY message the bot posts in a DM. Labels
        live only while a card is the last thing the bot said: once the bot
        chats ("want me to open the PR?"), a bare "yes" answers the chat, never
        an older card whose option happens to be "Yes".
        """
        card, count, labels = parse_card(content)
        if card:
            self.last_card[channel_id] = card
            self.options[channel_id] = count
        if card and is_card(content):
            self.labels[channel_id] = labels
        else:
            self.labels.pop(channel_id, None)

    def _recall(self, channel_id: str) -> None:
        if channel_id in self.last_card or self._lookup is None:
            return
        try:
            text = self._lookup(channel_id) or ""
        except Exception:       # history is a hint; the receiver can still resolve
            text = ""
        if text:
            self.note_own_message(channel_id, text)
            # History gives the newest CARD, not the newest bot message: chat may
            # follow it, so its labels are not known to be on screen.
            self.labels.pop(channel_id, None)

    def reply_for(self, channel_id: str, user_id: str, text: str,
                  reference_text: str = "") -> str:
        """The recorded outcome, or "" when the DM is not a card answer from an owner.

        ``reference_text`` is the text of the bot message this DM is a Discord
        reply to, or "" when it replies to nothing the bot posted.
        """
        if user_id not in self.owners:
            return ""
        t = (text or "").strip()
        if not t:
            return ""
        if reference_text and is_card(reference_text):
            card, count, labels = parse_card(reference_text)
            if card:
                return self._post(channel_id, user_id, reply_to_card(t, card, count, labels))
        self._recall(channel_id)            # numbers and labels need the card on screen
        card = self.last_card.get(channel_id, "")
        choice = label_choice(t, self.labels.get(channel_id, {}))
        if card and choice:
            return self._post(channel_id, user_id, f"{card} {choice}")
        if not looks_like_answer(t, self.options.get(channel_id, 0)):
            return ""
        return self._post(channel_id, user_id, t)

    def _post(self, channel_id: str, user_id: str, t: str) -> str:
        body = json.dumps({"platform": "discord", "user_id": user_id, "text": t,
                           "is_direct_message": True,
                           "last_sent_card": self.last_card.get(channel_id, "")}).encode()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        # A card answer that could not be applied must SAY so, never fall through
        # to a chat answer that reads as if it worked.
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return f"That answer was NOT applied: the decision service answered HTTP {exc.code}."
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return "That answer was NOT applied: the decision service is unreachable."
        reply = str((data or {}).get("reply") or "").strip()
        return reply or "That answer was NOT applied: the decision service gave no reply."


def reply_to_card(text: str, card: str, options: int = 0,
                  labels: dict[str, int] | None = None) -> str:
    """What to send for a Discord reply to card ``card``: an answer or a steer.

    The card id goes first so the receiver resolves THAT card, not the newest
    one. A list word, or a message that already names a card, is sent as is.
    """
    t = (text or "").strip()
    low = t.lower()
    if low.strip("/!.? ") in LIST_WORDS or _LEADING_ID_RE.match(low):
        return t
    if _STEER_RE.match(low):
        return f"{card} {t}"
    choice = label_choice(t, labels or {})
    if choice:
        return f"{card} {choice}"
    m = re.fullmatch(r"(\d{1,2})(?:[.!]?|\s+\S.*)", low, re.DOTALL)
    if m and (not options or 1 <= int(m.group(1)) <= options):
        return f"{card} {t}"
    return f"{card} steer {t}"


def looks_like_answer(text: str, options: int = 0) -> bool:
    """A card answer: "1", "2 only the MCP half", "d-ab12 1", "cards", "steer ...".

    A bare number counts even when the bot has not seen a card since it
    restarted: "1" is never a question for the agent, and the receiver knows
    which card it sent last. "<n> <text>" counts only when n is an option of
    the card on screen ("12 monkeys is great" stays chat). A card id in the
    middle of a sentence ("what was d-ab12 about?") is chat about the card.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.strip("/!.? ") in LIST_WORDS or _LEADING_ID_RE.match(t) or _STEER_RE.match(t):
        return True
    if re.fullmatch(r"\d{1,2}[.!]?", t):
        return True
    m = re.fullmatch(r"(\d{1,2})\s+\S.*", t, re.DOTALL)
    return bool(m and options and 1 <= int(m.group(1)) <= options)
