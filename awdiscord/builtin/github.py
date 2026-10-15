"""GitHub Projects: a server's board, its untracked issues, and new issues, from Discord.

Off by default. The bot host supplies one GitHub token (``AWDISCORD_GITHUB_TOKEN``,
fine-grained, read access to the org's projects and issues; add issues: write to
let members file issues). A server manager points the server at a board:

    /gh connect owner:<org or user> project:<number>

Members then get ``/gh board`` (items per status column), ``/gh untracked`` (open
issues in the owner's repos that are on no board column of this project) and
``/gh issue`` (file an issue in one of the owner's repos).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from awdiscord.plugins import Command, Option, Plugin, PluginError

API = "https://api.github.com/graphql"

_BOARD = """
query($owner: String!, $number: Int!, $after: String) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        title url
        items(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            status: fieldValueByName(name: "Status") {
              ... on ProjectV2ItemFieldSingleSelectValue { name }
            }
            content {
              ... on Issue { number title url state repository { name } }
              ... on PullRequest { number title url state repository { name } }
              ... on DraftIssue { title }
            }
          }
        }
      }
    }
  }
}"""

_OPEN_ISSUES = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { number title url repository { name } } }
  }
}"""


def _token() -> str:
    return os.getenv("AWDISCORD_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or ""


def graphql(query: str, variables: dict, opener=None) -> dict:
    token = _token()
    if not token:
        raise PluginError("The bot host has no GitHub token (AWDISCORD_GITHUB_TOKEN).")
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "awdiscord"}, method="POST")
    try:
        with opener(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PluginError(f"GitHub said HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PluginError(f"GitHub is unreachable: {exc}") from exc
    if body.get("errors"):
        raise PluginError("GitHub: " + "; ".join(e.get("message", "?")
                                                 for e in body["errors"])[:300])
    return body.get("data") or {}


def board_items(owner: str, number: int, opener=None, max_pages: int = 20) -> dict:
    """{"title", "url", "items": [{"status", "repo", "number", "title", "url", "state"}]}"""
    items, after, project = [], None, None
    for _ in range(max_pages):
        data = graphql(_BOARD, {"owner": owner, "number": number, "after": after}, opener)
        project = ((data.get("repositoryOwner") or {}).get("projectV2"))
        if project is None:
            raise PluginError(f"No project #{number} visible under {owner}.")
        page = project["items"]
        for node in page["nodes"]:
            c = node.get("content") or {}
            items.append({"status": (node.get("status") or {}).get("name") or "No status",
                          "repo": (c.get("repository") or {}).get("name"),
                          "number": c.get("number"), "title": c.get("title", "(untitled)"),
                          "url": c.get("url"), "state": c.get("state")})
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return {"title": project["title"], "url": project["url"], "items": items}


def open_issues(owner: str, opener=None, max_pages: int = 10) -> list[dict]:
    out, after = [], None
    for _ in range(max_pages):
        data = graphql(_OPEN_ISSUES, {"q": f"owner:{owner} is:issue is:open", "after": after},
                       opener)
        page = data["search"]
        out += [{"repo": n["repository"]["name"], "number": n["number"],
                 "title": n["title"], "url": n["url"]} for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return out


def _conf(ctx) -> dict:
    conf = ctx.store.get("board")
    if not conf:
        defaults = ctx.plugin.defaults
        if defaults.get("owner") and defaults.get("project"):
            return defaults
        raise PluginError("No board connected. A manager can run `/gh connect`.")
    return conf


def _connect(ctx):
    conf = {"owner": str(ctx.opt("owner")).strip(), "project": int(ctx.opt("project"))}

    def work() -> str:
        board = board_items(conf["owner"], conf["project"], opener=OPENER[0])
        ctx.store.set("board", conf)
        return (f"Connected to **{board['title']}** ({len(board['items'])} items).\n"
                f"{board['url']}")
    return ctx.defer(work, private=True)


def summarize(board: dict, done: tuple[str, ...] = ("Done",)) -> str:
    cols: dict[str, int] = {}
    for it in board["items"]:
        cols[it["status"]] = cols.get(it["status"], 0) + 1
    lines = [f"**{board['title']}** - {len(board['items'])} items", board["url"] or ""]
    lines += [f"`{n:>4}` {status}" for status, n in cols.items()]
    stuck = [it for it in board["items"] if it["status"] == "Needs Work"][:5]
    if stuck:
        lines.append("\nNeeds work:")
        lines += [f"- {it['repo']}#{it['number']} {it['title']}" for it in stuck]
    return "\n".join(lines)


def _board(ctx):
    conf = _conf(ctx)
    return ctx.defer(lambda: summarize(board_items(conf["owner"], int(conf["project"]),
                                                   opener=OPENER[0])))


def untracked(board: dict, issues: list[dict]) -> list[dict]:
    on_board = {(it["repo"], it["number"]) for it in board["items"]}
    return [i for i in issues if (i["repo"], i["number"]) not in on_board]


def _untracked(ctx):
    conf = _conf(ctx)

    def work() -> str:
        board = board_items(conf["owner"], int(conf["project"]), opener=OPENER[0])
        missing = untracked(board, open_issues(conf["owner"], opener=OPENER[0]))
        if not missing:
            return f"Every open issue under {conf['owner']} is on **{board['title']}**."
        by_repo: dict[str, int] = {}
        for i in missing:
            by_repo[i["repo"]] = by_repo.get(i["repo"], 0) + 1
        head = ", ".join(f"{r} {n}" for r, n in sorted(by_repo.items(), key=lambda x: -x[1]))
        rows = "\n".join(f"- {i['repo']}#{i['number']} {i['title']}" for i in missing[:15])
        return f"**{len(missing)}** open issues are on no board ({head}):\n{rows}"
    return ctx.defer(work)


def _issue(ctx):
    conf = _conf(ctx)
    repo, title = str(ctx.opt("repo")).strip(), str(ctx.opt("title")).strip()
    body = (ctx.opt("body") or "") + f"\n\n_Filed from Discord by user {ctx.user}._"

    def work() -> str:
        token = _token()
        if not token:
            raise PluginError("The bot host has no GitHub token.")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{conf['owner']}/{repo}/issues",
            data=json.dumps({"title": title, "body": body}).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "awdiscord",
                     "Accept": "application/vnd.github+json"})
        with (OPENER[0] or urllib.request.urlopen)(req, timeout=20) as resp:
            issue = json.loads(resp.read().decode("utf-8"))
        return f"Filed {repo}#{issue['number']}: {issue['html_url']}"
    return ctx.defer(work)


OPENER: list = [None]        # tests swap in a fake transport

PLUGIN = Plugin(
    name="github",
    version="0.1.0",
    description="A GitHub Projects board: status, untracked issues, filing issues",
    persona=("This server tracks work on a GitHub Projects board. For status questions, "
             "point members at /gh board and /gh untracked rather than guessing."),
    commands=[Command("gh", "This server's GitHub board", subcommands=[
        Command("board", "Items per status column", _board),
        Command("untracked", "Open issues that are on no board", _untracked),
        Command("issue", "File an issue", _issue, options=[
            Option("repo", "Repository name", required=True, max_length=100),
            Option("title", "Issue title", required=True, max_length=200),
            Option("body", "Details", max_length=1500)]),
        Command("connect", "Point this server at a board", _connect, admin=True, options=[
            Option("owner", "GitHub org or user", required=True, max_length=60),
            Option("project", "Project number", type="integer", required=True)]),
    ])],
)
