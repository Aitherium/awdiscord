"""POST /notify/owner: DMs every owner; the internal key opens this route only."""
import json
import types
import urllib.error
import urllib.request

import pytest
from awdiscord.agent import EchoAgent
from awdiscord.gateway import Bot

FAKE = "A" * 24 + "." + "B" * 6 + "." + "C" * 27   # bot-token shaped, not a token


def _bot(owners=("42", "43")):
    bot = Bot(FAKE, EchoAgent(), app_id="1", inbox_port=0, inbox_token="tok",
              owners=owners, internal_key="ik")
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


def test_internal_key_notifies_every_owner():
    bot, posted = _bot()
    try:
        out = json.loads(_post(bot, "/notify/owner", {"message": "New account - a@b.c"},
                               {"X-Internal-Key": "ik"}).read())
        assert out["messages"] == ["m-dm-42", "m-dm-43"]
        assert [p[0] for p in posted] == ["dm-42", "dm-43"]
        assert posted[0][1]["content"] == "New account - a@b.c"
    finally:
        bot.inbox_server.shutdown()


@pytest.mark.parametrize("path,body", [("/post", {"channel_id": "c", "content": "x"}),
                                       ("/approvals", {"title": "t"})])
def test_internal_key_does_not_open_other_routes(path, body):
    bot, posted = _bot()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(bot, path, body, {"X-Internal-Key": "ik"})
        assert exc.value.code == 401 and posted == []
    finally:
        bot.inbox_server.shutdown()


def test_wrong_key_refused_and_bearer_still_works():
    bot, posted = _bot()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(bot, "/notify/owner", {"message": "x"}, {"X-Internal-Key": "nope"})
        assert exc.value.code == 401
        out = json.loads(_post(bot, "/notify/owner", {"content": "y"},
                               {"Authorization": "Bearer tok"}).read())
        assert len(out["messages"]) == 2
    finally:
        bot.inbox_server.shutdown()


def test_no_owners_is_loud_not_silent():
    bot, posted = _bot(owners=())
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(bot, "/notify/owner", {"message": "x"}, {"X-Internal-Key": "ik"})
        assert exc.value.code == 409 and posted == []
    finally:
        bot.inbox_server.shutdown()
