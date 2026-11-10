"""Every slash command the bot TELLS a member to type must exist.

The join flow once told every new member to "claim it with `/claim`" and to
"find a first issue with `/find-work`" -- neither command was ever registered,
so the first instruction a newcomer got was a dead end. This pins member-facing
text to the real command table (top-level name, and subcommand when given).
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

from awdiscord import dispatch
from awdiscord.builtin import BUILTIN_MODULES

ROOT = Path(__file__).resolve().parents[1]
# Text a member reads: the join flow's replies and the server template guide.
MEMBER_FACING = ("awdiscord/builtin/join.py", "TEMPLATE.md")
# Single-backtick `/cmd sub` (a ``double-backtick`` docstring reference is not a
# message to a member, so a preceding backtick excludes it).
REF = re.compile(r"(?<!`)`/([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
VOICE = {"join": set(), "leave": set()}   # registered by awdiscord.voice


def _registry() -> dict[str, set[str]]:
    table: dict[str, set[str]] = {k: set(v) for k, v in VOICE.items()}
    for mod in BUILTIN_MODULES:
        for cmd in importlib.import_module(mod).PLUGIN.commands:
            table.setdefault(cmd.name, set()).update(s.name for s in cmd.subcommands)
    for cmd in dispatch.commands():
        table.setdefault(cmd["name"], set()).update(
            o["name"] for o in cmd.get("options") or [] if o.get("type") in (1, 2))
    return table


def _dangling(text: str, table: dict[str, set[str]]) -> list[str]:
    bad = []
    for name, sub in REF.findall(text):
        if name not in table:
            bad.append(f"/{name}")
        elif sub and table[name] and sub not in table[name]:
            bad.append(f"/{name} {sub}")
    return bad


def test_member_facing_text_names_only_real_commands():
    table = _registry()
    problems = {f: _dangling((ROOT / f).read_text(encoding="utf-8"), table)
                for f in MEMBER_FACING}
    assert not {f: p for f, p in problems.items() if p}, problems


def test_the_rule_fires_on_a_dead_command():
    table = _registry()
    assert _dangling("claim it with `/claim`.", table) == ["/claim"]
    assert _dangling("try `/gh nonsense`", table) == ["/gh nonsense"]
    assert _dangling("browse with `/gh board`", table) == []
    assert _dangling("``/claim`` in a docstring", table) == []
