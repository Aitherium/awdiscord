"""Persona posting through channel webhooks: Rest webhook calls and post_webhook's
avatar_url. Discord REST is mocked through the Rest opener."""
from __future__ import annotations

import io
import json
import urllib.parse

import pytest
from awdiscord import webhook
from awdiscord.rest import Rest

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer if answer is not None else {"id": "1"}

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        return _Resp(json.dumps(self.answer).encode())


def _rest(answer=None):
    op = Opener(answer)
    return Rest(TOKEN, opener=op), op


def test_execute_webhook_waits_and_never_mentions():
    rest, op = _rest()
    rest.execute_webhook("w1", "tok", {"content": "hi @everyone", "username": "Lyra",
                                       "avatar_url": "https://example.test/lyra.webp",
                                       "allowed_mentions": {"parse": ["everyone"]}})
    method, url, headers, body = op.calls[0]
    assert method == "POST"
    parsed = urllib.parse.urlparse(url)
    assert parsed.path.endswith("/webhooks/w1/tok")
    assert urllib.parse.parse_qs(parsed.query) == {"wait": ["true"]}
    assert body["allowed_mentions"] == {"parse": []}
    assert body["username"] == "Lyra"
    assert body["avatar_url"] == "https://example.test/lyra.webp"
    assert "Authorization" not in headers                 # the webhook token is the auth


def test_execute_webhook_without_wait():
    rest, op = _rest()
    rest.execute_webhook("w1", "tok", {"content": "x"}, wait=False)
    assert "wait" not in op.calls[0][1]


def test_channel_webhooks_and_create_webhook_paths():
    rest, op = _rest([])
    assert rest.channel_webhooks("c9") == []
    assert op.calls[0][0] == "GET" and op.calls[0][1].endswith("/channels/c9/webhooks")
    rest2, op2 = _rest({"id": "w", "token": "t"})
    assert rest2.create_webhook("c9", "Agents" * 30)["id"] == "w"
    method, url, headers, body = op2.calls[0]
    assert method == "POST" and url.endswith("/channels/c9/webhooks")
    assert len(body["name"]) <= 80
    assert headers["Authorization"].startswith("Bot ")


def test_post_webhook_carries_avatar_and_username(monkeypatch):
    seen = {}

    def fake_open(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp(b"{}")
    monkeypatch.setattr(webhook.urllib.request, "urlopen", fake_open)
    webhook.post_webhook("https://discord.com/api/webhooks/1/abc", "hello",
                         username="Atlas", avatar_url="https://example.test/atlas.webp")
    assert seen["body"]["username"] == "Atlas"
    assert seen["body"]["avatar_url"] == "https://example.test/atlas.webp"
    assert seen["body"]["allowed_mentions"] == {"parse": []}
    assert seen["url"].endswith("?wait=true")


def test_post_webhook_refuses_plain_http_avatar():
    with pytest.raises(webhook.WebhookError):
        webhook.post_webhook("https://discord.com/api/webhooks/1/abc", "hi",
                             avatar_url="http://example.test/a.webp")
