"""A chat answer built from a web search says only what the search returned.

Reproduces 2026-11-19 in a public channel: after a search the bot said "DuckDuckGo
operates its own search backend rather than proxying Google" (its results come
mostly from Bing), called YaCy "YACI", and addressed a "Demetri" nobody had named.
"""
from __future__ import annotations

import json

from awdiscord import factfloor
from awdiscord.agent import OpenAICompatAgent
from awdiscord.plugins import Tool

DDG = ("- DuckDuckGo - Wikipedia\n  https://en.wikipedia.org/wiki/DuckDuckGo\n"
       "  DuckDuckGo is a privacy-focused search engine. Its results are compiled "
       "from about 400 sources, mostly from Bing, plus its own crawler DuckDuckBot.")
YACY = ("- YaCy - Wikipedia\n  https://en.wikipedia.org/wiki/YaCy\n"
        "  YaCy is a free distributed search engine built on a peer-to-peer network.")


def _agent(monkeypatch, replies):
    agent = OpenAICompatAgent("https://x.invalid/v1", model="m")
    queue = list(replies)
    sent = []

    def fake(payload):
        sent.append(payload)
        return queue.pop(0)
    monkeypatch.setattr(agent, "_complete", fake)
    return agent, sent


def _call(args):
    return {"tool_calls": [{"id": "c1", "function": {"name": "web_search",
                                                      "arguments": json.dumps(args)}}]}


def _verdict(numbers):
    return {"content": json.dumps({"unsupported": numbers})}


def test_a_claim_the_results_contradict_is_dropped_and_sources_listed(monkeypatch):
    # MUTATION: skip _fact_floor in _ask -> the Google claim goes out, no sources.
    agent, sent = _agent(monkeypatch, [
        _call({"query": "duckduckgo backend"}),
        {"content": "DuckDuckGo is a privacy-focused search engine. DuckDuckGo "
                    "operates its own search backend rather than proxying Google."},
        _verdict([2])])
    out = agent.ask("does duckduckgo use google?",
                    tools=[Tool("web_search", "search", lambda query: DDG)])
    assert "own search backend" not in out
    assert out.startswith("DuckDuckGo is a privacy-focused search engine.")
    assert factfloor.UNSURE_NOTE in out
    assert "Sources:\n- <https://en.wikipedia.org/wiki/DuckDuckGo>" in out
    check = sent[-1]
    assert "tools" not in check and "mostly from Bing" in check["messages"][1]["content"]
    assert agent.last_fact_floor["checked"] is True and agent.last_fact_floor["dropped"] == 1


def test_a_misspelled_name_is_corrected_to_the_results_spelling(monkeypatch):
    agent, _ = _agent(monkeypatch, [
        _call({"query": "yacy search engine"}),
        {"content": "YACI is a distributed search engine built on peer-to-peer."},
        _verdict([])])
    out = agent.ask("what is yacy?", tools=[Tool("web_search", "search",
                                                 lambda query: YACY)])
    assert "YACI" not in out
    assert out.startswith("YaCy is a distributed search engine built on peer-to-peer.")
    assert factfloor.UNSURE_NOTE not in out
    assert agent.last_fact_floor["corrected"] == {"YACI": "YaCy"}


def test_a_name_nobody_mentioned_drops_its_sentence(monkeypatch):
    agent, _ = _agent(monkeypatch, [
        _call({"query": "yacy"}),
        {"content": "Good question, Demetri! YaCy is a free distributed search engine."},
        _verdict([])])
    out = agent.ask("is yacy any good?", tools=[Tool("web_search", "search",
                                                     lambda query: YACY)])
    assert "Demetri" not in out
    assert out.startswith("YaCy is a free distributed search engine.")
    assert factfloor.UNSURE_NOTE in out
    assert agent.last_fact_floor["unknown_names"] == ["Demetri"]


def test_a_name_from_the_conversation_is_kept(monkeypatch):
    agent, _ = _agent(monkeypatch, [
        _call({"query": "yacy"}),
        {"content": "Good question, Demetri! YaCy is a free distributed search engine."},
        _verdict([])])
    out = agent.ask("I'm Demetri. is yacy any good?",
                    tools=[Tool("web_search", "search", lambda query: YACY)])
    assert out.startswith("Good question, Demetri! YaCy is a free")
    assert factfloor.UNSURE_NOTE not in out


