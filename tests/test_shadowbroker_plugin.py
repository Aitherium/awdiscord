"""ShadowBroker is Aitherium's own board: the chat agent reads it, never web-searches its name."""

import hashlib
import hmac
import io
import json

import pytest
from awdiscord.builtin import shadowbroker as sb

ENVELOPE = {"ok": True, "command_id": "cmd_1", "tier": 1, "status": "completed",
            "result": {"ok": True, "data": {"results": [
                {"source_layer": "news", "title": "Series of US tech outages", "summary": "",
                 "source": "France 24", "link": "https://f24/x", "lat": None, "lng": None,
                 "risk_score": 1},
                {"source_layer": "liveuamap", "title": "Power outages in Kyiv after explosions",
                 "summary": "", "source": "liveuamap", "link": "https://lua/y",
                 "lat": 50.45182, "lng": 30.55967, "risk_score": None}],
                "version": 428, "truncated": False}}}


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("SHADOWBROKER_HMAC_SECRET", "s3cret")
    monkeypatch.setenv("SHADOWBROKER_URL", "https://board.example")


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, payload=ENVELOPE):
    seen = {}

    def fake(req, timeout=20):
        seen.update(url=req.full_url, headers=dict(req.header_items()), body=req.data)
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(sb, "_open", fake)
    return seen


def test_signature_matches_the_board_contract():
    h = sb.sign_headers("POST", "/api/ai/channel/command", b'{"a":1}', "k",
                        ts=1700000000, nonce="n1")
    msg = "POST|/api/ai/channel/command|1700000000|n1|" + hashlib.sha256(b'{"a":1}').hexdigest()
    assert h["X-SB-Signature"] == hmac.new(b"k", msg.encode(), hashlib.sha256).hexdigest()


def test_body_is_byte_identical_to_the_platform_client():
    body = sb._body({"cmd": "search_news", "args": {"query": "café", "limit": 3}})
    assert body == '{"cmd":"search_news","args":{"query":"café","limit":3}}'.encode()


def test_requests_carry_a_real_user_agent(configured, monkeypatch):
    seen = _capture(monkeypatch)
    sb.shadowbroker_news("outage", 3)
    ua = {k.lower(): v for k, v in seen["headers"].items()}["user-agent"]
    assert "urllib" not in ua.lower()      # the board's edge 403s urllib's default


def test_news_unwraps_the_envelope_and_shows_risk_and_place(configured, monkeypatch):
    seen = _capture(monkeypatch)
    out = sb.shadowbroker_news("outage", 5)
    assert seen["url"] == "https://board.example/api/ai/channel/command"
    assert "2 row(s)" in out and "Series of US tech outages" in out
    assert "risk 1" in out and "50.45,30.56" in out and "https://lua/y" in out


def test_unconfigured_says_so_and_forbids_a_name_search(monkeypatch, tmp_path):
    monkeypatch.delenv("SHADOWBROKER_HMAC_SECRET", raising=False)
    monkeypatch.delenv("SHADOWBROKER_AGENT_SECRET", raising=False)
    monkeypatch.setenv("SHADOWBROKER_ENV_FILE", str(tmp_path / "none.env"))
    out = sb.shadowbroker_news("x")
    assert "not configured" in out and "web search" in out


