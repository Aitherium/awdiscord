"""Community: native Discord polls (embed fallback), a server FAQ, rules, redaction."""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass

from awdiscord import redact
from awdiscord.plugins import Command, Option, Plugin, ScopedStore, Tool

log = logging.getLogger("awdiscord.community")

# ── polls ─────────────────────────────────────────────────────────────────────
#
# Two renderers, one command.
#
# * NATIVE (preferred): Discord's own poll object. It draws live percentages, shows
#   who voted, closes itself at the duration and marks the winner. Used whenever it
#   fits: 2-10 answers of at most 55 characters, the bot holds SEND_POLLS in that
#   channel (read from the interaction's ``app_permissions``), and nothing asks for
#   what native cannot do.
# * EMBED (fallback): our own message with buttons, for what native cannot do --
#   anonymous votes, role-weighted votes (store key ``vote_weights``), more than ten
#   answers, long answers -- and for a channel where the bot may not send polls.
#   When it closes, the same message is edited into the final result.

NATIVE_MAX = 10                 # Discord's answer cap for a native poll
NATIVE_ANSWER_MAX = 55          # characters per native answer
NATIVE_QUESTION_MAX = 300
EMBED_MAX = 20                  # 4 button rows of 5
MAX_CHOICES = EMBED_MAX
DEFAULT_HOURS = 24
MAX_HOURS = 768                 # 32 days, Discord's native limit (the embed keeps it)
SEND_POLLS = 1 << 49
BAR_WIDTH = 14
SWEEP_S = 30.0
#: An interaction token edits its reply for 15 minutes; fetch the message id before.
TOKEN_TTL_S = 14 * 60
BLURPLE, GREEN = 0x5865F2, 0x57F287
_EIGHTHS = " ▏▎▍▌▋▊▉"
#: Default button/label emoji for the embed: regional indicators A..T.
_LETTERS = [chr(0x1F1E6 + i) for i in range(EMBED_MAX)]
_CUSTOM_EMOJI = re.compile(r"^<(a?):(\w{2,32}):(\d{15,25})>\s*")
#: A list bullet ("- x", "* x", "• x"), never the sign of a choice like "-5".
_BULLET = re.compile(r"^[-*•]\s+")
_OPTION_NAMES = [f"option{i}" for i in range(1, NATIVE_MAX + 1)]
_sweep_started = threading.Event()


@dataclass
class _IntOption(Option):
    """An integer option with Discord-side bounds (the host's Option has none)."""
    min_value: int | None = None
    max_value: int | None = None

    def to_discord(self) -> dict:
        out = super().to_discord()
        if self.min_value is not None:
            out["min_value"] = self.min_value
        if self.max_value is not None:
            out["max_value"] = self.max_value
        return out


def _is_emoji_token(tok: str) -> bool:
    """True for a token made only of emoji code points (and joiners/selectors)."""
    if not tok:
        return False
    for ch in tok:
        cp = ord(ch)
        if cp in (0x200D, 0xFE0F, 0x20E3) or 0x1F3FB <= cp <= 0x1F3FF:
            continue
        if cp < 0x2100 or ch.isalnum():
            return False
    return True


def split_emoji(text: str) -> tuple[str, dict | None]:
    """``"🍕 pizza"`` -> ``("pizza", {"name": "🍕"})``; custom ``<:x:id>`` too."""
    text = text.strip()
    m = _CUSTOM_EMOJI.match(text)
    if m:
        rest = text[m.end():].strip()
        if rest:
            return rest, {"id": m.group(3), "name": m.group(2),
                          **({"animated": True} if m.group(1) else {})}
    head, _, rest = text.partition(" ")
    if rest.strip() and _is_emoji_token(head):
        return rest.strip(), {"name": head}
    return text, None


def parse_choices(raw: str) -> list[str]:
    """One choice per line, else ``|``-separated, else comma-separated."""
    raw = str(raw or "").strip()
    if not raw:
        return []
    if "\n" in raw:
        parts = raw.splitlines()
    elif "|" in raw:
        parts = raw.split("|")
    else:
        parts = raw.split(",")
    out = [_BULLET.sub("", p.strip()).strip() for p in parts]
    return [p for p in out if p]


def parse_poll_text(text: str) -> tuple[str, list[str]]:
    """``"q | a | b"`` or ``"q: a, b, c"`` -> (question, choices). Choices may be []."""
    text = str(text or "").strip()
    if "|" in text:
        head, _, rest = text.partition("|")
        return head.strip(), parse_choices(rest)
    head, sep, rest = text.partition(":")
    if sep and head.strip() and rest.strip() and not rest.startswith("//"):
        return head.strip(), parse_choices(rest)
    return text, []


def _dedupe(choices: list[str]) -> list[str]:
    seen, out = set(), []
    for c in choices:
        key = c.casefold()
        if key not in seen:
            seen.add(key)
            out.append(c[:80])
    return out


