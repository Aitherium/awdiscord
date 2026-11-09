"""The join plugin's assessment and ideals ladder: forms, commands and the sweep.

Part of the ``join`` plugin (``awdiscord.builtin.join`` registers these). The
rules live in ``awdiscord.ideals`` and the names in ``awdiscord.orders``; this
module is the Discord surface:

    [Take the assessment]          10 scenario pickers over 3 pages -> archetype
    /enlist action:assess          the same; a retake is allowed after 30 days
    /progress [user]               current ideal, archetype, squad, evidence, and
                                   exactly what the next ideal still needs
    /verify-github                 prove the GitHub account in your join record
                                   (a token in your GitHub bio); only a verified
                                   account's merged PRs count
    /prove-deploy url              prove a self-host deploy: the URL answers
                                   ``/health`` and serves your token at
                                   ``/.well-known/aither-challenge.txt``
    /review approve user track     approve a track practical (3rd ideal+ or operator)
    /nominate user milestone       nominate a squad-mate who led a milestone to release
    /council vote user evidence    a steward vote (5th ideal or operator)
    /mentor mentor                 name who mentors you (once)
    /ideal evidence|confirm|umbrella   operators: record evidence, confirm a 4th
                                   ideal, grant the agent-access role early

Every handler checks both join switches (``join.is_open``). Every grant goes
through ``join.promote``, which reads the Oath Log; nothing here sets a level.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from awdiscord import ideals, orders
from awdiscord.plugins import PluginError

log = logging.getLogger("awdiscord.join")

RETAKE_DAYS = 30
SWEEP_MAX = 30
CHALLENGE_PATH = "/.well-known/aither-challenge.txt"

#: tests swap in a fake: ``fn(url) -> text`` (raises ValueError on a refusal)
FETCH: list = [None]


def _j():
    from awdiscord.builtin import join
    return join


def _fetch(url: str) -> str:
    if FETCH[0] is not None:
        return FETCH[0](url)
    from awdiscord.builtin import web
    return web.fetch_url(url, max_chars=2000)


def _open_or_raise(ctx) -> None:
    if not _j().is_open(ctx.host, ctx.guild):
        raise PluginError("Joining is not open in this server.")


def _touch(store, user: str) -> None:
    r = ideals.record(store, user)
    if r:
        r["last_active"] = int(time.time())
        store.set(f"member:{user}", r)


def _is_operator(ctx) -> bool:
    return bool(ctx.is_admin)


# ── the assessment ────────────────────────────────────────────────────────────

def _answers(ctx) -> dict:
    a = ctx.store.get(f"assess:{ctx.user}")
    return a if isinstance(a, dict) else {}


def assess_view(ctx, page: int) -> tuple[str, list]:
    chosen = _answers(ctx)
    qs = orders.PAGES[page]
    lines = [f"**Assessment** - page {page + 1} of {len(orders.PAGES)}. "
             "Pick the answer closest to what you would really do."]
    rows = []
    for qi in qs:
        text, options = orders.QUESTIONS[qi]
        lines.append(f"**{qi + 1}.** {text}")
        rows.append({"type": 1, "components": [{
            "type": 3, "custom_id": _j().PLUGIN.button_id(f"aq:{qi}"),
            "placeholder": f"{qi + 1}. choose", "min_values": 1, "max_values": 1,
            "options": [{"label": label[:100], "value": str(i),
                         "default": chosen.get(str(qi)) == i}
                        for i, (label, _) in enumerate(options)]}]})
    last = page == len(orders.PAGES) - 1
    rows.append({"type": 1, "components": [{
        "type": 2, "style": 3 if last else 1, "label": "Finish" if last else "Next",
        "custom_id": _j().PLUGIN.button_id("afinish" if last else f"apage:{page + 1}")}]})
    return "\n".join(lines), rows


def retake_block(record: dict, now: float | None = None) -> str:
    """Why an assessment may not be (re)taken now, or ""."""
    if not record:
        return "Join first: run `/enlist`."
    done = record.get("assessment") or {}
    if not done:
        return ""
    now = time.time() if now is None else now
    wait = int(done.get("at") or 0) + RETAKE_DAYS * ideals.DAY - now
    if wait > 0:
        return (f"You can retake the assessment in {int(wait // ideals.DAY) + 1} day(s) "
                f"(once every {RETAKE_DAYS} days).")
    return ""


def start_assessment(ctx):
    _open_or_raise(ctx)
    block = retake_block(ideals.record(ctx.store, ctx.user))
    if block:
        return ctx.reply(block, private=True)
    ctx.store.set(f"assess:{ctx.user}", {})
    text, rows = assess_view(ctx, 0)
    return ctx.reply(text, private=True, components=rows)


def on_button(ctx, payload: str):
    """Assessment buttons and pickers (``assess``, ``aq:<i>``, ``apage:<n>``, ``afinish``)."""
    if payload == "assess":
        return start_assessment(ctx)
    if payload.startswith("aq:"):
        qi = int(payload[3:]) if payload[3:].isdigit() else -1
        if not 0 <= qi < len(orders.QUESTIONS):
            return ctx.reply("Unknown question.", private=True)
        values = (ctx.interaction.get("data") or {}).get("values") or []
        idx = int(values[0]) if values and str(values[0]).isdigit() else -1
        if not 0 <= idx < len(orders.QUESTIONS[qi][1]):
            return ctx.reply("Unknown answer.", private=True)
        a = _answers(ctx)
        a[str(qi)] = idx
        ctx.store.set(f"assess:{ctx.user}", a)
        page = next(n for n, qs in enumerate(orders.PAGES) if qi in qs)
        text, rows = assess_view(ctx, page)
        return ctx.update(text, components=rows)
    if payload.startswith("apage:"):
        page = int(payload[6:]) if payload[6:].isdigit() else 0
        page = max(0, min(page, len(orders.PAGES) - 1))
        a = _answers(ctx)
        missing = [qi + 1 for qi in orders.PAGES[page - 1] if str(qi) not in a] if page else []
        if missing:
            return ctx.reply("Answer " + ", ".join(f"Q{m}" for m in missing) + " first.",
                             private=True)
        text, rows = assess_view(ctx, page)
        return ctx.update(text, components=rows)
    if payload == "afinish":
        a = _answers(ctx)
        missing = [i + 1 for i in range(len(orders.QUESTIONS)) if str(i) not in a]
        if missing:
            return ctx.reply("Answer " + ", ".join(f"Q{m}" for m in missing) + " first.",
                             private=True)
        answers = [int(a[str(i)]) for i in range(len(orders.QUESTIONS))]
        guild, user = str(ctx.guild), ctx.user
        return ctx.defer(lambda: finish_assessment(ctx.host, guild, user, answers),
                         private=True)
    return None


def finish_assessment(host, guild: str, user: str, answers: list[int],
                      now: float | None = None) -> dict:
    """Score, store with the join record, swap the archetype role, promote."""
    from awdiscord.plugins import ScopedStore
    j = _j()
    store = ScopedStore(host.store, guild, j.NAME)
    conf = j.config(store)
    record = ideals.record(store, user)
    block = retake_block(record, now)
    if block:
        return {"content": block}
    key, axes = orders.score(answers)
    at = int(time.time() if now is None else now)
    old = (record.get("assessment") or {}).get("archetype", "")
    result = {"archetype": key, "axes": axes, "answers": answers, "at": at,
              "name_set": conf.get("name_set")}
    record["assessment"] = result
    record["assessments"] = (record.get("assessments") or [])[-9:] + [result]
    record["last_active"] = at
    store.set(f"member:{user}", record)
    store.delete(f"assess:{user}")
    ideals.log_event(store, user, "assessment", user, key, now=now)
    errors = []
    if old and old != key:
        errors.append(j.drop_role(host, guild, user, j.role_name(conf, "archetype", old)))
    errors.append(j.give_role(host, guild, user, j.role_name(conf, "archetype", key)))
    # Holding an order/archetype implies the umbrella (agent access): same step.
    umbrella = j.role_name(conf, "umbrella")
    errors.append(j.give_role(host, guild, user, umbrella, hoist=True))
    if not ideals.events(store, user, "umbrella"):
        ideals.log_event(store, user, "umbrella", "system", f"with {key}", now=now)
    errors = [e for e in errors if e]
    if errors:
        j.audit(store, "system", "role_gap", user, "; ".join(errors))
    gained = j.promote(host, guild, user, now)
    a = orders.BY_KEY[key]
    embed = {"title": f"Your archetype: {j.role_name(conf, 'archetype', key)}",
             "description": a.identity, "color": a.color,
             "fields": [{"name": "Strengths", "value": ", ".join(a.strengths)},
                        {"name": "In a squad", "value": a.squad_role}]}
    text = (f"**{orders.level_display(1, conf.get('name_set'))}**. You also hold "
            f"**{umbrella}**: the agents are open to you." if 1 in gained
            else "Assessment saved.")
    return {"content": text, "embeds": [embed]}


# ── /progress ─────────────────────────────────────────────────────────────────

def progress_card(host, guild: str, user: str, now: float | None = None) -> dict:
    from awdiscord.plugins import ScopedStore
    j = _j()
    store = ScopedStore(host.store, guild, j.NAME)
    conf = j.config(store)
    r = ideals.record(store, user)
    if not r:
        return {"content": f"<@{user}> has not joined yet."}
    lvl = int(r.get("ideal") or 0)
    head = orders.level_display(lvl, conf.get("name_set"))
    track = conf["tracks"].get(r.get("track") or "", "-")
    cert = ideals.certified_track(store, user)
    if cert in conf["tracks"]:
        track = orders.certified_display(conf["tracks"][cert], conf.get("name_set"))
    squads = ", ".join(conf["squads"].get(s, s) for s in r.get("squads") or []) or "-"
    evidence = [e for e in ideals.events(store, user)
                if e.get("evidence") and e.get("kind") not in ("joined", "assessment")]
    ev_lines = "\n".join(f"<t:{e['at']}:d> {e['kind']}: {e['evidence']}"
                         for e in evidence[-6:]) or "none yet"
    fields = j.archetype_fields(conf, r) + [
        {"name": "Ideal", "value": head, "inline": True},
        {"name": "Squad", "value": squads[:1024], "inline": True},
        {"name": "AAA track", "value": track, "inline": True},
        {"name": "Evidence", "value": ev_lines[:1024]}]
    if lvl < orders.MAX_IDEAL:
        req = ideals.requirements(store, user, lvl + 1, now)
        bar, _ = ideals.progress_bar(store, user, now)
        lines = [f"{'[x]' if x['met'] else '[ ]'} {x['label']}"
                 + ("" if x["met"] or not x["detail"] else f" - {x['detail']}") for x in req]
        fields.append({"name": f"Next: {orders.level_display(lvl + 1, conf.get('name_set'))}"
                               f" {bar}",
                       "value": "\n".join(lines)[:1024]})
    if ideals.dormant(store, user, now):
        fields.append({"name": "Status", "value": "dormant (90+ days quiet; nothing is "
                                                  "taken away)"})
    embed = {"title": f"{r.get('handle') or 'Member'} - {j._number(int(r.get('number') or 0))}",
             "color": 15844367, "fields": fields}
    return {"content": "", "embeds": [embed]}


def progress(ctx):
    _open_or_raise(ctx)
    target = str(ctx.opt("user") or ctx.user)
    guild = str(ctx.guild)
    _touch(ctx.store, ctx.user)

    def work() -> dict:
        if target == ctx.user:
            check_merged_pr(ctx.host, guild, target)
            _j().promote(ctx.host, guild, target)
        return progress_card(ctx.host, guild, target)
    return ctx.defer(work, private=True)


# ── evidence: GitHub and deploys ──────────────────────────────────────────────

def check_merged_pr(host, guild: str, user: str) -> str:
    """Log a merged PR by the member's VERIFIED GitHub account in a squad repo.

    Returns the PR URL logged, or "". An unverified handle never counts: anybody
    can type somebody else's username into a form.
    """
    from awdiscord.plugins import ScopedStore
    j = _j()
    store = ScopedStore(host.store, guild, j.NAME)
    conf = j.config(store)
    r = ideals.record(store, user)
    login = str(r.get("github") or "")
    if not login or not r.get("github_verified") or ideals.events(store, user, "merged_pr"):
        return ""
    repos = [conf["repos"][s] for s in r.get("squads") or [] if conf["repos"].get(s)]
    if not repos:
        return ""
    from awdiscord.builtin import github
    q = " ".join(f"repo:{x}" for x in repos) + f" is:pr is:merged author:{login}"
    query = ("query($q: String!) { search(query: $q, type: ISSUE, first: 1) "
             "{ nodes { ... on PullRequest { url } } } }")
    try:
        data = github.graphql(query, {"q": q}, opener=github.OPENER[0])
    except PluginError as exc:
        log.info("join: merged-PR check failed for %s: %s", user, exc)
        return ""
    nodes = [n for n in ((data.get("search") or {}).get("nodes") or []) if n and n.get("url")]
    if not nodes:
        return ""
    url = str(nodes[0]["url"])
    ideals.log_event(store, user, "merged_pr", "github", url, qualified=True)
    return url


def verify_github(ctx):
    _open_or_raise(ctx)
    r = ideals.record(ctx.store, ctx.user)
    login = str(r.get("github") or "")
    if not login:
        return ctx.reply("Add your GitHub username first: run `/enlist` again.", private=True)
    if r.get("github_verified"):
        return ctx.reply(f"GitHub **{login}** is already verified.", private=True)
    challenge = r.get("gh_challenge") or {}
    if not challenge.get("token"):
        r["gh_challenge"] = {"token": "aither-" + secrets.token_hex(8), "at": int(time.time())}
        ctx.store.set(f"member:{ctx.user}", r)
        return ctx.reply(f"Put `{r['gh_challenge']['token']}` in your GitHub profile bio "
                         f"(github.com/{login}), then run `/verify-github` again. You can "
                         "remove it afterwards.", private=True)
    guild, user, token = str(ctx.guild), ctx.user, challenge["token"]

    def work() -> str:
        from awdiscord.builtin import github
        data = github.graphql("query($l: String!) { user(login: $l) { bio } }",
                              {"l": login}, opener=github.OPENER[0])
        bio = str(((data.get("user") or {}).get("bio")) or "")
        if token not in bio:
            return f"`{token}` is not in github.com/{login}'s bio yet."
        rec = ideals.record(ctx.store, user)
        rec["github_verified"] = True
        rec.pop("gh_challenge", None)
        ctx.store.set(f"member:{user}", rec)
        ideals.log_event(ctx.store, user, "github_verified", user,
                         f"https://github.com/{login}", qualified=True)
        pr = check_merged_pr(ctx.host, guild, user)
        gained = _j().promote(ctx.host, guild, user)
        out = f"GitHub **{login}** verified."
        if pr:
            out += f" Merged PR found: {pr}."
        if gained:
            out += f" You reached ideal {gained[-1]}."
        return out
    return ctx.defer(work, private=True)


def _base(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise PluginError("Give the full URL, starting with https://")
    return url


def prove_deploy(ctx):
    _open_or_raise(ctx)
    base = _base(ctx.opt("url"))
    r = ideals.record(ctx.store, ctx.user)
    if not r:
        return ctx.reply("Join first: run `/enlist`.", private=True)
    challenge = r.get("deploy_challenge") or {}
    if challenge.get("url") != base or not challenge.get("token"):
        token = "aither-deploy-" + secrets.token_hex(10)
        r["deploy_challenge"] = {"url": base, "token": token, "at": int(time.time())}
        ctx.store.set(f"member:{ctx.user}", r)
        return ctx.reply(f"Serve `{token}` at `{base}{CHALLENGE_PATH}` and make sure "
                         f"`{base}/health` answers, then run this command again with the "
                         "same URL.", private=True)
    guild, user, token = str(ctx.guild), ctx.user, challenge["token"]

    def work() -> str:
        try:
            _fetch(base + "/health")
        except ValueError as exc:
            return f"`{base}/health` did not answer: {exc}"
        try:
            body = _fetch(base + CHALLENGE_PATH)
        except ValueError as exc:
            return f"`{base}{CHALLENGE_PATH}` did not answer: {exc}"
        if token not in body:
            return f"`{base}{CHALLENGE_PATH}` does not contain your token yet."
        rec = ideals.record(ctx.store, user)
        rec.pop("deploy_challenge", None)
        ctx.store.set(f"member:{user}", rec)
        ideals.log_event(ctx.store, user, "deploy_verified", user, base, qualified=True)
        gained = _j().promote(ctx.host, guild, user)
        return "Deploy verified." + (f" You reached ideal {gained[-1]}." if gained else "")
    return ctx.defer(work, private=True)


# ── peer and operator approvals ───────────────────────────────────────────────

def _target(ctx) -> str:
    target = str(ctx.opt("user") or "")
    if not target:
        raise PluginError("Name the member.")
    if target == ctx.user:
        raise PluginError("Nobody approves, nominates or votes for themselves.")
    if not ideals.record(ctx.store, target):
        raise PluginError("That member has not joined yet.")
    return target


def _after(ctx, target: str, done: str) -> Any:
    guild = str(ctx.guild)

    def work() -> str:
        gained = _j().promote(ctx.host, guild, target)
        return done + (f" <@{target}> reached ideal {gained[-1]}." if gained else "")
    return ctx.defer(work, private=True)


def review_approve(ctx):
    _open_or_raise(ctx)
    target = _target(ctx)
    track = str(ctx.opt("track") or "")
    conf = _j().config(ctx.store)
    if track not in conf["tracks"]:
        return ctx.reply(f"Pick one of: {', '.join(conf['tracks'])}.", private=True)
    op = _is_operator(ctx)
    if not op and ideals.level(ctx.store, ctx.user) < 3:
        return ctx.reply("Only members at the 3rd ideal or above, or an operator, can "
                         "approve a practical.", private=True)
    ideals.log_event(ctx.store, target, "review_approve", ctx.user,
                     str(ctx.opt("evidence") or ""), qualified=True, operator=op, track=track)
    _j().audit(ctx.store, ctx.user, "review_approve", target, track)
    return _after(ctx, target, f"Approval for <@{target}> ({conf['tracks'][track]}) logged.")


def nominate(ctx):
    _open_or_raise(ctx)
    target = _target(ctx)
    me = ideals.record(ctx.store, ctx.user)
    them = ideals.record(ctx.store, target)
    shared = set(me.get("squads") or []) & set(them.get("squads") or [])
    if not shared or int(me.get("ideal") or 0) < 1:
        return ctx.reply("A nomination comes from a sworn member of the same squad.",
                         private=True)
    milestone = str(ctx.opt("milestone") or "").strip()
    if not milestone.startswith("https://"):
        return ctx.reply("Link the release (https://...).", private=True)
    ideals.log_event(ctx.store, target, "nominate", ctx.user, milestone, qualified=True,
                     squads=sorted(shared))
    _j().audit(ctx.store, ctx.user, "nominate", target, milestone)
    return _after(ctx, target, f"Nomination of <@{target}> logged; an operator confirms "
                               "with `/ideal confirm`.")


def council_vote(ctx):
    _open_or_raise(ctx)
    target = _target(ctx)
    op = _is_operator(ctx)
    if not op and ideals.level(ctx.store, ctx.user) < 5:
        return ctx.reply("The council is members at the 5th ideal and operators.",
                         private=True)
    evidence = str(ctx.opt("evidence") or "").strip()
    if not evidence.startswith("https://"):
        return ctx.reply("A vote carries the adoption evidence (https://...).", private=True)
    prior = {e.get("by") for e in ideals.events(ctx.store, target, "council_vote")}
    if ctx.user in prior:
        return ctx.reply("You already voted for this member.", private=True)
    ideals.log_event(ctx.store, target, "council_vote", ctx.user, evidence, qualified=True,
                     operator=op)
    ideals.log_event(ctx.store, target, "adoption", ctx.user, evidence, qualified=True)
    _j().audit(ctx.store, ctx.user, "council_vote", target, evidence)
    return _after(ctx, target, f"Council vote for <@{target}> logged.")


def mentor(ctx):
    _open_or_raise(ctx)
    who = str(ctx.opt("mentor") or "")
    me = ideals.record(ctx.store, ctx.user)
    if not me:
        return ctx.reply("Join first: run `/enlist`.", private=True)
    if not who or who == ctx.user:
        return ctx.reply("Name someone other than yourself.", private=True)
    if not ideals.record(ctx.store, who):
        return ctx.reply("Your mentor has to have joined too.", private=True)
    if me.get("mentor") and not _is_operator(ctx):
        return ctx.reply(f"Your mentor is already <@{me['mentor']}>; an operator can "
                         "change it.", private=True)
    me["mentor"] = who
    ctx.store.set(f"member:{ctx.user}", me)
    ideals.log_event(ctx.store, ctx.user, "mentor_set", ctx.user, f"<@{who}>", mentor=who)
    return ctx.reply(f"<@{who}> is now your mentor.", private=True)


# ── operators ─────────────────────────────────────────────────────────────────

def _needs_operator(ctx) -> None:
    if not _is_operator(ctx):
        raise PluginError("Only server managers can do that.")


EVIDENCE_KINDS = {"merged_pr": "merged_pr", "deploy": "deploy_verified",
                  "adoption": "adoption"}


def ideal_evidence(ctx):
    _open_or_raise(ctx)
    _needs_operator(ctx)
    target = _target(ctx)
    kind = EVIDENCE_KINDS.get(str(ctx.opt("kind") or ""))
    url = str(ctx.opt("url") or "").strip()
    if kind is None or not url.startswith("https://"):
        return ctx.reply("Pick a kind and give an https:// evidence link.", private=True)
    ideals.log_event(ctx.store, target, kind, ctx.user, url, qualified=True, operator=True)
    _j().audit(ctx.store, ctx.user, "evidence", target, f"{kind} {url}")
    return _after(ctx, target, f"Evidence for <@{target}> logged.")


def ideal_confirm(ctx):
    _open_or_raise(ctx)
    _needs_operator(ctx)
    target = _target(ctx)
    if ideals.level(ctx.store, target) != 3:
        return ctx.reply("A confirmation is for a member at the 3rd ideal.", private=True)
    ideals.log_event(ctx.store, target, "confirm_4", ctx.user, "", operator=True,
                     qualified=True)
    _j().audit(ctx.store, ctx.user, "confirm_4", target)
    return _after(ctx, target, f"Confirmation for <@{target}> logged.")


def ideal_umbrella(ctx):
    """Grant (or take back) the agent-access role early, e.g. to a partner."""
    _open_or_raise(ctx)
    _needs_operator(ctx)
    target = str(ctx.opt("user") or "")
    if not target or target == ctx.user:
        return ctx.reply("Name someone other than yourself.", private=True)
    revoke = bool(ctx.opt("revoke", False))
    j = _j()
    conf = j.config(ctx.store)
    name = j.role_name(conf, "umbrella")
    guild, by = str(ctx.guild), ctx.user

    def work() -> str:
        err = (j.drop_role if revoke else j.give_role)(ctx.host, guild, target, name)
        if err:
            return err
        if ideals.record(ctx.store, target):
            ideals.log_event(ctx.store, target, "umbrella", by, "revoked" if revoke else
                             "granted early by an operator", operator=True)
        j.audit(ctx.store, by, "umbrella_revoke" if revoke else "umbrella_grant", target, name)
        return f"{'Removed' if revoke else 'Gave'} **{name}** {'from' if revoke else 'to'} " \
               f"<@{target}>."
    return ctx.defer(work, private=True)


# ── the sweep and the digest section ──────────────────────────────────────────

def sweep(host, guild: str, now: float | None = None) -> int:
    """Check verified members at the 1st ideal for a merged PR; promote. Count."""
    from awdiscord.plugins import ScopedStore
    j = _j()
    store = ScopedStore(host.store, guild, j.NAME)
    n = 0
    for key in store.keys("member:")[:2000]:
        if n >= SWEEP_MAX:
            break
        r = store.get(key)
        if not isinstance(r, dict) or int(r.get("ideal") or 0) != 1 \
                or not r.get("github_verified"):
            continue
        user = str(r.get("user"))
        n += 1
        if check_merged_pr(host, guild, user):
            j.promote(host, guild, user, now)
    return n


def close_to_next(store, squad: str, conf: dict, now: float | None = None) -> list[str]:
    """Members of ``squad`` at least half-way to their next ideal (not dormant)."""
    out = []
    for key in store.keys("member:"):
        r = store.get(key)
        if not isinstance(r, dict) or squad not in (r.get("squads") or []):
            continue
        user = str(r.get("user"))
        lvl = int(r.get("ideal") or 0)
        if lvl >= orders.MAX_IDEAL or ideals.dormant(store, user, now):
            continue
        bar, frac = ideals.progress_bar(store, user, now)
        if 0.5 <= frac < 1:
            out.append(f"<@{user}> {bar} to "
                       f"{orders.level_display(lvl + 1, conf.get('name_set'))}")
    return out[:10]

