"""Intake: a plugin may take a message addressed to the bot before the chat brain does.

WHY. A direct message from someone the operator serves but who is not the operator --
a customer's contact, a client -- reached nobody. With no brain for that sender the bot
answered once with the bring-your-own-model card and after that with silence, so
"tell the bot" was a dead end for exactly the people it matters most to answer.

A plugin that knows who the sender is can take the message::

    from awdiscord import intake
    intake.register(fn)          # fn(event: IntakeEvent) -> bool

``fn`` runs in the bot's worker thread (it may block on HTTP or a model call). It posts
its own answer through ``event.rest`` and returns True when the message was its to
handle; the chat brain then does not run. Return False for anyone it does not know and
the bot behaves exactly as before. The first intake that returns True wins. One that
raises is reported to ``on_error`` and skipped: a broken intake never eats a message.

Every message that would reach the chat brain is offered: a DM, and a mention in a
server. With nothing registered, nothing changes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class IntakeEvent:
    """One message addressed to the bot. ``guild_id`` is None for a direct message."""

    user: str
    channel_id: str
    message_id: str
    content: str
    guild_id: Optional[str] = None
    author_name: str = ""
    #: The message this one replies to (direct messages only), and its text when that
    #: message was the bot's own.
    reference_id: str = ""
    reference_text: str = ""
    attachments: List[dict] = field(default_factory=list)
    #: The bot's own agent (``.ask(prompt, user=...) -> str``) and REST client.
    agent: Any = None
    rest: Any = None

    @property
    def is_dm(self) -> bool:
        return not self.guild_id


Intake = Callable[[IntakeEvent], bool]
_INTAKES: List[Intake] = []
_lock = threading.Lock()


def register(fn: Intake) -> None:
    with _lock:
        if fn not in _INTAKES:
            _INTAKES.append(fn)


def registered() -> int:
    """How many intakes are registered in this process."""
    with _lock:
        return len(_INTAKES)


def clear() -> None:
    with _lock:
        del _INTAKES[:]


def offer(event: IntakeEvent,
          on_error: Optional[Callable[[Exception], None]] = None) -> bool:
    """True when a registered intake took the message."""
    with _lock:
        intakes = list(_INTAKES)
    for fn in intakes:
        try:
            if fn(event):
                return True
        except Exception as exc:  # noqa: BLE001 - reported; the next intake, then the brain
            if on_error is not None:
                on_error(exc)
    return False
