"""A reply to a picture carries the picture to the model.

Measured in a server: a member replied to an image with "what's in this?" and
the bot answered with an image-GENERATION attempt, because only the reply's own
attachments reached the brain and the reply had none.
"""
from types import SimpleNamespace

from awdiscord.gateway import _reply_images, _turn_attachments


def _att(i, name, ctype):
    return SimpleNamespace(id=i, filename=name, url=f"https://cdn/{name}",
                           proxy_url="", content_type=ctype, size=10)


def _msg(attachments=(), parent=None):
    ref = None if parent is None else SimpleNamespace(message_id=1, channel_id=2,
                                                      resolved=parent)
    return SimpleNamespace(attachments=list(attachments), reference=ref)


def test_a_reply_to_a_picture_carries_the_picture():
    parent = SimpleNamespace(attachments=[_att(7, "house.png", "image/png")])
    got = _turn_attachments(_msg(parent=parent))
    assert [a["filename"] for a in got] == ["house.png"]
    assert got[0]["content_type"] == "image/png"


def test_the_replys_own_files_win():
    parent = SimpleNamespace(attachments=[_att(7, "house.png", "image/png")])
    got = _turn_attachments(_msg([_att(8, "mine.jpg", "image/jpeg")], parent))
    assert [a["filename"] for a in got] == ["mine.jpg"]


def test_only_images_are_borrowed_from_the_parent():
    parent = SimpleNamespace(attachments=[_att(7, "notes.pdf", "application/pdf"),
                                          _att(9, "shot.WEBP", "IMAGE/WEBP")])
    assert [a["filename"] for a in _reply_images(_msg(parent=parent))] == ["shot.WEBP"]


def test_not_a_reply_or_unresolved_parent_borrows_nothing():
    assert _turn_attachments(_msg()) == []
    unresolved = SimpleNamespace(attachments=[], reference=SimpleNamespace(
        message_id=1, channel_id=2, resolved=None))
    assert _turn_attachments(unresolved) == []
    deleted = SimpleNamespace(attachments=[], reference=SimpleNamespace(
        message_id=1, channel_id=2, resolved=SimpleNamespace()))  # no .attachments
    assert _turn_attachments(deleted) == []
