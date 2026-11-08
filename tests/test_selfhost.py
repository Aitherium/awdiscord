"""S7: /selfhost walks a server admin from no model to this server's own.

The wizard is a set of static cards: it prints commands the ADMIN runs on their
own machine and never asks a model. These tests pin the three properties that
make that safe -- an unserved server can run it, a non-admin cannot, and no model
stub is ever called -- plus the content contract: every command a card prints is
in ``VERIFIED_COMMANDS`` (which deliberately has no skills install command).
"""
from __future__ import annotations

import re

from awdiscord.brain import BRAIN_MODAL_ID, BrainResolver
from awdiscord.brainsecrets import BrainSecrets
from awdiscord.dispatch import (
    EPHEMERAL,
    MESSAGE,
    MODAL,
    UPDATE,
    Dispatcher,
    commands,
)
from awdiscord.selfhost import LAST, PREFIX, STEPS, TIERS, VERIFIED_COMMANDS
from awdiscord.served import SETUP_TEXT, ServedGuilds
from awdiscord.store import GuildStore

GUILD = "111111111111111111"
OTHER = "222222222222222222"
ADMIN, MEMBER = "42", "7"
MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3


class SpyAgent:
    """A model stub. Every call is recorded, so "not called" is assertable."""

    name = "spy"
    sees_images = False

    def __init__(self):
        self.calls = []

    def ask(self, prompt, **kwargs):
        self.calls.append(prompt)
        return "owner answer"

    def health(self):
        return "ok"


def _world(tmp_path, *, served=None):
    spy = SpyAgent()
    served = served if served is not None else ServedGuilds()
    store = GuildStore(tmp_path / "guilds.db")
    brains = BrainResolver(spy, served=served, store=store,
                           secrets=BrainSecrets(tmp_path))
    return Dispatcher(spy, served=served, brains=brains), spy


def _cmd(*, guild=GUILD, user=ADMIN, permissions=MANAGE_GUILD):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}, "permissions": str(permissions)},
            "data": {"name": "selfhost"}}


def _press(action, *, guild=GUILD, user=ADMIN, permissions=MANAGE_GUILD):
    return {"type": 3, "id": "2", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}, "permissions": str(permissions)},
            "data": {"custom_id": f"{PREFIX}{action}", "components": []}}


def _content(result) -> str:
    return result.response["data"]["content"]


_BACKTICK = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"```\n(.*?)\n```", re.S)


def _printed_commands(content: str) -> set[str]:
    found = set(_BACKTICK.findall(content))
    for block in _FENCE.findall(content):
        found.update(line.strip() for line in block.splitlines() if line.strip())
    return found


# -- registration and the none-gate -------------------------------------------------
def test_selfhost_is_a_registered_server_command():
    defs = {c["name"]: c for c in commands()}
    assert "selfhost" in defs
    assert defs["selfhost"]["contexts"] == [0]      # a server, never a DM


def test_an_unserved_guild_can_run_the_wizard_and_never_calls_a_model(tmp_path):
    d, spy = _world(tmp_path)
    assert d.served.serves_guild(GUILD) is False
    assert d.agent_for(GUILD, MEMBER) is None

    first = d.handle(_cmd())
    assert first.response["type"] == MESSAGE
    assert first.response["data"]["flags"] == EPHEMERAL
    assert STEPS[0]["title"] in _content(first)

    for action in ("tier:nano", "next", "next", "back", "next", "next", "next"):
        d.handle(_press(action))
    assert d.wizard.state(GUILD)["step"] == LAST
    assert spy.calls == []


def test_a_non_admin_is_refused_and_the_step_does_not_move(tmp_path):
    d, spy = _world(tmp_path)
    refused = d.handle(_cmd(user=MEMBER, permissions=0))
    assert refused.response["type"] == MESSAGE
    assert "admin" in _content(refused).lower()
    assert d.wizard.state(GUILD)["step"] == 0

    pressed = d.handle(_press("next", user=MEMBER, permissions=0))
    assert pressed.response["type"] == MESSAGE       # never an UPDATE
    assert "admin" in _content(pressed).lower()
    assert d.wizard.state(GUILD)["step"] == 0
    assert spy.calls == []


def test_administrator_also_counts(tmp_path):
    d, _spy = _world(tmp_path)
    assert "Step 1" in _content(d.handle(_cmd(permissions=ADMINISTRATOR)))


def test_a_dm_is_refused(tmp_path):
    d, _spy = _world(tmp_path)
    it = _cmd()
    it.pop("guild_id")
    refused = d.handle(it)
    assert "inside your server" in _content(refused)


