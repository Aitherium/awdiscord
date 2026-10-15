"""`awdiscord setup`: profile, backend detection, operators, env file - offline."""
from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest
from awdiscord import setup as aw_setup

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38  # fake, shaped like a bot token


class FakeRest:
    def __init__(self, app, drop=()):
        self.app = dict(app)
        self.drop = set(drop)
        self.calls = []

    def me(self):
        return {"id": "1470273764708057120", "username": "Aither", "bot": True}

    def application(self):
        return self.app

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        self.app.update({k: v for k, v in (body or {}).items()
                         if k not in self.drop and k not in ("icon", "cover_image")})
        if body and "icon" in body and "icon" not in self.drop:
            self.app["icon"] = "hash"
        return self.app

    def put_commands(self, app_id, cmds, guild_id=None):
        self.calls.append(("PUT", app_id, guild_id))
        return cmds


def _opener(served: dict):
    def opener(req, timeout=0):
        url = req.full_url
        for base, models in served.items():
            if url == base + "/models":
                return io.BytesIO(json.dumps({"data": [{"id": m} for m in models]}).encode())
        raise urllib.error.URLError("refused")
    return opener


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AWDISCORD_PLUGIN_DIRS", raising=False)


def test_detects_first_live_backend():
    found = aw_setup.detect_backends(opener=_opener({"http://127.0.0.1:11434/v1": ["qwen3"]}))
    assert [(b.label, b.models) for b in found] == [("ollama", ["qwen3"])]


def test_profile_body_is_least_permission_and_validated():
    body = aw_setup.profile_body(description="hi", tags=["a", "b"])
    assert body["install_params"]["scopes"] == ["bot", "applications.commands"]
    assert body["integration_types_config"]["1"]["oauth2_install_params"]["permissions"] == "0"
    with pytest.raises(ValueError):
        aw_setup.profile_body(tags=list("abcdef"))
    with pytest.raises(ValueError):
        aw_setup.profile_body(description="x" * 401)


def test_operator_ids_prefers_accepted_team_members():
    app = {"owner": {"id": "9"}, "team": {"members": [
        {"user": {"id": "1"}, "membership_state": 2},
        {"user": {"id": "2"}, "membership_state": 1}]}}
    assert aw_setup.operator_ids(app) == ["1"]
    assert aw_setup.operator_ids({"owner": {"id": "9"}}) == ["9"]


def test_run_setup_end_to_end(tmp_path):
    rest = FakeRest({"id": "1470273764708057120", "owner": {"id": "178"}},
                    drop={"terms_of_service_url"})
    lines = []
    report = aw_setup.run_setup(TOKEN, rest=rest, description="Aither on Discord",
                         tags=["ai"], terms_url="https://example.com/t",
                         opener=_opener({"http://127.0.0.1:8090/v1": ["bonsai-27b"]}),
                         say=lines.append)
    assert report["backend"] == "http://127.0.0.1:8090/v1"
    assert report["model"] == "bonsai-27b"
    assert report["operators"] == ["178"]
    assert report["ignored"] == ["terms_of_service_url"]
    assert {"ask", "plugins", "poll"} <= set(report["commands"])
    env = aw_setup.read_env()
    assert env["DISCORD_BOT_TOKEN"] == TOKEN and env["AWDISCORD_AGENT_MODEL"] == "bonsai-27b"
    assert len(env["AWDISCORD_APPROVAL_SECRET"]) == 64
    if os.name != "nt":
        assert oct(os.stat(report["env"]).st_mode & 0o777) == "0o600"
    assert not any(TOKEN in line for line in lines)      # the token is never printed


def test_rerun_keeps_the_approval_secret():
    rest = FakeRest({"id": "1", "owner": {"id": "178"}})
    aw_setup.run_setup(TOKEN, rest=rest, opener=_opener({}), say=lambda *_: None)
    first = aw_setup.read_env()["AWDISCORD_APPROVAL_SECRET"]
    aw_setup.run_setup(TOKEN, rest=rest, opener=_opener({}), say=lambda *_: None)
    assert aw_setup.read_env()["AWDISCORD_APPROVAL_SECRET"] == first


def test_env_file_never_overrides_real_env(monkeypatch):
    aw_setup.write_env({"AWDISCORD_AGENT_URL": "http://file/v1", "AWDISCORD_AGENT_MODEL": "m"})
    monkeypatch.setenv("AWDISCORD_AGENT_URL", "http://real/v1")
    monkeypatch.delenv("AWDISCORD_AGENT_MODEL", raising=False)
    aw_setup.load_env_file()
    assert os.environ["AWDISCORD_AGENT_URL"] == "http://real/v1"
    assert os.environ["AWDISCORD_AGENT_MODEL"] == "m"


def test_icon_type_and_size_checked(tmp_path):
    bad = tmp_path / "x.bmp"
    bad.write_bytes(b"BM")
    with pytest.raises(ValueError):
        aw_setup.image_data_uri(bad)
    good = tmp_path / "x.png"
    good.write_bytes(b"\x89PNG")
    assert aw_setup.image_data_uri(good).startswith("data:image/png;base64,")
