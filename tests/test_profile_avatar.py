"""A persona bot's profile picture follows its portrait, and only uploads on a change."""
from __future__ import annotations

import pytest
from awdiscord import profile_avatar

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


class _Rest:
    def __init__(self):
        self.calls: list = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        return {}


def test_source_defaults_to_the_personas_portrait_and_can_be_off():
    assert profile_avatar.source_for("Saga", {}) == "https://aitherium.com/agents/saga.webp"
    assert profile_avatar.source_for("saga", {"AWDISCORD_PROFILE_AVATAR": "off"}) == ""
    assert profile_avatar.source_for("", {}) == ""
    assert profile_avatar.source_for("x", {"AWDISCORD_PROFILE_AVATAR": "/p.png"}) == "/p.png"


def test_uploads_once_then_only_when_the_portrait_changes(tmp_path):
    rest = _Rest()
    art = {"bytes": PNG}

    def run():
        return profile_avatar.sync(rest, "saga", tmp_path, env={},
                                   read=lambda src: art["bytes"], convert=lambda b: b)

    assert run() == "uploaded"
    [(method, path, body)] = rest.calls
    assert (method, path) == ("PATCH", "/users/@me")
    assert body["avatar"].startswith("data:image/png;base64,")
    assert run() == "unchanged" and len(rest.calls) == 1      # no API call
    art["bytes"] = PNG + b"new"
    assert run() == "uploaded" and len(rest.calls) == 2


def test_a_failed_upload_does_not_record_the_stamp(tmp_path):
    class _Down(_Rest):
        def request(self, *a, **k):
            raise RuntimeError("429")

    with pytest.raises(RuntimeError):
        profile_avatar.sync(_Down(), "saga", tmp_path, env={}, read=lambda s: PNG,
                            convert=lambda b: b)
    assert not (tmp_path / profile_avatar.STAMP).exists()