def _gather(opts: dict) -> tuple[str, list[str]]:
    """Question and choices from the slash options, whichever way they were given."""
    question = str(opts.get("question") or "").strip()
    choices = [str(opts.get(n) or "").strip() for n in _OPTION_NAMES]
    choices = [c for c in choices if c]
    choices += parse_choices(str(opts.get("choices") or ""))
    if not choices and question:
        question, choices = parse_poll_text(question)
    return question, _dedupe(choices)


def _hours(value, default: int = DEFAULT_HOURS) -> int:
    try:
        h = int(float(str(value).strip().lower().rstrip("h") or default))
    except (TypeError, ValueError):
        h = default
    return max(1, min(MAX_HOURS, h))


def _app_permissions(interaction: dict) -> int | None:
    raw = interaction.get("app_permissions")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def native_fits(question: str, choices: list[str], *, anonymous: bool,
                weighted: bool, app_permissions: int | None) -> tuple[bool, str]:
    """Can this poll be a native Discord poll? ``(yes, why not)``."""
    if anonymous:
        return False, "anonymous"
    if weighted:
        return False, "weighted"
    if len(choices) > NATIVE_MAX:
        return False, "more than 10 answers"
    if len(question) > NATIVE_QUESTION_MAX:
        return False, "question too long"
    if any(len(split_emoji(c)[0]) > NATIVE_ANSWER_MAX for c in choices):
        return False, "an answer is longer than 55 characters"
    if app_permissions is None or not app_permissions & SEND_POLLS:
        return False, "no SEND_POLLS"
    return True, ""


def native_poll(question: str, choices: list[str], hours: int, multi: bool) -> dict:
    """The message ``poll`` object Discord renders as its own poll."""
    answers = []
    for c in choices[:NATIVE_MAX]:
        text, emoji = split_emoji(c)
        media: dict = {"text": text[:NATIVE_ANSWER_MAX]}
        if emoji:
            media["emoji"] = ({"id": emoji["id"]} if emoji.get("id")
                              else {"name": emoji["name"]})
        answers.append({"poll_media": media})
    return {"question": {"text": question[:NATIVE_QUESTION_MAX]}, "answers": answers,
            "duration": _hours(hours), "allow_multiselect": bool(multi), "layout_type": 1}


# -- the embed poll ------------------------------------------------------------

def _choices_of(poll: dict) -> list[dict]:
    """Stored choices as ``{"text", "emoji"}`` (older polls stored plain strings)."""
    out = []
    for i, c in enumerate(poll.get("choices") or []):
        if isinstance(c, dict):
            out.append({"text": str(c.get("text") or ""), "emoji": c.get("emoji")})
        else:
            text, emoji = split_emoji(str(c))
            out.append({"text": text, "emoji": emoji})
        if not out[-1]["emoji"]:
            out[-1]["emoji"] = {"name": _LETTERS[i % len(_LETTERS)]}
    return out


def _picks(value) -> list[int]:
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [int(v) for v in value if isinstance(v, int) or str(v).isdigit()]
    return []


def tally(poll: dict) -> tuple[list[float], int]:
    """(weighted count per choice, number of voters)."""
    counts = [0.0] * len(poll.get("choices") or [])
    weights = poll.get("weights") or {}
    voters = 0
    for user, value in (poll.get("votes") or {}).items():
        picks = [i for i in _picks(value) if 0 <= i < len(counts)]
        if not picks:
            continue
        voters += 1
        w = float(weights.get(user, 1) or 1)
        for i in picks:
            counts[i] += w
    return counts, voters


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """A fixed-width progress bar in Unicode blocks, eighth-cell precise."""
    fraction = max(0.0, min(1.0, fraction))
    eighths = round(fraction * width * 8)
    full, part = divmod(eighths, 8)
    out = "█" * full + (_EIGHTHS[part] if part else "")
    return out + "░" * (width - len(out))


def _num(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:.1f}"


