"""The Order Chair seat (themed servers: Herald), and the rules of the Fifth Ideal.

Spec: ``docs/plans/2026-11-12-ideals-heralds-and-boards.md`` sections 2 and 3.

A CHAIR IS A SEAT, NOT A LEVEL. ``orders.MAX_IDEAL`` stays 5 and ``ideals.level``
never reads this module. One chair per order (at most ``len(orders.KEYS)`` = 10),
and one chair per person.

Two ways in, and only two:

  ELECTED  nominated (2 members of the order at level 3+, 14 days)
           -> voting (7 days; quorum 30% of the order's active weight, 60% yes)
           -> council (3 level-5 members or operators from OTHER orders, 14 days,
              the anti-capture rules of ``council_count``)
           -> confirm (the server owner or a seated founder ratifies a vote that
              PASSED; it can never turn a failed vote into a seat)
           -> seated for 12 months; a retention vote opens 30 days before the end.
  FOUNDER  the server owner seats a member directly (``seat_founder``). It is
           APPOINTED, NOT ELECTED, and every surface says so: the label
           ``FOUNDER_LABEL`` is identical in both name sets. This path writes no
           vote, council or confirm event of any kind, so nothing can be read as a
           vote that never happened.

Every step writes an Oath Log event (``ideals.log_event``) on the candidate's or
holder's log, so every step can be audited. Running totals of a vote stay sealed
until it closes; who voted how is stored for staff audit and never rendered.

Vote weight is ONE function, ``vote_weight`` (scaffold plan section 7):
``base(level) x well_regarded x eligibility``. The suggestion board uses it too.
"""
from __future__ import annotations

import secrets
import time
import unicodedata
from typing import Any, Callable

from awdiscord import ideals, orders

DAY = ideals.DAY

NOMINATIONS_NEEDED = 2
NOMINATOR_LEVEL = 3
NOMINATION_DAYS = 14
VOTE_DAYS = 7
COUNCIL_DAYS = 14
CONFIRM_DAYS = 14
TERM_DAYS = 365
RETENTION_LEAD_DAYS = 30
QUORUM = 0.30
MAJORITY = 0.60
RECALL_COUNCIL = 2
DORMANT_REVIEW_DAYS = 90
FOUNDER_CHAIRS_MAX = 4          # the join config ``founder_chairs`` overrides
TITLE_MAX = 40

#: Fifth Ideal (section 2)
ACTIVE_MONTHS_NEEDED = (4, 6)   # active in 4 of the last 6 calendar months
STEWARD_MENTEES_NEEDED = 2      # mentees who reached level 3
NEW_ACCOUNT_DAYS = 7

#: Vote weight (scaffold plan section 7). Index = ideal level; 0 = Acknowledged.
BASE_WEIGHT = (1.0, 1.25, 1.5, 1.75, 2.0, 2.0)
STAFF_WEIGHT = 2.0
WELL_REGARDED = 1.25

#: The appointed-seat label. NEVER themed, never shortened: a server cannot hide it.
FOUNDER_LABEL = "Founder, seated by the server owner, not elected"
FOUNDER_SELF = "Founder, seated by the server owner (self), not elected"
FOUNDER_RECORDED = ("Founder, recorded by an operator on the server owner's instruction, "
                    "not elected")
DISCORD_EPOCH_MS = 1420070400000

#: Event kinds this module writes (kept in each member's Oath Log).
KINDS = ("herald_nominate", "herald_vote", "herald_council", "herald_confirm",
         "herald_seated", "herald_resign", "herald_recall_sign", "herald_recall_open",
         "herald_recall_vote", "herald_unseated", "chair_founder", "chair_founder_removed",
         "title_set")
#: Kinds a founder seat must never produce (pinned by a test).
VOTE_KINDS = ("council_vote", "herald_vote", "herald_council", "herald_confirm")

OPEN_STATES = ("nominated", "voting", "council", "confirm")


class HeraldError(ValueError):
    """A refused step; the message is safe to show the member."""


def _now(now: float | None) -> int:
    return int(time.time() if now is None else now)


def _conf(store) -> dict:
    conf = store.get("config")
    return conf if isinstance(conf, dict) else {}


# ── who is who ────────────────────────────────────────────────────────────────

def order_of(store, user: str) -> str:
    """The member's order (assessed archetype), "" when not assessed."""
    return _order_of_record(ideals.record(store, user))


