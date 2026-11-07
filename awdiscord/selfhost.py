"""The admin-only /selfhost wizard: from "no model" to "this server thinks".

One card per step, ephemeral to the admin, with the step kept per server in the
GuildStore. The bot never runs anything on the admin's machine: every card shows
commands the ADMIN runs, and the last step hands the resulting URL to the
``/model`` form, which probes the endpoint before storing it.

Every command printed here was checked against its own README and PyPI on
2026-11-06. ``VERIFIED_COMMANDS`` is the closed set a card may print, and the
tests assert nothing outside it reaches a card (notably: the skills live in a
GitHub repository and are NOT on PyPI, so no install command for them exists).
"""
from __future__ import annotations

DOCS_URL = "https://aitherium.com/discord/"
SKILLS_URL = "https://github.com/Aitherium/awskills"

#: GuildStore scope: one row per server, no secrets, no cross-server reads.
PLUGIN = "selfhost"
STATE_KEY = "state"

#: Custom-id prefix for the wizard's buttons. The dispatcher recognises it
#: BEFORE the none-gate, so an unserved server can walk the whole wizard.
PREFIX = "awdiscord:selfhost:"

#: Every command a card may print. A test asserts the cards stay inside it.
VERIFIED_COMMANDS = frozenset({
    "pip install awdk",
    "adk setup --tier nano",
    "adk setup --tier standard-tq4",
    "adk setup --tier full",
    "adk doctor",
    "adk-serve --identity aither --port 8080",
    "pip install awtunnel",
    "awtunnel up --port 8080",
    "/model setup",
    "awnode mcp",
    "pip install awkno",
    "awkno",
})

#: tier id -> button label. The ids are exactly the ``adk setup --tier`` values.
TIERS = {
    "nano": "Nano - 6-8 GB of GPU memory",
    "standard-tq4": "Standard - 12-16 GB",
    "full": "Full - 24 GB or more",
}

STEPS = (
    {
        "title": "Step 1 of 6 - Choose your model tier",
        "body": (
            "Pick the largest tier your GPU's memory allows. Setup downloads and "
            "quantizes the model on YOUR machine; this bot never runs anything for "
            "you. Already running Ollama, LM Studio or vLLM? Use the last button."
        ),
        "commands": ["pip install awdk", "adk setup --tier nano",
                     "adk setup --tier standard-tq4", "adk setup --tier full"],
    },
    {
        "title": "Step 2 of 6 - Check the machine",
        "body": (
            "Run the doctor. It loads a model and prints what your machine can do. "
            "Fix anything it flags before moving on."
        ),
        "commands": ["adk doctor"],
    },
    {
        "title": "Step 3 of 6 - Start the endpoint",
        "body": (
            "Start the OpenAI-compatible endpoint in a terminal and leave it "
            "running. It answers on port 8080."
        ),
        "commands": ["adk-serve --identity aither --port 8080"],
    },
    {
        "title": "Step 4 of 6 - Give it a public address",
        "body": (
            "The hosted bot reaches your machine over https, so it needs a public "
            "address. Start a tunnel, copy the https URL it prints, and add /v1 to "
            "the end. Running awdiscord on this same machine? Use "
            "http://localhost:8080/v1 instead."
        ),
        "commands": ["pip install awtunnel", "awtunnel up --port 8080"],
    },
    {
        "title": "Step 5 of 6 - Connect it to this server",
        "body": (
            "Run the command below, then fill in the form: Endpoint is the https "
            "URL with /v1 on the end, Model is the model name your endpoint "
            "reports, and API key can be any placeholder if your endpoint has "
            "none. The form probes the endpoint before it saves anything."
        ),
        "commands": ["/model setup"],
    },
    {
        "title": "Step 6 of 6 - Optional extras",
        "body": (
            "Local tools for your agent, offline docs while you work, and "
            "ready-made skills. Run the docs tool followed by the brick you want. "
            f"Skills live in the Aitherium/awskills GitHub repository ({SKILLS_URL}) "
            "and are cloned from there; they are not on PyPI."
        ),
        "commands": ["awnode mcp", "pip install awkno", "awkno"],
    },
)

LAST = len(STEPS) - 1


