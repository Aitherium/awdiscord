"""S1: one predicate -- ``(guild, user) -> Brain`` -- governs every model call.

The resolver replaces the old ``agent_for(user)``: the owner's model is reachable
ONLY for a guild the allowlist serves (or an owner's DM). An unserved guild gets
``none`` everywhere -- chat, /ask, the plugins' ``Ctx.agent``, /share -- and the
model spy is asserted NOT to have been called (never "it returned a refusal").
"""
from __future__ import annotations

import pytest
from awdiscord.brain import (
    BYO,
    NONE,
    OWNER,
    Brain,
    BrainResolver,
    DenyResolver,
    install,
    resolve_brain,
)
from awdiscord.dispatch import Dispatcher
from awdiscord.plugins import Command, Plugin, PluginHost
from awdiscord.served import SETUP_SHORT, SETUP_TEXT, ServedGuilds
from awdiscord.store import GuildStore

HOME = "111111111111111111"
OTHER = "222222222222222222"
SERVED_ALSO = "333333333333333333"
USER = "42"


class SpyAgent:
    """A model stub: every call is recorded, so "not called" is assertable."""

    name = "spy"
    sees_images = False

    def __init__(self, answer="answered"):
        self.calls = []
        self._answer = answer

    def ask(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self._answer

    def health(self):
        return "ok"


def _ask(guild=OTHER, user=USER, question="hi"):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}},
            "data": {"name": "ask", "options": [
                {"name": "question", "value": question}]}}


def _served(*guilds):
    return ServedGuilds(home=HOME, guilds=guilds)


# -- resolution ------------------------------------------------------------------
def test_a_served_guild_resolves_to_the_owner():
    spy = SpyAgent()
    brains = BrainResolver(spy, served=_served(OTHER))
    brain = brains.resolve(OTHER, USER)
    assert brain.mode == OWNER and brain.agent is spy
    assert brains.resolve(HOME, USER).agent is spy


def test_an_unserved_guild_is_none_and_the_model_is_never_called(tmp_path):
    spy = SpyAgent()
    d = Dispatcher(spy, served=_served(OTHER), brains=BrainResolver(
        spy, served=_served(OTHER), store=GuildStore(tmp_path / "guilds.db")))
    assert d.chat(USER, SERVED_ALSO, "hello")["content"] == SETUP_TEXT
    result = d.handle(_ask(guild=SERVED_ALSO))
    assert result.response["data"]["content"] in (SETUP_TEXT, SETUP_SHORT)
    assert result.deferred is None
    assert spy.calls == []          # the negative path


def test_dms_are_served_only_to_an_owner():
    spy, fleet = SpyAgent(), SpyAgent("fleet")
    brains = BrainResolver(spy, owner_agent=fleet, owners=[USER],
                           served=_served(OTHER))
    assert brains.resolve(None, USER).agent is fleet
    assert brains.resolve(None, "7").mode == NONE
    assert brains.resolve(None, "7").agent is None


def test_a_stored_off_row_falls_back_to_the_default(tmp_path):
    """A row an older build wrote as "off" no longer parks the guild forever."""
    spy = SpyAgent()
    store = GuildStore(tmp_path / "guilds.db")
    store.set(HOME, "brain", "mode", "off")
    brains = BrainResolver(spy, served=_served(), store=store)
    brain = brains.resolve(HOME, USER)
    assert brain.mode == OWNER and brain.agent is spy


# -- an empty allowlist serves NOBODY, not everybody ------------------------------
def test_an_empty_allowlist_denies_every_guild(monkeypatch):
    """Missing config is not a standalone opt-in (the fail-open this guards)."""
    for name in ("AITHER_DISCORD_HOME_GUILD_ID", "AWDISCORD_HOME_GUILD_ID",
                 "AWDISCORD_SERVED_GUILDS"):
        monkeypatch.delenv(name, raising=False)
    served = ServedGuilds.from_env()
    assert served.guilds == frozenset()
    env_agent = SpyAgent()
    brains = BrainResolver(env_agent, served=served, legacy_agent=env_agent)
    assert brains.hosted is True          # strict SSRF policy, not permissive
    brain = brains.resolve(OTHER, USER)
    assert brain.mode == NONE and brain.agent is None
    assert brain.reason != "legacy_env"


def test_a_served_bot_is_hosted_even_with_an_empty_allowlist():
    env_agent = SpyAgent()
    brains = BrainResolver(env_agent, served=ServedGuilds(),
                           legacy_agent=env_agent)
    assert brains.hosted is True
    assert brains.resolve(OTHER, USER).mode == NONE


# -- plugins cannot bypass the resolver -------------------------------------------
def _probe_plugin() -> Plugin:
    def handler(ctx):
        return ctx.reply("agent:" + (getattr(ctx.agent, "name", "") or "none"))

    return Plugin(name="brainprobe", version="1", description="probe",
                  default_enabled=True,
                  commands=[Command(name="brainprobe", description="probe",
                                    handler=handler)])


def _probe_interaction(guild=OTHER, user=USER):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}},
            "data": {"name": "brainprobe"}}


def test_plugin_ctx_agent_is_none_on_an_unserved_server(tmp_path):
    spy = SpyAgent()
    store = GuildStore(tmp_path / "guilds.db")
    resolver = BrainResolver(spy, served=_served())
    host = PluginHost(store, agent=spy, resolver=resolver)
    host.add(_probe_plugin())
    assert host.handle(_probe_interaction()).response["data"]["content"] == "agent:none"


def test_plugin_ctx_agent_is_the_served_brain(tmp_path):
    spy = SpyAgent()
    store = GuildStore(tmp_path / "guilds.db")
    host = PluginHost(store, agent=spy, resolver=BrainResolver(spy, served=_served()))
    host.add(_probe_plugin())
    got = host.handle(_probe_interaction(guild=HOME)).response["data"]["content"]
    assert got == "agent:spy"


