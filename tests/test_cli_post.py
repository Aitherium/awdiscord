"""`awdiscord post` takes its text from a file or stdin, verbatim (2026-11-15)."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord import cli  # noqa: E402

MD = "**Plan**\n- `*-plan` forums\n- run `/roadmap` then $(whoami)\n"


class FakeRest:
    sent: list = []

    def __init__(self, token):
        pass

    def create_message(self, channel, body):
        FakeRest.sent.append((channel, body))
        return {"id": "m1"}


@pytest.fixture
def rest(monkeypatch):
    FakeRest.sent = []
    monkeypatch.setattr("awdiscord.rest.Rest", FakeRest)
    monkeypatch.setattr(cli, "_token", lambda: "t")
    return FakeRest


def test_post_file_is_verbatim(tmp_path, rest):
    f = tmp_path / "msg.md"
    f.write_text(MD, encoding="utf-8")
    assert cli.main(["post", "--channel", "c1", "--file", str(f)]) == 0
    assert rest.sent == [("c1", {"content": MD.rstrip("\n")})]
    assert "`*-plan`" in rest.sent[0][1]["content"]
    assert "`/roadmap`" in rest.sent[0][1]["content"]


@pytest.mark.parametrize("argv", [["post", "--channel", "c1", "--file", "-"],
                                  ["post", "--channel", "c1", "-"]])
def test_post_stdin(argv, rest, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(MD))
    assert cli.main(argv) == 0
    assert rest.sent[0][1]["content"] == MD.rstrip("\n")


def test_post_plain_text_still_works_and_bad_input_refused(tmp_path, rest):
    assert cli.main(["post", "--channel", "c1", "hello"]) == 0
    assert rest.sent[-1][1]["content"] == "hello"
    f = tmp_path / "m.md"
    f.write_text("x", encoding="utf-8")
    assert cli.main(["post", "--channel", "c1", "hi", "--file", str(f)]) == 2
    assert cli.main(["post", "--channel", "c1"]) == 2
    assert cli.main(["post", "--channel", "c1", "--file", str(tmp_path / "nope")]) == 2
    assert len(rest.sent) == 1
