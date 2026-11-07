"""Hub: operator-only ops commands, alert buttons, and the /notify/channel inbox route."""
from __future__ import annotations

import io
import json
import types
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from awdiscord.agent import EchoAgent
from awdiscord.builtin import hub
from awdiscord.dispatch import DEFERRED, EPHEMERAL, LIMIT
from awdiscord.gateway import Bot
from awdiscord.plugins import build_host
from awdiscord.store import GuildStore

NOW = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
FAKE = "A" * 24 + "." + "B" * 6 + "." + "C" * 27   # bot-token shaped, not a token

# Shapes measured from inside aitheros-awdiscord on 2026-10-31.
STATS = {"active_count": 16, "total_historical": 16, "acknowledged_count": 2,
         "critical_count": 11, "by_category": {"prometheus": 11},
         "total_occurrences_today": 782, "most_frequent": "pain:resource_aitherdemand"}
ALERTS = {"alerts": [
    {"alert_key": "pain:metric_alert", "title": "Metric Alert Firing", "severity": 1.0,
     "occurrence_count": 18, "acknowledged": False, "last_seen": "2026-10-31T16:08:15"},
    {"alert_key": "prometheus:BackupRefusedLowDiskSpace:strata", "severity": 0.9,
     "title": "Strata full backup REFUSED", "occurrence_count": 3, "acknowledged": True},
    {"alert_key": "pain:metric_alert", "title": "Metric Alert Firing", "severity": 0.5,
     "occurrence_count": 3, "acknowledged": False},
    {"alert_key": "x" * 120, "title": "long key", "severity": 0.2, "occurrence_count": 1},
], "count": 4, "stats": STATS}
REG = {"events": [
    {"timestamp": "2026-10-31T10:00:00Z",
     "data": {"username": "bob", "source": "registration:github"}},
    {"timestamp": "2026-10-20T10:00:00Z", "data": {"username": "old", "source": "x"}}]}
SUBS = {"events": [
    {"timestamp": "2026-10-31T12:00:00Z", "data": {"email": "alice@ex.com", "source": "blog"}},
    {"timestamp": "2026-10-31T13:00:00Z",
     "data": {"email": "dup@ex.com", "source": "blog", "already_subscribed": True}}]}
GH = {"items": [
    {"number": 7, "title": "help", "comments": 0, "html_url": "https://github.com/A/awdk/7",
     "repository_url": "https://api.github.com/repos/A/awdk", "created_at": "2026-10-01T0",
     "user": {"login": "cullenmeyers", "type": "User"}},
    {"number": 8, "title": "answered", "comments": 1, "html_url": "https://github.com/A/awdk/8",
     "comments_url": "https://api.github.com/c/8", "pull_request": {},
     "repository_url": "https://api.github.com/repos/A/awdk", "created_at": "2026-10-02T0",
     "user": {"login": "someone", "type": "User"}},
    {"number": 9, "title": "ours", "comments": 0, "html_url": "u",
     "repository_url": "r/awdk", "user": {"login": "wizzense"}},
    {"number": 10, "title": "bot", "comments": 0, "html_url": "u",
     "repository_url": "r/awdk", "user": {"login": "dependabot[bot]", "type": "Bot"}}]}
REPORTS = {"reports": [{"created_at": "2026-10-31T01", "channel": "general",
                        "message_author": "troll", "reason": "spam",
                        "message_content": "buy now"}], "count": 1}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Fake:
    """Routes a urllib Request to a canned body by URL substring; records calls."""

    def __init__(self, routes: dict, fail: dict | None = None):
        self.routes, self.fail, self.calls = routes, fail or {}, []

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        self.calls.append((req.get_method(), url, dict(req.header_items()), context))
        for frag, code in self.fail.items():
            if frag in url:
                if code == 0:
                    raise urllib.error.URLError("Name or service not known")
                raise urllib.error.HTTPError(url, code, "no", {}, None)
        for frag, body in self.routes.items():
            if frag in url:
                return _Resp(json.dumps(body).encode())
        raise urllib.error.HTTPError(url, 404, "no route", {}, None)


