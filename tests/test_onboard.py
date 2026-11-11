"""/signup (plugin link-onboard): terms first, then a modal, then a Genesis-minted single-use link.

Each test names the MUTATION that must make it fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord.builtin import BUILTIN_MODULES, onboard  # noqa: E402
from awdiscord.plugins import PluginHost  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

G = "100000000000000001"
MEMBER = "200000000000000002"
OTHER = "300000000000000003"


class _Resp:
    def __init__(self, body):
        self._raw = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


@pytest.fixture()
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_INTERNAL_SECRET", "internal-test")
    monkeypatch.setenv("AWDISCORD_GENESIS_URL", "https://genesis.test:8001")
    monkeypatch.delenv("AWDISCORD_CA_BUNDLE", raising=False)
    calls = []

    def fake_open(req, timeout=None, context=None):
        calls.append(req)
        body = json.loads(req.data.decode())
        url = "https://app.aitherium.com/signup?discord_signup=t-" + body["discord_user_id"]
        return _Resp({"url": url, "token": "t", "expires_in_seconds": 3600})
    monkeypatch.setattr(onboard, "OPENER", [fake_open])
    h = PluginHost(GuildStore(), plugins=[onboard.PLUGIN])
    h.set_enabled(G, onboard.NAME, True)
    return h, calls


def _member(user=MEMBER):
    return {"guild_id": G, "channel_id": "c1", "member": {"user": {"id": user}}}


def _slash(h):
    return h.handle({**_member(), "data": {"name": onboard.COMMAND}})


def _press(h):
    return h.handle_button({**_member(), "data": {"custom_id": onboard.PLUGIN.button_id("accept")}})


def _submit(h, values: dict, user=MEMBER):
    rows = [{"type": 1, "components": [{"type": 4, "custom_id": k, "value": v}]}
            for k, v in values.items()]
    return h.handle_modal({**_member(user), "type": 5, "data": {
        "custom_id": onboard.PLUGIN.button_id("accept"), "components": rows}})


def _finish(result) -> str:
    assert result.deferred is not None, result.response
    return result.deferred()["content"]


def test_onboard_is_a_builtin_and_takes_no_owned_name():
    """MUTATION: drop the module from BUILTIN_MODULES, or name the plugin/command after a
    surface someone else owns: ``onboard`` (the Collective onboarding pack, plugin AND
    command), ``link`` (collective core), ``join`` (voice), ``enlist`` (builtin.join)."""
    assert "awdiscord.builtin.onboard" in BUILTIN_MODULES
    assert onboard.PLUGIN.name == "link-onboard" != "onboard"
    names = [c.name for c in onboard.PLUGIN.commands]
    assert names == ["signup"]
    assert not set(names) & {"onboard", "link", "join", "enlist"}


def test_slash_shows_the_terms_and_mints_nothing(host):
    """MUTATION: mint the link on /signup itself, before the member accepted."""
    h, calls = host
    res = _slash(h)
    data = res.response["data"]
    assert data["flags"] == 64
    assert onboard.TERMS_URL in data["content"] and onboard.PRIVACY_URL in data["content"]
    assert "no cash value and cannot be spent yet" in data["content"]
    button = data["components"][0]["components"][0]
    assert button["custom_id"] == onboard.PLUGIN.button_id("accept")
    assert calls == []


def test_button_opens_the_accept_modal(host):
    """MUTATION: skip the modal (no explicit acceptance)."""
    h, calls = host
    res = _press(h)
    assert res.response["type"] == 9
    field = res.response["data"]["components"][0]["components"][0]
    assert field["custom_id"] == "confirm" and field["required"] is True
    assert calls == []


@pytest.mark.parametrize("typed", ["", "yes", "I accep", "accept"])
def test_modal_without_the_phrase_links_nothing(host, typed):
    """MUTATION: accept any submission as consent."""
    h, calls = host
    res = _submit(h, {"confirm": typed})
    assert "Nothing was linked" in res.response["data"]["content"]
    assert calls == []


def test_modal_with_the_phrase_mints_for_the_signed_user(host):
    """POSITIVE: an accepted submission asks Genesis as the platform caller."""
    h, calls = host
    text = _finish(_submit(h, {"confirm": "  i   accept "}))
    assert "discord_signup=t-" + MEMBER in text
    assert "no cash value and cannot be spent yet" in text
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://genesis.test:8001" + onboard.SIGNUP_LINK_PATH
    assert req.get_header("X-internal-key") == "internal-test"
    assert req.get_header("X-caller-type") == "platform"


def test_a_typed_discord_id_is_never_used(host):
    """MUTATION: read the Discord id from a modal field instead of the interaction."""
    h, calls = host
    _finish(_submit(h, {"confirm": "I ACCEPT", "discord_user_id": OTHER, "user": OTHER}))
    assert json.loads(calls[0].data.decode()) == {"discord_user_id": MEMBER}


def test_missing_internal_key_fails_closed(host, monkeypatch):
    """MUTATION: call Genesis without the service identity, or hand out a URL anyway."""
    h, calls = host
    monkeypatch.delenv("AITHER_INTERNAL_SECRET")
    text = _finish(_submit(h, {"confirm": "I ACCEPT"}))
    assert text.startswith("No link was made") and "https://" not in text
    assert calls == []


def test_plain_http_genesis_is_refused(host, monkeypatch):
    """MUTATION: allow an http:// Genesis (the fleet speaks TLS; http hangs or leaks)."""
    h, calls = host
    monkeypatch.setenv("AWDISCORD_GENESIS_URL", "http://genesis.test:8001")
    text = _finish(_submit(h, {"confirm": "I ACCEPT"}))
    assert text.startswith("No link was made") and calls == []


def test_genesis_refusal_hands_out_no_url(host, monkeypatch):
    """MUTATION: treat a non-2xx (e.g. 503 no signing key) as success."""
    import urllib.error
    h, _ = host

    def refuse(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 503, "no key", {}, None)
    monkeypatch.setattr(onboard, "OPENER", [refuse])
    text = _finish(_submit(h, {"confirm": "I ACCEPT"}))
    assert "HTTP 503" in text and "discord_signup" not in text


def test_token_wording_matches_the_ledger_side():
    """MUTATION: let the bot's token wording drift from lib/economy/OnboardGrant."""
    src = (PKG.parents[1] / "lib" / "economy" / "OnboardGrant.py").read_text(encoding="utf-8")
    first, second = onboard.TOKEN_WORDING.split(". ", 1)
    assert f'"{first}. "' in src and f'"{second}"' in src