def test_refusal_is_reported_not_raised(configured, monkeypatch):
    import urllib.error

    def refuse(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(sb, "_open", refuse)
    assert "HTTP 403" in sb.shadowbroker_news("x")


def test_secret_can_come_from_the_state_file(monkeypatch, tmp_path):
    monkeypatch.delenv("SHADOWBROKER_HMAC_SECRET", raising=False)
    monkeypatch.delenv("SHADOWBROKER_AGENT_SECRET", raising=False)
    f = tmp_path / "sb.env"
    f.write_text("SHADOWBROKER_HMAC_SECRET=abc\nSHADOWBROKER_URL=https://b\n")
    monkeypatch.setenv("SHADOWBROKER_ENV_FILE", str(f))
    assert sb.config() == ("https://b", "abc")


def test_the_plugin_tells_every_persona_it_is_our_board():
    assert {t.name for t in sb.PLUGIN.tools} == {
        "shadowbroker_news", "shadowbroker_telemetry", "shadowbroker_status"}
    assert "Never web-search" in sb.PLUGIN.persona
    assert sb.PLUGIN.default_enabled


# --- the alert watch -----------------------------------------------------------

from awdiscord.plugins import ScopedStore  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402


class _Rest:
    def __init__(self, fail=False):
        self.posts, self.fail = [], fail

    def request(self, method, path, body=None, auth=True):
        if self.fail:
            raise RuntimeError("403")
        self.posts.append((path, body))
        return {"id": "m1"}


class _Host:
    def __init__(self, rest):
        self.store, self.rest = GuildStore(":memory:"), rest


def _rows_fixture(monkeypatch, rows):
    monkeypatch.setattr(sb, "sweep_board", lambda topics=sb.ALERT_TOPICS, per_topic=6: [
        dict(r) for r in rows])


ROWS = [{"title": "Explosion reported in Dnipro city", "source": "liveuamap",
         "source_layer": "liveuamap", "link": "https://l/1", "lat": 48.4, "lng": 35.1,
         "risk_score": None, "_topic": "explosion"},
        {"title": "Putin to meet Trump and Xi after attack on bridge", "source": "NPR",
         "source_layer": "news", "link": "https://n/2", "lat": None, "lng": None,
         "risk_score": 3, "_topic": "attack"}]


def test_alerts_are_off_until_a_channel_is_set(monkeypatch):
    host = _Host(_Rest())
    _rows_fixture(monkeypatch, ROWS)
    assert sb.run_alerts(host, "g1") == 0 and not host.rest.posts


def test_new_items_post_once_as_cards_highest_risk_first(monkeypatch):
    host = _Host(_Rest())
    ScopedStore(host.store, "g1", sb.PLUGIN.name).set("alerts", {"channel": "c9"})
    _rows_fixture(monkeypatch, ROWS)
    assert sb.run_alerts(host, "g1") == 2
    path, body = host.rest.posts[0]
    assert path == "/channels/c9/messages"
    assert body["embeds"][0]["title"].startswith("Putin")        # risk 3 first
    assert "risk 3" in body["embeds"][0]["footer"]["text"]
    assert "48.40, 35.10" in body["embeds"][1]["footer"]["text"]
    assert "By topic:" in body["content"]
    assert sb.run_alerts(host, "g1") == 0 and len(host.rest.posts) == 1   # remembered


def test_a_failed_post_retries_the_same_items(monkeypatch):
    host = _Host(_Rest(fail=True))
    ScopedStore(host.store, "g1", sb.PLUGIN.name).set("alerts", {"channel": "c9"})
    _rows_fixture(monkeypatch, ROWS)
    assert sb.run_alerts(host, "g1") == 0
    host.rest.fail = False
    assert sb.run_alerts(host, "g1") == 2


def test_trend_line_counts_topics_and_recurring_words():
    rows = [dict(title="Explosion in Dnipro", _topic="explosion"),
            dict(title="Another explosion in Dnipro", _topic="explosion"),
            dict(title="Drone attack on Kyiv", _topic="drone")]
    line = sb.trend_line(rows)
    assert line.startswith("By topic: explosion 2, drone 1")
    assert "dnipro" in line and "explosion" in line


NOW = 1795000000.0
LAYERS = {"news": [
    {"source_layer": "news", "title": "Explosion reported in Dnipro city",
     "link": "https://n/1", "source": "Kyiv Independent",
     "published": "Tue, 18 Nov 2026 10:48:55 -0500"},
    {"source_layer": "news", "title": "Old strike report from last week",
     "link": "https://n/2", "published": "Tue, 11 Nov 2026 10:00:00 GMT"},
    {"source_layer": "news", "title": "Outage hits payment terminals",
     "link": "https://n/3", "published": "Tue, 18 Nov 2026 14:58:55 GMT"}],
    "liveuamap": [
        {"title": "Drone attack on Kyiv", "link": "https://l/1", "timestamp": "21 hour ago"},
        {"title": "Missile strike weeks ago", "link": "https://l/2", "date": "1 week ago"}],
    "telegram_osint": {"posts": [
        {"title": "Missile strike overnight, per channel", "link": "https://t/1",
         "published": "2026-11-18T13:42:50+00:00"}]}}


def test_published_at_reads_both_board_date_formats():
    assert sb.published_at("Tue, 18 Nov 2026 10:00:00 GMT", now=NOW) == 1794996000.0
    assert sb.published_at("Tue, 18 Nov 2026 10:00:00 -0500", now=NOW) == 1795014000.0
    assert sb.published_at("21 hour ago", now=NOW) == NOW - 21 * 3600
    assert sb.published_at("1 week ago", now=NOW) == NOW - 7 * 86400
    assert sb.published_at("", now=NOW) is None
    assert sb.published_at("sometime", now=NOW) is None


def _slice_envelope(layers):
    """The board's real get_layer_slice envelope (pinned 2026-11-18)."""
    return {"ok": True, "status": 200, "data": {
        "ok": True, "command_id": "cmd_1", "status": "completed",
        "result": {"ok": True, "data": {"layers": layers, "version": 1}}}}


def test_sweep_keeps_only_recent_dated_topic_rows(configured, monkeypatch):
    monkeypatch.setattr(sb, "call", lambda method, path, payload=None: _slice_envelope(LAYERS))
    rows = sb.sweep_board(now=NOW, fresh_s=24 * 3600,
                          topics=("outage", "explosion", "drone", "missile",
                                  "attack", "strike"))
    titles = sorted(r["title"] for r in rows)
    # published within the window and matching a topic
    assert titles == ["Drone attack on Kyiv", "Explosion reported in Dnipro city",
                      "Missile strike overnight, per channel", "Outage hits payment terminals"]
    assert all(r["_published"] for r in rows)
    # a week-old report is dropped even though this server has never seen it
    assert "Old strike report from last week" not in titles
    assert "Missile strike weeks ago" not in titles


def test_an_undated_item_is_never_an_alert(configured, monkeypatch):
    monkeypatch.setattr(sb, "call", lambda method, path, payload=None: _slice_envelope(
        {"news": [{"title": "Explosion with no date", "link": "https://n/x"}]}))
    assert sb.sweep_board(now=NOW) == []


def test_commands_are_manager_only():
    group = next(c for c in sb.PLUGIN.commands if c.name == "shadowbroker")
    assert all(c.admin for c in group.subcommands)
