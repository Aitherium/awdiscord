"""Archetypes ("orders"), the five ideals and the assessment that sorts people.

Ten archetypes sit on a ring of ten capability axes; each archetype is two
neighbouring axes. An assessment answer weights the two axes of one archetype
(+2 each), and the archetype whose pair scores highest is the result. Ten
scenario questions, four answers each, every archetype offered exactly four
times, so every archetype is reachable and scoring is a pure function of the
answers (no randomness, no model).

Two NAME SETS, chosen per server in the join config (``name_set``):

  ``professional`` (default)  original names: "Aitherium Academy: Agentic
                              Engineering", Member..Steward, "ACAE: <Track>",
                              umbrella "Aitherium Engineer"; every public surface
  ``themed``                  the Order of Aitherium: the order names a server
                              already uses (``awdiscord.radiant``), First..Fifth
                              Ideal, umbrella "Knight Radiant"; opt-in per server

Other modules import names from here, never duplicate them: ``NAMES``,
``archetype_name``, ``ideal_name``, ``level_display``, ``certified_role``,
``umbrella_name``, ``partner_name``, ``academy_name``, ``oath_line``, ``score``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

#: The ring of capability axes, in order. Archetype i = axes i and i+1 (mod 10).
AXES = ("guarding", "ordering", "breaking", "enduring", "mending",
        "revealing", "transforming", "scouting", "building", "connecting")


@dataclass(frozen=True)
class Archetype:
    key: str
    name: str                 # the original (public) name
    identity: str
    strengths: tuple[str, str, str]
    squad_role: str
    color: int


ARCHETYPES: tuple[Archetype, ...] = (
    Archetype("warden", "Warden", "Keeps what is running alive, so others can build on it.",
              ("incident response", "reliability", "deploys"),
              "holds production steady and owns the on-call", 0x38BDF8),
    Archetype("arbiter", "Arbiter", "Turns hard-won lessons into rules that keep people safe.",
              ("security", "standards", "review"),
              "sets the bar and guards it in review", 0x64748B),
    Archetype("breaker", "Breaker", "Breaks things on purpose, so they fail on our terms.",
              ("testing", "chaos drills", "edge cases"),
              "finds the fault line before users do", 0xEF4444),
    Archetype("lamplighter", "Lamplighter", "Makes sure nobody is left in the dark.",
              ("mentoring", "onboarding", "patience"),
              "brings newcomers up to speed and keeps the squad whole", 0xF472B6),
    Archetype("seeker", "Seeker", "Follows the evidence until the system tells the truth.",
              ("debugging", "observability", "data"),
              "the one who finds out why", 0x10B981),
    Archetype("weaver", "Weaver", "Turns ideas into something people can see and feel.",
              ("frontend", "design", "demos"),
              "shapes what users touch and how the work is shown", 0xF59E0B),
    Archetype("gatewright", "Gatewright", "Sees the boundaries and redraws them.",
              ("architecture", "interfaces", "abstraction"),
              "draws the system map and the seams between parts", 0x8B5CF6),
    Archetype("pathfinder", "Pathfinder", "Goes first and builds what does not exist yet.",
              ("prototyping", "exploration", "open source"),
              "scouts new ground and ships the first version", 0x06B6D4),
    Archetype("keystone", "Keystone", "The foundation everything else stands on.",
              ("backends", "databases", "APIs"),
              "builds the parts that must never move", 0xA3A3A3),
    Archetype("binder", "Binder", "Holds people and parts together.",
              ("leadership", "integration", "communication"),
              "connects the squad and carries what others cannot", 0xFBBF24),
)
BY_KEY = {a.key: a for a in ARCHETYPES}
KEYS = tuple(a.key for a in ARCHETYPES)


def axes_of(key: str) -> tuple[str, str]:
    i = KEYS.index(key)
    return AXES[i], AXES[(i + 1) % len(AXES)]


# ── the assessment ────────────────────────────────────────────────────────────

#: (question, ((answer text, archetype key) x4)). Each archetype appears 4 times.
QUESTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Production is down at 3 a.m. You first...", (
        ("Restore service: roll back, then dig", "warden"),
        ("Check what changed against what we agreed", "arbiter"),
        ("Reproduce it on purpose to see where it cracks", "breaker"),
        ("Stay with it, patiently, until it is truly fixed", "lamplighter"))),
    ("You inherit a codebase nobody understands.", (
        ("Add logs and traces until it tells me what it does", "seeker"),
        ("Draw it so everyone can finally see it", "weaver"),
        ("Map its boundaries and redraw the architecture", "gatewright"),
        ("Build something small on top to learn by doing", "pathfinder"))),
    ("A conflict is slowing your squad down.", (
        ("Set up a shared contract both sides can build on", "keystone"),
        ("Talk to both and find the common ground", "binder"),
        ("Shield the rest of the squad so work goes on", "warden"),
        ("Point to the agreed process and apply it fairly", "arbiter"))),
    ("A hard deadline is two days away.", (
        ("Cut scope and test the riskiest part first", "breaker"),
        ("Make sure the newest person is not drowning", "lamplighter"),
        ("Measure where the time is really going", "seeker"),
        ("Ship the most visible, polished slice", "weaver"))),
    ("You meet an undocumented API.", (
        ("Model it as an interface I could swap later", "gatewright"),
        ("Poke it and have a prototype client by tonight", "pathfinder"),
        ("Write a solid typed wrapper others can rely on", "keystone"),
        ("Find whoever owns it and open a channel", "binder"))),
    ("A security report lands in your inbox.", (
        ("Contain first: rotate, block, protect users", "warden"),
        ("Triage it against our policy and disclosure rules", "arbiter"),
        ("Write the proof of concept to show the impact", "breaker"),
        ("Thank the reporter and walk them through next steps", "lamplighter"))),
    ("Your squad needs its next feature.", (
        ("Read the data on where users really struggle", "seeker"),
        ("Mock up the experience so people can feel it", "weaver"),
        ("Ask what the system could become in a year", "gatewright"),
        ("Find the idea nobody has tried yet", "pathfinder"))),
    ("Something you built is getting popular.", (
        ("Harden the foundations: schema, indexes, backups", "keystone"),
        ("Bring in collaborators and hand them ownership", "binder"),
        ("Add monitoring and alerts before it falls over", "warden"),
        ("Write the rules for contributions and releases", "arbiter"))),
    ("A newcomer asks you for help.", (
        ("Hand them a bug to break and fix", "breaker"),
        ("Pair with them until they can do it alone", "lamplighter"),
        ("Show them how to find answers in the logs", "seeker"),
        ("Explain it with a picture or a live demo", "weaver"))),
    ("What should people say about your work?", (
        ("It opened doors nobody else saw", "gatewright"),
        ("It was the first of its kind", "pathfinder"),
        ("It never moved; everything stood on it", "keystone"),
        ("It brought people together", "binder"))),
)
PAGES = ((0, 1, 2, 3), (4, 5, 6, 7), (8, 9))     # questions per form page (<=4 + a button)


class AssessmentError(ValueError):
    """Answers that cannot be scored (wrong count, unknown answer)."""


def score(answers: Sequence[int]) -> tuple[str, dict[str, int]]:
    """(archetype key, axis scores) for one answer index (0-3) per question.

    Deterministic. Ties go to the archetype picked directly most often, then
    to the first on the ring.
    """
    if len(answers) != len(QUESTIONS):
        raise AssessmentError(f"expected {len(QUESTIONS)} answers, got {len(answers)}")
    axes = {a: 0 for a in AXES}
    picked = {k: 0 for k in KEYS}
    for (_, options), idx in zip(QUESTIONS, answers):
        if not isinstance(idx, int) or not 0 <= idx < len(options):
            raise AssessmentError(f"answer {idx!r} is not one of the options")
        key = options[idx][1]
        picked[key] += 1
        for axis in axes_of(key):
            axes[axis] += 2
    best = max(KEYS, key=lambda k: (sum(axes[a] for a in axes_of(k)), picked[k],
                                    -KEYS.index(k)))
    return best, axes


# ── names ─────────────────────────────────────────────────────────────────────
#
# Every display string and role name lives in NAMES, one dict per set; the join
# config picks the set (``name_set``) and may override any single string
# (``academy``, ``umbrella``). Changing the brand is a one-line edit here.

NAMES: dict[str, dict] = {
    "professional": {
        "academy": "Aitherium Academy: Agentic Engineering",
        "academy_short": "ACAE",
        "levels": ("Member", "Contributor", "Certified", "Lead", "Steward"),
        "level_roles": ("Aitherium Member", "Aitherium Contributor", "Aitherium Certified",
                        "Aitherium Lead", "Aitherium Steward"),
        "level_display": "{level}",
        "certified_role": "ACAE: {track}",
        "certified_display": "ACAE: {track}",
        "umbrella": "Aitherium Engineer",
        "partner": "Aitherium Partner",
        "seat": "Order Chair, {order}",
        "seat_plural": "Order Chairs",
        "order_heading": "Order: {order}",
    },
    "themed": {
        "academy": "Order of Aitherium",
        "academy_short": "ACAE",
        "levels": ("Member", "Contributor", "Certified", "Lead", "Steward"),
        "level_roles": ("First Ideal", "Second Ideal", "Third Ideal", "Fourth Ideal",
                        "Fifth Ideal"),
        "level_display": "Sworn to the {ordinal} Ideal",
        "certified_role": "ACAE: {track}",
        "certified_display": "Sworn to the Third Ideal, ACAE: {track}",
        "umbrella": "Knight Radiant",
        "partner": "Ally of the Order",
        "seat": "Herald of the {order}",
        "seat_plural": "Heralds",
        "order_heading": "Order of {order}",
    },
}
NAME_SETS = tuple(NAMES)
DEFAULT_SET = "professional"
#: Older spellings a stored config may still carry.
ALIASES = {"archetype": "professional", "radiant": "themed"}
ORDINALS = ("First", "Second", "Third", "Fourth", "Fifth")
LEARN_URL = "https://academy.aitherium.com"
MAX_IDEAL = 5


def name_set(value: str | None) -> str:
    value = ALIASES.get(str(value or ""), str(value or ""))
    return value if value in NAMES else DEFAULT_SET


def names(value: str | None) -> dict:
    return NAMES[name_set(value)]


def archetype_name(key: str, set_name: str | None = None) -> str:
    if name_set(set_name) == "themed":
        from awdiscord.radiant import ARCHETYPE_TO_ORDER
        return ARCHETYPE_TO_ORDER[key]
    return BY_KEY[key].name


def ideal_name(level: int, set_name: str | None = None) -> str:
    """The ROLE for a level (``Aitherium Contributor`` / ``Second Ideal``)."""
    return names(set_name)["level_roles"][level - 1]


def ideal_title(level: int) -> str:
    """The plain ladder word (Member ... Steward), the same in both sets."""
    return NAMES[DEFAULT_SET]["levels"][level - 1]


def level_display(level: int, set_name: str | None = None) -> str:
    """How a level reads on a card: ``Contributor`` / ``Sworn to the Second Ideal``."""
    if level < 1:
        return "not yet sworn" if name_set(set_name) == "themed" else "not yet joined"
    return names(set_name)["level_display"].format(level=ideal_title(level),
                                                   ordinal=ORDINALS[level - 1])


def certified_role(track_label: str, set_name: str | None = None) -> str:
    return names(set_name)["certified_role"].format(track=track_label)


def certified_display(track_label: str, set_name: str | None = None) -> str:
    return names(set_name)["certified_display"].format(track=track_label)


def umbrella_name(set_name: str | None = None, override: str = "") -> str:
    return override.strip() or names(set_name)["umbrella"]


def partner_name(set_name: str | None = None) -> str:
    return names(set_name)["partner"]


def academy_name(set_name: str | None = None, override: str = "") -> str:
    return override.strip() or names(set_name)["academy"]


#: Every role name either set may use for a level, the umbrella or the partner:
#: the join path never hands one of these out (only the ladder does).
def earned_role_names() -> set[str]:
    out: set[str] = set()
    for n in NAMES.values():
        out |= set(n["level_roles"]) | {n["umbrella"], n["partner"]}
    return out


_OATHS = {
    1: "I ship before I polish, I face the hard problem, and I value the path over the badge.",
    2: "My work runs where others depend on it.",
    3: "I have proven my craft, and my peers have vouched for it.",
    4: "I lift others as I climb, and I finish what my squad starts.",
    5: "I keep what we built for those who come after.",
}
_THEMED_LATE = {
    4: "I will lead so that others may stand.",
    5: "I will keep the guild for those who come after.",
}


def oath_line(level: int, archetype: str = "", set_name: str | None = None) -> str:
    """The words for reaching ``level``: original by default, the source's when themed."""
    if name_set(set_name) != "themed":
        return _OATHS[level]
    from awdiscord import radiant
    if level == 1:
        return radiant.FIRST_IDEAL_TEXT
    if level in (2, 3) and archetype in radiant.ORDER_IDEALS:
        return radiant.ORDER_IDEALS[archetype][level - 2]
    return _THEMED_LATE.get(level, _OATHS[level])
