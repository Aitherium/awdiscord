"""Community: polls with buttons, a server FAQ the agent can answer from, rules."""
from __future__ import annotations

import secrets
import time

from awdiscord.plugins import Command, Option, Plugin

MAX_CHOICES = 5


# ── polls ─────────────────────────────────────────────────────────────────────

def _poll_view(poll: dict) -> tuple[str, list, list]:
    votes: dict = poll.get("votes", {})
    counts = [0] * len(poll["choices"])
    for idx in votes.values():
        if 0 <= idx < len(counts):
            counts[idx] += 1
    total = sum(counts)
    lines = []
    for i, (choice, n) in enumerate(zip(poll["choices"], counts)):
        bar = "#" * (round(10 * n / total) if total else 0)
        lines.append(f"**{i + 1}. {choice}** - {n} `{bar:<10}`")
    embed = {"title": poll["question"][:256], "description": "\n".join(lines),
             "footer": {"text": f"{total} vote(s) - one per person, press again to change"}}
    buttons = [{"type": 2, "style": 1, "label": c[:80],
                "custom_id": PLUGIN.button_id(f"v:{poll['id']}:{i}")}
               for i, c in enumerate(poll["choices"])]
    return "", [embed], [{"type": 1, "components": buttons}]


def _poll(ctx):
    raw = str(ctx.opt("choices", ""))
    choices = [c.strip() for c in raw.split("|") if c.strip()][:MAX_CHOICES]
    if len(choices) < 2:
        return ctx.reply("Give at least two choices separated by `|`.", private=True)
    poll = {"id": secrets.token_hex(4), "question": str(ctx.opt("question")),
            "choices": choices, "votes": {}, "by": ctx.user, "at": int(time.time())}
    ctx.store.set(f"poll:{poll['id']}", poll)
    text, embeds, components = _poll_view(poll)
    return ctx.reply(text, embeds=embeds, components=components)


def _on_button(ctx, payload: str):
    kind, _, rest = payload.partition(":")
    if kind != "v":
        return ctx.reply("Unknown button.", private=True)
    poll_id, _, idx = rest.partition(":")
    poll = ctx.store.get(f"poll:{poll_id}")
    if poll is None:
        return ctx.reply("This poll has closed.", private=True)
    poll["votes"][ctx.user] = int(idx)
    ctx.store.set(f"poll:{poll_id}", poll)
    text, embeds, components = _poll_view(poll)
    return ctx.update(text, embeds=embeds, components=components)


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


_topic = Option("topic", "FAQ topic", required=True, max_length=80)

PLUGIN = Plugin(
    name="community",
    version="0.1.0",
    description="Polls, a server FAQ the agent answers from, and rules",
    default_enabled=True,
    persona=("You are also this server's community helper: point members at /faq and "
             "/rules before answering policy questions from your own knowledge."),
    on_button=_on_button,
    commands=[
        Command("poll", "Start a poll with buttons", _poll, options=[
            Option("question", "The question", required=True, max_length=200),
            Option("choices", "Choices separated by |  (2 to 5)", required=True,
                   max_length=400)]),
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
        Command("rules", "Server rules", subcommands=[
            Command("show", "Show the rules", _rules_show),
            Command("set", "Set the rules (use \\n for new lines)", _rules_set, admin=True,
                    options=[Option("text", "The rules", required=True, max_length=4000)]),
        ]),
    ],
)
