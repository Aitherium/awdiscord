"""A plugin's own message context-menu command (``Command(..., message=True)``).

Any member may use it (the three built-in "Aither: ..." ones stay operator-only);
the plugin's per-server switch still decides whether it runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Runnable from any directory and with no conftest: the brick is beside this folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord.dispatch import (  # noqa: E402
    COMMAND,
    MESSAGE_COMMAND,
    MESSAGE_COMMANDS,
    Dispatcher,
    commands,
)
from awdiscord.plugins import (  # noqa: E402
    Command,
    Plugin,
    PluginCommandConflictError,
    PluginHost,
)
from awdiscord.store import GuildStore  # noqa: E402

GUILD, MEMBER, CHANNEL, MSG = "100000000000000001", "2", "20", "30"
NAME = "Save to my Space"


class Agent:
    name = "model"

    def ask(self, prompt, **kw):
        return "answer"


def _plugin(seen: list):
    def save(ctx):
        seen.append((ctx.user, ctx.target_message.get("content")))
        return ctx.reply("saved", private=True)

    return Plugin(name="notes", version="1", description="notes",
                  commands=[Command(NAME, "", save, message=True)])


def _dispatcher(tmp_path, seen):
    store = GuildStore(tmp_path / "g.db")
    host = PluginHost(store, plugins=[_plugin(seen)])
    return Dispatcher(Agent(), host=host, store=store), host


def _interaction(name=NAME, resolved=True):
    messages = {MSG: {"id": MSG, "content": "hello", "author": {"id": MEMBER}}}
    return {"type": COMMAND, "guild_id": GUILD, "channel_id": CHANNEL,
            "member": {"user": {"id": MEMBER}, "permissions": "0"},
            "data": {"type": MESSAGE_COMMAND, "name": name, "target_id": MSG,
                     "resolved": {"messages": messages if resolved else {}}}}


def test_it_is_registered_as_a_message_command_with_no_options(tmp_path):
    _, host = _dispatcher(tmp_path, [])
    mine = [d for d in commands(host=host) if d["name"] == NAME]
    assert mine == [{"name": NAME, "type": MESSAGE_COMMAND, "contexts": [0],
                     "integration_types": [0]}]


def test_a_plain_member_reaches_the_plugin_handler_with_the_signed_message(tmp_path):
    seen: list = []
    dispatcher, host = _dispatcher(tmp_path, seen)
    host.set_enabled(GUILD, "notes", True)
    out = dispatcher.handle(_interaction())
    assert out.response["data"]["content"] == "saved"
    assert seen == [(MEMBER, "hello")]


def test_it_obeys_the_plugins_per_server_switch(tmp_path):
    seen: list = []
    dispatcher, _ = _dispatcher(tmp_path, seen)
    out = dispatcher.handle(_interaction())
    assert "off in this" in out.response["data"]["content"] and seen == []


def test_the_built_in_message_commands_stay_operator_only(tmp_path):
    seen: list = []
    dispatcher, host = _dispatcher(tmp_path, seen)
    host.set_enabled(GUILD, "notes", True)
    out = dispatcher.handle(_interaction(name="Aither: investigate"))
    assert "Only an operator or a server admin" in out.response["data"]["content"]
    assert seen == []


def test_an_unresolved_target_is_an_empty_message_never_a_guess(tmp_path):
    seen: list = []
    dispatcher, host = _dispatcher(tmp_path, seen)
    host.set_enabled(GUILD, "notes", True)
    dispatcher.handle(_interaction(resolved=False))
    assert seen == [(MEMBER, None)]


@pytest.mark.parametrize("command", [
    Command(next(iter(MESSAGE_COMMANDS)), "", lambda ctx: None, message=True),
    Command(NAME, "", None, message=True),
])
def test_a_plugin_cannot_take_a_core_name_or_register_no_handler(tmp_path, command):
    host = PluginHost(GuildStore(tmp_path / "g.db"))
    with pytest.raises(PluginCommandConflictError):
        host.add(Plugin(name="x", version="1", description="x", commands=[command]))