def _order_of_record(r: dict) -> str:
    key = str(r.get("archetype") or (r.get("assessment") or {}).get("archetype") or "")
    return key if key in orders.BY_KEY else ""


def members_of(store, order: str) -> list[str]:
    out = []
    for key in store.keys("member:"):
        r = store.get(key)
        if isinstance(r, dict) and _order_of_record(r) == order and r.get("user"):
            out.append(str(r["user"]))
    return out


def is_operator(store, user: str) -> bool:
    """Operators are listed in the join config (``operators``) or flagged on the record."""
    ops = {str(x) for x in _conf(store).get("operators") or []}
    return str(user) in ops or bool(ideals.record(store, user).get("operator"))


def discord_age_days(user: str, now: float | None = None) -> float | None:
    """Days since the Discord ACCOUNT was created (from its snowflake id); None
    when ``user`` is not a snowflake (tests, CLI names)."""
    u = str(user)
    if not u.isdigit() or len(u) < 15:
        return None
    created = ((int(u) >> 22) + DISCORD_EPOCH_MS) / 1000
    return (_now(now) - created) / DAY


def account_too_new(user: str, now: float | None = None,
                    days: int = NEW_ACCOUNT_DAYS) -> bool:
    age = discord_age_days(user, now)
    return age is not None and age < days


def vote_weight(store, user: str, now: float | None = None, staff: bool = False) -> float:
    """``base x well_regarded x eligibility``: the ONE weight every vote uses.

    A member with no record weighs 0. An account under 7 days old, or one under
    review, weighs 0: the vote is recorded and changes nothing.
    """
    now = _now(now)
    r = ideals.record(store, user)
    if not r:
        return STAFF_WEIGHT if staff else 0.0
    if account_too_new(user, now):              # a fresh Discord account (alt)
        return 0.0
    joined = int(r.get("at") or now)
    if now - joined < NEW_ACCOUNT_DAYS * DAY or r.get("under_review"):
        return 0.0
    lvl = max(0, min(len(BASE_WEIGHT) - 1, int(r.get("ideal") or 0)))
    base = max(BASE_WEIGHT[lvl], STAFF_WEIGHT if staff else 0.0)
    wr = WELL_REGARDED if r.get("well_regarded") else 1.0
    return round(base * wr, 4)


def active_weight(store, order: str, now: float | None = None) -> float:
    """The order's active weight: the quorum base (non-dormant members only)."""
    return round(sum(vote_weight(store, u, now) for u in members_of(store, order)
                     if not ideals.dormant(store, u, now)), 4)


# ── the Fifth Ideal (section 2) ───────────────────────────────────────────────

def note_active(store, user: str, now: float | None = None) -> None:
    """Record this calendar month as active for ``user`` (a vote or an Oath event)."""
    r = ideals.record(store, user)
    if not r:
        return
    month = time.strftime("%Y-%m", time.gmtime(_now(now)))
    months = [m for m in r.get("active_months") or [] if isinstance(m, str)]
    if month not in months:
        r["active_months"] = (months + [month])[-24:]
        store.set(f"member:{user}", r)


def _months_back(now: int, n: int) -> list[str]:
    y, m = map(int, time.strftime("%Y %m", time.gmtime(now)).split())
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def active_months(store, user: str, now: float | None = None) -> int:
    """How many of the last ``ACTIVE_MONTHS_NEEDED[1]`` calendar months were active."""
    now = _now(now)
    window = set(_months_back(now, ACTIVE_MONTHS_NEEDED[1]))
    seen = {m for m in ideals.record(store, user).get("active_months") or []
            if isinstance(m, str)}
    for e in ideals.events(store, user):
        if e.get("kind") != "dormant_seen":
            seen.add(time.strftime("%Y-%m", time.gmtime(int(e.get("at") or 0))))
    return len(window & seen)


def steward_mentees(store, mentor: str) -> list[str]:
    """Mentees of ``mentor`` who reached level 3 (Certified)."""
    out = []
    for key in store.keys("member:"):
        r = store.get(key)
        if isinstance(r, dict) and str(r.get("mentor") or "") == str(mentor) \
                and str(r.get("user")) != str(mentor) and int(r.get("ideal") or 0) >= 3:
            out.append(str(r.get("user")))
    return out


