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
    first, second = agent.prompts
    assert first.startswith("Be brief.\n\n(It is now ") and first.endswith("\n\nhi")
    assert second.startswith("(It is now ") and second.endswith("\n\nhi")


def test_a_voice_turn_knows_the_local_clock_time(monkeypatch):
    from awdiscord.voice import now_line
    monkeypatch.setenv("AWDISCORD_TIMEZONE", "America/Los_Angeles")
    line = now_line()
    assert "(America/Los_Angeles)" in line and ("PST" in line or "PDT" in line)
    assert any(t in line for t in (" AM ", " PM "))
    monkeypatch.setenv("AWDISCORD_TIMEZONE", "Not/AZone")
    assert " UTC." in now_line()             # an unknown zone says UTC, never guesses