ROUTES = {"/alerts/stats": STATS, "/alerts/": {"ok": True}, "/alerts": ALERTS,
          "growth.account.registered": REG, "growth.newsletter.subscribed": SUBS,
          "search/issues": GH, "/c/8": [{"user": {"login": "wizzense"}}],
          "/v1/reports": REPORTS, "status=open": {"tickets": [], "total": 4},
          "status=in_progress": {"tickets": [], "total": 1}}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("AITHER_INTERNAL_SECRET", "ik")
    monkeypatch.setenv("AWDISCORD_CA_BUNDLE", "")
    monkeypatch.setattr(hub, "DEFAULT_CA", "/nonexistent/ca.pem")
    f = Fake(dict(ROUTES))
    monkeypatch.setattr(hub, "OPENER", [f])
    return f


def _cmd(name, user="7", options=None, guild="g"):
    it = {"type": 2, "id": "1", "token": "t", "guild_id": guild, "channel_id": "c",
          "data": {"name": name, "options": options or []},
          "member": {"user": {"id": user}, "permissions": str(1 << 5)}}
    if guild is None:
        it.pop("guild_id")
        it["user"] = it.pop("member")["user"]
    return it


def _host():
    return build_host(GuildStore(":memory:"), operators={"7"})


# ── sources ──────────────────────────────────────────────────────────────────

def test_every_fleet_call_carries_the_internal_key_and_verifies_tls(fake):
    hub.pulse_stats()
    method, url, headers, ctx = fake.calls[0]
    assert url == "https://aither-pulse:8081/alerts/stats" and method == "GET"
    assert headers["X-internal-key"] == "ik" and headers["X-caller-type"] == "platform"
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_no_internal_key_is_an_error_not_an_anonymous_call(fake, monkeypatch):
    monkeypatch.delenv("AITHER_INTERNAL_SECRET")
    with pytest.raises(hub.SourceError, match="AITHER_INTERNAL_SECRET unset"):
        hub.pulse_stats()
    assert fake.calls == []


def test_base_urls_follow_env(fake, monkeypatch):
    monkeypatch.setenv("AWDISCORD_PULSE_URL", "https://pulse.test:1/")
    hub.pulse_stats()
    assert fake.calls[0][1] == "https://pulse.test:1/alerts/stats"


def test_alerts_group_by_key_worst_first(fake):
    rows = hub.pulse_alerts()
    assert [r["key"] for r in rows][:2] == ["pain:metric_alert",
                                            "prometheus:BackupRefusedLowDiskSpace:strata"]
    assert rows[0]["count"] == 21 and rows[0]["severity"] == 1.0
    assert rows[0]["acknowledged"] is False and rows[1]["acknowledged"] is True


def test_signups_last_window_masks_email_and_drops_resubscribes(fake):
    rows = hub.signups(24, now=NOW)
    assert [(r["kind"], r["who"], r["method"]) for r in rows] == [
        ("blog subscriber", "a***@ex.com", "blog"), ("account", "bob", "github")]


def test_signups_partial_failure_is_named(fake):
    fake.fail = {"growth.newsletter": 503}
    rows = hub.signups(24, now=NOW)
    assert rows[-1] == {"error": "newsletter.subscribed: HTTP 503"}
    fake.fail = {"/api/events": 503}
    with pytest.raises(hub.SourceError, match="account.registered: HTTP 503"):
        hub.signups(24, now=NOW)


def test_issues_waiting_skips_ours_bots_and_answered(fake):
    waiting = hub.issues_waiting()
    assert [w["ref"] for w in waiting] == ["awdk#7"]
    assert "org%3AAitherium+-repo%3AAitherium%2FAitherOS+is%3Aopen" in fake.calls[0][1]


def test_issue_comment_read_failure_is_counted_and_shown(fake):
    # GitHub 403 (unauthenticated rate limit) on the comments call: the item stays
    # listed, but the miss is named, never silently folded into the number.
    fake.fail = {"/c/8": 403}
    waiting = hub.issues_waiting()
    assert [w["ref"] for w in waiting] == ["awdk#7", "awdk#8"]
    assert waiting[1]["unverified"] == "HTTP 403"
    out = hub.today(NOW)
    assert ("GitHub: 2 outside issues/PRs waiting on a reply "
            "(partial: 1 could not be checked for our reply: HTTP 403)") in out
    msg = hub.issues_message(waiting)
    assert "(partial: 1 could not be checked" in msg and "[reply unverified]" in msg


# ── /today ───────────────────────────────────────────────────────────────────