def test_nothing_supported_says_it_is_unsure(monkeypatch):
    agent, _ = _agent(monkeypatch, [
        _call({"query": "duckduckgo"}),
        {"content": "DuckDuckGo runs its own index of the whole web."},
        _verdict([1])])
    out = agent.ask("ddg index?", tools=[Tool("web_search", "search", lambda query: DDG)])
    assert out.startswith(factfloor.UNSURE_ALL) and "own index" not in out
    assert "<https://en.wikipedia.org/wiki/DuckDuckGo>" in out


def test_no_search_means_no_floor_and_no_extra_call(monkeypatch):
    agent, sent = _agent(monkeypatch, [{"content": "Hi Demetri, how can I help?"}])
    assert agent.ask("hello") == "Hi Demetri, how can I help?"
    assert len(sent) == 1 and agent.last_fact_floor == {}


def test_a_failed_search_is_not_evidence(monkeypatch):
    def boom(query):
        raise TimeoutError("search timed out")
    agent, sent = _agent(monkeypatch, [_call({"query": "x"}),
                                       {"content": "The web search failed, so I cannot say."}])
    out = agent.ask("x", tools=[Tool("web_search", "search", boom)])
    assert out == "The web search failed, so I cannot say." and len(sent) == 2


def test_an_unavailable_checker_still_checks_names(monkeypatch):
    # The claim checker raising (queue exhausted) must never block the answer.
    agent, _ = _agent(monkeypatch, [
        _call({"query": "yacy"}),
        {"content": "Good question, Demetri! YACI is a free distributed search engine."}])
    out = agent.ask("yacy?", tools=[Tool("web_search", "search", lambda query: YACY)])
    assert "Demetri" not in out and out.startswith("YaCy is a free distributed")
    assert agent.last_fact_floor["checked"] is False


def test_the_checker_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AWDISCORD_FACT_CHECK", "0")
    agent, sent = _agent(monkeypatch, [
        _call({"query": "yacy"}), {"content": "YaCy is a free distributed search engine."}])
    out = agent.ask("yacy?", tools=[Tool("web_search", "search", lambda query: YACY)])
    assert len(sent) == 2 and out.startswith("YaCy is a free distributed search engine.")
    assert "Sources:" in out


def test_pure_helpers():
    assert factfloor.names_in("However, YACI and DuckDuckGo met Demetri in May.") == [
        "YACI", "DuckDuckGo", "Demetri"]
    assert factfloor.names_in("The engine is fast.") == []
    assert factfloor.near_miss("YACI", ["Wikipedia", "YaCy"]) == "YaCy"
    assert factfloor.near_miss("Demetri", ["DuckDuckGo", "Bing"]) == ""
    assert factfloor.evidence_urls([DDG, YACY, DDG]) == [
        "https://en.wikipedia.org/wiki/DuckDuckGo", "https://en.wikipedia.org/wiki/YaCy"]
    assert factfloor._parse_unsupported('sure: {"unsupported": [2, "1", 9]}', 3) == [1, 2]
    assert factfloor._parse_unsupported("no json", 3) is None
    text, report = factfloor.apply("Anything.", [], "", None)
    assert text == "Anything." and report["sources"] == []
    # A capitalised word that only starts a sentence is not a name ...
    assert factfloor.evidence_names(
        "Must users install it? The engine is written in Rust.") == ["Rust"]
    # ... unless it also recurs mid-sentence, where it is a real name.
    assert factfloor.evidence_names(
        "Bing is widely used. Results come from Bing.") == ["Bing"]
    # Names are matched as whole words, not as substrings of other words.
    assert factfloor._mentioned("tell me about atomic energy", "tom") is False
    assert factfloor._mentioned("demetrius wrote it", "demetri") is False
    assert factfloor._mentioned("then tom spoke", "tom") is True


