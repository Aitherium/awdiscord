# Add Aither to your Discord server

Aither is an agent you can talk to in your server. You install it from one public link, and a
server manager finishes the setup inside Discord: no portal, no config file, no terminal. This
page is for someone who has never managed a Discord before.

## Install

**[Add Aither to Discord](https://discord.com/oauth2/authorize?client_id=1470273764708057120&scope=bot%20applications.commands&permissions=84992)**

You need the **Manage Server** permission to install a bot.

The link asks for four permissions and nothing else. Together they are the number `84992`
(`1024 + 2048 + 16384 + 65536`), and it is the same link `awdiscord install-url` prints.

| permission | why it is asked for |
|---|---|
| View Channels | see the channel it is posting in |
| Send Messages | post answers, cards and setup messages |
| Embed Links | render a titled card instead of a bare URL |
| Read Message History | read the recent messages it was asked about |

Slash commands need no channel permission at all; these four exist so `/share` and the setup
card can post outside an interaction reply. Anything that can take a server over —
administrator, manage server, ban, kick, mention everyone — is refused by the link builder
itself, not merely left unticked.

## Setup, in four steps

When the bot joins, it posts **one** setup card. A server manager (anyone with Manage Server)
runs `/aitherium setup`; the card walks through four steps and shows the status of each.

1. **Create or link an Aitherium account.**
   *Create your Aitherium account* gives you a personal signed signup link, valid for one hour
   and one use — the Discord account is linked automatically when the account is created.
   *Link existing account* gives a one-time code to enter at
   <https://app.aitherium.com/link/discord>. Workspaces are free; create one if your account
   has none yet.

2. **Connect this server.**
   *Connect this server* checks two facts together and takes neither from message text:
   Discord's signed interaction proves you hold Manage Server, and your linked account proves
   which Aitherium account you are. The bot records your consent — the Discord user id, the
   account id and the interaction id — and binds the server to that account's workspace.
   A server belongs to one workspace at a time. If it is already connected to a different one,
   Aither refuses and never takes it over.

3. **Grant management permissions.**
   *Grant management permissions* opens a **separate** OAuth screen, locked to this server
   (the link carries the guild id with guild selection disabled, so it cannot land on another
   server). It is a second, deliberate grant, so the chat bot does not hold these bits from the
   moment you add it. See the list below.

4. **Preview and apply the starter layout.**
   *Preview layout* lists exactly what would be created and writes nothing. *Apply* is
   **additive only**: it creates missing roles, categories and channels and never deletes,
   renames or edits an existing channel's permissions. The starter community is:
   - roles **Founders**, **Builders**, **Friends** — no moderation powers;
   - **Start Here**: welcome, rules, introductions;
   - **Community**: general, ideas, showcase;
   - **Build**: projects (forum), decisions, briefs, agents, github-feed, founders;
   - **Voice**: Lounge, Standup.

   Everything is readable by `@everyone` except `#founders`, which only Founders can see. A
   layout file that grants administrator or manage-server is refused before anything is
   written.

`/aitherium status` shows the same four steps read-only for any member.

## What "management permissions" include and exclude

The second link asks for what arranging a server needs. It is not a moderator grant and it is
not administrator.

**It can**

- create and arrange categories, text channels, voice channels and forums;
- create roles and assign them, and set channel permissions for those roles;
- manage webhooks, events and threads, create public threads and reply inside threads;
- add reactions, attach files, connect and speak in voice, and change its own nickname.

**It cannot**

- **Administrator** — every permission at once. Never requested.
- **Manage Server** — rename, delete or transfer the server. Never requested.
- **Ban Members**, **Kick Members**, **Timeout Members** — never requested.
- **Mention Everyone** — it cannot ping the whole server. Never requested.
- delete or rename an existing channel, or remove a role above its own.

Discord also enforces the ceiling: a bot can only manage roles below its own highest role, so
it cannot touch the owner role or anything above it.

## Run it on your own machine instead

The hosted bot is a convenience. The same bot runs on your machine, on your model, with your
data — and needs no Aitherium account at all.

```bash
pip install awdk
adk onboard --discord
```

`adk onboard --discord` installs the agent pack, validates your bot token live against
Discord, prints your own install link, and can launch the bot. Create the Discord application
once at <https://discord.com/developers/applications> and paste the token.

Prefer the package alone?

```bash
pip install "awdiscord[gateway]"
awdiscord setup
```

Source: <https://github.com/Aitherium/awdiscord>
