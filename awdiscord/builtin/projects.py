"""Projects: a per-server task board, and a standup the agent writes from it.

Off by default. A pack for a specific project (for example one that syncs to an
external tracker) can replace the storage by shipping its own plugin; this one
keeps everything in the server's own store.
"""
from __future__ import annotations

import time

from awdiscord.plugins import Command, Option, Plugin, PluginError

STATES = ("todo", "doing", "done")


def _next_id(store) -> int:
    n = int(store.get("seq", 0)) + 1
    store.set("seq", n)
    return n


def _load(store, task_id) -> dict:
    task = store.get(f"task:{int(task_id):06d}")
    if task is None:
        raise PluginError(f"No task #{task_id}.")
    return task


def _save(store, task: dict) -> None:
    store.set(f"task:{task['id']:06d}", task)


def _line(t: dict) -> str:
    who = f" <@{t['assignee']}>" if t.get("assignee") else ""
    proj = f" [{t['project']}]" if t.get("project") else ""
    return f"`#{t['id']}` {t['state']:<5} {t['title']}{proj}{who}"


def tasks(store, state: str | None = None) -> list[dict]:
    out = [store.get(k) for k in store.keys("task:")]
    return [t for t in out if t and (state is None or t["state"] == state)]


def _add(ctx):
    t = {"id": _next_id(ctx.store), "title": str(ctx.opt("title")).strip(),
         "state": "todo", "assignee": ctx.opt("assignee"),
         "project": (ctx.opt("project") or "").strip() or None,
         "by": ctx.user, "at": int(time.time())}
    _save(ctx.store, t)
    return ctx.reply(f"Added {_line(t)}")


def _list(ctx):
    mine = bool(ctx.opt("mine", False))
    project = (ctx.opt("project") or "").strip() or None
    rows = [t for t in tasks(ctx.store) if t["state"] != "done"
            and (not mine or t.get("assignee") == ctx.user)
            and (project is None or t.get("project") == project)]
    if not rows:
        return ctx.reply("No open tasks.", private=True)
    return ctx.reply("\n".join(_line(t) for t in rows[:40]), private=True)


def _move(state: str):
    def handler(ctx):
        t = _load(ctx.store, ctx.opt("id"))
        t["state"] = state
        _save(ctx.store, t)
        return ctx.reply(f"{_line(t)}")
    return handler


def _assign(ctx):
    t = _load(ctx.store, ctx.opt("id"))
    t["assignee"] = ctx.opt("user")
    _save(ctx.store, t)
    return ctx.reply(f"{_line(t)}")


def _drop(ctx):
    t = _load(ctx.store, ctx.opt("id"))
    if t.get("by") != ctx.user and not ctx.is_admin:
        return ctx.reply("Only whoever added it, or a manager, can drop it.", private=True)
    ctx.store.delete(f"task:{t['id']:06d}")
    return ctx.reply(f"Dropped #{t['id']}.", private=True)


def _standup(ctx):
    board = {s: [_line(t) for t in tasks(ctx.store, s)] for s in STATES}
    text = "\n".join(f"{s.upper()}:\n" + ("\n".join(rows[:30]) or "(none)")
                     for s, rows in board.items())
    agent = ctx.agent

    def work() -> str:
        if agent is None:
            return text
        return agent.ask("Write a short team standup from this task board: what moved, "
                         "what is in progress and who owns it, and what looks stuck. "
                         "Use the task ids. No preamble.\n\n" + text, user=ctx.user)
    return ctx.defer(work)


_id = Option("id", "Task number", type="integer", required=True)
_project = Option("project", "Project name", max_length=60)

PLUGIN = Plugin(
    name="projects",
    version="0.1.0",
    description="A task board per server, and a standup written from it",
    persona="You can see this server's /task board; refer to tasks by #id.",
    commands=[
        Command("task", "The task board", subcommands=[
            Command("add", "Add a task", _add, options=[
                Option("title", "What needs doing", required=True, max_length=200),
                Option("assignee", "Who owns it", type="user"), _project]),
            Command("list", "Open tasks", _list, options=[
                Option("mine", "Only mine", type="boolean"), _project]),
            Command("start", "Mark a task in progress", _move("doing"), options=[_id]),
            Command("done", "Mark a task done", _move("done"), options=[_id]),
            Command("assign", "Give a task to someone", _assign, options=[
                _id, Option("user", "New owner", type="user", required=True)]),
            Command("drop", "Delete a task", _drop, options=[_id]),
        ]),
        Command("standup", "A standup written from the task board", _standup),
    ],
)