def test_without_a_per_server_store_it_says_so():
    d = Dispatcher(SpyAgent())         # no resolver, no store
    reply = d.handle(_cmd())
    assert reply.response["data"]["flags"] == EPHEMERAL
    assert "per-server store" in _content(reply)


# -- the cards -----------------------------------------------------------------------
def test_each_step_renders(tmp_path):
    d, _spy = _world(tmp_path)
    for step, spec in enumerate(STEPS):
        d.wizard.save(GUILD, {"step": step})
        card = d.handle(_cmd())
        content = _content(card)
        assert spec["title"] in content
        assert card.response["data"]["components"]
        for command in spec["commands"]:
            assert command in content


def test_next_and_back_move_the_saved_step(tmp_path):
    d, _spy = _world(tmp_path)
    d.handle(_cmd())
    forward = d.handle(_press("next"))
    assert forward.response["type"] == UPDATE
    assert STEPS[1]["title"] in _content(forward)
    assert d.wizard.state(GUILD)["step"] == 1

    back = d.handle(_press("back"))
    assert STEPS[0]["title"] in _content(back)
    assert d.wizard.state(GUILD)["step"] == 0

    d.handle(_press("back"))           # already first: stays, never goes negative
    assert d.wizard.state(GUILD)["step"] == 0


def test_the_state_is_per_server(tmp_path):
    d, _spy = _world(tmp_path)
    d.handle(_press("next"))
    d.handle(_press("next"))
    assert d.wizard.state(GUILD)["step"] == 2
    assert d.wizard.state(OTHER)["step"] == 0
    assert STEPS[0]["title"] in _content(d.handle(_cmd(guild=OTHER)))


def test_a_tier_choice_is_remembered_and_advances(tmp_path):
    d, _spy = _world(tmp_path)
    d.handle(_cmd())
    picked = d.handle(_press("tier:nano"))
    assert d.wizard.state(GUILD)["tier"] == "nano"
    assert d.wizard.state(GUILD)["step"] == 1
    assert TIERS["nano"] in _content(picked)


def test_an_existing_model_server_skips_to_the_public_url_step(tmp_path):
    d, _spy = _world(tmp_path)
    d.handle(_cmd())
    jumped = d.handle(_press("existing"))
    assert d.wizard.state(GUILD)["existing"] is True
    assert d.wizard.state(GUILD)["step"] == 3
    assert "public https" in _content(jumped)


def test_every_printed_command_is_in_the_verified_set(tmp_path):
    d, _spy = _world(tmp_path)
    printed: set[str] = set()
    cards: list[str] = []
    for step in range(len(STEPS)):
        d.wizard.save(GUILD, {"step": step})
        content = _content(d.handle(_cmd()))
        cards.append(content)
        printed |= _printed_commands(content)
    assert printed, "the wizard printed no command at all"
    assert printed <= VERIFIED_COMMANDS, sorted(printed - VERIFIED_COMMANDS)

    everything = "\n".join(cards)
    assert "pip install awskills" not in everything
    # The skills are cloned, never installed: the only awskills URL is GitHub.
    assert "github.com/Aitherium/awskills" in STEPS[-1]["body"]


def test_an_unserved_finish_step_offers_no_model_form(tmp_path):
    """On the public bot an unserved server's own model is refused (byo is for
    served guilds only), so the wizard never offers the form there."""
    d, _spy = _world(tmp_path)
    d.wizard.save(GUILD, {"step": 4})
    pressed = d.handle(_press("model"))
    assert pressed.response["type"] != MODAL
    ids = [c.get("custom_id") for row in pressed.response["data"]["components"]
           for c in row["components"]]
    assert PREFIX + "model" not in ids


def test_the_finish_step_points_at_model_setup_and_opens_its_form(tmp_path):
    d, _spy = _world(tmp_path, served=ServedGuilds(home=GUILD))
    d.wizard.save(GUILD, {"step": 4})
    card = d.handle(_cmd())
    assert "/model setup" in _content(card)

    opened = d.handle(_press("model"))
    assert opened.response["type"] == MODAL
    assert opened.response["data"]["custom_id"] == BRAIN_MODAL_ID
    fields = [row["components"][0]["custom_id"]
              for row in opened.response["data"]["components"]]
    assert fields == ["endpoint", "model", "api_key", "vision"]


def test_a_foreign_component_still_gets_the_served_refusal(tmp_path):
    d, _spy = _world(tmp_path)
    it = _press("next")
    it["data"]["custom_id"] = "awp:someplugin:x"
    reply = d.handle(it)
    assert SETUP_TEXT in _content(reply)
    assert d.wizard.state(GUILD)["step"] == 0