def test_today_one_message_every_source(fake):
    out = hub.today(NOW)
    assert "Alerts: 16 active, 11 critical, 14 unacked" in out
    assert "Last 24h: 1 new accounts, 1 new blog subscribers" in out
    assert "GitHub: 1 outside issues/PRs waiting on a reply" in out
    assert "Moderation: 1 open reports" in out
    assert "Support: 4 open tickets, 1 in progress" in out
    assert len(out) <= LIMIT


def test_today_shows_each_source_error_instead_of_a_number(fake):
    fake.fail = {"aither-pulse": 503, "communication-core": 0, "api.github.com": 403}
    out = hub.today(NOW)
    assert "Alerts: could not read (HTTP 503)" in out
    assert "Moderation: could not read (unreachable (Name or service not known))" in out
    assert "GitHub: could not read (GitHub HTTP 403)" in out
    assert "Support: 4 open tickets" in out          # the others still answer


# ── commands ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["today", "alerts", "signups", "issues", "reports"])
def test_commands_refuse_non_operators_even_server_managers(fake, name):
    out = _host().handle(_cmd(name, user="8"))
    assert out.response["data"]["flags"] == EPHEMERAL
    assert "operators" in out.response["data"]["content"]
    assert fake.calls == []


@pytest.mark.parametrize("name", ["today", "alerts", "signups", "issues", "reports"])
def test_commands_ack_deferred_then_answer(fake, name):
    out = _host().handle(_cmd(name))
    assert out.response["type"] == DEFERRED          # ack inside Discord's 3 s
    payload = out.deferred()
    assert payload["content"] and len(payload["content"]) <= LIMIT
    assert payload["allowed_mentions"] == {"parse": []}


def test_commands_are_hidden_from_members_and_work_in_dms(fake):
    defs = {d["name"]: d for d in _host().command_defs()}
    for name in ("today", "alerts", "signups", "issues", "reports"):
        assert defs[name]["default_member_permissions"] == str(1 << 5)
        assert 1 in defs[name]["contexts"]
    out = _host().handle(_cmd("today", guild=None))
    assert out.response["type"] == DEFERRED


@pytest.mark.parametrize("name", ["alerts", "issues", "reports"])
def test_sensitive_replies_are_private(fake, name):
    # /reports carries reported content + author + reason; /issues outsiders' logins;
    # /alerts internal alert keys: none of it goes to everyone in the channel.
    out = _host().handle(_cmd(name))
    assert out.response["type"] == DEFERRED
    assert out.response["data"]["flags"] == EPHEMERAL


def test_today_counts_stay_in_channel(fake):
    assert _host().handle(_cmd("today")).response["data"].get("flags") is None


def test_signups_reply_is_private(fake, monkeypatch):
    # The command reads the real clock; pin it to the fixture's day, or the canned
    # 2026-10-31 events fall outside the window two days later and the test rots.
    real = hub.signups
    monkeypatch.setattr(hub, "signups", lambda hours=24, now=None: real(hours, now=now or NOW))
    out = _host().handle(_cmd("signups", options=[{"name": "hours", "value": 48}]))
    assert out.response["data"]["flags"] == EPHEMERAL
    assert "bob via github" in out.deferred()["content"]


def test_alerts_rows_carry_ack_resolve_buttons(fake):
    payload = _host().handle(_cmd("alerts", options=[{"name": "n", "value": 10}])).deferred()
    assert "x21" in payload["content"] and "long key" in payload["content"]
    rows = payload["components"]
    assert len(rows) == 3                     # the 120-char key gets a digest button
    long_id = rows[2]["components"][1]["custom_id"]
    assert long_id.startswith("awp:hub:r#") and len(long_id) <= 100
    first = rows[0]["components"]
    assert first[0]["custom_id"] == "awp:hub:a:pain:metric_alert"
    assert first[1]["custom_id"] == "awp:hub:r:pain:metric_alert"
    assert rows[1]["components"][0]["disabled"] is True        # already acked


def test_source_down_answers_with_the_error(fake):
    fake.fail = {"/v1/reports": 401}
    payload = _host().handle(_cmd("reports")).deferred()
    assert payload["content"] == "Could not read: HTTP 401"


def _press(cid, user="7"):
    return {"type": 3, "id": "1", "token": "t", "guild_id": "g", "channel_id": "c",
            "data": {"custom_id": cid}, "member": {"user": {"id": user}, "permissions": "0"}}