def _extra(step: int, state: dict) -> str:
    """One line that reflects a choice the admin already made, or empty."""
    tier = str(state.get("tier") or "")
    if step in (0, 1) and tier in TIERS:
        return f"Selected: {TIERS[tier]}."
    if step == 3 and state.get("existing"):
        return ("You already run a model server: if it already has a public https "
                "URL, use that and skip the tunnel.")
    if step == 4:
        return "Nothing is stored until the /model form checks the endpoint."
    return ""


def _button(label: str, action: str, *, style: int = 2, disabled: bool = False) -> dict:
    return {"type": 2, "style": style, "label": label,
            "custom_id": f"{PREFIX}{action}", "disabled": bool(disabled)}


def _rows(step: int, state: dict, can_setup: bool) -> list[dict]:
    rows: list[dict] = []
    if step == 0:
        rows.append({"type": 1, "components": [
            _button(TIERS["nano"], "tier:nano"),
            _button(TIERS["standard-tq4"], "tier:standard-tq4"),
            _button(TIERS["full"], "tier:full")]})
        rows.append({"type": 1, "components": [
            _button("I already run Ollama / LM Studio / vLLM", "existing")]})
    if step == 4 and can_setup:
        rows.append({"type": 1, "components": [
            _button("Open the /model form", "model", style=1)]})
    rows.append({"type": 1, "components": [
        _button("Back", "back", disabled=step == 0),
        _button("Next", "next", style=1, disabled=step == LAST)]})
    return rows


def render(step: int, state: dict | None = None, *, can_setup: bool = False) -> dict:
    """One step as a Discord message payload (ephemeral is the caller's flag)."""
    step = max(0, min(LAST, int(step)))
    state = state or {}
    spec = STEPS[step]
    lines = [f"**{spec['title']}**", "", spec["body"]]
    extra = _extra(step, state)
    if extra:
        lines += ["", extra]
    if spec["commands"]:
        lines += ["", "```", *spec["commands"], "```"]
    lines += ["", f"Full walkthrough and docs: <{DOCS_URL}>"]
    return {"content": "\n".join(lines), "components": _rows(step, state, can_setup),
            "allowed_mentions": {"parse": []}}


class SelfHostWizard:
    """The per-server step state. ``store=None`` means the wizard is unavailable.

    A server's state is three non-secret fields (``step``, ``tier``, ``existing``)
    in one GuildStore row; nothing here reads or writes another server's row.
    """

    def __init__(self, store=None):
        self.store = store

    @property
    def available(self) -> bool:
        return self.store is not None

    def state(self, guild: str) -> dict:
        if self.store is None or not guild:
            return {"step": 0, "tier": "", "existing": False}
        raw = self.store.get(guild, PLUGIN, STATE_KEY, {})
        saved = raw if isinstance(raw, dict) else {}
        try:
            step = max(0, min(LAST, int(saved.get("step", 0))))
        except (TypeError, ValueError):
            step = 0
        return {"step": step, "tier": str(saved.get("tier") or ""),
                "existing": bool(saved.get("existing"))}

    def save(self, guild: str, state: dict) -> None:
        if self.store is not None and guild:
            self.store.set(guild, PLUGIN, STATE_KEY, state)

    def card(self, guild: str, *, can_setup: bool = False) -> dict:
        state = self.state(guild)
        return render(state["step"], state, can_setup=can_setup)

    def press(self, guild: str, action: str, *, can_setup: bool = False):
        """Apply one button, persist it, and return ("card", payload) or ("modal", None).

        An unknown action changes nothing and re-renders the current step, so a
        stale button in an old card can never land the admin on a blank page.
        """
        state = self.state(guild)
        step = state["step"]
        if action == "next":
            state["step"] = min(LAST, step + 1)
        elif action == "back":
            state["step"] = max(0, step - 1)
        elif action.startswith("tier:") and action[len("tier:"):] in TIERS:
            state["tier"] = action[len("tier:"):]
            state["existing"] = False
            state["step"] = max(step, 1)
        elif action == "existing":
            state["tier"] = ""
            state["existing"] = True
            state["step"] = 3
        elif action == "model":
            if can_setup:
                return ("modal", None)
            return ("card", render(step, state, can_setup=can_setup))
        self.save(guild, state)
        return ("card", render(state["step"], state, can_setup=can_setup))
