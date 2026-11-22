"""A reply whose PARENT calls the bot is still a call to the bot.

Owner, 2026-11-17, watching #fightclub: Tom wrote "@Marjery @Aither memes only…";
Marjery replied to that message with "Aither, explain…"; Aither said nothing, because
the only @Aither in the thread sat in the parent. `mentions_bot` now reads the parent
through `reference.resolved`, which Discord populates for a reply.
"""
from __future__ import annotations

from awdiscord.gateway import mentions_bot


class _Obj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Ref:
    def __init__(self, resolved):
        self.resolved = resolved


BOT = _Obj(id="42", tags=None)


def _msg(mentions=(), role_mentions=(), reference=None):
    return _Obj(mentions=list(mentions), role_mentions=list(role_mentions),
                reference=reference)


def test_a_direct_mention_still_counts() -> None:
    assert mentions_bot(_msg(mentions=[BOT]), BOT) is True


def test_a_plain_message_is_not_a_call() -> None:
    assert mentions_bot(_msg(), BOT) is False


def test_a_reply_whose_parent_mentions_the_bot_is_a_call() -> None:
    parent = _msg(mentions=[BOT])
    reply = _msg(reference=_Ref(parent))
    assert mentions_bot(reply, BOT) is True


def test_a_reply_whose_parent_does_not_mention_the_bot_is_silent() -> None:
    reply = _msg(reference=_Ref(_msg()))
    assert mentions_bot(reply, BOT) is False


def test_an_unresolved_reference_is_not_a_call() -> None:
    """Discord omits the parent when it cannot resolve it: never guess a mention."""
    assert mentions_bot(_msg(reference=_Ref(None)), BOT) is False


def test_the_bot_role_in_the_parent_counts_too() -> None:
    role = _Obj(tags=_Obj(bot_id="42"))
    reply = _msg(reference=_Ref(_msg(role_mentions=[role])))
    assert mentions_bot(reply, BOT) is True
