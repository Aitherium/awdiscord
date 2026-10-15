"""awdiscord plugins: per-server enablement, built-ins, discovery, isolation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord.agent import CallableAgent, EchoAgent  # noqa: E402
from awdiscord.dispatch import DEFERRED, MESSAGE, UPDATE, Dispatcher, commands  # noqa: E402
from awdiscord.plugins import (  # noqa: E402
    MANAGE_GUILD,
    Command,
    Plugin,
    PluginError,
    PluginHost,
    build_host,
)
from awdiscord.ratelimit import RateLimiter  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402

ADMIN = str(MANAGE_GUILD)


def _it(cmd, verb=None, guild="g1", user="7", perms="0", **opts):
    options = [{"name": k, "value": v} for k, v in opts.items()]
    if verb:
        options = [{"type": 1, "name": verb, "options": options}]
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}, "permissions": perms},
            "data": {"name": cmd, "options": options}}


def _content(result):
    return result.response["data"]["content"]


@pytest.fixture(autouse=True)
def _no_user_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AWDISCORD_PLUGIN_DIRS", raising=False)


@pytest.fixture
def disp():
    agent = EchoAgent()
    host = build_host(GuildStore(":memory:"), agent=agent)
    lim = RateLimiter(per_user=(1000, 1.0), per_guild=(1000, 1.0), global_=(1000, 1.0))
    return Dispatcher(agent, lim, None, host=host)


def test_builtins_load_and_register(disp):
    assert {"plugins", "community", "projects"} <= set(disp.host.plugins)
    names = [c["name"] for c in commands([], host=disp.host)]
    assert names[:3] == ["ask", "share", "status"]
    assert {"plugins", "poll", "faq", "task", "standup"} <= set(names)
    assert disp.host.errors == {}


def test_admin_commands_hidden_from_members_by_default(disp):
    defs = {c["name"]: c for c in disp.host.command_defs()}
    assert "default_member_permissions" not in defs["plugins"]   # list is for everyone
    assert defs["task"].get("default_member_permissions") is None


def test_enable_needs_manager_then_command_answers(disp):
    off = disp.handle(_it("task", "add", title="ship it"))
    assert "is off in this server" in _content(off)
    denied = disp.handle(_it("plugins", "enable", name="projects"))
    assert "Only server managers" in _content(denied)
    ok = disp.handle(_it("plugins", "enable", perms=ADMIN, name="projects"))
    assert "is on" in _content(ok)
    added = disp.handle(_it("task", "add", title="ship it"))
    assert added.response["type"] == MESSAGE and "#1" in _content(added)


def test_enablement_is_per_server(disp):
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="projects"))
    other = disp.handle(_it("task", "add", guild="g2", title="x"))
    assert "is off in this server" in _content(other)


def test_store_is_isolated_per_server(disp):
    disp.handle(_it("faq", "set", perms=ADMIN, topic="hours", answer="9 to 5"))
    mine = disp.handle(_it("faq", "ask", question="hours"))
    assert _content(mine) == "9 to 5"
    theirs = disp.handle(_it("faq", "list", guild="g2"))
    assert "none yet" in _content(theirs)


def test_faq_falls_back_to_agent_with_only_the_faq(disp):
    seen = {}

    def fn(prompt):
        seen["prompt"] = prompt
        return "We open at 9."
    disp.host.agent = CallableAgent(fn)
    disp.handle(_it("faq", "set", perms=ADMIN, topic="hours", answer="9 to 5"))
    res = disp.handle(_it("faq", "ask", question="when do you open?"))
    assert res.response["type"] == DEFERRED
    assert res.deferred()["content"] == "We open at 9."
    assert "hours: 9 to 5" in seen["prompt"] and "ONLY" in seen["prompt"]


def test_poll_buttons_count_one_vote_per_person(disp):
    res = disp.handle(_it("poll", question="Lunch?", choices="pizza | tacos"))
    buttons = res.response["data"]["components"][0]["components"]
    pizza, tacos = buttons[0]["custom_id"], buttons[1]["custom_id"]

    def press(cid, user):
        return disp.handle({"type": 3, "guild_id": "g1",
                            "member": {"user": {"id": user}},
                            "data": {"custom_id": cid}})
    press(pizza, "1")
    press(pizza, "2")
    final = press(tacos, "2")                       # user 2 changes their vote
    assert final.response["type"] == UPDATE
    desc = final.response["data"]["embeds"][0]["description"]
    assert "pizza** - 1" in desc and "tacos** - 1" in desc


def test_task_lifecycle_and_standup(disp):
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="projects"))
    disp.handle(_it("task", "add", title="write docs"))
    disp.handle(_it("task", "add", title="cut release"))
    assert "#2" in _content(disp.handle(_it("task", "done", id=2)))
    listing = _content(disp.handle(_it("task", "list")))
    assert "write docs" in listing and "cut release" not in listing
    standup = disp.handle(_it("standup"))
    out = standup.deferred()["content"]
    assert "DONE" in out and "cut release" in out
    assert "No task #9" in _content(disp.handle(_it("task", "done", id=9)))


def test_drop_only_by_author_or_manager(disp):
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="projects"))
    disp.handle(_it("task", "add", user="1", title="mine"))
    assert "Only whoever" in _content(disp.handle(_it("task", "drop", user="2", id=1)))
    assert "Dropped" in _content(disp.handle(_it("task", "drop", user="2", perms=ADMIN, id=1)))


def test_plugins_manager_cannot_be_disabled(disp):
    res = disp.handle(_it("plugins", "disable", perms=ADMIN, name="plugins"))
    assert "cannot be turned off" in _content(res)


def test_plugin_persona_reaches_ask(disp):
    got = {}

    class Agent(EchoAgent):
        def ask(self, prompt, *, persona=None, user="", context=""):
            got["context"] = context
            return "ok"
    disp.agent = Agent()
    disp.handle(_it("ask", question="hi")).deferred()
    assert "community helper" in got["context"]
    assert "/task board" not in got["context"]           # projects is off here


def test_plugin_crash_becomes_private_error():
    def boom(ctx):
        raise RuntimeError("bad")
    host = PluginHost(GuildStore(":memory:"),
                      plugins=[Plugin("boom", "1", "x", default_enabled=True,
                                      commands=[Command("boom", "x", boom)])])
    res = Dispatcher(EchoAgent(), host=host).handle(_it("boom"))
    assert res.response["data"]["flags"] == 64
    assert "plugin failed" in _content(res)


def test_command_collisions_refused():
    host = PluginHost(GuildStore(":memory:"))
    with pytest.raises(PluginError):
        host.add(Plugin("x", "1", "x", commands=[Command("ask", "x", None)]))
    host.add(Plugin("a", "1", "x", commands=[Command("same", "x", None)]))
    with pytest.raises(PluginError):
        host.add(Plugin("b", "1", "x", commands=[Command("same", "x", None)]))


def test_dir_plugins_load_and_broken_ones_are_reported(tmp_path, monkeypatch):
    from awdiscord.cli import main
    d = tmp_path / "plugins"
    assert main(["new-plugin", "greeter", "--dir", str(d)]) == 0
    (d / "broken.py").write_text("raise ImportError('nope')\n", encoding="utf-8")
    monkeypatch.setenv("AWDISCORD_PLUGIN_DIRS", str(d))
    host = build_host(GuildStore(":memory:"), agent=EchoAgent())
    assert "greeter" in host.plugins
    assert any("broken.py" in k and "nope" in v for k, v in host.errors.items())
    disp = Dispatcher(EchoAgent(), host=host)
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="greeter"))
    first = disp.handle(_it("greeter", "hello"))
    assert "said 1 time" in _content(first)
    assert "said 2 time" in _content(disp.handle(_it("greeter", "hello")))


def test_only_filter_keeps_the_manager():
    host = build_host(GuildStore(":memory:"), only=["projects"])
    assert set(host.plugins) == {"plugins", "projects"}


def test_mention_chat_strips_the_mention_and_uses_plugin_context(disp):
    got = {}

    class Agent(EchoAgent):
        def ask(self, prompt, *, persona=None, user="", context=""):
            got.update(prompt=prompt, context=context)
            return "alive"
    disp.agent = Agent()
    out = disp.chat("7", "g1", "<@999> are you alive", bot_id="999")
    assert out["content"] == "alive" and got["prompt"] == "are you alive"
    assert "community helper" in got["context"]
    assert out["allowed_mentions"] == {"parse": []}
    share = disp.handle(_it("share"))
    assert "alive" in _content(share)


def test_bare_mention_gets_a_hint_not_the_agent(disp):
    disp.agent = None                                   # would crash if called
    assert "ask me anything" in disp.chat("7", None, "<@!999>", bot_id="999")["content"]


def test_mention_chat_is_rate_limited():
    lim = RateLimiter(per_user=(1, 600.0))
    d = Dispatcher(EchoAgent(), lim)
    assert d.chat("7", "g", "hi")["content"] == "echo: hi"
    assert "Slow down" in d.chat("7", "g", "again")["content"]
