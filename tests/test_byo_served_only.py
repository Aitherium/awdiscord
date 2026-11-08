"""On the public bot, a server's own model (byo/hosted) is for SERVED guilds only.

Without this gate any admin in any guild could run /model with their own endpoint;
`byo` then skipped the served-guild allowlist, so the guild got model turns whose
plugin tools make platform-keyed calls (X-Internal-Key) from a stranger's server.
The allowlist (ServedGuilds) is now the one answer for every model mode, fail
closed. A standalone bot (hosted=False) is the server owner's own and unchanged.
"""
from __future__ import annotations

import urllib.request

import pytest
from awdiscord.brain import BRAIN_MODAL_ID, BYO, HOSTED, NONE, BrainResolver
from awdiscord.brainsecrets import BrainSecrets
from awdiscord.dispatch import Dispatcher
from awdiscord.served import ServedGuilds
from awdiscord.store import GuildStore

SERVED = "111111111111111111"
STRANGER = "222222222222222222"
ADMIN, MEMBER = "42", "7"
MANAGE_GUILD = 1 << 5
ENDPOINT = "https://models.example.com/v1"


class SpyAgent:
    name = "spy"
    sees_images = False

    def __init__(self, answer="owner answer"):
        self.calls = []
        self.answer = answer

    def ask(self, prompt, **kwargs):
        self.calls.append(prompt)
        return self.answer

    def health(self):
        return "ok"


class _NoHostedLookup:
    """A Genesis client that must never be asked about an unserved guild."""

    def __init__(self):
        self.lookups = []

    def lookup(self, guild):
        self.lookups.append(guild)
        return {"mode": "hosted", "status": "active"}


def _public_dns(host, port):
    return ["93.184.216.34"]


class _Rig:
    """The public bot's resolver (hosted=True, a served gate), network stubbed."""

    def __init__(self, tmp_path):
        self.owner = SpyAgent()
        self.byo_agent = SpyAgent("from own model")
        self.probes: list[str] = []
        self.hosted_client = _NoHostedLookup()

        def probe(base_url, **kwargs):
            self.probes.append(base_url)
            return {"ok": True, "status": 200, "latency_ms": 1, "detail": "ok"}

        def factory(**kwargs):
            return self.byo_agent

        self.store = GuildStore(tmp_path / "guilds.db")
        self.secrets = BrainSecrets(tmp_path)
        self.brains = BrainResolver(
            self.owner, served=ServedGuilds(home=SERVED), store=self.store,
            secrets=self.secrets, resolver=_public_dns, probe=probe,
            agent_factory=factory, hosted_client=self.hosted_client)
        assert self.brains.hosted is True
        self.d = Dispatcher(self.owner, brains=self.brains,
                            served=ServedGuilds(home=SERVED))

    def plant_row(self, guild, mode=BYO):
        """A row written before the gate existed (or by a buggy path)."""
        self.store.set(guild, "brain", "mode", mode)
        self.store.set(guild, "brain", "byo", {"base_url": ENDPOINT, "model": "m"})
        self.secrets.put(guild, "sk-stranger-test")


def _command(action, guild):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": ADMIN}, "permissions": str(MANAGE_GUILD)},
            "data": {"name": "model",
                     "options": [{"name": "action", "value": action}]}}


def _modal(guild):
    return {"type": 5, "id": "2", "token": "t", "guild_id": guild,
            "member": {"user": {"id": ADMIN}, "permissions": str(MANAGE_GUILD)},
            "data": {"custom_id": BRAIN_MODAL_ID, "components": [{"type": 1, "components": [
                {"type": 4, "custom_id": "endpoint", "value": ENDPOINT},
                {"type": 4, "custom_id": "model", "value": "own-model"},
                {"type": 4, "custom_id": "api_key", "value": "sk-stranger-test"},
                {"type": 4, "custom_id": "vision", "value": ""},
            ]}]}}