def test_a_sentence_initial_capital_never_rewrites_a_real_name(monkeypatch):
    # MUTATION: ev_names = every capitalised word -> the real "Rust" is
    # "corrected" to the sentence-opening "Must".
    agent, _ = _agent(monkeypatch, [
        _call({"query": "yacy language"}),
        {"content": "YaCy is a distributed search engine written in Rust."},
        _verdict([])])
    evidence = ("YaCy is a distributed search engine. Must users install anything? "
                "It is written in Java.")
    out = agent.ask("what language is yacy written in?",
                    tools=[Tool("web_search", "search", lambda query: evidence)])
    assert "Must" not in out
    assert factfloor.UNSURE_ALL in out
    assert agent.last_fact_floor["corrected"] == {}
    assert agent.last_fact_floor["unknown_names"] == ["Rust"]


def test_a_sentence_final_capital_does_not_rewrite_bing_to_being(monkeypatch):
    # MUTATION: ev_names = every capitalised word -> "Bing" becomes "Being".
    agent, _ = _agent(monkeypatch, [
        _call({"query": "duckduckgo bing"}),
        {"content": "DuckDuckGo draws many results from Bing."},
        _verdict([])])
    evidence = "DuckDuckGo draws many results from many sources. Being online is common."
    out = agent.ask("what does duckduckgo use under the hood?",
                    tools=[Tool("web_search", "search", lambda query: evidence)])
    assert "Being" not in out
    assert agent.last_fact_floor["corrected"] == {}
    assert agent.last_fact_floor["unknown_names"] == ["Bing"]


def test_a_name_that_is_only_a_substring_of_a_reference_word_is_unknown(monkeypatch):
    # MUTATION: `_bare(name) in ref_lower` -> "Tom" survives because the question
    # says "atomic".
    agent, _ = _agent(monkeypatch, [
        _call({"query": "engine"}),
        {"content": "Then Tom wrote the engine."},
        _verdict([])])
    out = agent.ask("how are atomic engines built?",
                    tools=[Tool("web_search", "search",
                                lambda query: "The engine is fast.")])
    assert "Tom" not in out
    assert agent.last_fact_floor["unknown_names"] == ["Tom"]


def test_an_invented_name_that_prefixes_a_real_one_is_unknown(monkeypatch):
    # MUTATION: substring test -> "Demetri" passes because the results say
    # "Demetrius".
    agent, _ = _agent(monkeypatch, [
        _call({"query": "engine"}),
        {"content": "Then Demetri wrote the engine."},
        _verdict([])])
    out = agent.ask("who wrote it?",
                    tools=[Tool("web_search", "search",
                                lambda query: "Demetrius wrote the engine.")])
    assert "Demetri" not in out
    assert agent.last_fact_floor["unknown_names"] == ["Demetri"]


# An adversarial, realistic multi-source blob: nearly every other sentence opens
# with a common capitalised word, some real names appear mid-sentence and some
# only open a sentence. It is not shaped to the fix; it is what a search returns.
ADVERSARIAL = (
    "- Search engine - overview\n  https://example.org/engines\n"
    "  Many engines exist. Most of them crawl the web. Users often ask which is "
    "private. Results vary. Search is hard. However, DuckDuckGo markets privacy. "
    "It draws from many sources. This includes Bing. Bing is widely used. "
    "Nothing is perfect. DuckDuckBot is its own crawler. YaCy is peer-to-peer. "
    "GitHub hosts its source. Being open matters. Must users self-host? "
    "Then results depend on the index.")


def test_adversarial_evidence_keeps_real_names_and_invents_none():
    lowered = {n.lower() for n in factfloor.evidence_names(ADVERSARIAL)}
    for real in ("duckduckgo", "bing", "duckduckbot", "yacy", "github"):
        assert real in lowered
    for common in ("must", "being", "however", "users", "results", "search",
                   "this", "nothing", "then", "many", "most"):
        assert common not in lowered
    answer = ("DuckDuckGo draws from Bing. YaCy is peer-to-peer and its source is "
              "on GitHub. Part of it is written in Rust.")
    text, report = factfloor.apply(answer, [ADVERSARIAL], "", None)
    assert report["corrected"] == {}
    assert report["unknown_names"] == ["Rust"]
    assert "DuckDuckGo draws from Bing." in text
    assert "YaCy is peer-to-peer" in text
    assert "Rust" not in text
    assert factfloor.UNSURE_NOTE in text
