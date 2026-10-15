"""The web plugin's tools and the agent's tool loop - offline."""
from __future__ import annotations

import io
import json

import pytest
from awdiscord import agent as agent_mod
from awdiscord.agent import OpenAICompatAgent
from awdiscord.builtin import web
from awdiscord.plugins import build_host
from awdiscord.store import GuildStore

RSS = b"""<rss><channel><item><title>Big story</title><pubDate>Sun, 04 Oct 2026</pubDate>
<link>https://news.example/1</link></item></channel></rss>"""


class Resp(io.BytesIO):
    def __init__(self, body, url="https://news.example/"):
        super().__init__(body)
        self._url = url

    def geturl(self):
        return self._url


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("AWDISCORD_SEARCH_URL", raising=False)
    yield
    web.OPENER[0] = None


def test_news_headlines_parses_rss():
    web.OPENER[0] = lambda req, timeout=0, context=None: Resp(RSS)
    out = web.news_headlines()
    assert "Big story" in out and "https://news.example/1" not in out
    assert "https://news.example/1" in web.news_headlines(links=True)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8150/v1", "http://10.0.0.5/",
                                 "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                                 "http://localhost:8371/health"])
def test_fetch_refuses_non_public_targets(url):
    with pytest.raises(ValueError):
        web.fetch_url(url)


def test_fetch_refuses_a_redirect_into_the_network(monkeypatch):
    monkeypatch.setattr(web, "public_url",
                        lambda u: (_ for _ in ()).throw(ValueError("private"))
                        if "10.0.0.1" in u else u)
    web.OPENER[0] = lambda req, timeout=0, context=None: Resp(b"x", url="http://10.0.0.1/")
    with pytest.raises(ValueError):
        web.fetch_url("https://ok.example/")


def test_search_service_shape(monkeypatch):
    monkeypatch.setenv("AWDISCORD_SEARCH_URL", "https://search.internal:8114")
    body = json.dumps({"results": [{"title": "T", "url": "https://u", "snippet": "S"}]}).encode()
    web.OPENER[0] = lambda req, timeout=0, context=None: Resp(body)
    assert "T\n  https://u\n  S" in web.web_search("q")


def test_web_plugin_is_on_and_offers_its_tools():
    host = build_host(GuildStore(":memory:"))
    names = {t.name for t in host.tools_for("g")}
    assert {"news_headlines", "web_search", "fetch_url"} <= names


def test_agent_runs_the_tool_loop(monkeypatch):
    turns = []

    def complete(self, payload):
        turns.append(payload)
        if len(turns) == 1:
            assert "tools" in payload and "Today is" in payload["messages"][0]["content"]
            return {"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "news_headlines", "arguments": "{}"}}]}
        tool_msg = payload["messages"][-1]
        assert tool_msg["role"] == "tool" and "Big story" in tool_msg["content"]
        return {"content": "Top story: Big story"}
    monkeypatch.setattr(OpenAICompatAgent, "_complete", complete)
    web.OPENER[0] = lambda req, timeout=0, context=None: Resp(RSS)
    a = OpenAICompatAgent("http://x/v1")
    assert a.ask("news?", tools=web.TOOLS) == "Top story: Big story"


def test_tool_errors_go_back_to_the_model(monkeypatch):
    seen = []

    def complete(self, payload):
        seen.append(payload["messages"][-1])
        if len(seen) == 1:
            return {"tool_calls": [{"id": "c1", "function": {
                "name": "fetch_url", "arguments": '{"url": "http://127.0.0.1/"}'}}]}
        return {"content": "could not read it"}
    monkeypatch.setattr(OpenAICompatAgent, "_complete", complete)
    assert OpenAICompatAgent("http://x/v1").ask("read", tools=web.TOOLS) == "could not read it"
    assert seen[-1]["content"].startswith("tool error: ValueError")


def test_endless_tool_calls_are_cut_off(monkeypatch):
    n = []

    def complete(self, payload):
        n.append(1)
        if "tools" not in payload:
            return {"content": "final"}
        return {"tool_calls": [{"id": "c", "function": {"name": "nope", "arguments": "{}"}}]}
    monkeypatch.setattr(OpenAICompatAgent, "_complete", complete)
    assert OpenAICompatAgent("http://x/v1").ask("x", tools=web.TOOLS) == "final"
    assert len(n) == agent_mod.MAX_TOOL_ROUNDS + 1
