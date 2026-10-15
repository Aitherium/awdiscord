"""Packs: install plugin packs and awdk agent packs; the github plugin and dgg-pm pack."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from awdiscord import packs
from awdiscord.agent import EchoAgent
from awdiscord.builtin import github as gh
from awdiscord.dispatch import Dispatcher
from awdiscord.plugins import MANAGE_GUILD, PluginError, build_host
from awdiscord.store import GuildStore

ROOT = Path(__file__).resolve().parents[1]
DGG = ROOT / "packs" / "dgg_pm"
ADMIN = str(MANAGE_GUILD)


def _it(cmd, verb=None, perms="0", **opts):
    options = [{"name": k, "value": v} for k, v in opts.items()]
    if verb:
        options = [{"type": 1, "name": verb, "options": options}]
    return {"type": 2, "id": "1", "token": "t", "guild_id": "g",
            "member": {"user": {"id": "7"}, "permissions": perms},
            "data": {"name": cmd, "options": options}}


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AWDISCORD_PLUGIN_DIRS", raising=False)
    monkeypatch.setenv("AWDISCORD_GITHUB_TOKEN", "test-token")
    yield
    gh.OPENER[0] = None


def _agent_pack(tmp_path) -> Path:
    d = tmp_path / "Research Pack"
    (d / "skills" / "grading").mkdir(parents=True)
    (d / "agent.yaml").write_text("name: research-pack\nversion: '1.2.0'\n"
                                  "description: grades sources\n", encoding="utf-8")
    (d / "brain_pack.yaml").write_text("app_name: Research\nsystem_prompt: |\n"
                                       "  Quote before you interpret.\n", encoding="utf-8")
    (d / "skills" / "grading" / "SKILL.md").write_text("# Grade sources A-F", encoding="utf-8")
    return d


def _dispatch():
    host = build_host(GuildStore(":memory:"), agent=EchoAgent())
    return host, Dispatcher(EchoAgent(), host=host)


def test_agent_pack_installs_and_bridges(tmp_path):
    pytest.importorskip("yaml")
    out = packs.install(str(_agent_pack(tmp_path)))
    assert out["name"] == "research-pack" and out["kind"] == "agent-pack"
    host, disp = _dispatch()
    plugin = host.plugins["research-pack"]
    assert "Quote before you interpret." in plugin.persona
    assert set(plugin.skills) == {"grading"}
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="research-pack"))
    assert "Quote before" in host.persona_for("g")
    skills = disp.handle(_it("research-pack", "skills"))
    assert "grading" in skills.response["data"]["content"]
    answer = disp.handle(_it("research-pack", "ask", question="hi")).deferred()
    assert answer["content"] == "echo: hi"


REAL_DGG_PACK = ROOT.parents[2] / "awdk" / "adk" / "packs" / "dgg_research"


@pytest.mark.skipif(not REAL_DGG_PACK.is_dir(), reason="awdk tree not beside this package")
def test_real_dgg_research_agent_pack_loads():
    pytest.importorskip("yaml")
    real = REAL_DGG_PACK
    plugin = packs.agent_pack_plugin(real)
    assert plugin.name == "dgg-research" and "documentary record" in plugin.persona


def test_install_refuses_duplicates_and_junk(tmp_path):
    packs.install(str(DGG))
    with pytest.raises(PluginError):
        packs.install(str(DGG))
    packs.install(str(DGG), force=True)
    junk = tmp_path / "junk"
    junk.mkdir()
    with pytest.raises(PluginError):
        packs.install(str(junk))
    packs.remove("dgg-pm")
    with pytest.raises(PluginError):
        packs.remove("dgg-pm")


def _graphql_fake(board_nodes, search_nodes):
    def opener(req, timeout=0):
        assert req.get_header("Authorization") == "Bearer test-token"
        q = json.loads(req.data)
        if "projectV2" in q["query"]:
            body = {"data": {"repositoryOwner": {"projectV2": {
                "title": "Pragmatic Papers Development", "url": "https://x/3",
                "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                          "nodes": board_nodes}}}}}
        else:
            body = {"data": {"search": {"pageInfo": {"hasNextPage": False},
                                        "nodes": search_nodes}}}
        return io.BytesIO(json.dumps(body).encode())
    return opener


def _node(status, repo, n):
    return {"status": {"name": status} if status else None,
            "content": {"number": n, "title": f"t{n}", "url": "u", "state": "OPEN",
                        "repository": {"name": repo}}}


def test_dgg_pm_board_and_untracked():
    packs.install(str(DGG))
    gh.OPENER[0] = _graphql_fake(
        [_node("In review", "pragmatic-papers", 1), _node("Needs Work", "pragmatic-papers", 2),
         _node("Done", "pragmatic-papers", 3)],
        [{"number": 1, "title": "t1", "url": "u", "repository": {"name": "pragmatic-papers"}},
         {"number": 9, "title": "bot crash", "url": "u",
          "repository": {"name": "digital-ground-game-discord-bot"}}])
    host, disp = _dispatch()
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="dgg-pm"))
    board = disp.handle(_it("dgg", "board")).deferred()["content"]
    assert "Pragmatic Papers Development" in board and "Needs Work" in board
    assert "pragmatic-papers#2" in board
    missing = disp.handle(_it("dgg", "untracked")).deferred()["content"]
    assert "**1** open issues" in missing and "digital-ground-game-discord-bot#9" in missing


def test_github_plugin_needs_a_connected_board():
    host, disp = _dispatch()
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="github"))
    res = disp.handle(_it("gh", "board"))
    assert "No board connected" in res.response["data"]["content"]
    gh.OPENER[0] = _graphql_fake([_node(None, "r", 1)], [])
    conn = disp.handle(_it("gh", "connect", perms=ADMIN, owner="org", project=3))
    assert "Connected to" in conn.deferred()["content"]
    assert "No status" in disp.handle(_it("gh", "board")).deferred()["content"]


def test_github_errors_reach_the_user_not_a_crash(monkeypatch):
    monkeypatch.delenv("AWDISCORD_GITHUB_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    host, disp = _dispatch()
    disp.handle(_it("plugins", "enable", perms=ADMIN, name="github"))
    disp.host.store.set("g", "github", "board", {"owner": "o", "project": 1})
    out = disp.handle(_it("gh", "board")).deferred()["content"]
    assert "no GitHub token" in out
