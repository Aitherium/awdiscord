"""The gateway hands plugins the invoker's guild roles and the guild's name.

discord.py turns the interaction into objects; the gateway rebuilds the raw dict.
Role-aware plugins need ``member.roles`` (id strings, as Discord sends them) and
``member.role_names``; ``@everyone`` carries no information and is left out.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord.gateway import _raw_interaction  # noqa: E402


def _role(rid, name, default=False):
    return SimpleNamespace(id=rid, name=name, is_default=lambda: default)


def _interaction(guild_id=10, roles=(), guild_name="Test Guild"):
    user = SimpleNamespace(id=42, roles=list(roles))
    return SimpleNamespace(
        id=1, token="t", type=SimpleNamespace(value=2), data={"name": "whoami"},
        channel_id=5, guild_id=guild_id, user=user, permissions=SimpleNamespace(value=32),
        guild=SimpleNamespace(name=guild_name) if guild_id else None)


def test_roles_and_names_survive_the_rebuild():
    raw = _raw_interaction(_interaction(roles=[
        _role(10, "@everyone", default=True), _role(77, "Builders"), _role(78, "Founders")]))
    member = raw["member"]
    assert member["roles"] == ["77", "78"]
    assert member["role_names"] == ["Builders", "Founders"]
    assert member["permissions"] == "32"
    assert member["user"] == {"id": "42"}
    assert raw["guild_name"] == "Test Guild"


def test_member_without_roles_is_an_empty_list():
    raw = _raw_interaction(_interaction(roles=[]))
    assert raw["member"]["roles"] == [] and raw["member"]["role_names"] == []


def test_dm_has_no_member_block():
    raw = _raw_interaction(_interaction(guild_id=None))
    assert "member" not in raw and raw["user"] == {"id": "42"}
    assert "guild_name" not in raw


def test_the_invokers_display_name_survives_the_rebuild():
    """MUTATION: rebuild the user as {"id"} only (every filing reads "by a member")."""
    it = _interaction(roles=[])
    it.user.name = "ana_k"
    it.user.global_name = "Ana"
    raw = _raw_interaction(it)
    assert raw["member"]["user"] == {"id": "42", "username": "ana_k", "global_name": "Ana"}
