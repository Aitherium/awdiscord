"""The five ideals and the Oath Log: who has earned what, and the evidence for it.

The ladder (``awdiscord.orders.NAMES`` levels):

  1 Joined           the join form AND the assessment are done            (auto)
  2 Contributor      a merged PR in a squad repo by the member's VERIFIED
                     GitHub account, or a verified self-host deploy       (auto)
  3 Certified        a track practical approved by 2 members at the 3rd
                     ideal or above, or by an operator                    (semi-auto)
  4 Lead and Mentor  a squad milestone led to release (a squad member's
                     nomination) + 3 mentees at the 2nd ideal + an
                     operator's confirmation
  5 Steward          adoption or core-contribution evidence + 6 months
                     active + a council of 3 (5th ideal or operators)

Levels are sequential: a requirement for level n is only checked once the
member holds n-1. Nothing is purchasable and nothing is self-granted: every
approval, nomination or vote by the member about themselves is ignored, and
only events marked ``qualified`` at the time they were cast count.

THE OATH LOG is append-only: ``log_event`` writes one key per event
(``oath:<user>:<time>:<nonce>``) and this module offers no way to edit or
delete one. Each event carries who did it, the evidence link and when.
``advance`` reads the log, promotes as far as the evidence allows, and logs a
``promoted`` event per level; the caller gives the roles and posts the card.

THE FOUNDER SEAT is the one visible exception: the server owner lists people in the
join config (``founders``) and logs a ``founder`` event for each (``set_founder`` in
join_ladder, which checks the caller IS the guild owner). A listed founder with that
event meets every requirement up to the guild's ``founder_cap`` (default 4) and nothing
above it; the requirement list says "Founder seat, set by the server owner on <date>"
instead of hiding it. The 5th ideal's council stays as it is for everyone else.

Inactive for ``DORMANT_DAYS`` = dormant: shown on the progress card, never a
demotion.
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Iterable

from awdiscord.orders import MAX_IDEAL

DORMANT_DAYS = 90
STEWARD_DAYS = 182
REVIEWS_NEEDED = 2
MENTEES_NEEDED = 3
COUNCIL_NEEDED = 3
DAY = 86400

#: Event kinds the ladder reads. Anything else is kept in the log but ignored.
KINDS = ("joined", "assessment", "github_verified", "merged_pr", "deploy_verified",
         "review_approve", "nominate", "confirm_4", "adoption", "council_vote",
         "mentor_set", "promoted", "umbrella", "dormant_seen", "founder")
FOUNDER_CAP = 4              # default; the guild's join config ``founder_cap`` overrides
FOUNDER_LABEL = "Founder seat, set by the server owner on {date}"


def log_event(store, user: str, kind: str, by: str, evidence: str = "",
              now: float | None = None, **extra: Any) -> dict:
    """Append one event. ``store`` is the join plugin's ScopedStore."""
    at = time.time() if now is None else float(now)
    entry = {"user": str(user), "kind": kind, "by": str(by), "evidence": evidence[:500],
             "at": int(at), **extra}
    # A per-guild sequence keeps the log in write order (two events in one
    # millisecond would otherwise sort by their random suffix).
    update = getattr(store, "update", None)
    seq = update("oath:seq", lambda n: (int(n or 0) + 1, int(n or 0) + 1), 0)         if callable(update) else 0
    entry["seq"] = seq
    store.set(f"oath:{user}:{int(at * 1000):016d}:{seq:09d}:{secrets.token_hex(3)}", entry)
    return entry


def events(store, user: str, kind: str | None = None) -> list[dict]:
    rows = [store.get(k) for k in store.keys(f"oath:{user}:")]
    rows = [r for r in rows if isinstance(r, dict) and (kind is None or r.get("kind") == kind)]
    return sorted(rows, key=lambda r: (int(r.get("at") or 0), int(r.get("seq") or 0)))


def record(store, user: str) -> dict:
    r = store.get(f"member:{user}")
    return r if isinstance(r, dict) else {}


def level(store, user: str) -> int:
    return int(record(store, user).get("ideal") or 0)


def last_active(store, user: str) -> int:
    r = record(store, user)
    times = [int(r.get(k) or 0) for k in ("last_active", "updated", "at")]
    times += [int(e.get("at") or 0) for e in events(store, user)]
    return max(times or [0])


