"""The server's own emoji and stickers reach the agent as context, and stickers send.

Context, not training: the agent learns a server's expressions from a short block in
its prompt (``awdiscord.expressions``), and a ``[sticker:name]`` marker in its answer
becomes ``sticker_ids`` on the reply.
"""
from __future__ import annotations

from awdiscord import expressions as ex
from awdiscord import gateway, quiet
from awdiscord.dispatch import Dispatcher, reply_parts
from awdiscord.rest import RestError

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped

EMOJIS = [
    {"id": "11", "name": "pepe_laugh", "roles": [], "available": True},
    {"id": "12", "name": "party_parrot", "animated": True, "available": True},
    {"id": "13", "name": "boosters_only", "roles": ["r1"], "available": True},
    {"id": "14", "name": "lost_boost", "roles": [], "available": False},
]
STICKERS = [
    {"id": "21", "name": "Wave", "tags": "wave,hello", "description": "waving hello"},
    {"id": "22", "name": "gone", "tags": "x", "available": False},
]


class _Rest:
    def __init__(self, emojis=EMOJIS, stickers=STICKERS, fail=False):
        self.emojis, self.stickers, self.fail = emojis, stickers, fail
        self.fetches = 0

    def guild_emojis(self, guild):
        self.fetches += 1
        if self.fail:
            raise RestError(403, "Missing Access")
        return self.emojis

    def guild_stickers(self, guild):
        return self.stickers


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# -- the cache ----------------------------------------------------------------------

def test_cache_reuses_within_ttl_and_refetches_after():
    rest, clock = _Rest(), _Clock()
    cache = ex.ExpressionCache(rest, ttl=600, clock=clock)
    assert cache.get("g1") == (EMOJIS, STICKERS)
    clock.t += 599
    cache.get("g1")
    assert rest.fetches == 1
    clock.t += 2
    cache.get("g1")
    assert rest.fetches == 2


def test_cache_fails_soft_and_remembers_the_failure_briefly():
    rest, clock = _Rest(fail=True), _Clock()
    cache = ex.ExpressionCache(rest, ttl=600, fail_ttl=60, clock=clock)
    assert cache.get("g1") == ([], [])
    cache.get("g1")
    assert rest.fetches == 1                       # not hammered once per message
    clock.t += 61
    cache.get("g1")
    assert rest.fetches == 2


def test_cache_without_a_client_or_guild_is_empty():
    assert ex.ExpressionCache(lambda: None).get("g1") == ([], [])
    assert ex.ExpressionCache(_Rest()).get(None) == ([], [])


def test_cache_accepts_a_lazy_client():
    rest = _Rest()
    holder = {"rest": None}
    cache = ex.ExpressionCache(lambda: holder["rest"], fail_ttl=0)
    assert cache.get("g1") == ([], [])
    holder["rest"] = rest
    assert cache.get("g1") == (EMOJIS, STICKERS)


# -- the block ----------------------------------------------------------------------

def test_block_lists_usable_emoji_and_stickers_with_guidance():
    block = ex.context_block(EMOJIS, STICKERS)
    assert "<:pepe_laugh:11> pepe laugh" in block
    assert "<a:party_parrot:12> party parrot" in block
    assert "boosters_only" not in block            # role-restricted
    assert "lost_boost" not in block               # unavailable
    assert "- Wave: waving hello; tags: wave,hello" in block
    assert "gone" not in block
    assert "[sticker:name]" in block
    for rule in ("At most 1-2 per message", "serious, support, error or moderation",
                 "in place of words"):
        assert rule in block


def test_block_uses_the_operator_meanings():
    block = ex.context_block(EMOJIS, STICKERS, {"pepe_laugh": "something is funny"})
    assert "<:pepe_laugh:11> something is funny" in block


def test_block_is_empty_when_nothing_is_usable():
    assert ex.context_block([], []) == ""
    assert ex.context_block([EMOJIS[2], EMOJIS[3]], [STICKERS[1]]) == ""


def test_block_caps_entries_and_keeps_the_relevant_ones():
    many = [{"id": str(100 + i), "name": f"filler_{i}"} for i in range(80)]
    many.append({"id": "999", "name": "coffee_time"})
    block = ex.context_block(many, [], query="anyone want coffee?")
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(lines) == ex.MAX_ENTRIES
    assert "<:coffee_time:999>" in block
    assert "filler_79" not in block


