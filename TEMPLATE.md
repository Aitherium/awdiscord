# Your Discord as a dev business: the minimal template

Add the bot to your server. It posts one card: **Set up this server as a dev business?
Minimal | Full | Aitherium section.** Press one, and the bot shows you exactly what it
would create. Nothing happens until you press **Confirm**. Only this server's admins
(Manage Server or Administrator) can press these buttons, or run `/template plan` and
`/template apply` yourself.

The bot only ADDS. It never deletes, renames or changes a channel or role you already
have. If you already have an `#announcements` somewhere, it keeps yours.

## What Minimal gives your server

**The front door**

| channel | what it is for |
|---|---|
| #welcome | what this server builds and how to join (read-only) |
| #join | the Join card: a new member gets a number, a squad and a track |
| #announcements | releases and news; Squad Leads and the bot post here |
| #bot | where people talk to the AI agents |
| #help | ask anything; a person or an agent answers |
| #showcase | show what you shipped |

**Projects**: one channel per project, plus #releases, where every release lands with
its notes. You name the projects (`/template apply kind:minimal projects:"api, web"`).

**Squads**: #squad-leads (only Squad Leads see it) and #lfg (find a squad or a reviewer).

**Roles**

- **Ten archetypes** (Warden to Binder): the kind of builder someone is. Members find
  theirs with a ten-question assessment when they join.
- **Five levels**: steps people climb as they ship (Aitherium Member up to Aitherium
  Steward).
- **Aitherium Engineer**: the member role. Only Engineers (and Partners) can talk to the
  agents in #bot. This keeps the agents for your team.
- **Squad Lead**: runs a squad and can post releases and announcements.
- **Aitherium Partner**: for people outside the team who still work with the agents.

Prefer the names from the Aitherium Collective? `/template names set:themed` switches
the roles to those names (Knight Radiant, First to Fifth Ideal, the ten orders).

No role gets admin or moderation powers. The template refuses to create one.

**Plugins it turns on**: the Join flow, GitHub (`/gh board`, `/gh untracked`, `/gh issue`), community
tools (polls, FAQ, rules) and the orchestrator (images and video through Iris). If you
already turned a plugin on or off, your choice stays.

## Already have a server you like? Add just an Aitherium section

Press **Aitherium section** instead (or `/template apply kind:partner`). It adds ONE
category, **Aitherium**, and changes nothing else in your server:

| channel | what it is for |
|---|---|
| #bot | Aither lives here; Aitherium Engineers and Partners talk to it |
| #join | the Join card for contributors |
| #projects | a forum of verified projects, tagged Verified, Seeking contributors, In progress or Needs review |

Two roles only: **Aitherium Engineer** and **Aitherium Partner**.

#projects starts empty. A project appears there only once the Aitherium Verified program
lists it; the badge is granted, never self-applied, so an Aitherium operator posts it.
The card shows the program's own fields: the repo, the date it was verified, the
verification report, and whether it is looking for contributors. A project looking for
contributors also gets a **+ Join as contributor** button, which opens the join form
with that project's squad already picked. Forums need a Community server in Discord;
on other servers #projects is a normal channel and the cards are messages.

## Full adds

Work (#decisions and #briefs from the agents, #repo-updates, #social-media drafts,
R&D and products forums), Community (#general, #off-topic), Voice (General, Standup),
and an ideas forum plus a tasks channel for every project. Forums need a Community
server in Discord; on other servers they are skipped and the plan says so.

## If something is missing

The bot needs **Manage Roles** and **Manage Channels**. If it lacks one, the plan says
which, and how many items need it, and nothing is created. Grant it, press again.
