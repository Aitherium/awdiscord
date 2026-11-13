"""The commands that answer BEFORE a server is served.

An unserved server must never reach a model, but it must be able to CLAIM: the
connect wizard (`/aitherium setup`), the account link (`/link`) and the pack's
buttons are how a stranger's server becomes served. Every other command stays
behind the none-gate, and none of these may reach the agent.
"""
from __future__ import annotations

from awdiscord.brain import NONE
from awdiscord.dispatch import (
    CLAIM_CMD,
    CLAIM_PREFIX,
    LINK_CMD,
    Dispatcher,
    commands,
)
from awdiscord.served import SETUP_TEXT, ServedGuilds

HOME = "111111111111111111"
OTHER = "222222222222222222"


class SpyAgent:
    name = "spy"

    def __init__(self):
        self.calls = []

    def ask(self, prompt, **kwargs):
        self.calls.append(prompt)
        return "answered"

    def health(self):
        return "ok"


class FakeHost:
    """A plugin host that records routes instead of answering them."""

    def __init__(self, owned=()):
        self._owned = set(owned)
        self.command_calls = []
        self.button_calls = []
        self.limiter = None
        self.desk = None

    def owns(self, name):
        return name in self._owned

    def handle(self, it):
        self.command_calls.append(it)
        return {"route": "command"}

    def handle_button(self, it):
        self.button_calls.append(it)
        return {"route": "button"}

    def command_defs(self):
        return []

    def persona_for(self, guild):
        return ""


def _command(name, options=None, guild=OTHER, user="42"):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}},
            "data": {"name": name, "options": options or []}}


def _button(custom_id, guild=OTHER, user="42"):
    return {"type": 3, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}},
            "data": {"custom_id": custom_id}}


def _dispatcher(host, agent=None, guilds=()):
    return Dispatcher(agent or SpyAgent(), host=host,
                      served=ServedGuilds(home=HOME, guilds=guilds))


def _sub(name):
    return {"name": name, "type": 1, "options": []}


# -- /aitherium (the claim wizard) --------------------------------------------------
def test_the_claim_wizard_answers_an_unserved_server_without_a_model():
    host = FakeHost(owned={CLAIM_CMD})
    spy = SpyAgent()
    d = _dispatcher(host, spy)
    result = d.handle(_command(CLAIM_CMD, [_sub("setup")]))
    assert result == {"route": "command"}
    assert len(host.command_calls) == 1
    assert spy.calls == []


def test_claim_and_status_are_accepted_subcommands_too():
    host = FakeHost(owned={CLAIM_CMD})
    d = _dispatcher(host)
    for sub in ("claim", "status"):
        assert d.handle(_command(CLAIM_CMD, [_sub(sub)])) == {"route": "command"}
    assert len(host.command_calls) == 2


def test_an_unknown_subcommand_stays_behind_the_none_gate():
    host = FakeHost(owned={CLAIM_CMD})
    d = _dispatcher(host)
    result = d.handle(_command(CLAIM_CMD, [_sub("delete-everything")]))
    assert result.response["data"]["content"] == SETUP_TEXT
    assert host.command_calls == []


def test_the_wizard_says_so_when_the_pack_is_not_loaded():
    d = _dispatcher(FakeHost(owned=set()))
    result = d.handle(_command(CLAIM_CMD, [_sub("setup")]))
    assert "isn't installed" in result.response["data"]["content"]


# -- /link -----------------------------------------------------------------------------
def test_link_answers_an_unserved_server():
    host = FakeHost(owned={LINK_CMD})
    spy = SpyAgent()
    d = _dispatcher(host, spy)
    assert d.handle(_command(LINK_CMD)) == {"route": "command"}
    assert len(host.command_calls) == 1
    assert spy.calls == []


def test_link_with_no_plugin_says_unknown_rather_than_claiming_success():
    d = _dispatcher(FakeHost(owned=set()))
    result = d.handle(_command(LINK_CMD))
    assert result.response["data"]["content"] == "Unknown command."


# -- the claim pack's buttons ----------------------------------------------------------
def test_a_claim_button_answers_an_unserved_server():
    host = FakeHost(owned={CLAIM_CMD})
    spy = SpyAgent()
    d = _dispatcher(host, spy)
    cid = f"{CLAIM_PREFIX}connect:123"
    assert d.handle(_button(cid)) == {"route": "button"}
    assert len(host.button_calls) == 1
    assert spy.calls == []


def test_another_plugins_button_stays_behind_the_none_gate():
    host = FakeHost(owned={"aitherium_connect"})
    d = _dispatcher(host)
    result = d.handle(_button("awp:something_else:do"))
    assert result.response["data"]["content"] == SETUP_TEXT
    assert host.button_calls == []


# -- everything else --------------------------------------------------------------------
def test_ask_in_an_unserved_server_is_still_refused():
    host = FakeHost()
    spy = SpyAgent()
    d = _dispatcher(host, spy)
    result = d.handle(_command("ask", [{"name": "question", "type": 3,
                                        "value": "hello"}]))
    assert result.response["data"]["content"] == SETUP_TEXT
    assert spy.calls == []


def test_a_served_guild_still_resolves_its_own_brain_mode():
    host = FakeHost()
    d = _dispatcher(host, guilds=(OTHER,))
    assert d._brain_for(_command("ask", guild=OTHER)).mode != NONE
    assert d._brain_for(_command("ask", guild="999999999999999999")).mode == NONE


# -- registration ------------------------------------------------------------------------
def test_the_two_commands_are_registered_when_no_plugin_owns_them():
    names = {body["name"] for body in commands(host=None)}
    assert {CLAIM_CMD, LINK_CMD} <= names


def test_a_plugin_that_owns_them_is_not_double_registered():
    defs = [{"name": CLAIM_CMD, "type": 1}, {"name": LINK_CMD, "type": 1}]

    class Host:
        def owns(self, name):
            return name in (CLAIM_CMD, LINK_CMD)

        def command_defs(self):
            return [dict(d) for d in defs]

    names = [body["name"] for body in commands(host=Host())]
    assert names.count(CLAIM_CMD) == 1
    assert names.count(LINK_CMD) == 1
