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


def _fake_dns(monkeypatch, table):
    """socket.getaddrinfo answering from ``table`` (host -> one IP)."""
    def getaddrinfo(host, port, *a, **kw):
        if host not in table:
            raise web.socket.gaierror(host)
        return [(2, 1, 6, "", (table[host], port))]
    monkeypatch.setattr(web.socket, "getaddrinfo", getaddrinfo)


class _FakeResponse:
    def __init__(self, status, location, body):
        self.status, self._location, self._body = status, location, body

    def getheader(self, name):
        return self._location if name == "Location" else None

    def read(self, n=-1):
        return self._body


def _fake_connect(answers):
    """Pinned-connection factory replaying ``answers``; returns the dial log."""
    dialled, queue = [], list(answers)

    class Conn:
        def __init__(self, scheme, host, ip, port, timeout):
            dialled.append((scheme, host, ip, port))

        def connect(self):
            pass

        def request(self, method, path, headers=None):
            dialled[-1] += (path, headers.get("Host"))

        def getresponse(self):
            return _FakeResponse(*queue.pop(0))

        def close(self):
            pass

    web.CONNECT[0] = Conn
    return dialled


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("AWDISCORD_SEARCH_URL", raising=False)
    monkeypatch.delenv("AWDISCORD_EGRESS_PROXY", raising=False)
    # the fakes resolve to documentation addresses (RFC 5737); count them as public
    testnets = [web.ipaddress.ip_network(n) for n in ("203.0.113.0/24", "198.51.100.0/24")]
    monkeypatch.setattr(web, "_is_public",
                        lambda ip: ip.is_global or any(ip in n for n in testnets))
    monkeypatch.setattr(web.egress, "DECOYS", {4: web.ipaddress.ip_address("192.0.2.44"),
                                               6: web.ipaddress.ip_address("2001:db8:ffff::44")})
    yield
    web.OPENER[0] = None
    web.CONNECT[0] = None


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
    _fake_dns(monkeypatch, {"ok.example": "203.0.113.10", "inside.example": "10.0.0.1"})
    dialled = _fake_connect([(302, "http://inside.example/admin", b"")])
    with pytest.raises(ValueError, match="not a public address"):
        web.fetch_url("https://ok.example/")
    assert [d[2] for d in dialled] == ["203.0.113.10"]   # the private hop is never dialled


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


def test_a_stray_call_on_the_last_round_still_gets_an_answer(monkeypatch):
    """Live 2026-11-01: the no-tools round still returned a tool call and the customer
    read "kept calling tools without answering". Now the bot asks once more for words."""
    sent = []

    def complete(self, payload):
        sent.append(payload)
        if "tools" in payload or len(sent) == agent_mod.MAX_TOOL_ROUNDS + 1:
            return {"tool_calls": [{"id": "c", "function": {"name": "nope", "arguments": "{}"}}]}
        return {"content": "here is what I found"}
    monkeypatch.setattr(OpenAICompatAgent, "_complete", complete)
    assert OpenAICompatAgent("http://x/v1").ask("x", tools=web.TOOLS) == "here is what I found"
    assert "tools" not in sent[-1]
    assert "no longer available" in sent[-1]["messages"][-1]["content"]


def test_a_model_that_never_answers_still_raises(monkeypatch):
    def complete(self, payload):
        return {"tool_calls": [{"id": "c", "function": {"name": "nope", "arguments": "{}"}}]}
    monkeypatch.setattr(OpenAICompatAgent, "_complete", complete)
    import pytest
    with pytest.raises(agent_mod.AgentError):
        OpenAICompatAgent("http://x/v1").ask("x", tools=web.TOOLS)


# -- the host's own address never reaches a member ------------------------------

@pytest.mark.parametrize("url", [
    "https://api.ipify.org", "https://api64.ipify.org/?format=json", "https://ifconfig.me/ip",
    "https://ifconfig.co/json", "https://icanhazip.com", "https://ipv6.icanhazip.com",
    "https://ipinfo.io/json", "http://ip-api.com/json", "https://checkip.amazonaws.com",
    "https://www.whatismyip.com/", "https://whatismyipaddress.com/", "https://myip.wtf/",
    "https://www.iplocation.net/", "https://httpbin.org/ip", "https://MyIP.Example.net/"])
def test_what_is_my_ip_services_answer_the_decoy(url, monkeypatch):
    dialled = _fake_connect([(200, None, b"203.0.113.7")])
    out = web.fetch_url(url)
    assert dialled == []                      # the service is never contacted
    six = "6" in url.split("//")[1].split(".")[0]
    assert out == ("2001:db8:ffff::44" if six else "192.0.2.44")
    assert web.fetch_url(url) == out          # asked again: the same answer


@pytest.mark.parametrize("host", ["news.example", "ipv6.google.com", "shipify.example",
                                  "ipfs.io", "zipinfo.example"])
def test_ordinary_hosts_are_not_ip_echoes(host):
    assert not web.is_ip_echo(host)


def test_a_redirect_onto_an_ip_echo_answers_the_decoy(monkeypatch):
    _fake_dns(monkeypatch, {"short.example": "203.0.113.20"})
    dialled = _fake_connect([(301, "https://api.ipify.org/", b"")])
    assert web.fetch_url("https://short.example/x") == "192.0.2.44"
    assert len(dialled) == 1                  # the echo hop is never dialled


