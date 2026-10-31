"""A pack can replace a built-in plugin by name; an undeclared collision is still refused."""
import pytest
from awdiscord.plugins import Command, Plugin, PluginError, PluginHost, discover
from awdiscord.store import GuildStore


def _ok(ctx):
    return ctx.reply("ok")


def _builtin():
    return Plugin(name="board", version="1", description="built-in",
                  commands=[Command("task", "t", _ok), Command("standup", "s", _ok)])


def _host(*plugins):
    return PluginHost(GuildStore(":memory:"), plugins=list(plugins))


def test_undeclared_collision_still_refused():
    host = _host(_builtin())
    rival = Plugin(name="rival", version="1", description="d",
                   commands=[Command("task", "t", _ok)])
    with pytest.raises(PluginError, match="taken by board"):
        host.add(rival)
    assert host._by_command["task"].name == "board"


def test_declared_override_takes_the_commands():
    host = _host(_builtin())
    pack = Plugin(name="team", version="1", description="d", overrides=("board",),
                  commands=[Command("task", "t", _ok), Command("project", "p", _ok)])
    host.add(pack)
    assert "board" not in host.plugins
    assert host._by_command["task"].name == "team"
    assert "standup" not in host._by_command      # the replaced plugin's other names go too
    assert host.owns("project")


def test_replaced_plugin_cannot_come_back_later():
    pack = Plugin(name="team", version="1", description="d", overrides=("board",),
                  commands=[Command("task", "t", _ok)])
    host = _host(pack)
    with pytest.raises(PluginError, match="replaced by 'team'"):
        host.add(_builtin())


def test_override_cannot_evict_an_always_on_plugin():
    core = Plugin(name="core", version="1", description="d", always_on=True,
                  commands=[Command("plugins", "p", _ok)])
    host = _host(core)
    bad = Plugin(name="bad", version="1", description="d", overrides=("core",),
                 commands=[Command("plugins", "p", _ok)])
    with pytest.raises(PluginError, match="always on"):
        host.add(bad)
    assert host.plugins["core"] is core


def test_failed_override_leaves_the_host_unchanged():
    other = Plugin(name="other", version="1", description="d",
                   commands=[Command("decide", "d", _ok)])
    host = _host(_builtin(), other)
    pack = Plugin(name="team", version="1", description="d", overrides=("board",),
                  commands=[Command("task", "t", _ok), Command("decide", "d", _ok)])
    with pytest.raises(PluginError, match="taken by other"):
        host.add(pack)
    assert host._by_command["task"].name == "board"   # nothing was evicted


def test_discover_drops_the_builtin_projects_for_a_pack_that_overrides_it(tmp_path,
                                                                           monkeypatch):
    (tmp_path / "teamboard.py").write_text(
        "from awdiscord.plugins import Command, Plugin\n"
        "PLUGIN = Plugin(name='teamboard', version='1', description='d',\n"
        "                overrides=('projects',),\n"
        "                commands=[Command('task', 't', lambda c: c.reply('x'))])\n",
        encoding="utf-8")
    monkeypatch.setenv("AWDISCORD_PLUGIN_DIRS", str(tmp_path))
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    found, errors = discover()
    names = [p.name for p in found]
    assert "teamboard" in names and "projects" not in names, (names, errors)