def test_plugin_ctx_agent_is_none_when_the_resolver_raises(tmp_path):
    class Broken:
        def resolve(self, guild, user):
            raise RuntimeError("boom")

    host = PluginHost(GuildStore(tmp_path / "guilds.db"),
                      agent=SpyAgent(), resolver=Broken())
    host.add(_probe_plugin())
    assert host.handle(_probe_interaction()).response["data"]["content"] == "agent:none"


# -- /share cannot cross servers ---------------------------------------------------
def test_share_in_another_server_is_refused():
    spy = SpyAgent()
    d = Dispatcher(spy, served=_served(OTHER), brains=BrainResolver(
        spy, served=_served(OTHER)))
    assert d.chat(USER, HOME, "hello")["content"] == "answered"
    share = {"type": 2, "id": "2", "token": "t", "guild_id": OTHER,
             "member": {"user": {"id": USER}}, "data": {"name": "share"}}
    result = d.handle(share)
    assert "another server" in result.response["data"]["content"]
    assert result.log.get("refused") == "cross_guild"


def test_share_in_the_same_server_posts_the_answer():
    spy = SpyAgent()
    d = Dispatcher(spy, served=_served(OTHER), brains=BrainResolver(
        spy, served=_served(OTHER)))
    d.chat(USER, HOME, "hello")
    share = {"type": 2, "id": "2", "token": "t", "guild_id": HOME,
             "member": {"user": {"id": USER}}, "data": {"name": "share"}}
    result = d.handle(share)
    assert "answered" in result.response["data"]["content"]
    assert not result.response["data"].get("flags")


# -- the image seer is the guild's brain, never the owner's ------------------------
class SeeingAgent(SpyAgent):
    sees_images = True


def test_a_byo_guild_sees_images_with_its_own_model(tmp_path):
    from awdiscord.media import Media

    owner = SpyAgent()                 # cannot see, and must never be asked
    byo = SeeingAgent("byo answer")
    store = GuildStore(tmp_path / "guilds.db")
    store.set(OTHER, "brain", "mode", "byo")
    store.set(OTHER, "brain", "byo", {"base_url": "http://127.0.0.1:9999/v1",
                                      "model": "m", "vision_models": None})
    from awdiscord.brainsecrets import BrainSecrets
    secrets = BrainSecrets(tmp_path)
    secrets.put(OTHER, "sk-local")
    resolver = BrainResolver(owner, served=None, hosted=False, store=store,
                             secrets=secrets,
                             agent_factory=lambda **kw: byo)
    d = Dispatcher(owner, brains=resolver)
    d.media_reader = lambda atts: Media(images=["data:image/jpeg;base64,AA"],
                                        notes=["a photo"])
    out = d.chat(USER, OTHER, "what is this?", attachments=[{"filename": "a.jpg"}])
    assert out["content"] == "byo answer"
    assert byo.calls and byo.calls[0][1].get("images")
    assert owner.calls == []


# -- fail closed -------------------------------------------------------------------
def test_fail_closed_when_the_resolver_cannot_be_built(monkeypatch):
    import awdiscord.brain as brain_mod

    class Exploding:
        def __init__(self, *a, **kw):
            raise RuntimeError("no store")

    monkeypatch.setattr(brain_mod, "BrainResolver", Exploding)
    spy = SpyAgent()
    d = Dispatcher(spy, served=_served(OTHER))
    assert isinstance(d.brains, DenyResolver)
    assert d.chat(USER, OTHER, "hello")["content"] == SETUP_TEXT
    assert spy.calls == []


def test_resolve_brain_with_nothing_installed_is_none():
    install(None)
    brain = resolve_brain(OTHER, USER)
    assert brain.mode == NONE and brain.agent is None
    assert brain.reason == "resolver_unavailable"


def test_a_resolver_that_raises_is_none_not_the_owner():
    class Broken:
        def resolve(self, guild, user):
            raise RuntimeError("boom")

    d = Dispatcher(SpyAgent(), brains=Broken(), served=_served())
    result = d.handle(_ask(guild=OTHER))
    assert result.response["data"]["content"] == SETUP_TEXT


# -- legacy standalone keeps answering ---------------------------------------------
def test_a_standalone_bot_with_no_brain_row_keeps_its_env_agent(tmp_path):
    env_agent = SpyAgent("legacy answer")
    brains = BrainResolver(env_agent, served=None, hosted=False, store=None,
                           legacy_agent=env_agent)
    brain = brains.resolve(OTHER, USER)
    assert brain.mode == BYO and brain.agent is env_agent
    assert brain.reason == "legacy_env"

    d = Dispatcher(env_agent, brains=brains)
    assert d.chat(USER, OTHER, "hello")["content"] == "legacy answer"
    assert env_agent.calls


def test_a_hosted_bot_does_not_use_the_legacy_path():
    spy = SpyAgent()
    brains = BrainResolver(spy, served=ServedGuilds(home=HOME), hosted=True,
                           legacy_agent=spy)
    brain = brains.resolve(OTHER, USER)
    assert brain.mode == NONE and brain.agent is None


def test_brain_handles_an_empty_guild_id():
    spy = SpyAgent()
    brains = BrainResolver(spy, served=_served())
    assert brains.resolve("", USER).mode == NONE
    assert brains.resolve(None, "").mode == NONE


def test_brain_is_frozen():
    brain = Brain(OWNER, object(), endpoint="https://api.example.com/v1")
    with pytest.raises(Exception):
        brain.mode = NONE
    assert brain.host() == "api.example.com"
