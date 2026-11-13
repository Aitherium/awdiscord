"""The join plugin's Order Chair (themed: Herald) commands. Rules: ``awdiscord.heralds``.

    /chair  nominate user          a level-3+ member of the same order nominates
    /chair  vote yes|no            a weighted vote in your order's open election
    /chair  council user           a level-5 member or operator from ANOTHER order
    /chair  confirm user           the server owner or a seated founder ratifies a
                                   vote that passed (never a failed one)
    /chair  resign                 the sitting chair steps down
    /chair  recall order [start]   sign the recall petition; council members "start"
    /chair  status [order]         stage, deadlines; totals only after the vote closed
    /chair  order [order]          the order view: chair, Founder or Elected, title,
                                   lead agent, open elections
    /founder set user order title  SERVER OWNER only: an appointed seat, shown
                                   publicly as "Founder, ... not elected"
    /founder title user text       SERVER OWNER only: a title (model-screened)
    /founder remove order          SERVER OWNER only (or the founder): end the seat
    /founder list                  everyone: every founder seat and how it was set

The owner is read from Discord (``GET /guilds/{id}``) on every call, never cached
and never typed. Every refusal is a private reply.
"""
from __future__ import annotations

from typing import Any

from awdiscord import heralds, orders
from awdiscord.heralds import HeraldError

#: tests swap in a fake: ``fn(host, guild) -> owner id``
GUILD_OWNER: list = [None]
#: tests swap in a fake: ``fn(text) -> True | False | None`` (the model's verdict)
SCREEN: list = [None]

#: The order's lead agent (docs/ORDER_CODEX.md, generated from
#: AitherOS/config/radiants/lineage.yaml). A chair sits beside it.
LEAD_AGENTS = {"warden": "Hera", "arbiter": "Themis", "breaker": "Chaos",
               "lamplighter": "Saga", "seeker": "Vera", "weaver": "Iris",
               "gatewright": "Lyra", "pathfinder": "Demiurge", "keystone": "Atlas",
               "binder": "Aither"}

SCREEN_PROMPT = ("You screen a public title for a Discord community profile. Answer OK "
                 "if it is a plain, non-offensive job or honorific title that does not "
                 "impersonate staff of another organisation; otherwise answer HOLD and "
                 "one short reason.")


def _j():
    from awdiscord.builtin import join
    return join


def guild_owner(host, guild: str) -> str:
    """The guild's owner id, read from Discord on every call (join_ladder's reader)."""
    if GUILD_OWNER[0] is not None:
        return str(GUILD_OWNER[0](host, guild) or "")
    from awdiscord.builtin import join_ladder
    return join_ladder.guild_owner(host, guild)


def model_screen(ctx) -> Any:
    """The model's verdict on a title: True ok, False refused, None could not judge."""
    if SCREEN[0] is not None:
        return SCREEN[0]
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return None

    def screen(text: str) -> bool | None:
        answer = str(agent.ask(f"Title: {text}", user=str(ctx.user),
                               context=SCREEN_PROMPT) or "").strip().upper()
        if answer.startswith("OK"):
            return True
        if answer.startswith("HOLD"):
            return False
        return None
    return screen


def _set(ctx) -> str:
    return _j().config(ctx.store).get("name_set")


def _order_opt(ctx, default: str = "") -> str:
    o = str(ctx.opt("order") or default or "").lower()
    return o if o in orders.BY_KEY else ""


def _run(ctx, fn) -> Any:
    try:
        out = fn()
    except HeraldError as exc:
        return ctx.reply(str(exc), private=True)
    for note in heralds.advance(ctx.store):
        _j().audit(ctx.store, "system", "chair_review", "", note)
    return ctx.reply(out, private=True)


def _open_election(ctx, candidate: str = "", kinds=("seat", "retention", "recall")) -> dict:
    order = heralds.order_of(ctx.store, ctx.user) if not candidate \
        else heralds.order_of(ctx.store, candidate)
    for e in heralds.elections(ctx.store, order):
        if e.get("kind") in kinds and (not candidate or e["candidate"] == candidate):
            return e
    raise HeraldError("No open election matches.")


def nominate(ctx):
    target = str(ctx.opt("user") or "")
    return _run(ctx, lambda: _say_stage(heralds.nominate(ctx.store, target, ctx.user)))


def _say_stage(e: dict) -> str:
    s = heralds.public_status(e)
    return (f"Election {s['id']} for <@{s['candidate']}> ({s['order']}, {s['kind']}): "
            f"{s['state']} until <t:{s['closes_at']}:d>.")


def vote(ctx):
    yes = str(ctx.opt("choice") or "").lower() == "yes"

    kind = str(ctx.opt("kind") or "")

    def go() -> str:
        e = heralds.pick_election(ctx.store, heralds.order_of(ctx.store, ctx.user), kind)
        heralds.vote(ctx.store, e["id"], ctx.user, yes)
        return ("Vote recorded. You may change it until the vote closes; totals stay "
                "sealed until then.")
    return _run(ctx, go)