def test_fetch_dials_the_validated_address_with_the_real_host(monkeypatch):
    _fake_dns(monkeypatch, {"page.example": "203.0.113.30", "cdn.example": "198.51.100.4"})
    dialled = _fake_connect([(302, "//cdn.example/a?b=1", b""),
                             (200, None, b"<p>hello <b>world</b></p>")])
    assert web.fetch_url("https://page.example/start") == "hello world"
    assert dialled == [("https", "page.example", "203.0.113.30", 443, "/start", "page.example"),
                       ("https", "cdn.example", "198.51.100.4", 443, "/a?b=1", "cdn.example")]


def test_fetch_stops_after_too_many_redirects(monkeypatch):
    _fake_dns(monkeypatch, {"loop.example": "203.0.113.40"})
    _fake_connect([(302, "/again", b"")] * (web.MAX_REDIRECTS + 1))
    with pytest.raises(ValueError, match="redirects"):
        web.fetch_url("https://loop.example/")


def test_fetch_output_redacts_the_host_egress_address(monkeypatch):
    _fake_dns(monkeypatch, {"echo.example": "203.0.113.50"})
    _fake_connect([(200, None, b"you came from 198.51.100.77 today")])
    monkeypatch.setenv("AWDISCORD_REDACT_IPS", "198.51.100.77")
    out = web.fetch_url("https://echo.example/")
    assert out == "you came from 192.0.2.44 today"


def test_search_drops_ip_echo_results(monkeypatch):
    monkeypatch.setenv("AWDISCORD_SEARCH_URL", "https://search.internal:8114")
    rows = [{"title": "Your IP", "url": "https://api.ipify.org", "snippet": "s"},
            {"title": "What is my IP", "url": "https://www.whatismyipaddress.com/", "snippet": "s"},
            {"title": "IP explained", "url": "https://wiki.example/IP_address", "snippet": "s"}]
    body = json.dumps({"results": rows}).encode()
    web.OPENER[0] = lambda req, timeout=0, context=None: Resp(body)
    out = web.web_search("what is my ip")
    assert "ipify" not in out and "whatismyipaddress" not in out
    assert "wiki.example" in out


def test_egress_proxy_is_used_by_the_opener(monkeypatch):
    monkeypatch.setenv("AWDISCORD_EGRESS_PROXY", "http://proxy.example:3128")
    seen = {}

    class Opener:
        def open(self, req, timeout=0):
            seen["ok"] = True
            return Resp(RSS)

    def build_opener(*handlers):
        seen["proxies"] = [h.proxies for h in handlers
                           if isinstance(h, web.urllib.request.ProxyHandler)]
        return Opener()

    monkeypatch.setattr(web.urllib.request, "build_opener", build_opener)
    assert "Big story" in web.news_headlines()
    assert seen["proxies"] == [{"http": "http://proxy.example:3128",
                                "https": "http://proxy.example:3128"}]


def test_an_empty_default_search_falls_back_to_keyed_providers(monkeypatch):
    # 2026-11-11: the keyless default pick was bot-blocked upstream and returned
    # zero rows every time; the agent told members "the search tools are
    # returning nothing". A keyed provider is asked next.
    monkeypatch.setenv("AWDISCORD_SEARCH_URL", "https://search.internal:8114")
    monkeypatch.delenv("AWDISCORD_SEARCH_FALLBACK", raising=False)
    asked = []

    def opener(req, timeout=0, context=None):
        body = json.loads(req.data or b"{}")
        asked.append(body.get("provider", ""))
        rows = ([{"title": "Hit", "url": "https://exa.example/a", "snippet": "S"}]
                if body.get("provider") == "exa" else [])
        return Resp(json.dumps({"results": rows}).encode())
    web.OPENER[0] = opener
    out = web.web_search("clean room implementation")
    assert asked == ["", "exa"]
    assert "https://exa.example/a" in out


def test_a_failing_default_search_still_reaches_the_keyed_providers(monkeypatch):
    # Measured live 2026-11-11: the default pick timed out, the SearXNG-shape
    # retry got a 405, and that exception escaped before any fallback ran.
    import urllib.error
    monkeypatch.setenv("AWDISCORD_SEARCH_URL", "https://search.internal:8114")
    monkeypatch.delenv("AWDISCORD_SEARCH_FALLBACK", raising=False)

    def opener(req, timeout=0, context=None):
        body = json.loads(req.data or b"{}") if req.data else {}
        if body.get("provider") == "exa":
            return Resp(json.dumps({"results": [
                {"title": "Hit", "url": "https://exa.example/b", "snippet": "S"}]}).encode())
        if req.data:
            raise TimeoutError("The read operation timed out")
        raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {}, None)
    web.OPENER[0] = opener
    assert "https://exa.example/b" in web.web_search("q")


def test_search_fallback_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AWDISCORD_SEARCH_URL", "https://search.internal:8114")
    monkeypatch.setenv("AWDISCORD_SEARCH_FALLBACK", "")
    web.OPENER[0] = lambda req, timeout=0, context=None: Resp(b'{"results": []}')
    assert web.web_search("q").startswith("No results")