def _related(store, a: str, b: str) -> bool:
    """Mentor and mentee, either way round."""
    ra, rb = ideals.record(store, a), ideals.record(store, b)
    return str(ra.get("mentor") or "") == str(b) or str(rb.get("mentor") or "") == str(a)


def stewards(store) -> list[str]:
    out = []
    for key in store.keys("member:"):
        r = store.get(key)
        if isinstance(r, dict) and int(r.get("ideal") or 0) >= 5 and r.get("user"):
            out.append(str(r["user"]))
    return out


def council_count(store, candidate: str, voters: dict[str, dict]) -> tuple[int, str]:
    """The votes that COUNT under the anti-capture rules, and why any did not.

    * the candidate's own vote never counts (``ideals._distinct_qualified`` already
      drops it; dropped again here for callers that pass raw rows);
    * at most ONE counted vote from the candidate's mentor or mentees;
    * counted voters must span 2 or more orders. While fewer than 3 people hold
      level 5, operators count as an order of their own.

    Returns ``(counted, note)``; ``counted`` is 0 when the order spread fails.
    """
    few_stewards = len(stewards(store)) < 3
    counted: list[str] = []
    related_used = False
    for uid in voters:
        if str(uid) == str(candidate):
            continue
        if _related(store, uid, candidate):
            if related_used:
                continue
            related_used = True
        counted.append(str(uid))
    spread = set()
    for uid in counted:
        o = order_of(store, uid)
        op = bool((voters.get(uid) or {}).get("operator")) or is_operator(store, uid)
        if not o and few_stewards and op:
            o = "operators"
        if o:
            spread.add(o)
    if len(counted) >= 2 and len(spread) < 2:
        return 0, "counted votes must come from 2 or more orders"
    return len(counted), ""


