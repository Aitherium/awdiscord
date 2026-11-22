"""The ONE shape for a contribution event: who did what on GitHub, and its stable id.

Producers (a bounty poller, a pull-request intake bot) build a ``ContribEvent`` from
what GitHub itself says; a points service pays for it. Nothing here is self-declared:
every field comes from a GitHub read.

``event_id`` is idempotent: the same real-world event always hashes to the same id, so a
replayed poll, a second producer or a restart credits ONCE (the points ledger is UNIQUE
on ``(account, source, event key)``). The hash matches the ledger's own
``event_key_for`` byte for byte; a test pins the two equal.

Pure: no I/O, no lib, no Discord.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

#: What can happen. ``revert`` and ``pr_closed`` are clawbacks, never awards.
AWARD_KINDS = ("claim", "pr_opened", "pr_merged", "review_recorded", "qa_report",
               "docs_fix", "security_confirmed")
CLAWBACK_KINDS = ("revert", "pr_closed")
KINDS = AWARD_KINDS + CLAWBACK_KINDS

#: Merged-PR size buckets by changed lines (additions + deletions). Bigger is not
#: better past ``l``: an ``xl`` PR pays the same as ``l`` in the published table.
SIZE_BUCKETS = (("xs", 10), ("s", 100), ("m", 500), ("l", 2000))
SIZE_TOP = "xl"

_BOT_LOGIN = re.compile(r"(\[bot\]$|^dependabot|^renovate|^github-actions)", re.IGNORECASE)
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")


def event_key_for(kind: str, *parts: Any) -> str:
    """``EarnLedger.event_key_for``, copied (the bot cannot import lib)."""
    raw = "|".join([kind, *[str(p) for p in parts]])
    return f"{kind}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def size_bucket(changed_lines: int) -> str:
    n = max(0, int(changed_lines or 0))
    for name, below in SIZE_BUCKETS:
        if n < below:
            return name
    return SIZE_TOP


def is_bot_login(login: str) -> bool:
    """A GitHub app/bot account earns nothing (``x[bot]``, dependabot, renovate...)."""
    return bool(_BOT_LOGIN.search(str(login or "")))


def valid_login(login: str) -> bool:
    return bool(_LOGIN.match(str(login or "")))


@dataclass
class ContribEvent:
    """One contribution, as GitHub recorded it.

    ``github_login`` is the account that did the work (a member's verified login, or
    their enrolled agent's). ``discord_id`` is the MEMBER the credit goes to, resolved
    by the producer from a verified link -- never typed by anyone. ``agent`` names the
    agent login when the member's agent did it (the member still gets the credit).
    ``target`` points a clawback at the PR it undoes (``repo#number``).
    """

    kind: str
    repo: str
    number: int
    github_login: str
    sha: str = ""
    discord_id: str = ""
    agent: str = ""
    size: str = ""
    url: str = ""
    at: str = ""
    target: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Stable across producers: kind, repo, number, head sha, login."""
        return event_key_for("contrib_" + self.kind, self.repo.lower(), int(self.number),
                             self.sha, self.github_login.lower())

    def problems(self) -> list[str]:
        out = []
        if self.kind not in KINDS:
            out.append(f"unknown kind {self.kind!r}")
        if "/" not in self.repo:
            out.append("repo must be owner/name")
        if int(self.number or 0) <= 0:
            out.append("number must be positive")
        if not valid_login(self.github_login):
            out.append("github_login is not a GitHub login")
        if self.kind == "pr_merged" and self.size not in {b for b, _ in SIZE_BUCKETS} | {
                SIZE_TOP}:
            out.append("pr_merged needs a size bucket")
        if self.kind in CLAWBACK_KINDS and not self.target:
            out.append(f"{self.kind} needs a target repo#number")
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ContribEvent":
        names = {f for f in cls.__dataclass_fields__}       # noqa: C416 - explicit
        kw = {k: v for k, v in dict(d or {}).items() if k in names}
        kw["number"] = int(kw.get("number") or 0)
        kw["extra"] = dict(kw.get("extra") or {})
        return cls(**kw)


def target_of(repo: str, number: int) -> str:
    return f"{repo.lower()}#{int(number)}"
