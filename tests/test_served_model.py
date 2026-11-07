"""/status names the model that ACTUALLY answered, and a substitution is spelled out.

2026-11-01: the bot reported its configured model (bonsai-27b-dgx) while that lane was
down and another model answered. The configured name alone is not the active model.
"""
from awdiscord.agent import OpenAICompatAgent, served_route
from awdiscord.dispatch import Dispatcher


def test_route_is_read_from_aither_route_then_model_field():
    r = served_route("bonsai2-27b", {"aither_route": {
        "requested": "bonsai2-27b", "served_by": "deepseek-v4-flash-pool",
        "cross_model": True}})
    assert r == {"requested": "bonsai2-27b", "served_by": "deepseek-v4-flash-pool",
                 "cross_model": True}
    assert served_route("a", {"model": "b"})["cross_model"] is True
    assert served_route("a", {"model": "a"})["cross_model"] is False
    assert served_route("a", {})["served_by"] == ""          # unknown is never "a"


def _agent(monkeypatch, body):
    a = OpenAICompatAgent("https://ms.invalid/v1", model="bonsai2-27b-5090")
    monkeypatch.setattr(a, "health", lambda: "ok")

    class R:
        status = 200

        def read(self):
            import json
            return json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: R())
    return a


def test_status_shows_the_substitute_that_answered(monkeypatch):
    a = _agent(monkeypatch, {"aither_route": {"requested": "bonsai2-27b-5090",
                                              "served_by": "deepseek-v4-flash-pool",
                                              "cross_model": True},
                             "choices": [{"message": {"content": "hi"}}]})
    assert a.ask("hello") == "hi"
    out = Dispatcher(a).handle({"type": 2, "user": {"id": "1"},
                                "data": {"name": "status"}}).response["data"]["content"]
    assert "ANSWERED BY deepseek-v4-flash-pool" in out and "substituted" in out


def test_status_before_any_turn_says_so(monkeypatch):
    a = _agent(monkeypatch, {})
    out = Dispatcher(a).handle({"type": 2, "user": {"id": "1"},
                                "data": {"name": "status"}}).response["data"]["content"]
    assert "no turn answered yet" in out