def dormant(store, user: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    seen = last_active(store, user)
    return bool(seen) and now - seen > DORMANT_DAYS * DAY


def _distinct_qualified(rows: Iterable[dict], user: str) -> dict[str, dict]:
    """Qualified events by distinct actors, never the member themselves."""
    out: dict[str, dict] = {}
    for e in rows:
        by = str(e.get("by") or "")
        if by and by != str(user) and e.get("qualified"):
            out.setdefault(by, e)
    return out


def mentees(store, mentor: str) -> list[str]:
    """Members whose recorded mentor is ``mentor`` and who hold the 2nd ideal."""
    out = []
    for key in store.keys("member:"):
        r = store.get(key)
        if isinstance(r, dict) and str(r.get("mentor") or "") == str(mentor) \
                and str(r.get("user")) != str(mentor) and int(r.get("ideal") or 0) >= 2:
            out.append(str(r.get("user")))
    return out


def certified_track(store, user: str) -> str:
    """The track whose practical is approved (operator, or 2 qualified reviewers)."""
    by_track: dict[str, list[dict]] = {}
    for e in events(store, user, "review_approve"):
        by_track.setdefault(str(e.get("track") or ""), []).append(e)
    for track, rows in by_track.items():
        ok = _distinct_qualified(rows, user)
        if any(e.get("operator") for e in ok.values()) or len(ok) >= REVIEWS_NEEDED:
            return track
    return ""


def _config(store) -> dict:
    conf = store.get("config") if store is not None else None
    return conf if isinstance(conf, dict) else {}


def founder_cap(store) -> int:
    """The highest ideal a founder seat satisfies in this server (0..MAX_IDEAL)."""
    try:
        cap = int(_config(store).get("founder_cap", FOUNDER_CAP))
    except (TypeError, ValueError):
        cap = FOUNDER_CAP
    return max(0, min(MAX_IDEAL, cap))


def founder_seat(store, user: str) -> dict | None:
    """The live ``founder`` event for ``user``, or None.

    Both halves must hold: the user is in the config's ``founders`` list AND the
    newest ``founder`` event was cast by the guild owner and not revoked. A list
    edited by hand without the event, or an event without the list, is no seat.
    """
    founders = {str(f) for f in (_config(store).get("founders") or [])}
    if str(user) not in founders:
        return None
    rows = events(store, user, "founder")
    last = rows[-1] if rows else None
    if not last or not last.get("guild_owner") or last.get("revoked"):
        return None
    return last


def founder_text(seat: dict) -> str:
    return FOUNDER_LABEL.format(date=time.strftime("%Y-%m-%d",
                                                   time.gmtime(int(seat.get("at") or 0))))


def requirements(store, user: str, target: int, now: float | None = None) -> list[dict]:
    """What reaching ``target`` needs: ``[{"label", "met", "detail"}]``."""
    now = time.time() if now is None else now
    r = record(store, user)
    req: list[dict] = []
    seat = founder_seat(store, user) if 1 <= target <= founder_cap(store) else None
    if seat is not None:
        return [{"label": founder_text(seat), "met": True, "detail": ""}]

    def add(label: str, met: bool, detail: str = "") -> None:
        req.append({"label": label, "met": bool(met), "detail": detail})

    if target == 1:
        add("join form submitted", bool(r))
        add("assessment done", bool(r.get("assessment")), "press Take the assessment")
    elif target == 2:
        prs = events(store, user, "merged_pr")
        deploys = events(store, user, "deploy_verified")
        add("a merged PR in a squad repo (verified GitHub) or a verified self-host deploy",
            bool(prs or deploys),
            "run /verify-github, then merge a PR in your squad's repo; or /prove-deploy <url>")
    elif target == 3:
        track = certified_track(store, user)
        rows = events(store, user, "review_approve")
        n = len(_distinct_qualified(rows, user))
        add("track practical approved (2 reviewers at the 3rd ideal+, or an operator)",
            bool(track), f"{n}/{REVIEWS_NEEDED} approvals; ask with /review")
    elif target == 4:
        noms = _distinct_qualified(events(store, user, "nominate"), user)
        add("led a squad milestone to release (nominated by your squad)", bool(noms),
            "a squad member runs /nominate with the release link")
        m = len(mentees(store, user))
        add(f"mentored {MENTEES_NEEDED} people to their 2nd ideal", m >= MENTEES_NEEDED,
            f"{m}/{MENTEES_NEEDED} mentees")
        confirmed = [e for e in events(store, user, "confirm_4")
                     if e.get("operator") and e.get("by") != user]
        add("confirmed by an operator", bool(confirmed))
    elif target == 5:
        adopted = [e for e in events(store, user, "adoption") if e.get("qualified")
                   and e.get("by") != user]
        add("work adopted in production elsewhere, or a core contribution", bool(adopted),
            "the evidence link comes with a council vote")
        joined = int(r.get("at") or now)
        days = int((now - joined) // DAY)
        active = days >= STEWARD_DAYS and not dormant(store, user, now)
        add("6 months of active participation", active, f"{days}/{STEWARD_DAYS} days")
        votes = _distinct_qualified(events(store, user, "council_vote"), user)
        add(f"council vote: {COUNCIL_NEEDED} members at the 5th ideal or operators",
            len(votes) >= COUNCIL_NEEDED, f"{len(votes)}/{COUNCIL_NEEDED} votes")
    return req


def ready(store, user: str, target: int, now: float | None = None) -> bool:
    req = requirements(store, user, target, now)
    return bool(req) and all(x["met"] for x in req)


def advance(store, user: str, now: float | None = None) -> list[int]:
    """Promote ``user`` as far as the evidence allows; the levels gained, in order."""
    gained: list[int] = []
    while True:
        cur = level(store, user)
        target = cur + 1
        if target > MAX_IDEAL or not record(store, user) or not ready(store, user, target, now):
            return gained
        r = record(store, user)
        r["ideal"] = target
        r.setdefault("ideal_at", {})[str(target)] = int(time.time() if now is None else now)
        store.set(f"member:{user}", r)
        log_event(store, user, "promoted", "system", now=now, level=target)
        gained.append(target)


def progress_bar(store, user: str, now: float | None = None, width: int = 10) -> tuple[str, float]:
    """``("[####------] 2/5", fraction)`` toward the next ideal; full at the top."""
    target = level(store, user) + 1
    if target > MAX_IDEAL:
        return "[" + "#" * width + "] top of the ladder", 1.0
    req = requirements(store, user, target, now)
    met = sum(1 for x in req if x["met"])
    frac = met / len(req) if req else 0.0
    filled = round(frac * width)
    return f"[{'#' * filled}{'-' * (width - filled)}] {met}/{len(req)}", frac
