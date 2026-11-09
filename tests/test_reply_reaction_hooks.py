"""0.4.0: plugins see posted answers (on_reply) and reactions (on_reaction), commands
nest one group deep, and modals take paragraphs."""
from __future__ import annotations

from types import SimpleNamespace

from awdiscord import gateway
from awdiscord.dispatch import COMMAND, Dispatcher
from awdiscord.plugins import Command, Ctx, Plugin, PluginHost
from awdiscord.store import GuildStore

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped

def _host(tmp_path, *plugins):
    return PluginHost(GuildStore(tmp_path / "g.db"), plugins=list(plugins))


def _turn(**kw):
    t = {"guild_id": "g1", "channel_id": "c1", "user": "u1", "message_id": "m9",
         "prompt": "q", "reply": "a", "agent": "x", "via": "mention"}
    t.update(kw)
    return t


# -- command groups and modals ---------------------------------------------------

def test_subcommand_group_definition_and_routing(tmp_path):
    hit = []
    p = Plugin("p", "0.1", "p", default_enabled=True, commands=[Command(
        "aither", "A", subcommands=[Command("feedback", "F", subcommands=[
            Command("stats", "S", lambda ctx: hit.append(ctx.sub) or ctx.reply("ok"))])])])
    body = p.commands[0].to_discord()
    assert body["options"][0]["type"] == 2
    assert body["options"][0]["options"][0] == {"type": 1, "name": "stats",
                                                "description": "S", "options": []}
    host = _host(tmp_path, p)
    it = {"type": COMMAND, "guild_id": "g1", "member": {"user": {"id": "u"}},
          "data": {"name": "aither", "options": [{"type": 2, "name": "feedback",
                                                  "options": [{"type": 1, "name": "stats",
                                                               "options": []}]}]}}
    res = host.handle(it)
    assert hit == ["feedback stats"] and res.response["data"]["content"] == "ok"
    it["data"]["options"] = [{"type": 2, "name": "feedback", "options": []}]
    assert "subcommand" in host.handle(it).response["data"]["content"]


def test_modal_paragraph_field(tmp_path):
    p = Plugin("p", "0.1", "p")
    ctx = Ctx(_host(tmp_path, p), p, {"guild_id": "g"}, {}, None)
    rows = ctx.modal("x", "T", [{"id": "a", "label": "A", "paragraph": True},
                                {"id": "b", "label": "B"}]).response["data"]["components"]
    assert [r["components"][0]["style"] for r in rows] == [2, 1]


# -- reply hooks -----------------------------------------------------------------

def test_reply_hooks_only_for_active_plugins_and_survive_a_bug(tmp_path):
    seen = []

    def boom(e):
        raise RuntimeError("bug")
    row = {"type": 1, "components": []}
    on = Plugin("on", "0.1", "on", default_enabled=True,
                on_reply=[boom, lambda e: seen.append((e.message_id, e.reply, e.via)) or [row]])
    off = Plugin("off", "0.1", "off", on_reply=[lambda e: seen.append("off") or [row]])
    host = _host(tmp_path, on, off)
    assert host.dispatch_reply(_turn()) == [row]
    assert seen == [("m9", "a", "mention")]
    assert host.dispatch_reply(_turn(message_id="")) == []
    assert host.has_reply_hooks("g1") and host.has_reply_hooks(None)


def test_dispatcher_fills_the_turn_for_mentions():
    class A:
        name = "bonsai"

        def ask(self, prompt, *, user=""):
            return "long answer " * 300
    turn: dict = {}
    out = Dispatcher(A()).chat("u1", "g1", "<@1> hi", bot_id="1", turn=turn)
    assert turn["prompt"] == "hi" and turn["agent"] == "bonsai" and turn["via"] == "mention"
    assert len(turn["reply"]) > 2000 and len(out["content"]) <= 2000
    refused: dict = {}
    Dispatcher(A()).chat("u1", "g1", "", turn=refused)
    assert refused == {}


