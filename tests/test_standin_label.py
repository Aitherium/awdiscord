"""An answer from a model outside the bot's usual set says so (2026-11-15 chat outage)."""
from awdiscord.agent import OpenAICompatAgent, with_standin_label


def test_the_usual_model_carries_no_label():
    assert with_standin_label("hi", {"served_by": "bonsai2-27b"}) == "hi"
    assert with_standin_label("hi", {}) == "hi"


def test_a_standin_answer_names_the_model_and_where_it_ran():
    out = with_standin_label("hi", {"served_by": "bonsai-4b-5090",
                                    "served_on": "llamacpp_bonsai4b"})
    assert out.startswith("hi\n-# ") and "`bonsai-4b-5090` on llamacpp_bonsai4b" in out


def test_the_expected_set_is_configurable_and_non_text_is_untouched(monkeypatch):
    monkeypatch.setenv("AWDISCORD_EXPECTED_MODELS", "bonsai-4b-5090")
    assert with_standin_label("hi", {"served_by": "bonsai-4b-5090"}) == "hi"
    assert with_standin_label({"x": 1}, {"served_by": "other"}) == {"x": 1}
    assert with_standin_label("  ", {"served_by": "other"}) == "  "


def test_a_substitution_is_labelled_even_when_the_substitute_is_usual(monkeypatch):
    # 2026-11-17: aither-orchestrator was replaced by bonsai2-27b on every turn. The 27B
    # sits inside the expected set, so the allowlist called the substitution normal and
    # the owner had no way to see it. The scheduler's own cross_model flag settles it.
    out = with_standin_label("hi", {"requested": "aither-orchestrator",
                                    "served_by": "bonsai2-27b", "cross_model": True})
    assert "Asked for `aither-orchestrator`" in out and "`bonsai2-27b`" in out
    out2 = with_standin_label("hi", {"requested": "aither-orchestrator",
                                     "served_by": "bonsai2-27b"})
    assert "`bonsai2-27b`" in out2


def test_ask_appends_the_label_from_the_scheduler_route(monkeypatch):
    agent = OpenAICompatAgent.__new__(OpenAICompatAgent)
    agent.model, agent.tool_model, agent.vision_models = "aither-orchestrator", "", []

    def fake_ask(self, *a, **k):
        self.last_route = {"requested": "aither-orchestrator", "served_by": "bonsai-4b-5090",
                           "cross_model": True}
        return "answer"
    monkeypatch.setattr(OpenAICompatAgent, "_ask", fake_ask)
    assert "`bonsai-4b-5090`" in agent.ask("q")