def test_button_rechecks_operator_then_posts_to_pulse(fake):
    host = _host()
    denied = host.handle_button(_press("awp:hub:r:pain:metric_alert", user="8"))
    assert "operators" in denied.response["data"]["content"] and fake.calls == []
    out = host.handle_button(_press("awp:hub:a:prometheus:Backup:strata"))
    assert out.response["data"]["flags"] == EPHEMERAL
    assert "Acknowledged `prometheus:Backup:strata`" in out.deferred()["content"]
    method, url, _, _ = fake.calls[-1]
    assert method == "POST"
    assert url == "https://aither-pulse:8081/alerts/prometheus%3ABackup%3Astrata/acknowledge"


def test_button_pulse_refusal_is_shown(fake):
    fake.fail = {"/resolve": 404}
    out = _host().handle_button(_press("awp:hub:r:gone")).deferred()
    assert out["content"] == "Pulse refused to resolve `gone`: HTTP 404"


def test_digest_button_resolves_the_long_key_among_active_alerts(fake):
    host = _host()
    rows = host.handle(_cmd("alerts", options=[{"name": "n", "value": 10}])).deferred()
    cid = rows["components"][2]["components"][1]["custom_id"]
    out = host.handle_button(_press(cid)).deferred()
    assert out["content"].startswith("Resolved `" + "x" * 120 + "`")
    assert fake.calls[-1][1].endswith("/alerts/" + "x" * 120 + "/resolve")
    gone = host.handle_button(_press("awp:hub:a#0123456789abcdef")).deferred()
    assert gone["content"].endswith("that alert is no longer active")


def test_unknown_button_payload_refused(fake):
    out = _host().handle_button(_press("awp:hub:z:k"))
    assert "not one of the hub's" in out.response["data"]["content"]


# ── /notify/channel ──────────────────────────────────────────────────────────

def _bot(ops_channel="900"):
    bot = Bot(FAKE, EchoAgent(), app_id="1", inbox_port=0, inbox_token="tok",
              owners=("42",), internal_key="ik", ops_channel=ops_channel)
    posted = []
    bot.rest = types.SimpleNamespace(
        dm_channel=lambda uid: {"id": f"dm-{uid}"},
        create_message=lambda ch, payload: posted.append((ch, payload)) or {"id": f"m-{ch}"})
    bot._start_inbox()
    return bot, posted


def _post(bot, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{bot.inbox_port}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    return urllib.request.urlopen(req, timeout=5)


def test_notify_channel_posts_to_the_ops_channel_with_internal_key():
    bot, posted = _bot()
    try:
        out = json.loads(_post(bot, "/notify/channel", {"message": "digest"},
                               {"X-Internal-Key": "ik"}).read())
        assert out == {"id": "m-900", "channel_id": "900"}
        assert posted == [("900", {"content": "digest"})]
        out = json.loads(_post(bot, "/notify/channel", {"content": "b"},
                               {"Authorization": "Bearer tok"}).read())
        assert out["channel_id"] == "900"
    finally:
        bot.inbox_server.shutdown()


def test_notify_channel_refuses_wrong_key_and_ignores_a_channel_override():
    bot, posted = _bot()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(bot, "/notify/channel", {"message": "x"}, {"X-Internal-Key": "nope"})
        assert exc.value.code == 401 and posted == []
        _post(bot, "/notify/channel", {"message": "x", "channel_id": "evil"},
              {"X-Internal-Key": "ik"})
        assert posted == [("900", {"content": "x"})]
    finally:
        bot.inbox_server.shutdown()


def test_no_ops_channel_is_409_not_a_silent_drop():
    bot, posted = _bot(ops_channel="")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(bot, "/notify/channel", {"message": "x"}, {"X-Internal-Key": "ik"})
        assert exc.value.code == 409 and posted == []
        # The explicit marker callers key "skipped" on (not the bare status code).
        assert json.loads(exc.value.read())["reason"] == "no_ops_channel"
    finally:
        bot.inbox_server.shutdown()


def test_ops_channel_comes_from_env(monkeypatch):
    monkeypatch.setenv("AWDISCORD_TOKEN", FAKE)
    monkeypatch.setenv("AWDISCORD_OPS_CHANNEL", "123")
    monkeypatch.setenv("AWDISCORD_NO_PLUGINS", "1")
    assert Bot.from_env(agent=EchoAgent()).ops_channel == "123"
