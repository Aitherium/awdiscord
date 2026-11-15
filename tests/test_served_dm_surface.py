"""wb-8: a Discord DM is the owner's private surface only when an OWNER sent it.

Before this, ``surface_for`` returned ``dm`` for every guild-less message, and Genesis
treats ``dm`` as private, so any DM sender drew on owner-private memory.
"""
from __future__ import annotations

from awdiscord.agent import GenesisAgent
from awdiscord.served import DM_MEMBER_SURFACE, surface_for


def test_owner_dm_is_private(monkeypatch):
    monkeypatch.setenv("AWDISCORD_OWNERS", "111,222")
    monkeypatch.delenv("AWDISCORD_OPERATORS", raising=False)
    assert surface_for("", "111") == "dm"
    assert surface_for(None, "222") == "dm"


def test_non_owner_dm_is_not_private(monkeypatch):
    monkeypatch.setenv("AWDISCORD_OWNERS", "111")
    assert surface_for("", "999") == DM_MEMBER_SURFACE
    assert surface_for("", "") == DM_MEMBER_SURFACE


def test_no_owner_configured_means_no_private_dm(monkeypatch):
    monkeypatch.delenv("AWDISCORD_OWNERS", raising=False)
    monkeypatch.delenv("AWDISCORD_OPERATORS", raising=False)
    assert surface_for("", "111") == DM_MEMBER_SURFACE


def test_operators_are_owners_when_owners_unset(monkeypatch):
    monkeypatch.delenv("AWDISCORD_OWNERS", raising=False)
    monkeypatch.setenv("AWDISCORD_OPERATORS", "333")
    assert surface_for("", "333") == "dm"


def test_genesis_agent_stamps_the_author_aware_surface(monkeypatch):
    monkeypatch.setenv("AWDISCORD_OWNERS", "111")
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"response": "ok"}'

    def fake_urlopen(req, *a, **kw):
        import json
        sent.update(json.loads(req.data))
        return _Resp()

    import awdiscord.agent as agent_mod
    monkeypatch.setattr(agent_mod.urllib.request, "urlopen", fake_urlopen)
    GenesisAgent("https://genesis.invalid").ask("hi", user="999")
    assert sent["context"]["surface"] == DM_MEMBER_SURFACE
    GenesisAgent("https://genesis.invalid").ask("hi", user="111")
    assert sent["context"]["surface"] == "dm"