@pytest.fixture
def keyed_calls(monkeypatch):
    """Every outbound urllib request that carries the platform key."""
    monkeypatch.setenv("AITHER_INTERNAL_SECRET", "platform-key-for-test")
    seen: list[str] = []
    real = urllib.request.urlopen

    def spy(req, *a, **kw):
        headers = {k.lower() for k in getattr(req, "headers", {}) or {}}
        if "x-internal-key" in headers:
            seen.append(getattr(req, "full_url", str(req)))
            raise OSError("platform-keyed call refused in test")
        return real(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    return seen


# -- unserved guild ------------------------------------------------------------------
@pytest.mark.parametrize("action", ["setup", "test", "status"])
def test_model_is_refused_in_an_unserved_guild(tmp_path, action):
    rig = _Rig(tmp_path)
    out = rig.d.handle(_command(action, STRANGER))
    assert out.response["type"] == 4                     # a reply, never a modal
    assert "not available here" in out.response["data"]["content"]
    assert out.log.get("refused") == "unserved"
    assert out.deferred is None
    assert rig.probes == []


def test_the_setup_modal_is_refused_and_nothing_is_saved(tmp_path):
    rig = _Rig(tmp_path)
    out = rig.d.handle(_modal(STRANGER))
    assert out.deferred is None
    assert out.log.get("refused") == "unserved"
    assert rig.brains.mode(STRANGER) == ""
    assert rig.secrets.get(STRANGER) in ("", None)
    assert rig.probes == []


def test_save_byo_itself_refuses_an_unserved_guild(tmp_path):
    rig = _Rig(tmp_path)
    from awdiscord.brain import BrainError
    with pytest.raises(BrainError):
        rig.brains.save_byo(STRANGER, base_url=ENDPOINT, model="m",
                            api_key="sk-stranger-test", vision_models=None)
    assert rig.probes == []


def test_a_planted_byo_row_never_reaches_a_model_or_the_platform_key(tmp_path,
                                                                     keyed_calls):
    rig = _Rig(tmp_path)
    rig.plant_row(STRANGER, BYO)
    brain = rig.brains.resolve(STRANGER, MEMBER)
    assert brain.mode == NONE and brain.reason == "byo_unserved"
    assert brain.agent is None
    rig.d.chat(MEMBER, STRANGER, "use a tool that calls the platform")
    assert rig.byo_agent.calls == [] and rig.owner.calls == []
    assert keyed_calls == []
    assert rig.brains.test(STRANGER)["ok"] is False
    assert rig.probes == []


def test_hosted_stays_on_the_genesis_binding_not_the_env_allowlist(tmp_path,
                                                                   monkeypatch):
    """A paid guild is not an owner-inference ServedGuilds entry: Genesis decides."""
    monkeypatch.setenv("AWDISCORD_HOSTED", "1")
    rig = _Rig(tmp_path)
    rig.plant_row(STRANGER, HOSTED)
    rig.brains.resolve(STRANGER, MEMBER)
    assert rig.hosted_client.lookups == [STRANGER]


def test_off_still_works_in_an_unserved_guild(tmp_path):
    rig = _Rig(tmp_path)
    rig.plant_row(STRANGER, BYO)
    out = rig.d.handle(_command("off", STRANGER))
    assert out.log.get("action") == "off"
    assert rig.brains.mode(STRANGER) == ""
    assert not rig.secrets.get(STRANGER)


def test_a_bot_with_no_allowlist_fails_closed(tmp_path):
    brains = BrainResolver(SpyAgent(), owner_agent=SpyAgent(), served=None,
                           store=GuildStore(tmp_path / "g.db"),
                           secrets=BrainSecrets(tmp_path))
    assert brains.hosted is True
    assert brains.own_model_allowed(SERVED) is False


def test_a_raising_allowlist_fails_closed(tmp_path):
    class Broken(ServedGuilds):
        def serves_guild(self, guild_id):
            raise RuntimeError("boom")

    brains = BrainResolver(SpyAgent(), served=Broken(home=SERVED),
                           store=GuildStore(tmp_path / "g.db"),
                           secrets=BrainSecrets(tmp_path))
    assert brains.own_model_allowed(SERVED) is False


# -- served guild: unchanged ---------------------------------------------------------
def test_a_served_guild_still_configures_and_answers_with_its_own_model(tmp_path):
    rig = _Rig(tmp_path)
    opened = rig.d.handle(_command("setup", SERVED))
    assert opened.response["type"] == 9
    saved = rig.d.handle(_modal(SERVED)).deferred()
    assert "now answers with `own-model`" in saved["content"]
    assert rig.probes == [ENDPOINT]
    assert rig.brains.mode(SERVED) == BYO
    assert rig.brains.resolve(SERVED, MEMBER).mode == BYO
    out = rig.d.chat(MEMBER, SERVED, "hello")
    assert out["content"] == "from own model"


def test_a_standalone_bot_is_unchanged(tmp_path):
    brains = BrainResolver(SpyAgent(), served=ServedGuilds(home=SERVED), hosted=False,
                           store=GuildStore(tmp_path / "g.db"),
                           secrets=BrainSecrets(tmp_path))
    assert brains.own_model_allowed(STRANGER) is True