def test_block_stickers_share_the_cap():
    em = [{"id": str(i), "name": f"e{i}"} for i in range(60)]
    st = [{"id": f"s{i}", "name": f"s{i}"} for i in range(20)]
    lines = [ln for ln in ex.context_block(em, st).splitlines() if ln.startswith("- ")]
    assert len(lines) == ex.MAX_ENTRIES
    assert sum(1 for ln in lines if ln.startswith("- s")) == ex.MAX_STICKERS


def test_block_without_stickers_for_slash_answers():
    block = ex.context_block(EMOJIS, STICKERS, allow_stickers=False)
    assert "pepe_laugh" in block and "Wave" not in block and "[sticker:" not in block


# -- the sticker marker -------------------------------------------------------------

def test_take_sticker_removes_the_marker_and_returns_one_id():
    text, sid = ex.take_sticker("Hello there! [sticker:wave]\n[sticker:Wave]", STICKERS)
    assert sid == "21"
    assert text == "Hello there!"


def test_unknown_or_unavailable_sticker_is_dropped_silently():
    assert ex.take_sticker("Hi [sticker:nope]", STICKERS) == ("Hi", None)
    assert ex.take_sticker("Hi [sticker:gone]", STICKERS) == ("Hi", None)


def test_first_known_sticker_wins():
    two = STICKERS + [{"id": "23", "name": "cheer"}]
    assert ex.take_sticker("[sticker:nope] [sticker:cheer] [sticker:wave] ok", two)[1] == "23"


def test_reply_parts_never_shows_a_marker():
    assert reply_parts("Done. [sticker:wave]")["content"] == "Done."


def test_polish_keeps_custom_emoji_codes():
    assert "<:pepe_laugh:11>" in quiet.polish("That is great <:pepe_laugh:11>")


# -- the dispatcher -----------------------------------------------------------------

class _Agent:
    name = "bonsai"
    personas: dict = {}

    def __init__(self, answer):
        self.answer, self.contexts = answer, []

    def ask(self, prompt, user="", context="", **kw):
        self.contexts.append(context)
        return self.answer


class _Host:
    store = None

    def __init__(self, rest):
        self.rest = rest

    def persona_for(self, guild):
        return "Plugin persona."

    def __getattr__(self, name):            # anything else the dispatcher asks: absent
        raise AttributeError(name)


def _dispatcher(agent, rest):
    d = Dispatcher(agent)
    d.expressions = ex.ExpressionCache(rest)
    return d


def test_chat_gives_the_agent_the_block_and_sends_the_sticker():
    agent = _Agent("Welcome aboard! <:pepe_laugh:11> [sticker:wave]")
    out = _dispatcher(agent, _Rest()).chat("u1", "g1", "hello everyone")
    assert "<:pepe_laugh:11>" in agent.contexts[0]
    assert out["content"] == "Welcome aboard! <:pepe_laugh:11>"
    assert out["sticker_ids"] == ["21"]


def test_chat_with_unknown_sticker_sends_text_only():
    out = _dispatcher(_Agent("Hi [sticker:nope]"), _Rest()).chat("u1", "g1", "hi")
    assert out["content"] == "Hi" and "sticker_ids" not in out


def test_rest_failure_gives_an_empty_block_and_a_normal_reply():
    agent = _Agent("Hello! [sticker:wave]")
    out = _dispatcher(agent, _Rest(fail=True)).chat("u1", "g1", "hi")
    assert "custom emoji" not in agent.contexts[0]
    assert out["content"] == "Hello!" and "sticker_ids" not in out


def test_dm_has_no_expressions():
    agent = _Agent("Hi [sticker:wave]")
    out = _dispatcher(agent, _Rest()).chat("u1", None, "hi")
    assert "custom emoji" not in (agent.contexts[0] or "")
    assert "sticker_ids" not in out


def test_dispatcher_reads_the_hosts_rest_lazily():
    rest = _Rest()
    d = Dispatcher(_Agent("ok"))
    d.host = _Host(rest)
    assert "<:pepe_laugh:11>" in d.expressions_context("g1", "hi")


# -- the gateway --------------------------------------------------------------------

class _PostRest:
    def __init__(self, refuse_sticker=True):
        self.refuse, self.posts = refuse_sticker, []

    def create_message(self, channel, payload):
        if self.refuse and payload.get("sticker_ids"):
            raise RestError(400, "Invalid Form Body")
        self.posts.append((channel, payload))
        return {"id": "p1"}


def test_gateway_drops_a_refused_sticker_never_the_answer():
    bot = gateway.Bot(TOKEN, _Agent("x"), app_id="app")
    bot.rest = _PostRest()
    bot._post_reply("c1", {"content": "Hello", "sticker_ids": ["21"]})
    assert bot.rest.posts == [("c1", {"content": "Hello"})]