def _emoji_text(emoji: dict | None) -> str:
    if not emoji:
        return ""
    if emoji.get("id"):
        return f"<{'a' if emoji.get('animated') else ''}:{emoji.get('name') or 'e'}:{emoji['id']}>"
    return str(emoji.get("name") or "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _poll_view(poll: dict, now: float | None = None) -> tuple[str, list, list]:
    """The embed and buttons for an open poll, or the final result once closed."""
    now = time.time() if now is None else now
    choices = _choices_of(poll)
    counts, voters = tally(poll)
    total = sum(counts)
    top = max(counts) if counts and total else 0
    leaders = {i for i, n in enumerate(counts) if top and n == top}
    closed = bool(poll.get("closed"))
    lines = []
    if closed:
        if not total:
            lines.append("**No votes were cast.**")
        elif len(leaders) == 1:
            w = next(iter(leaders))
            lines.append(f"🏆 **Winner: {_emoji_text(choices[w]['emoji'])} "
                         f"{choices[w]['text']}** with {round(100 * counts[w] / total)}%")
        else:
            names = ", ".join(choices[i]["text"] for i in sorted(leaders))
            lines.append(f"🤝 **Tie: {names}**")
        lines.append("")
    count_w = max(len(_num(n)) for n in counts) if counts else 1
    for i, c in enumerate(choices):
        n = counts[i]
        pct = round(100 * n / total) if total else 0
        lead = i in leaders
        mark = (" 🏆" if closed else "  ◀ leading") if lead and len(leaders) == 1 else ""
        label = f"**{c['text']}**" if lead else c["text"]
        lines.append(f"{_emoji_text(c['emoji'])} {label}{mark}")
        lines.append(f"`{bar(n / total if total else 0)} {pct:>3}% {_num(n):>{count_w}}`")
    closes = int(poll.get("closes_at") or 0)
    if closes and not closed:
        lines.append("")
        lines.append(f"⏳ Closes <t:{closes}:R>")
    notes = [_plural(voters, "vote")]
    if poll.get("anonymous"):
        notes.append("anonymous")
    if poll.get("weighted"):
        notes.append("weighted by role")
    if closed:
        notes.append("final result")
    else:
        notes.append("choose several" if poll.get("multi") else "one per person")
        notes.append("tap again to change your vote")
    embed = {"title": f"📊 {poll['question']}"[:256], "description": "\n".join(lines)[:4096],
             "color": GREEN if closed else BLURPLE, "footer": {"text": " · ".join(notes)}}
    if closed:
        return "", [embed], []
    rows: list = []
    for i, c in enumerate(choices):
        if i % 5 == 0:
            rows.append({"type": 1, "components": []})
        button = {"type": 2, "style": 3 if i in leaders else 2,
                  "label": f"{c['text'][:72]} · {_num(counts[i])}",
                  "custom_id": PLUGIN.button_id(f"v:{poll['id']}:{i}")}
        emoji = c["emoji"]
        if emoji:
            button["emoji"] = ({"id": emoji["id"], "name": emoji.get("name") or "e"}
                               if emoji.get("id") else {"name": emoji["name"]})
        rows[-1]["components"].append(button)
    return "", [embed], rows


def _vote_weight(ctx, member_roles: list) -> float:
    """The member's vote weight: the highest of their roles in ``vote_weights``."""
    weights = ctx.store.get("vote_weights") or {}
    if not isinstance(weights, dict) or not weights:
        return 1.0
    best = None
    for role in member_roles or []:
        if str(role) in weights:
            try:
                w = float(weights[str(role)])
            except (TypeError, ValueError):
                continue
            best = w if best is None else max(best, w)
    return float(weights.get("default", 1) if best is None else best)


def _start(ctx, question: str, choices: list[str], hours: int, multi: bool,
           anonymous: bool):
    question = question.strip()[:256] or "Poll"
    choices = _dedupe(choices)[:EMBED_MAX]
    if len(choices) < 2:
        return _ask_form(ctx, question, choices, hours, multi, anonymous)
    weighted = bool(ctx.store.get("vote_weights"))
    fits, why = native_fits(question, choices, anonymous=anonymous, weighted=weighted,
                            app_permissions=_app_permissions(ctx.interaction))
    if fits:
        from awdiscord.dispatch import MESSAGE, NO_MENTIONS, Result
        return Result({"type": MESSAGE, "data": {
            "poll": native_poll(question, choices, hours, multi),
            "allowed_mentions": NO_MENTIONS}},
            log={"plugin": PLUGIN.name, "user": ctx.user, "poll": "native",
                 "answers": len(choices)})
    now = int(time.time())
    poll = {"id": secrets.token_hex(4), "question": question,
            "choices": [dict(zip(("text", "emoji"), split_emoji(c))) for c in choices],
            "votes": {}, "weights": {}, "weighted": weighted,
            "multi": bool(multi), "anonymous": bool(anonymous), "by": ctx.user,
            "at": now, "closes_at": now + 3600 * _hours(hours), "closed": False,
            "channel": str(ctx.channel or ""),
            "app_id": str(ctx.interaction.get("application_id") or ""),
            "token": str(ctx.interaction.get("token") or ""), "message": ""}
    ctx.store.set(f"poll:{poll['id']}", poll)
    text, embeds, components = _poll_view(poll)
    result = ctx.reply(text, embeds=embeds, components=components)
    result.log.update({"poll": "embed", "why": why, "answers": len(choices)})
    return result


def _ask_form(ctx, question: str, choices: list[str], hours: int, multi: bool,
              anonymous: bool):
    """No usable choices: open a form instead of answering with a usage error."""
    flags = f"{int(bool(multi))}{int(bool(anonymous))}"
    return ctx.modal(f"new:{flags}", "Start a poll", [
        {"id": "question", "label": "Question", "max_length": 256,
         "placeholder": "Where should we eat?", "value": question},
        {"id": "choices", "label": "Choices, one per line (2 to 20)", "paragraph": True,
         "max_length": 1600, "placeholder": "🍕 Pizza\n🌮 Tacos\n🍣 Sushi",
         "value": "\n".join(choices)},
        {"id": "hours", "label": "Open for how many hours? (1 to 768)", "required": False,
         "max_length": 4, "placeholder": str(DEFAULT_HOURS), "value": str(hours)},
    ])


def _opt_bool(ctx, name: str) -> bool:
    v = ctx.opt(name, False)
    return v is True or str(v).lower() in ("true", "1", "yes")


def _poll(ctx):
    question, choices = _gather(ctx.options)
    hours = _hours(ctx.opt("hours", DEFAULT_HOURS))
    return _start(ctx, question, choices, hours, _opt_bool(ctx, "multi"),
                  _opt_bool(ctx, "anonymous"))


def _on_modal(ctx, payload: str, values: dict):
    kind, _, flags = payload.partition(":")
    if kind != "new":
        return ctx.reply("Unknown form.", private=True)
    multi, anonymous = (flags + "00")[0] == "1", (flags + "00")[1] == "1"
    question = str(values.get("question") or "").strip()
    choices = parse_choices(str(values.get("choices") or ""))
    if not choices:
        question, choices = parse_poll_text(question)
    if len(_dedupe(choices)) < 2:
        return ctx.reply("A poll needs at least two different choices - put each one on "
                         "its own line in the form.", private=True)
    return _start(ctx, question, choices, _hours(values.get("hours") or DEFAULT_HOURS),
                  multi, anonymous)


def _close(poll: dict) -> dict:
    poll = dict(poll)
    poll["closed"] = True
    poll["token"] = ""
    return poll


def _on_button(ctx, payload: str):
    kind, _, rest = payload.partition(":")
    if kind in ("rd", "rx"):
        return _redact_button(ctx, kind, rest)
    if kind != "v" or not ctx.guild:
        return ctx.reply("Unknown button.", private=True)
    poll_id, _, idx_s = rest.partition(":")
    key = f"poll:{poll_id}"
    try:
        idx = int(idx_s)
    except ValueError:
        return ctx.reply("Unknown button.", private=True)
    message = ctx.interaction.get("message") or {}
    member = ctx.interaction.get("member") or {}
    weight = _vote_weight(ctx, member.get("roles") or [])
    now = time.time()

    def vote(poll):
        if poll is None:
            return None, None
        poll = dict(poll)
        if message.get("id"):
            poll["message"] = str(message["id"])
            poll["channel"] = str(ctx.channel or poll.get("channel") or "")
            poll["token"] = ""
        if poll.get("closed") or now >= float(poll.get("closes_at") or 1e18):
            poll = _close(poll)
            return poll, poll
        if not 0 <= idx < len(poll.get("choices") or []):
            return poll, poll
        votes = dict(poll.get("votes") or {})
        mine = _picks(votes.get(ctx.user))
        if idx in mine:
            mine = [i for i in mine if i != idx]           # tap again: take it back
        elif poll.get("multi"):
            mine = sorted(mine + [idx])
        else:
            mine = [idx]
        if mine:
            votes[ctx.user] = mine
        else:
            votes.pop(ctx.user, None)
        poll["votes"] = votes
        if poll.get("weighted"):
            weights = dict(poll.get("weights") or {})
            weights[ctx.user] = weight
            poll["weights"] = weights
        return poll, poll

    poll = ctx.store.update(key, vote)
    if poll is None:
        return ctx.reply("This poll has closed.", private=True)
    text, embeds, components = _poll_view(poll, now)
    return ctx.update(text, embeds=embeds, components=components)


# -- closing: edit the message into the final result, post nothing else --------

def _edit(host, poll: dict, body: dict) -> bool:
    rest = getattr(host, "rest", None)
    if rest is None:
        return False
    if poll.get("message") and poll.get("channel"):
        rest.request("PATCH", f"/channels/{poll['channel']}/messages/{poll['message']}", body)
        return True
    if poll.get("token") and poll.get("app_id") and \
            time.time() - float(poll.get("at") or 0) < TOKEN_TTL_S:
        rest.edit_original(poll["app_id"], poll["token"], body)
        return True
    return False


def sweep(host, guild: str, now: float | None = None) -> list[str]:
    """Close every embed poll in ``guild`` whose time is up. Returns the closed ids."""
    now = time.time() if now is None else now
    store = ScopedStore(host.store, guild, PLUGIN.name)
    rest = getattr(host, "rest", None)
    closed = []
    for key in store.keys("poll:"):
        poll = store.get(key)
        if not isinstance(poll, dict) or poll.get("closed"):
            continue
        # Learn the message id while the interaction token still works.
        if (not poll.get("message") and poll.get("token") and poll.get("app_id")
                and rest is not None and now - float(poll.get("at") or 0) < TOKEN_TTL_S):
            try:
                msg = rest.request(
                    "GET", f"/webhooks/{poll['app_id']}/{poll['token']}/messages/@original",
                    auth=False)
                if isinstance(msg, dict) and msg.get("id"):
                    poll = {**poll, "message": str(msg["id"]),
                            "channel": str(msg.get("channel_id") or poll.get("channel")),
                            "token": ""}
                    store.set(key, poll)
            except Exception as exc:                      # noqa: BLE001
                log.info("poll %s: original not fetched: %s", poll.get("id"), exc)
        if now < float(poll.get("closes_at") or 1e18):
            continue
        final = _close(poll)
        text, embeds, components = _poll_view(final, now)
        try:
            _edit(host, poll, {"content": text, "embeds": embeds, "components": components,
                               "allowed_mentions": {"parse": []}})
        except Exception as exc:                          # noqa: BLE001
            log.warning("poll %s: final edit failed: %s", poll.get("id"), exc)
        store.set(key, final)
        closed.append(str(poll.get("id")))
    return closed


def _on_ready(host) -> None:
    if _sweep_started.is_set():
        return
    _sweep_started.set()

    def loop() -> None:
        while True:
            try:
                for guild in host.store.guilds():
                    sweep(host, guild)
            except Exception as exc:                      # noqa: BLE001
                log.warning("poll sweep failed: %s", exc)
            time.sleep(SWEEP_S)

    threading.Thread(target=loop, name="poll-sweep", daemon=True).start()


# ── FAQ ───────────────────────────────────────────────────────────────────────

def _faq_set(ctx):
    topic = str(ctx.opt("topic")).strip().lower()
    ctx.store.set(f"faq:{topic}", {"answer": str(ctx.opt("answer")), "by": ctx.user})
    return ctx.reply(f"Saved FAQ **{topic}**.", private=True)


def _faq_remove(ctx):
    topic = str(ctx.opt("topic")).strip().lower()
    ctx.store.delete(f"faq:{topic}")
    return ctx.reply(f"Removed FAQ **{topic}**.", private=True)


def _faq_list(ctx):
    topics = [k.split(":", 1)[1] for k in ctx.store.keys("faq:")]
    return ctx.reply("FAQ topics: " + (", ".join(topics) if topics else "none yet"),
                     private=True)


def faq_entries(store) -> dict[str, str]:
    return {k.split(":", 1)[1]: (store.get(k) or {}).get("answer", "")
            for k in store.keys("faq:")}


def _faq_ask(ctx):
    question = str(ctx.opt("question")).strip()
    entries = faq_entries(ctx.store)
    exact = entries.get(question.lower())
    if exact:
        return ctx.reply(exact)
    if not entries:
        return ctx.reply("This server has no FAQ yet. A manager can add one with "
                         "`/faq set`.", private=True)
    store_view = "\n".join(f"- {t}: {a}" for t, a in sorted(entries.items()))
    agent = ctx.agent

    def work() -> str:
        if agent is None:
            return "No agent is configured to answer from the FAQ."
        prompt = ("Answer the member's question using ONLY this server's FAQ. If the FAQ "
                  "does not cover it, say so in one line and suggest asking a moderator.\n\n"
                  f"FAQ:\n{store_view}\n\nQuestion: {question}")
        return agent.ask(prompt, user=ctx.user)
    return ctx.defer(work)


# ── rules ─────────────────────────────────────────────────────────────────────

def _rules_show(ctx):
    rules = ctx.store.get("rules")
    if not rules:
        return ctx.reply("No rules set. A manager can set them with `/rules set`.",
                         private=True)
    return ctx.reply("", embeds=[{"title": "Server rules", "description": rules[:4000]}])


def _rules_set(ctx):
    text = str(ctx.opt("text")).replace("\\n", "\n")
    ctx.store.set("rules", text)
    return ctx.reply("Rules saved.", private=True)


# ── redaction (staff only; the engine is awdiscord.redact) ────────────────────
#
# The pending confirmation lives in this plugin's store under the server, keyed
# ``redact:<token>``, and holds message ids only -- never the phrase. Its buttons
# may be pressed in the asker's DM (an @mention turn cannot answer privately), so
# the payload names the server: ``rd:<guild>:<token>`` deletes, ``rx:`` cancels.

DEFERRED_UPDATE = 6             # interaction response: ack a press, edit its message later
_HOST: dict = {}


def _redact_ready(host) -> None:
    """Remember the host, so the agent tool reaches the bot's REST client."""
    _HOST["host"] = host


def _rest(host=None):
    host = host or _HOST.get("host")
    return getattr(host, "rest", None) if host is not None else None


def _confirm_rows(guild: str, token: str) -> list:
    return [{"type": 1, "components": [
        {"type": 2, "style": 4, "label": "Delete these",
         "custom_id": PLUGIN.button_id(f"rd:{guild}:{token}")},
        {"type": 2, "style": 2, "label": "Cancel",
         "custom_id": PLUGIN.button_id(f"rx:{guild}:{token}")}]}]


def _stage(host, guild: str, pending: dict) -> str:
    token = redact.new_token()
    ScopedStore(host.store, guild, PLUGIN.name).set(redact.PENDING_PREFIX + token, pending)
    return token


_STAFF_ONLY = ("Only server staff (Manage Messages or Manage Server) can redact messages "
               "in this channel.")


def _redact_slash(ctx):
    """/redact phrase days: the same engine as the tool, answered privately."""
    phrase = str(ctx.opt("phrase", "") or "")
    days = ctx.opt("days", redact.DEFAULT_DAYS)
    host, user = ctx.host, ctx.user
    guild, channel = str(ctx.guild or ""), str(ctx.channel or "")
    operator = user in host.operators
    perms = ctx.permissions        # Discord's resolved permissions in this channel
    if not (operator or redact.is_staff(perms)):
        return ctx.reply(_STAFF_ONLY, private=True)

    def work():
        out = redact.run(_rest(host), guild=guild, channel=channel, user=user,
                         phrase=phrase, days=days, operator=operator,
                         caller_permissions=perms)
        if out.pending is None:
            return out.text
        token = _stage(host, guild, out.pending)
        return {"content": out.text[:2000], "components": _confirm_rows(guild, token)}

    return ctx.defer(work, private=True)


def _redact_button(ctx, kind: str, rest: str):
    """Delete these / Cancel on a redaction preview (in the server or in a DM)."""
    guild, _, token = rest.partition(":")
    if not guild or not token or (ctx.guild and str(ctx.guild) != guild):
        return ctx.reply("Unknown button.", private=True)
    store = ScopedStore(ctx.host.store, guild, PLUGIN.name)
    key = redact.PENDING_PREFIX + token
    pending = store.get(key)
    if pending and str(pending.get("by")) != ctx.user:
        return ctx.reply("Only the person who asked for this redaction can confirm it.",
                         private=True)
    if kind == "rx":
        store.delete(key)
        return ctx.update("Cancelled; nothing was deleted.", components=[])
    host, presser = ctx.host, ctx.user
    operator = presser in host.operators
    # In the server a press carries Discord's resolved permissions; a DM carries
    # none, so the engine reads them over REST.
    perms = ctx.permissions if ctx.guild else None
    store.delete(key)               # one press, one deletion run

    def work():
        return {"content": redact.confirm(_rest(host), pending, presser=presser,
                                          operator=operator, presser_permissions=perms),
                "components": []}

    from awdiscord.dispatch import Result
    return Result({"type": DEFERRED_UPDATE}, deferred=work,
                  log={"plugin": PLUGIN.name, "user": presser, "redact": "confirm"})


def _dm(rest, user: str, text: str, guild: str, token: str | None) -> bool:
    """Send the asker the private list (and the confirm buttons). False on refusal."""
    try:
        dm = rest.dm_channel(user) or {}
        body: dict = {"content": text[:2000]}
        if token:
            body["components"] = _confirm_rows(guild, token)
        rest.create_message(str(dm.get("id") or ""), body)
        return True
    except Exception as exc:  # noqa: BLE001 - a closed inbox falls back to /redact
        log.info("redact: DM to the asker failed (%s)", type(exc).__name__)
        return False


def redact_phrase(phrase: str = "", days: int = redact.DEFAULT_DAYS, caller=None) -> str:
    """The agent's tool: same engine as /redact; the asker is the host's, never the model's.

    Returns counts only -- the phrase and the list of other members' messages go to
    the asker in a DM, never into a public answer.
    """
    caller = caller or {}
    guild, channel = str(caller.get("guild") or ""), str(caller.get("channel") or "")
    user = str(caller.get("user") or "")
    host = _HOST.get("host")
    if not user or host is None:
        return "I can't tell who is asking here, so I changed nothing."
    rest = _rest(host)
    out = redact.run(rest, guild=guild, channel=channel, user=user, phrase=phrase,
                     days=days, operator=bool(caller.get("operator")))
    if out.refused:
        return out.text
    tail = " Do not repeat the phrase in your reply."
    if out.no_permission:
        sent = _dm(rest, user, out.text, guild, None)
        return (out.summary + " I lack Manage Messages in this channel, so I can't delete "
                "the other members' messages" + ("; I DMed the asker the list." if sent
                                                 else ".") + tail)
    if out.pending is None:
        return out.summary + tail
    token = _stage(host, guild, out.pending)
    if _dm(rest, user, out.text, guild, token):
        return (out.summary + " I DMed the asker the list with a Delete button; nothing "
                "of theirs is deleted until they press it." + tail)
    return (out.summary + " I could not DM the asker; they can run /redact with the same "
            "phrase to see the list privately and confirm." + tail)


def _in_a_server_channel(caller: dict) -> bool:
    return bool(caller.get("guild")) and bool(caller.get("channel"))


REDACT_TOOL = Tool(
    "redact_phrase",
    "Remove a phrase (a person's name, an address, any text) from THIS channel's recent "
    "messages when server staff ask to purge, redact, scrub or remove it. You CAN do "
    "this: your own messages containing it are edited to say [redacted]; other members' "
    "messages are listed to the asker privately and deleted only after they press a "
    "confirm button, and only if you hold Manage Messages here (nobody can edit another "
    "person's message). Staff only (Manage Messages or Manage Server): the tool checks "
    "and refuses anyone else. Scans back `days` (default 30, max 90, at most 1000 "
    "messages). Never repeat the phrase in your reply.",
    redact_phrase,
    {"type": "object", "properties": {
        "phrase": {"type": "string", "description": "the exact text to remove"},
        "days": {"type": "integer", "minimum": 1, "maximum": redact.MAX_DAYS}},
     "required": ["phrase"]},
    wants_caller=True, offered=_in_a_server_channel, secret_args=("phrase",))

def start_poll(question: str = "", choices=None, hours: int = DEFAULT_HOURS,
               multi: bool = False, caller=None) -> str:
    """The agent's tool: post a native Discord poll where the member asked for one.

    Same builder as /poll (``native_poll``); the channel is the host's, never the
    model's. Before this existed a member who asked Aither for a poll in plain words
    got told to type /poll, or an agent went around the bot with its raw token.
    """
    caller = caller or {}
    channel = str(caller.get("channel") or "")
    rest = _rest()
    if not channel or rest is None:
        return "I can only start a poll from a server channel, and none is known here."
    if isinstance(choices, str):
        choices = parse_choices(choices)
    question = str(question or "").strip()
    picks = _dedupe([str(c).strip() for c in (choices or []) if str(c).strip()])
    if not question or len(picks) < 2:
        return "A poll needs a question and at least 2 choices; ask the member for them."
    fits, why = native_fits(question, picks, anonymous=False, weighted=False,
                            app_permissions=SEND_POLLS)
    if not fits:
        return (f"That poll does not fit Discord's own poll ({why}): at most "
                f"{NATIVE_MAX} choices of {NATIVE_ANSWER_MAX} characters and a question "
                f"of {NATIVE_QUESTION_MAX}. Shorten it and try again, or the member can "
                "use /poll, which also handles up to 20 choices and anonymous votes.")
    from awdiscord.dispatch import NO_MENTIONS
    try:
        msg = rest.request("POST", f"/channels/{channel}/messages", {
            "poll": native_poll(question, picks, _hours(hours), bool(multi)),
            "allowed_mentions": NO_MENTIONS}) or {}
    except Exception as exc:  # noqa: BLE001 - Discord's refusal is the answer
        log.info("start_poll: Discord refused (%s: %s)", type(exc).__name__, exc)
        return ("Discord refused the poll here (most often the bot lacks Send Polls in "
                f"this channel): {type(exc).__name__}. The member can try /poll.")
    log.info("start_poll: native poll %s in %s by %s (%d answers)", msg.get("id"),
             channel, caller.get("user"), len(picks))
    return (f"Poll posted in this channel with {len(picks)} choices, open "
            f"{_hours(hours)} h. Do not repeat the choices; just say it is up.")


def server_faq(topic: str = "", caller=None) -> str:
    """The agent's tool: this server's own FAQ and rules, read-only.

    The same entries /faq ask and /rules show answer from. Before this the persona
    told the agent to point members at /faq, so "what are the rules?" asked in plain
    words got a slash command back instead of the rules.
    """
    caller = caller or {}
    guild, host = str(caller.get("guild") or ""), _HOST.get("host")
    if not guild or host is None or getattr(host, "store", None) is None:
        return "No server is known here, so there is no FAQ or rules to read."
    store = ScopedStore(host.store, guild, PLUGIN.name)
    entries = faq_entries(store)
    rules = str(store.get("rules") or "").strip()
    topic = str(topic or "").strip().lower()
    if topic and topic in entries:
        entries = {topic: entries[topic]}
    parts = []
    if entries:
        parts.append("FAQ:\n" + "\n".join(f"- {t}: {a}" for t, a in sorted(entries.items())))
    if rules:
        parts.append("Rules:\n" + rules)
    if not parts:
        return ("This server has no FAQ or rules set yet. Say so, and that a manager "
                "can add them with /faq set and /rules set.")
    return ("Answer ONLY from this; if it does not cover the question, say so and "
            "suggest a moderator.\n\n" + "\n\n".join(parts))[:6000]


FAQ_TOOL = Tool(
    "server_faq",
    "Read THIS server's own FAQ and rules. Call it whenever a member asks about the "
    "server's rules, policies, how things work here, or anything an FAQ would cover, "
    "and answer from what it returns instead of your own knowledge or pointing them at "
    "/faq or /rules.",
    server_faq,
    {"type": "object", "properties": {
        "topic": {"type": "string", "description": "an FAQ topic name, if the member "
                  "named one; leave empty to get everything"}}},
    wants_caller=True, offered=_in_a_server_channel)


POLL_TOOL = Tool(
    "start_poll",
    "Post a poll in THIS channel when a member asks for a poll, vote or survey (\"make "
    "a poll about X\", \"let's vote on A, B or C\"). You CAN do this: it posts Discord's "
    "own poll, which members vote on with a click. 2-10 choices of at most 55 "
    "characters each; question at most 300 characters. Write the choices from the "
    "member's words; if they gave none, propose sensible ones only when the topic makes "
    "them obvious, otherwise ask. Never tell a member you cannot make polls.",
    start_poll,
    {"type": "object", "properties": {
        "question": {"type": "string", "description": "the poll question"},
        "choices": {"type": "array", "items": {"type": "string"}, "minItems": 2,
                    "maxItems": NATIVE_MAX, "description": "the answers to pick from"},
        "hours": {"type": "integer", "minimum": 1, "maximum": MAX_HOURS,
                  "description": f"how long it stays open (default {DEFAULT_HOURS})"},
        "multi": {"type": "boolean", "description": "let people pick more than one"}},
     "required": ["question", "choices"]},
    wants_caller=True, offered=_in_a_server_channel)

_topic = Option("topic", "FAQ topic", required=True, max_length=80)

PLUGIN = Plugin(
    name="community",
    version="0.2.0",
    description="Native polls, a server FAQ the agent answers from, and rules",
    default_enabled=True,
    persona=("You are also this server's community helper: for rules, policy or FAQ "
             "questions call server_faq and answer from it, not from your own knowledge. "
             "When a member asks for a poll or a vote, call start_poll: you post "
             "Discord's own poll here yourself; never send them to /poll instead. "
             "You CAN edit your own messages and, with Manage Messages, delete other "
             "members' messages: when staff ask you to purge or redact a name or phrase "
             "here, call redact_phrase (or point them at /redact). Never claim you cannot "
             "edit or delete messages, and never repeat the phrase being redacted."),
    on_button=_on_button,
    on_modal=_on_modal,
    on_ready=[_on_ready, _redact_ready],
    dm_buttons=True,          # only the redaction confirm is answered in a DM
    tools=[REDACT_TOOL, POLL_TOOL, FAQ_TOOL],
    commands=[
        Command("poll", "Start a poll (Discord's own poll when it fits)", _poll, options=[
            Option("question", "The question, or the whole poll: 'Lunch? | pizza | tacos'",
                   max_length=300),
            *[Option(n, f"Choice {i} (start it with an emoji to show one)", max_length=80)
              for i, n in enumerate(_OPTION_NAMES, 1)],
            Option("choices", "More choices, separated by | or commas (up to 20 in all)",
                   max_length=1600),
            _IntOption("hours", "How long it stays open, in hours (default 24)",
                       type="integer", min_value=1, max_value=MAX_HOURS),
            Option("multi", "Let people pick more than one", type="boolean"),
            Option("anonymous", "Hide who voted (uses the bot's own poll card)",
                   type="boolean")]),
        Command("faq", "This server's FAQ", subcommands=[
            Command("ask", "Ask the FAQ", _faq_ask,
                    options=[Option("question", "Your question", required=True,
                                    max_length=300)]),
            Command("list", "List FAQ topics", _faq_list),
            Command("set", "Add or change an answer", _faq_set, admin=True,
                    options=[_topic, Option("answer", "The answer", required=True,
                                            max_length=1500)]),
            Command("remove", "Remove an answer", _faq_remove, admin=True,
                    options=[_topic]),
        ]),
        Command("redact", "Staff: remove a phrase from this channel's recent messages",
                _redact_slash, options=[
                    Option("phrase", "The exact text to remove (never shown publicly)",
                           required=True, max_length=200),
                    _IntOption("days", "How far back, in days (default 30, max 90)",
                               type="integer", min_value=1, max_value=redact.MAX_DAYS)]),
        Command("rules", "Server rules", subcommands=[
            Command("show", "Show the rules", _rules_show),
            Command("set", "Set the rules (use \\n for new lines)", _rules_set, admin=True,
                    options=[Option("text", "The rules", required=True, max_length=4000)]),
        ]),
    ],
)