def council(ctx):
    target = str(ctx.opt("user") or "")

    def go() -> str:
        e = _open_election(ctx, target, kinds=("seat",))
        return _say_stage(heralds.council(ctx.store, e["id"], ctx.user,
                                          operator=bool(ctx.is_admin)))
    return _run(ctx, go)


def confirm(ctx):
    target = str(ctx.opt("user") or "")
    guild, host = str(ctx.guild), ctx.host

    def go() -> str:
        e = _open_election(ctx, target, kinds=("seat",))
        seat = heralds.confirm(ctx.store, e["id"], ctx.user, guild_owner(host, guild))
        _j().audit(ctx.store, ctx.user, "chair_confirm", target, seat["order"])
        return f"<@{target}> is seated: {heralds.seat_line(ctx.store, target, _set(ctx))}."
    return _run(ctx, go)


def resign(ctx):
    def go() -> str:
        o = heralds.resign(ctx.store, ctx.user)
        _j().audit(ctx.store, ctx.user, "chair_resign", ctx.user, o)
        return f"You stepped down from the {o} chair."
    return _run(ctx, go)


def recall(ctx):
    order = _order_opt(ctx)
    start = bool(ctx.opt("start", False))

    def go() -> str:
        if not order:
            raise HeraldError("Name the order.")
        opened = (heralds.recall_council(ctx.store, order, ctx.user,
                                         operator=bool(ctx.is_admin))
                  if start else heralds.recall_sign(ctx.store, order, ctx.user))
        return _say_stage(opened) if opened else "Recorded; the recall vote has not opened."
    return _run(ctx, go)


def status(ctx):
    order = _order_opt(ctx, heralds.order_of(ctx.store, ctx.user))
    rows = [_say_stage(e) + (f" Final: {e['final']}" if e.get("final") else "")
            for e in heralds.elections(ctx.store, order or None, open_only=False)[-5:]]
    return ctx.reply("\n".join(rows) or "No elections yet.", private=True)


def order_view(ctx):
    order = _order_opt(ctx, heralds.order_of(ctx.store, ctx.user)) or "warden"
    return ctx.reply(heralds.order_view(ctx.store, order, LEAD_AGENTS.get(order, ""),
                                        _set(ctx)), private=True)


def founder_set(ctx):
    order, user = _order_opt(ctx), str(ctx.opt("user") or "")
    title = str(ctx.opt("title") or "")
    guild, host = str(ctx.guild), ctx.host

    def work() -> str:
        owner = guild_owner(host, guild)
        try:
            heralds.seat_founder(ctx.store, order, user, ctx.user, owner, via="slash")
        except HeraldError as exc:
            return str(exc)
        _j().audit(ctx.store, ctx.user, "chair_founder", user, order)
        note = ""
        if title:
            try:
                heralds.set_title(ctx.store, user, title, ctx.user, owner, model_screen(ctx))
            except HeraldError as exc:
                note = f" Title not set: {exc}"
        return f"<@{user}>: {heralds.seat_line(ctx.store, user, _set(ctx))}.{note}"
    return ctx.defer(work, private=True)


def founder_title(ctx):
    user, text = str(ctx.opt("user") or ""), str(ctx.opt("text") or "")
    guild, host = str(ctx.guild), ctx.host

    def work() -> str:
        try:
            t = heralds.set_title(ctx.store, user, text, ctx.user, guild_owner(host, guild),
                                  model_screen(ctx))
        except HeraldError as exc:
            return str(exc)
        return f"<@{user}>'s title is now {t}."
    return ctx.defer(work, private=True)


def founder_remove(ctx):
    order = _order_opt(ctx)
    guild, host = str(ctx.guild), ctx.host
    return _run(ctx, lambda: "Founder seat ended: " + str(heralds.remove_founder(
        ctx.store, order, ctx.user, guild_owner(host, guild))["holder"]))


def founder_list(ctx):
    rows = []
    for o, c in heralds.chairs(ctx.store).items():
        if c.get("source") == "founder":
            line = heralds.seat_line(ctx.store, c["holder"], _set(ctx))
            rows.append(f"<@{c['holder']}> · {line} · via {c.get('via')}")
    return ctx.reply("\n".join(rows) or "No founder seats.", private=True)


def progress_line(store, user: str, set_name: str | None = None) -> str:
    """The seat and title line /progress adds (empty when neither)."""
    seat = heralds.seat_line(store, user, set_name)
    if seat:
        return seat
    return heralds.title_of(store, user)


def record_founder_cli(store, order: str, user: str, owner_id: str, title: str,
                       instruction_at: str, operator: str) -> dict:
    """The operator CLI path: record the owner's SPOKEN instruction. ``by`` is the
    owner (it is his decision); ``recorded_by`` names the operator and the public
    label says an operator recorded it, so nobody reads it as the owner's own click.
    The title is not model-screened here: the operator types the owner's words."""
    if not str(operator or "").strip():
        raise HeraldError("Name the operator recording the instruction.")
    if title:
        heralds.clean_title(title)
    return heralds.seat_founder(store, order, user, owner_id, owner_id, title=title,
                                via=f"owner-instruction {instruction_at}",
                                recorded_by=str(operator))

