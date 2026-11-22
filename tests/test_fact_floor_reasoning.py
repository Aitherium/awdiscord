"""The fact floor may correct outside-world facts; it must not gut an answer.

2026-11-22: "How I'd actually solve this (the real answer)" came out EMPTY except
"I'm not sure about part of what I first wrote" -- the Byzantine-generals explanation
was deleted because a web search did not "support" a piece of reasoning. A header
with nothing under it is worse than no header.
"""
from __future__ import annotations

import json

from awdiscord import factfloor

BFT_EVIDENCE = [
    "- Byzantine fault tolerance - overview\n  https://example.org/bft\n"
    "  Byzantine fault tolerance is a classic distributed-systems problem."]

OURS = (
    "First-party lookup: awpermit\n"
    "- awpermit — status: public; kind: tool\n"
    "  Let an agent through your door with a scope and a spend cap.\n"
    "  docs: https://aitherium.github.io/awpermit/\n"
    "Sources: https://aitherium.com/ecosystem.json "
    "https://aitherium.github.io/awpermit/")


def _verdict(numbers):
    style = json.dumps({"unsupported": numbers})
    return lambda messages: style


def test_a_reasoning_section_survives_a_checker_that_flags_everything():
    answer = (
        "## How I'd actually solve this\n"
        "Suppose there are 3f+1 nodes. Any two quorums of 2f+1 intersect in at least "
        "f+1 nodes, therefore one honest node is shared.")
    out, report = factfloor.apply(answer, BFT_EVIDENCE, "solve byzantine generals",
                                  _verdict([1, 2, 3, 4]))
    assert "## How I'd actually solve this" in out
    assert "therefore one honest node is shared" in out
    assert factfloor.UNSURE_ALL not in out
    assert report["dropped"] == 0
    assert report["reasoning"] >= 2


def test_a_header_whose_whole_section_was_dropped_goes_with_it():
    answer = (
        "## Background\n"
        "DuckDuckGo runs its own index of the entire web.\n\n"
        "## How I'd actually solve this\n"
        "Because 3f+1 nodes tolerate f faults, a quorum of 2f+1 is enough.")
    evidence = ["- DuckDuckGo - Wikipedia\n  https://en.wikipedia.org/wiki/DuckDuckGo\n"
                "  DuckDuckGo is a privacy-focused search engine."]
    out, _ = factfloor.apply(answer, evidence, "", _verdict([1]))
    assert "## Background" not in out            # never ship an empty section
    assert "own index of the entire web" not in out
    assert "## How I'd actually solve this" in out
    assert "Because 3f+1 nodes tolerate f faults" in out


def test_a_header_with_no_body_at_all_is_not_shipped():
    out, _ = factfloor.apply("## Conclusion\n\n## Next steps\nReal work follows here.",
                             ["- X\n  https://example.org/x\n  X is real."], "", None)
    assert "## Conclusion" not in out
    assert "## Next steps" in out
    # A plain reasoning label ending in ':' is a heading too.
    out2, _ = factfloor.apply("How I'd solve it:\n\n## Next steps\nReal work follows.",
                              ["- X\n  https://example.org/x\n  X is real."], "", None)
    assert "How I'd solve it:" not in out2
    assert "## Next steps" in out2


def test_a_claim_our_own_registry_supports_is_never_dropped():
    # A checker that cannot see our registry must not delete the product's name.
    answer = "awpermit is a public Aitherium brick."
    out, report = factfloor.apply(answer, [OURS], "", _verdict([1]), first_party=[OURS])
    assert "awpermit is a public Aitherium brick." in out
    assert report["unknown_names"] == []
    assert report["corrected"] == {}


def test_a_plain_english_sentence_is_still_dropped_when_unsupported():
    # The reasoning exemption must not become a blanket amnesty.
    answer = "DuckDuckGo runs its own index of the entire web."
    out, report = factfloor.apply(answer, ["- DuckDuckGo\n  https://example.org\n"
                                           "  DuckDuckGo is a search engine."],
                                  "", _verdict([1]))
    assert out.startswith(factfloor.UNSURE_ALL)
    assert report["dropped"] == 1
