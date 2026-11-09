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
   - roles **Aitherium Steward**, **Aitherium Engineer**, **Aitherium Partner** — no
     moderation powers (`/template names set:themed` switches them to the themed names
     before the layout is applied; existing roles are never renamed);
   - **Start Here**: welcome, rules, introductions;
   - **Community**: general, ideas, showcase;
   - **Build**: projects (forum), decisions, briefs, agents, github-feed, stewards;
   - **Voice**: Lounge, Standup.

   Everything is readable by `@everyone` except `#stewards`, which only the steward role can see. A
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

## Point the bot at your own model with `/model`

You do not have to use Aither's model. A server manager can give any server its own
OpenAI-compatible endpoint — llama.cpp, Ollama, LM Studio, vLLM, or a hosted API — and the bot
will answer there instead.

1. Run `/model setup` (Manage Server). A form asks for the **endpoint base URL**
   (`https://…/v1`), the **model name**, the **API key**, and optionally the **vision model(s)**.
2. The bot checks the endpoint before saving anything: it asks for the model list or sends a
   one-token completion. If the endpoint does not answer, nothing is stored and you are told
   why.
3. `/model status` shows what answers for this server, including a short fingerprint of the
   key — never the key itself. `/model test` re-checks the endpoint. `/model off` deletes the
   key and puts the server back on the bot's default model.

This works even on a server the hosted bot does not normally serve, so you can bootstrap your
own endpoint without waiting for anyone. `/model` only ever writes this server's own setting;
the admin check reads the permissions Discord signs into the interaction, so it cannot be
spoofed from a message.

The API key stays on the bot's host, written to a file that only the bot's user can read
(mode `0600` inside a `0700` directory under the bot's state directory); it is never put in
the bot's database. On Windows, file modes come from the inherited ACL, so run the bot under
an account that other users on the machine cannot read.

If you run the public hosted bot, only a public `https://` endpoint on port 443 is accepted:
`http://`, loopback, LAN and cloud-metadata addresses are refused, and redirects are never
followed. A bot you run yourself may point at a model on `localhost` or your LAN.

## Starting from no model at all: `/selfhost`

If you have no model server yet, a server manager can run `/selfhost` (Manage Server). It
shows one card per step, visible only to you, with **Back** and **Next** buttons; the bot
remembers where you are for this server. It never runs anything on your machine — you run
each command yourself — and it never asks a model for an answer.

1. **Choose a tier.** Install the toolkit, then pick the command for your GPU's memory:

   ```bash
   pip install awdk
   adk setup --tier nano          # 6-8 GB of VRAM
   adk setup --tier standard-tq4  # 12-16 GB
   adk setup --tier full          # 24 GB or more
   ```

   Already running Ollama, LM Studio or vLLM? Press **I already run Ollama / LM Studio /
   vLLM** and skip to the public-address step.

2. **Check the machine.** `adk doctor` loads a model and reports what your hardware can do.
3. **Start the endpoint.** `adk-serve --identity aither --port 8080` serves an
   OpenAI-compatible API on port 8080; leave it running.
4. **Give it a public address.** The hosted bot must reach your endpoint over `https://`, so
   start a tunnel and copy the address it prints:

   ```bash
   pip install awtunnel
   awtunnel up --port 8080
   ```

   Add `/v1` to the end of that `https://` URL. If you run this bot on the same machine,
   `http://localhost:8080/v1` is enough.
5. **Connect it.** Press **Open the /model form** (or run `/model setup`) and paste that URL,
   the model name your endpoint reports, and any placeholder as the API key if your endpoint
   has none. The form probes the endpoint before it saves anything.
6. **Optional extras.** `awnode mcp` gives your agent local tools; `pip install awkno` then
   `awkno <brick>` gives it offline docs. Ready-made skills live in the
   [Aitherium/awskills](https://github.com/Aitherium/awskills) repository — clone them from
   there; they are not on PyPI.

Every card links <https://aitherium.com/discord/>. `/selfhost` works in a server the hosted
bot does not normally answer in, so you can bootstrap your own model without waiting for
anyone.

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
