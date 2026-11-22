"""`awdiscord post --attach`: files ride the same message as the text, limits refuse early."""
from types import SimpleNamespace

import pytest
from awdiscord import cli


def test_attach_reads_files_with_types(tmp_path):
    img = tmp_path / "preview.png"
    img.write_bytes(b"\x89PNG....")
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"\x00" * 10)
    out = cli.attach_files([str(img), str(vid)])
    assert [(n, len(d), t) for n, d, t in out] == [
        ("preview.png", 8, "image/png"), ("clip.mp4", 10, "video/mp4")]


def test_attach_refuses_missing_and_oversize(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not found"):
        cli.attach_files([str(tmp_path / "nope.png")])
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 64)
    monkeypatch.setattr(cli, "ATTACH_MAX_BYTES", 32)
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    with pytest.raises(ValueError, match="Discord takes"):
        cli.attach_files([str(big)])
    assert not opened, "an oversize file must be refused before it is read"
    monkeypatch.setattr("builtins.open", real_open)
    with pytest.raises(ValueError, match="at most"):
        cli.attach_files([str(big)] * (cli.ATTACH_MAX_FILES + 1))


def test_post_with_attach_sends_one_multipart_message(tmp_path, monkeypatch):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    calls = {}

    class FakeRest:
        def __init__(self, token):
            pass

        def request_files(self, method, path, payload, files):
            calls.update(method=method, path=path, payload=payload, files=files)
            return {"id": "9", "attachments": [{"filename": "a.png"}]}

        def create_message(self, *a, **k):
            raise AssertionError("text-only path used for an attachment post")

    import awdiscord.rest as rest
    monkeypatch.setattr(rest, "Rest", FakeRest)
    monkeypatch.setattr(cli, "_token", lambda: "t")
    args = SimpleNamespace(text="hello", file=None, attach=[str(img)], channel="123")
    assert cli.cmd_post(args) == 0
    assert calls["path"] == "/channels/123/messages"
    assert calls["payload"]["content"] == "hello"
    assert calls["payload"]["attachments"] == [{"id": 0, "filename": "a.png"}]
    assert calls["files"][0][0] == "a.png"


def test_post_bad_attach_sends_nothing(tmp_path, monkeypatch):
    import awdiscord.rest as rest

    def boom(token):
        raise AssertionError("Rest built for a refused post")

    monkeypatch.setattr(rest, "Rest", boom)
    args = SimpleNamespace(text="hi", file=None, attach=[str(tmp_path / "x")], channel="1")
    assert cli.cmd_post(args) == 2