class _Rest:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        return {}

    def create_message(self, channel, payload):
        self.calls.append(("POST", channel, payload))
        return {"id": "m9", "channel_id": channel}

    def interaction_callback(self, iid, token, payload):
        self.calls.append(("CB", iid, payload))

    def edit_original(self, app, token, payload):
        self.calls.append(("EDIT", token, payload))
        return {"id": "m10", "channel_id": "c1"}


class _Agent:
    name = "bonsai"
    personas: dict = {}

    def ask(self, prompt, **k):
        return "the answer"


def _bot(tmp_path, plugin):
    bot = gateway.Bot(TOKEN, _Agent(), host=_host(tmp_path, plugin), app_id="app")
    bot.rest = _Rest()
    return bot


def test_mention_answer_gets_the_plugins_buttons(tmp_path):
    got = []
    row = {"type": 1, "components": [{"type": 2, "custom_id": "awp:p:up:m9"}]}
    p = Plugin("p", "0.1", "p", default_enabled=True,
               on_reply=[lambda e: got.append((e.prompt, e.reply, e.prompt_message_id,
                                               e.agent)) or [row]])
    bot = _bot(tmp_path, p)
    bot.handle_message({"id": "m1", "channel_id": "c1", "guild_id": "g1", "user": "u1",
                        "content": "<@b> why?", "bot_id": "b"})
    assert got == [("why?", "the answer", "m1", "bonsai")]
    assert ("PATCH", "/channels/c1/messages/m9", {"components": [row]}) in bot.rest.calls


def test_no_reply_hook_means_no_extra_call(tmp_path):
    bot = _bot(tmp_path, Plugin("p", "0.1", "p", default_enabled=True))
    bot.handle_message({"id": "m1", "channel_id": "c1", "guild_id": "g1", "user": "u1",
                        "content": "hi"})
    calls = [c for c in bot.rest.calls if not str(c[1]).startswith("/guilds/")]
    assert [c[0] for c in calls] == ["POST", "POST"]   # typing + the answer (minus
    # containment's one channel-list lookup)


def test_ask_answer_gets_buttons_on_the_original(tmp_path):
    row = {"type": 1, "components": []}
    got = []
    p = Plugin("p", "0.1", "p", default_enabled=True,
               on_reply=[lambda e: got.append((e.message_id, e.via, e.user)) or [row]])
    bot = _bot(tmp_path, p)
    bot.handle_raw({"id": "i1", "token": "t", "type": COMMAND, "guild_id": "g1",
                    "channel_id": "c1", "member": {"user": {"id": "u1"}},
                    "data": {"name": "ask", "options": [{"name": "question",
                                                         "value": "why"}]}})
    assert got == [("m10", "ask", "u1")]
    assert bot.rest.calls[-1] == ("EDIT", "t", {"components": [row]})


# -- reactions -------------------------------------------------------------------

def test_reactions_reach_hooks_but_never_from_bots(tmp_path):
    seen = []
    p = Plugin("p", "0.1", "p", default_enabled=True,
               on_reaction=[lambda e: seen.append((e.emoji, e.added, e.message_id))])
    host = _host(tmp_path, p)
    evt = {"guild_id": "g1", "channel_id": "c1", "user": "u1", "message_id": "m9",
           "emoji": "\U0001f44d", "added": True}
    assert host.dispatch_reaction(evt) == {"p": "ok"}
    assert host.dispatch_reaction({**evt, "user_bot": True}) == {}
    assert host.dispatch_reaction({**evt, "added": False}) == {"p": "ok"}
    assert seen == [("\U0001f44d", True, "m9"), ("\U0001f44d", False, "m9")]
    assert _bot(tmp_path / "b", p).handle_reaction(evt) == {"p": "ok"}


def test_reactions_intent_is_opt_in(monkeypatch):
    class Intents:
        @classmethod
        def none(cls):
            return SimpleNamespace()
    d = SimpleNamespace(Intents=Intents)
    monkeypatch.delenv("AWDISCORD_REACTIONS", raising=False)
    intents, _, _ = gateway.build_intents(d)
    assert intents.guild_reactions is False and intents.dm_reactions is False
    monkeypatch.setenv("AWDISCORD_REACTIONS", "1")
    intents, _, _ = gateway.build_intents(d)
    assert intents.guild_reactions is True and intents.dm_reactions is True
