"""Operator-only status tools."""
from __future__ import annotations

import json

import pytest
from awdiscord.builtin import ops
from awdiscord.plugins import build_host
from awdiscord.store import GuildStore


@pytest.fixture
def status(tmp_path, monkeypatch):
    (tmp_path / "disk").mkdir()
    (tmp_path / "disk" / "host-disks.json").write_text(json.dumps(
        {"drives": {"C:": {"free_gb": 5.92, "status": "critical"}}}, indent=2))
    (tmp_path / "secret.key").write_text("nope")
    monkeypatch.setenv("AWDISCORD_STATUS_DIR", str(tmp_path))
    return tmp_path


def test_status_tools_are_offered_to_operators_only(status):
    host = build_host(GuildStore(":memory:"), operators={"7"})
    names = lambda u: {t.name for t in host.tools_for("g", u)}  # noqa: E731
    assert {"status_files", "read_status"} <= names("7")
    assert not {"status_files", "read_status"} & names("8")
    assert not {"status_files", "read_status"} & names("")


def test_lists_and_reads_records(status):
    listing = ops.status_files()
    assert "disk/host-disks.json" in listing and "secret.key" not in listing
    out = ops.read_status("disk/host-disks.json")
    assert '"free_gb":5.92' in out and "critical" in out


@pytest.mark.parametrize("name", ["../etc/passwd", "/etc/passwd", "secret.key", "missing.json"])
def test_read_stays_inside_the_directory(status, name):
    assert "No status record" in ops.read_status(name)


def test_no_directory_says_so(monkeypatch):
    monkeypatch.delenv("AWDISCORD_STATUS_DIR", raising=False)
    assert "No status directory" in ops.status_files()
