"""dgg-pm - Digital Ground Game project management on Discord.

Install:   awdiscord pack install <this directory>
Then:      /plugins enable dgg-pm

Preset to DGG's org board (digitalgroundgame, project #3) and its own status
flow: Backlog -> Ready -> In progress -> In review -> Needs Work -> Done. The
bot host needs AWDISCORD_GITHUB_TOKEN with read access to the org's projects
and issues (issues: write to let members file issues with /dgg issue).

A server can point at a different board with `/gh connect` from the github
plugin; this pack only changes the defaults and the agent's voice.
"""
from awdiscord.builtin import github as gh
from awdiscord.plugins import Command, Option, Plugin

FLOW = ("Backlog", "Ready", "In progress", "In review", "Needs Work", "Done")


def _board(ctx):
    conf = gh._conf(ctx)

    def work() -> str:
        board = gh.board_items(conf["owner"], int(conf["project"]), opener=gh.OPENER[0])
        counts = {s: 0 for s in FLOW}
        for it in board["items"]:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        lines = [f"**{board['title']}** - {len(board['items'])} items", board["url"] or ""]
        lines += [f"`{n:>4}` {s}" for s, n in counts.items()]
        review = [it for it in board["items"] if it["status"] in ("In review", "Needs Work")]
        if review:
            lines.append("\nWaiting on review or rework:")
            lines += [f"- {it['status']}: {it['repo']}#{it['number']} {it['title']}"
                      for it in review[:8]]
        return "\n".join(lines)
    return ctx.defer(work)


PLUGIN = Plugin(
    name="dgg-pm",
    version="0.1.0",
    description="Digital Ground Game: the org board, untracked issues, filing issues",
    homepage="https://dgg.aitherium.com",
    defaults={"owner": "digitalgroundgame", "project": 3},
    persona=(
        "You help coordinate Digital Ground Game's volunteer software projects. Work is "
        "tracked on the org's GitHub Projects board with the status flow Backlog, Ready, "
        "In progress, In review, Needs Work, Done. 'Needs Work' means it was reviewed and "
        "sent back - not that it was never started. Point volunteers at /dgg board for "
        "status and /dgg untracked for issues no board shows. Never invent issue numbers."),
    commands=[Command("dgg", "Digital Ground Game project board", subcommands=[
        Command("board", "Items per status column, and what is waiting on review", _board),
        Command("untracked", "Open DGG issues that are on no board", gh._untracked),
        Command("issue", "File an issue in a DGG repo", gh._issue, options=[
            Option("repo", "Repository, e.g. pragmatic-papers", required=True,
                   max_length=100),
            Option("title", "Issue title", required=True, max_length=200),
            Option("body", "Details", max_length=1500)]),
    ])],
)
