"""A spoken turn asks for a short, plain answer; every extra token is time spent waiting."""

from awdiscord.voice import BotThinker


class _Agent:
    def __init__(self):
        self.prompts = []

    def ask(self, prompt, **kw):
        self.prompts.append(prompt)
        return "ok"


def test_a_voice_turn_asks_for_a_short_spoken_answer(monkeypatch):
    monkeypatch.delenv("AWDISCORD_VOICE_STYLE", raising=False)
    agent = _Agent()
    BotThinker(agent).think("what's up?")
    assert agent.prompts[0].endswith("what's up?")
    assert "voice channel" in agent.prompts[0] and "no markdown" in agent.prompts[0]


def test_the_style_can_be_replaced_or_turned_off(monkeypatch):
    agent = _Agent()
    monkeypatch.setenv("AWDISCORD_VOICE_STYLE", "Be brief.")
    BotThinker(agent).think("hi")
    monkeypatch.setenv("AWDISCORD_VOICE_STYLE", "off")
    BotThinker(agent).think("hi")
    assert agent.prompts == ["Be brief.\n\nhi", "hi"]