def fifth_ideal_rows(store, user: str, now: float | None = None) -> list[dict]:
    """R1-R4 for ``ideals.requirements(target=5)``: ``[{"label", "met", "detail"}]``."""
    now = _now(now)
    r = ideals.record(store, user)
    rows: list[dict] = []

    def add(label: str, met: bool, detail: str = "") -> None:
        rows.append({"label": label, "met": bool(met), "detail": detail})

    joined = int(r.get("at") or now)
    days = int((now - joined) // DAY)
    months = active_months(store, user, now)
    need, of = ACTIVE_MONTHS_NEEDED
    add("R1 sustained contribution: 6 months a member, not dormant, active "
        f"{need} of the last {of} months",
        days >= ideals.STEWARD_DAYS and not ideals.dormant(store, user, now) and months >= need,
        f"{days}/{ideals.STEWARD_DAYS} days, {months}/{need} active months")
    m = len(steward_mentees(store, user))
    add(f"R2 mentorship: {STEWARD_MENTEES_NEEDED} mentees reached level 3",
        m >= STEWARD_MENTEES_NEEDED, f"{m}/{STEWARD_MENTEES_NEEDED} mentees")
    adopted = [e for e in ideals.events(store, user, "adoption") if e.get("qualified")
               and str(e.get("by")) != str(user)]
    add("R3 a shipped project others depend on (logged by someone else)", bool(adopted),
        "a council member or operator logs the evidence link")
    votes = ideals._distinct_qualified(ideals.events(store, user, "council_vote"), user)
    n, why = council_count(store, user, votes)
    add(f"R4 council vote: {ideals.COUNCIL_NEEDED} members at the 5th ideal or operators, "
        "from 2+ orders", n >= ideals.COUNCIL_NEEDED,
        f"{n}/{ideals.COUNCIL_NEEDED} counted votes" + (f"; {why}" if why else ""))
    return rows


STEWARD_LINE = ("Steward: looks after the whole community. Sustained work, people you "
                "brought up, something others run on, and the council's trust.")


# ── seats ─────────────────────────────────────────────────────────────────────

def chair(store, order: str, now: float | None = None) -> dict | None:
    """The live chair of ``order`` or None (an ended term is no chair)."""
    c = store.get(f"chair:{order}")
    if not isinstance(c, dict) or not c.get("holder"):
        return None
    end = c.get("term_end")
    if end and _now(now) >= int(end):
        return None
    return c


def chairs(store, now: float | None = None) -> dict[str, dict]:
    return {o: c for o in orders.KEYS if (c := chair(store, o, now)) is not None}


def chair_of(store, user: str, now: float | None = None) -> str:
    for o, c in chairs(store, now).items():
        if str(c.get("holder")) == str(user):
            return o
    return ""


def founder_chairs_max(store) -> int:
    try:
        return max(0, int(_conf(store).get("founder_chairs", FOUNDER_CHAIRS_MAX)))
    except (TypeError, ValueError):
        return FOUNDER_CHAIRS_MAX


def is_seated_founder(store, user: str, now: float | None = None) -> bool:
    o = chair_of(store, user, now)
    return bool(o) and chairs(store, now)[o].get("source") == "founder"


def _vacate(store, order: str, reason: str, by: str, now: int) -> dict | None:
    c = store.get(f"chair:{order}")
    if not isinstance(c, dict) or not c.get("holder"):
        return None
    holder = str(c["holder"])
    store.set(f"chair:{order}", {"order": order, "holder": "", "ended_at": now,
                                 "ended_reason": reason, "previous": c})
    ideals.log_event(store, holder, "herald_unseated" if reason != "resigned"
                     else "herald_resign", by, reason, now=now, order=order,
                     source=c.get("source"))
    return c


def seat_founder(store, order: str, user: str, by: str, owner_id: str, title: str = "",
                 via: str = "slash", now: float | None = None,
                 recorded_by: str = "") -> dict:
    """The founder path: the server OWNER seats ``user`` in ``order``'s vacant chair.

    ``owner_id`` is the guild's owner as read from Discord at run time; an empty
    one refuses. Writes ``chair_founder`` and NOTHING that reads as a vote.
    """
    now = _now(now)
    if not owner_id or str(by) != str(owner_id):
        raise HeraldError("Only the server owner seats a founder.")
    if order not in orders.BY_KEY:
        raise HeraldError(f"Unknown order {order!r}.")
    if not user:
        raise HeraldError("Name the member.")
    if chair(store, order, now) is not None:
        raise HeraldError("That order's chair is held; a founder seat never displaces a "
                          "seated chair.")
    held = chair_of(store, user, now)
    if held:
        raise HeraldError(f"<@{user}> already holds the {held} chair (one seat per person).")
    if candidacy(store, user):
        raise HeraldError(f"<@{user}> is a candidate in an open election; a founder seat "
                          "waits until it closes (one seat per person).")
    n = sum(1 for c in chairs(store, now).values() if c.get("source") == "founder")
    if n >= founder_chairs_max(store):
        raise HeraldError(f"This server allows {founder_chairs_max(store)} founder seats; "
                          "all are taken.")
    if title:
        title = clean_title(title)
    seat = {"order": order, "holder": str(user), "source": "founder", "seated_at": now,
            "term_end": None, "seated_by": str(by), "via": str(via)[:80],
            "self": str(user) == str(by), "title": title}
    if recorded_by:
        seat["recorded_by"] = str(recorded_by)
    store.set(f"chair:{order}", seat)
    if title:
        _set_title(store, user, title, by, now)
    ideals.log_event(store, user, "chair_founder", by, founder_label(seat), now=now,
                     order=order, via=seat["via"], guild_owner=True)
    return seat


def founder_label(seat: dict) -> str:
    if seat.get("recorded_by"):
        return FOUNDER_RECORDED
    return FOUNDER_SELF if seat.get("self") else FOUNDER_LABEL


def candidacy(store, user: str) -> dict | None:
    """The open SEAT election ``user`` stands in, in any order; None when none."""
    return next((e for e in elections(store) if e.get("kind") == "seat"
                 and str(e.get("candidate")) == str(user)), None)


def remove_founder(store, order: str, by: str, owner_id: str,
                   now: float | None = None) -> dict:
    now = _now(now)
    c = chair(store, order, now)
    if c is None or c.get("source") != "founder":
        raise HeraldError("That order has no founder seat.")
    if not owner_id or str(by) not in (str(owner_id), str(c.get("holder"))):
        raise HeraldError("Only the server owner, or the founder, ends a founder seat.")
    _vacate(store, order, "resigned" if str(by) == str(c["holder"]) else
            "founder seat removed by the server owner", by, now)
    ideals.log_event(store, c["holder"], "chair_founder_removed", by, "", now=now,
                     order=order)
    return c


def resign(store, user: str, now: float | None = None) -> str:
    now = _now(now)
    o = chair_of(store, user, now)
    if not o:
        raise HeraldError("You hold no chair.")
    _vacate(store, o, "resigned", user, now)
    return o


# ── titles ────────────────────────────────────────────────────────────────────

def clean_title(text: str) -> str:
    t = " ".join(str(text or "").split())
    if not t or len(t) > TITLE_MAX:
        raise HeraldError(f"A title is 1 to {TITLE_MAX} characters.")
    if any(unicodedata.category(ch).startswith("C") for ch in t):
        raise HeraldError("A title is printable text only.")
    return t


def _set_title(store, user: str, title: str, by: str, now: int) -> None:
    r = ideals.record(store, user) or {"user": str(user), "at": now}
    r["title"] = title
    store.set(f"member:{user}", r)
    ideals.log_event(store, user, "title_set", by, title, now=now)


def set_title(store, user: str, text: str, by: str, owner_id: str,
              screen: Callable[[str], bool | None] | None,
              now: float | None = None) -> str:
    """The server owner sets a title. ``screen`` is the MODEL's verdict (True = ok,
    False = refused, None = could not judge); no screen, or no verdict, holds it."""
    now = _now(now)
    if not owner_id or str(by) != str(owner_id):
        raise HeraldError("Only the server owner sets titles.")
    t = clean_title(text)
    verdict = None
    if screen is not None:
        try:
            verdict = screen(t)
        except Exception:                  # noqa: BLE001 - a model outage holds it
            verdict = None
    if verdict is None:
        raise HeraldError("The title could not be screened right now; nothing was set.")
    if verdict is False:
        raise HeraldError("The screen refused that title.")
    _set_title(store, user, t, by, now)
    return t


def title_of(store, user: str) -> str:
    return str(ideals.record(store, user).get("title") or "")


# ── elections ─────────────────────────────────────────────────────────────────

def _eid() -> str:
    return secrets.token_hex(4)


def election(store, eid: str) -> dict | None:
    e = store.get(f"election:{eid}")
    return e if isinstance(e, dict) else None


def elections(store, order: str | None = None, open_only: bool = True) -> list[dict]:
    out = []
    for k in store.keys("election:"):
        e = store.get(k)
        if isinstance(e, dict) and (order is None or e.get("order") == order) \
                and (not open_only or e.get("state") in OPEN_STATES):
            out.append(e)
    return sorted(out, key=lambda e: int(e.get("opened_at") or 0))


def _save(store, e: dict) -> dict:
    store.set(f"election:{e['id']}", e)
    return e


def nominate(store, candidate: str, by: str, now: float | None = None) -> dict:
    now = _now(now)
    candidate, by = str(candidate), str(by)
    if candidate == by:
        raise HeraldError("Self-nominations are ignored.")
    order = order_of(store, candidate)
    if not order:
        raise HeraldError("The candidate has no order yet (take the assessment).")
    if ideals.level(store, candidate) < 5 or ideals.dormant(store, candidate, now):
        raise HeraldError("A chair candidate holds the 5th ideal and is not dormant.")
    if order_of(store, by) != order or ideals.level(store, by) < NOMINATOR_LEVEL:
        raise HeraldError(f"Nominations come from members of the same order at level "
                          f"{NOMINATOR_LEVEL}+.")
    if chair(store, order, now) is not None:
        raise HeraldError("That order's chair is held; no election opens while it is.")
    if chair_of(store, candidate, now):
        raise HeraldError("The candidate already holds a chair.")
    advance(store, now)
    other = candidacy(store, candidate)
    if other and other.get("order") != order:
        raise HeraldError("The candidate already stands in another order's election.")
    live = [e for e in elections(store, order) if e.get("kind") == "seat"]
    e = next((x for x in live if x["candidate"] == candidate), None)
    if live and e is None:
        raise HeraldError("Another election for this order is already open.")
    if e is None:
        e = {"id": _eid(), "kind": "seat", "order": order, "candidate": candidate,
             "state": "nominated", "noms": [], "votes": {}, "council": {},
             "opened_at": now, "closes_at": now + NOMINATION_DAYS * DAY}
    if e["state"] != "nominated":
        raise HeraldError("Nominations are closed; the vote is open.")
    if by in e["noms"]:
        raise HeraldError("You already nominated this member.")
    e["noms"].append(by)
    ideals.log_event(store, candidate, "herald_nominate", by, "", now=now, order=order,
                     election_id=e["id"], qualified=True)
    note_active(store, by, now)
    if len(e["noms"]) >= NOMINATIONS_NEEDED:
        e["state"], e["closes_at"] = "voting", now + VOTE_DAYS * DAY
    return _save(store, e)


def _open_vote(store, kind: str, order: str, candidate: str, now: int,
               term_holder: str = "") -> dict:
    e = {"id": _eid(), "kind": kind, "order": order, "candidate": candidate,
         "state": "voting", "noms": [], "votes": {}, "council": {}, "opened_at": now,
         "closes_at": now + VOTE_DAYS * DAY}
    return _save(store, e)


def vote(store, eid: str, voter: str, yes: bool, now: float | None = None) -> dict:
    """One weighted vote; the latest replaces the earlier until the vote closes."""
    now = _now(now)
    advance(store, now)
    e = election(store, eid)
    if e is None or e.get("state") != "voting":
        raise HeraldError("That vote is not open.")
    voter = str(voter)
    if voter == e["candidate"]:
        raise HeraldError("Self-votes are ignored.")
    if order_of(store, voter) != e["order"] or not ideals.record(store, voter):
        raise HeraldError("Only members of the order vote.")
    w = vote_weight(store, voter, now)
    e["votes"][voter] = {"sign": 1 if yes else -1, "w": w, "at": now}
    ideals.log_event(store, e["candidate"], "herald_vote", voter, "", now=now,
                     election_id=eid, choice="yes" if yes else "no", w=w)
    note_active(store, voter, now)
    return _save(store, e)


def pick_election(store, order: str, kind: str = "") -> dict:
    """The open VOTE in ``order``; with two open at once ``kind`` must name one."""
    live = [e for e in elections(store, order) if e.get("state") == "voting"
            and (not kind or e.get("kind") == kind)]
    if not live:
        raise HeraldError("No open vote matches.")
    if len(live) > 1:
        raise HeraldError("More than one vote is open: name the kind ("
                          + ", ".join(sorted({x["kind"] for x in live})) + ").")
    return live[0]


def tally(e: dict) -> dict:
    yes = round(sum(v["w"] for v in e.get("votes", {}).values() if v["sign"] > 0), 4)
    no = round(sum(v["w"] for v in e.get("votes", {}).values() if v["sign"] < 0), 4)
    return {"yes_w": yes, "no_w": no, "cast_w": round(yes + no, 4)}


def passes(cast: float, yes: float, active: float) -> bool:
    return active > 0 and cast >= QUORUM * active and cast > 0 and yes >= MAJORITY * cast


def public_status(e: dict) -> dict:
    """What anyone may see: stage and deadlines; totals only once the vote closed.
    Never a voter's name."""
    out = {k: e.get(k) for k in ("id", "kind", "order", "candidate", "state", "opened_at",
                                 "closes_at")}
    out["voters"] = len(e.get("votes") or {})
    if "final" in e:
        out["final"] = dict(e["final"])
    return out


def _close_vote(store, e: dict, now: int) -> dict:
    left = [v for v in e.get("votes", {}) if order_of(store, v) != e["order"]]
    for v in left:                     # a member who left the order no longer votes in it
        e["votes"].pop(v, None)
    if left:
        e["dropped_votes"] = len(left)
    t = tally(e)
    active = active_weight(store, e["order"], now)
    ok = passes(t["cast_w"], t["yes_w"], active)
    e["final"] = {**t, "active_w": active, "quorum_w": round(QUORUM * active, 4),
                  "passed": ok}
    if e["kind"] == "seat":
        e["state"] = "council" if ok else "failed"
        e["closes_at"] = now + COUNCIL_DAYS * DAY
    elif e["kind"] == "retention":
        c = store.get(f"chair:{e['order']}")
        if ok and isinstance(c, dict) and c.get("holder") == e["candidate"]:
            c["term_end"] = int(c.get("term_end") or now) + TERM_DAYS * DAY
            store.set(f"chair:{e['order']}", c)
        e["state"] = "renewed" if ok else "not_renewed"
    elif e["kind"] == "recall":
        if ok:
            _vacate(store, e["order"], "recalled by the order's vote", "system", now)
        e["state"] = "recalled" if ok else "recall_failed"
    return _save(store, e)


def council(store, eid: str, by: str, operator: bool = False,
            now: float | None = None) -> dict:
    now = _now(now)
    advance(store, now)
    e = election(store, eid)
    if e is None or e.get("state") != "council":
        raise HeraldError("That election is not at the council stage.")
    by = str(by)
    if by == e["candidate"]:
        raise HeraldError("Self-votes are ignored.")
    if not (operator or is_operator(store, by)) and ideals.level(store, by) < 5:
        raise HeraldError("Council confirmations come from level-5 members or operators.")
    if order_of(store, by) == e["order"]:
        raise HeraldError("Council confirmations come from outside the candidate's order.")
    e["council"][by] = {"at": now, "operator": bool(operator)}
    ideals.log_event(store, e["candidate"], "herald_council", by, "", now=now,
                     election_id=eid, qualified=True, operator=bool(operator))
    n, _ = council_count(store, e["candidate"], e["council"])
    if n >= ideals.COUNCIL_NEEDED:
        e["state"], e["closes_at"] = "confirm", now + CONFIRM_DAYS * DAY
    return _save(store, e)


def confirm(store, eid: str, by: str, owner_id: str, now: float | None = None) -> dict:
    """Ratify a PASSED vote. Never rescues a failed one: only state ``confirm`` is
    accepted, and that state is reachable only through a passed vote and council."""
    now = _now(now)
    advance(store, now)
    e = election(store, eid)
    if e is None or e.get("state") != "confirm" or not (e.get("final") or {}).get("passed"):
        raise HeraldError("Only an election whose vote passed and whose council confirmed "
                          "can be ratified.")
    if not ((owner_id and str(by) == str(owner_id)) or is_seated_founder(store, by, now)):
        raise HeraldError("The server owner or a seated founder ratifies.")
    if chair(store, e["order"], now) is not None:
        raise HeraldError("That order's chair is already held.")
    held = chair_of(store, e["candidate"], now)
    if held:
        raise HeraldError(f"The candidate already holds the {held} chair (one seat per "
                          "person).")
    if order_of(store, e["candidate"]) != e["order"]:
        raise HeraldError("The candidate has left this order; the election cannot seat them.")
    ideals.log_event(store, e["candidate"], "herald_confirm", by, "", now=now,
                     election_id=eid)
    seat = {"order": e["order"], "holder": e["candidate"], "source": "elected",
            "seated_at": now, "term_end": now + TERM_DAYS * DAY, "seated_by": str(by),
            "via": "election", "election_id": eid, "title": title_of(store, e["candidate"])}
    store.set(f"chair:{e['order']}", seat)
    ideals.log_event(store, e["candidate"], "herald_seated", "system", "", now=now,
                     order=e["order"], term_end=seat["term_end"])
    e["state"] = "seated"
    _save(store, e)
    return seat


def withdraw(store, eid: str, by: str, now: float | None = None) -> dict:
    e = election(store, eid)
    if e is None or e.get("state") not in OPEN_STATES or str(by) != e["candidate"]:
        raise HeraldError("Only the candidate withdraws an open election.")
    e["state"] = "withdrawn"
    return _save(store, e)


# ── recall ────────────────────────────────────────────────────────────────────

def recall_sign(store, order: str, by: str, now: float | None = None) -> dict | None:
    """Sign the recall petition; opens the recall vote once signers hold 30% of
    the order's active weight. Returns the recall election when it opened."""
    now = _now(now)
    c = chair(store, order, now)
    if c is None:
        raise HeraldError("That order has no chair.")
    if order_of(store, by) != order:
        raise HeraldError("Only members of the order sign its recall petition.")
    key = f"recall:{order}:{c['seated_at']}"
    pet = store.get(key) or {"signers": {}}
    pet["signers"][str(by)] = vote_weight(store, by, now)
    store.set(key, pet)
    ideals.log_event(store, c["holder"], "herald_recall_sign", by, "", now=now, order=order)
    if sum(pet["signers"].values()) >= QUORUM * active_weight(store, order, now):
        return _open_recall(store, order, c, now, "petition")
    return None


def recall_council(store, order: str, by: str, operator: bool = False,
                   now: float | None = None) -> dict | None:
    """A council member backs a recall; 2 of them open the recall vote."""
    now = _now(now)
    c = chair(store, order, now)
    if c is None:
        raise HeraldError("That order has no chair.")
    if not (operator or is_operator(store, by)) and ideals.level(store, by) < 5:
        raise HeraldError("Only council members (level 5 or operators) start a recall.")
    key = f"recall:{order}:{c['seated_at']}"
    pet = store.get(key) or {"signers": {}}
    pet.setdefault("council", [])
    if str(by) not in pet["council"] and str(by) != str(c["holder"]):
        pet["council"].append(str(by))
    store.set(key, pet)
    if len(pet["council"]) >= RECALL_COUNCIL:
        return _open_recall(store, order, c, now, "council")
    return None


def _open_recall(store, order: str, c: dict, now: int, how: str) -> dict:
    live = [e for e in elections(store, order) if e.get("kind") == "recall"]
    if live:
        return live[0]
    ideals.log_event(store, c["holder"], "herald_recall_open", "system", how, now=now,
                     order=order)
    return _open_vote(store, "recall", order, str(c["holder"]), now)


# ── time: deadlines, terms, dormancy ─────────────────────────────────────────

def advance(store, now: float | None = None) -> list[str]:
    """Move every election and term past its deadline; returns staff notes
    (dormancy reviews). Safe to call often; the hourly job calls it."""
    now = _now(now)
    notes: list[str] = []
    for e in elections(store):
        if now < int(e.get("closes_at") or 0):
            continue
        if e["state"] == "voting":
            _close_vote(store, e, now)
        elif e["state"] in ("nominated", "council", "confirm"):
            e["state"] = "lapsed"
            _save(store, e)
    for order, c in chairs(store, now).items():
        end = c.get("term_end")
        if end and int(end) - now <= RETENTION_LEAD_DAYS * DAY:
            if not any(x.get("kind") == "retention" and x.get("candidate") == c["holder"]
                       and int(x.get("opened_at") or 0) >= int(end) - RETENTION_LEAD_DAYS * DAY
                       for x in elections(store, order, open_only=False)):
                _open_vote(store, "retention", order, str(c["holder"]), now)
        last = ideals.last_active(store, c["holder"])
        if last and now - last > DORMANT_REVIEW_DAYS * DAY and not c.get("review_raised"):
            c["review_raised"] = now
            store.set(f"chair:{order}", c)
            notes.append(f"The {order} chair <@{c['holder']}> has been quiet for "
                         f"{DORMANT_REVIEW_DAYS}+ days: a council review, nothing automatic.")
    return notes


# ── display ───────────────────────────────────────────────────────────────────

def seat_name(order: str, set_name: str | None = None) -> str:
    n = orders.names(set_name)
    return n["seat"].format(order=orders.archetype_name(order, set_name))


def seat_line(store, user: str, set_name: str | None = None,
              now: float | None = None) -> str:
    """The one-line badge for /progress and the order view; "" for no seat."""
    o = chair_of(store, user, now)
    if not o:
        return ""
    c = chairs(store, now)[o]
    title = c.get("title") or title_of(store, user)
    parts = [title] if title else []
    if c.get("source") == "founder":
        label = founder_label(c)
        kind = (seat_name(o, set_name) if ideals.level(store, user) >= 5
                else f"Founder Chair, {orders.archetype_name(o, set_name)}")
        parts += [kind, label]
    else:
        end = time.strftime("%Y-%m-%d", time.gmtime(int(c.get("term_end") or 0)))
        parts += [seat_name(o, set_name), f"Elected, term ends {end}"]
    return " · ".join(parts)


def order_view(store, order: str, lead_agent: str = "", set_name: str | None = None,
               now: float | None = None) -> str:
    n = orders.names(set_name)
    head = n["order_heading"].format(order=orders.archetype_name(order, set_name))
    c = chair(store, order, now)
    lines = [head]
    if c is None:
        lines.append(f"{n['seat'].format(order=orders.archetype_name(order, set_name))}: "
                     "vacant")
    else:
        lines.append(f"<@{c['holder']}> · {seat_line(store, c['holder'], set_name, now)}")
    if lead_agent:
        lines.append(f"Lead agent: {lead_agent}")
    for e in elections(store, order):
        s = public_status(e)
        lines.append(f"Open {s['kind']} for <@{s['candidate']}>: {s['state']} until "
                     f"<t:{s['closes_at']}:d>")
    return "\n".join(lines)


def describe(seat: dict) -> dict[str, Any]:
    """A seat as plain data (tests and the CLI)."""
    return {k: seat.get(k) for k in ("order", "holder", "source", "title", "via",
                                     "seated_by", "term_end", "self")}
