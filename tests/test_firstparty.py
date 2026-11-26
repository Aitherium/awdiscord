"""Our own stack is answered from our own registry, not from a web search.

Reproduces 2026-11-22, #bot on OccupySaaS: asked about awpermit, awrun, awdelphi,
awrelay, AitherNet, index.aitherium.com and awgit, the bot called them things the
asker was "proposing, not things that exist". Every one is a real entry.

A tiny synthetic corpus keeps this test self-contained: the awdiscord brick is
mirrored to its own repo, where the monorepo's registry is not present. One extra
test validates the REAL registry when the monorepo checkout has it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from awdiscord import firstparty

_HERE = Path(__file__).resolve()
#: The monorepo root when this test runs inside it; the repo root in the mirrored
#: awdiscord repo (where the monorepo-only files below simply do not exist).
ROOT = _HERE.parents[4] if len(_HERE.parents) > 4 else _HERE.parent
ECOSYSTEM = ROOT / "AitherOS" / "apps" / "AitherVeil" / "public" / "ecosystem.json"
PAGES = ROOT / "AitherOS" / "packages" / "awkno" / "awkno" / "pages"

OWNER_QUESTION = (
    "solve the Byzantine generals problem and tell me how you did it, and how we can "
    "use AitherRelay/awrelay + AitherNet and index.aitherium.com + awgit to deploy and "
    "manage agent swarms"
)

REGISTRY = {
    "schemaVersion": "1",
    "generated_at": "2026-11-22T00:00:00Z",
    "count": 7,
    "bricks": [
        {"id": "awpermit", "status": "public", "kind": "tool",
         "tagline": "Let an agent through your door with a scope and a spend cap.",
         "install": "pip install awpermit"},
        {"id": "awrun", "status": "public", "kind": "runtime",
         "tagline": "A priority-aware queue and dispatcher.", "install": "pip install awrun"},
        {"id": "awrelay", "status": "public", "kind": "tool",
         "tagline": "Portable agent messaging.", "install": "pip install awrelay"},
        {"id": "awgit", "status": "public", "kind": "tool",
         "tagline": "Semantic version control on top of git.",
         "install": "pip install awgit"},
        {"id": "awnet", "status": "public", "kind": "runtime",
         "tagline": "The agentic web.", "install": "pip install awnet"},
        {"id": "awedgerunner", "status": "unpublished", "kind": "runtime",
         "tagline": "Edge runner.", "install": "not yet published; builds in-tree"},
        {"id": "awindex", "status": "unpublished", "kind": "tool",
         "tagline": "A curated, git-backed map of the internet.",
         "install": "pip install awindex"},
    ],
    "products": [],
    "stacks": [],
}

PAGE_IDS = ("awpermit", "awrun", "awrelay", "awgit", "awnet", "awedgerunner", "awindex")


def _page(topic: str, synopsis: str) -> dict:
    return {"topic": topic, "status": "public", "synopsis": synopsis,
            "adopt": "Try it on its own.",
            "description": f"Kind: tool\n\nDocs\nhttps://aitherium.github.io/{topic}/"}


def _write_corpus(root: Path) -> None:
    (root / "awkno-pages").mkdir(parents=True)
    (root / "ecosystem.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    for topic in PAGE_IDS:
        (root / "awkno-pages" / f"{topic}.json").write_text(
            json.dumps(_page(topic, f"{topic} does its own job.")), encoding="utf-8")
    (root / "awkno-pages" / "world.json").write_text(json.dumps({
        "topic": "world", "status": "public", "synopsis": "Owned zones and public surfaces",
        "body": "Zones the platform owns:\n\n- aitherium.com\n\n"
                "Surfaces that answer on them:\n\n- https://index.aitherium.com",
    }), encoding="utf-8")


@pytest.fixture(autouse=True)
def corpus(monkeypatch, tmp_path):
    _write_corpus(tmp_path)
    monkeypatch.setenv("AWDISCORD_FIRSTPARTY_DIR", str(tmp_path))
    monkeypatch.delenv("AWDISCORD_ECOSYSTEM_FILE", raising=False)
    monkeypatch.delenv("AWDISCORD_AWKNO_PAGES", raising=False)
    monkeypatch.setenv("AWDISCORD_ECOSYSTEM_URL", "")
    firstparty.reset()
    yield
    firstparty.reset()


def test_a_public_brick_exists_with_its_status_and_docs():
    out = firstparty.lookup("awpermit")
    assert "status: public" in out
    assert "https://aitherium.github.io/awpermit/" in out
    assert "Not found" not in out


def test_an_unpublished_brick_is_reported_with_its_real_status():
    out = firstparty.lookup("awedgerunner")
    assert "status: unpublished" in out
    assert "no public repo" in out.lower()
    assert "Not found" not in out


def test_the_human_alias_resolves_to_the_brick():
    # The owner said "AitherNet"; the registry id is awnet.
    out = firstparty.lookup("AitherNet")
    assert "- awnet" in out and "status: public" in out
    relay = firstparty.lookup("AitherRelay")
    assert "- awrelay" in relay and "status: public" in relay


def test_an_owned_surface_is_answered_from_the_world_page():
    out = firstparty.lookup("index.aitherium.com")
    assert "index.aitherium.com" in out
    assert "owned surfaces" in out
    assert "awindex" in out and "unpublished" in out


def test_the_owner_question_grounds_every_name_it_contains():
    block = firstparty.grounding(OWNER_QUESTION)
    for name in ("awrelay", "awnet", "index.aitherium.com", "awgit"):
        assert name in block
    assert "does not exist" not in block.lower()
    # The block must TELL the model not to call a listed product nonexistent.
    assert "never call a listed product nonexistent" in block.lower()


def test_plain_english_aw_words_do_not_trigger_grounding():
    assert firstparty.grounding("the award goes to whoever is aware") == ""
    assert firstparty.grounding("that was an awesome movie") == ""


def test_an_unconfigured_deployment_is_inert(monkeypatch):
    monkeypatch.delenv("AWDISCORD_ECOSYSTEM_FILE", raising=False)
    monkeypatch.delenv("AWDISCORD_AWKNO_PAGES", raising=False)
    monkeypatch.delenv("AWDISCORD_FIRSTPARTY_DIR", raising=False)
    monkeypatch.delenv("AWDISCORD_ECOSYSTEM_URL", raising=False)
    firstparty.reset()
    assert firstparty.available() is False
    assert firstparty.grounding(OWNER_QUESTION) == ""
    assert "No first-party corpus is configured" in firstparty.lookup("awpermit")


def test_an_unreadable_registry_never_says_nonexistent(monkeypatch, tmp_path):
    # A bad mount must not turn into "that product does not exist".
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("AWDISCORD_FIRSTPARTY_DIR", str(empty))
    monkeypatch.delenv("AWDISCORD_ECOSYSTEM_FILE", raising=False)
    monkeypatch.delenv("AWDISCORD_AWKNO_PAGES", raising=False)
    monkeypatch.setenv("AWDISCORD_ECOSYSTEM_URL", "")
    firstparty.reset()
    out = firstparty.lookup("awpermit")
    assert "could not be read" in out
    assert "Not found" not in out
    assert firstparty.grounding(OWNER_QUESTION) == ""


def test_the_answer_path_calls_the_tool_and_cites_our_docs(monkeypatch):
    """A tool-using turn: the model asks our registry, and the names survive the floor."""
    from awdiscord.agent import OpenAICompatAgent
    from awdiscord.plugins import Tool

    agent = OpenAICompatAgent("https://x.invalid/v1", model="m")
    queue = [
        {"tool_calls": [{"id": "c1", "function": {
            "name": "first_party_lookup",
            "arguments": json.dumps({"name": "awpermit"})}}]},
        {"content": "awpermit is a public Aitherium brick. awrun is a public brick too."},
        {"content": '{"unsupported": []}'},
    ]
    sent = []

    def fake(payload):
        sent.append(payload)
        return queue.pop(0)

    monkeypatch.setattr(agent, "_complete", fake)
    out = agent.ask("do awpermit and awrun exist?",
                    tools=[Tool("first_party_lookup", "look up", firstparty.lookup)])
    assert "does not exist" not in out
    assert "awpermit" in out and "awrun" in out
    # The sources line cites OUR registry/docs, not a web page.
    assert "https://aitherium.github.io/awpermit/" in out
    assert agent.last_tools[0]["tool"] == "first_party_lookup"
    assert agent.last_fact_floor["checked"] is True


def test_the_owner_question_replays_through_the_dispatch_and_the_floor():
    """The exact question, through the real _agent_kwargs pre-grounding + fact floor."""
    from awdiscord import factfloor
    from awdiscord.agent import EchoAgent
    from awdiscord.dispatch import Dispatcher

    class Recorder(EchoAgent):
        def __init__(self):
            super().__init__()
            self.context = ""

        def ask(self, prompt, *, persona=None, user="", context=""):
            self.context = context
            return "ok"

    agent = Recorder()
    Dispatcher(agent).chat("1", "1561399233846972457", OWNER_QUESTION)
    assert "first_party_lookup for this question" in agent.context
    for name in ("awrelay", "awnet", "index.aitherium.com", "awgit"):
        assert name in agent.context
    assert "never call a listed product nonexistent" in agent.context.lower()

    # A reasoning answer written after that grounding survives the claim filter.
    answer = ("## How I'd actually solve this\n"
              "Suppose 3f+1 nodes exist. A quorum is 2f+1, therefore two quorums "
              "share at least one honest node.")
    ours = firstparty.lookup("awgit")
    out, report = factfloor.apply(answer, [ours], OWNER_QUESTION,
                                  lambda messages: '{"unsupported": [1, 2, 3]}',
                                  first_party=[ours])
    assert "## How I'd actually solve this" in out
    assert "therefore two quorums share at least one honest node" in out
    assert report["dropped"] == 0


@pytest.mark.skipif(not (ECOSYSTEM.is_file() and PAGES.is_dir()),
                    reason="monorepo registry not materialised in this checkout")
def test_the_real_registry_answers_the_owner_question(monkeypatch):
    """The live registry, when this checkout has it (skipped in the mirrored repo)."""
    monkeypatch.setenv("AWDISCORD_ECOSYSTEM_FILE", str(ECOSYSTEM))
    monkeypatch.setenv("AWDISCORD_AWKNO_PAGES", str(PAGES))
    monkeypatch.delenv("AWDISCORD_FIRSTPARTY_DIR", raising=False)
    monkeypatch.setenv("AWDISCORD_ECOSYSTEM_URL", "")
    firstparty.reset()
    assert "status: public" in firstparty.lookup("awpermit")
    assert "status: public" in firstparty.lookup("awrun")
    assert "status: public" in firstparty.lookup("awedgerunner")
    assert "- awnet" in firstparty.lookup("AitherNet")
    block = firstparty.grounding(OWNER_QUESTION)
    for name in ("awrelay", "awnet", "index.aitherium.com", "awgit"):
        assert name in block
    firstparty.reset()
