# awdiscord

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awdiscord/)**  ·  [Source](https://github.com/Aitherium/awdiscord)  ·  `pip install git+https://github.com/Aitherium/awdiscord.git`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awdiscord** is one of its 71 bricks — each installs on its own, runs offline, and needs no account. 5 more are in development and 15 are planned.
>
> **Start here:** Put an agent you already run into your Discord server as a bot with /ask.

<!-- aither-header:end -->

**Put any agent on Discord as a real bot, with an operator in the loop.**

An agent behind an OpenAI-compatible endpoint (an `adk serve` agent, llama.cpp /
Bonsai, vLLM, Ollama, LM Studio, a gateway) becomes a Discord bot with slash
commands, plugins each server turns on for itself, rate limits, and approval
buttons that only your operators can press. Your model, your machine.

```bash
pip install "awdiscord[gateway]"
awdiscord setup        # paste the bot token once; it does the rest
awdiscord run
```

`setup` checks the token is a bot token, writes the app's profile and
least-permission install settings, finds a model server running on this machine
(Bonsai/llama.cpp :8090/:8080, Ollama :11434, LM Studio :1234, vLLM :8000), makes
the app's owner (or team) the operators, saves everything to
`~/.awdiscord/awdiscord.env` (mode 600), registers the commands and prints the
install link. The one step Discord does not allow from an API is creating the
application: do that at https://discord.com/developers/applications, open **Bot**,
press **Reset Token**.

| command | what it does |
|---|---|
| `/ask question [persona] [private]` | the agent answers; `private` shows only you |
| `/share` | posts your last answer in the channel |
| `/status` | is the agent up, which personas exist |
| `/model setup\|test\|status\|off` | run this server on its own OpenAI-compatible endpoint (Manage Server) |
| `/selfhost` | an admin-only wizard from no model to this server's own endpoint, then hands off to `/model setup` (Manage Server) |
| `/plugins list\|info\|enable\|disable` | what this server has on (enable/disable: Manage Server) |

## Plugins

Built in:

| plugin | default | commands |
|---|---|---|
| `community` | on | `/poll` with vote buttons, `/faq ask\|list\|set\|remove` (the agent answers from the FAQ only), `/rules` |
| `projects` | off | `/task add\|list\|start\|done\|assign\|drop`, `/standup` written by the agent from the board |
| `github` | off | `/gh board\|untracked\|issue\|connect` for a GitHub Projects board (`AWDISCORD_GITHUB_TOKEN`) |
| `hub` | on, operators only | `/today`, `/alerts [n]` (Ack/Resolve buttons), `/signups [hours]`, `/issues`, `/reports` from an AitherOS fleet (`AWDISCORD_PULSE_URL`, `_CHRONICLE_URL`, `_RELAY_URL`, `_SUPPORT_URL`; internal key, TLS via `AWDISCORD_CA_BUNDLE`); only `/today` answers in the channel, the rest privately |
| `join` | off; also needs `/enlist action:enable` | `/enlist` (then a 10-scenario archetype assessment; `/progress` for the five-ideal ladder, see `awdiscord/ideals.py`): a two-step join form -> member number, Founding Cohort (first `cohort_cap`, default 100), track and squad roles, welcome card to the bot channel + DM. Managers: `/enlist action:status|setup|post|export|audit|digest|layout|set`, `/certify`, `/lead` (the only way to give Certified/Squad Lead; audited). Needs Manage Roles and a role above the ones it hands out |

Each server turns plugins on and off for itself; its data is kept per server and
never visible to another. A plugin's persona text is added to `/ask` only where
it is on.

Write your own:

```bash
awdiscord new-plugin standup-bot      # scaffolds ~/.awdiscord/plugins/standup_bot.py
awdiscord plugins                     # proves it loads (exit 1 names anything that failed)
```

```python
from awdiscord.plugins import Command, Option, Plugin

def hello(ctx):                        # ctx.user, ctx.guild, ctx.store, ctx.agent, ctx.opt()
    return ctx.reply(f"hello <@{ctx.user}>")

PLUGIN = Plugin(name="hello", version="0.1.0", description="Says hello",
                commands=[Command("hello", "Say hello", hello)])
```

Plugins are found in `awdiscord.builtin`, the `awdiscord.plugins` entry-point group
(so `pip install` a plugin package and it loads), `AWDISCORD_PLUGIN_DIRS` and
`~/.awdiscord/plugins`. `AWDISCORD_PLUGINS=a,b` narrows the set.

A plugin can also see every answer the agent posts (`on_reply=[fn]`, returning
component rows to add under it - rating buttons, say) and reactions people add or
remove (`on_reaction=[fn]`, with `AWDISCORD_REACTIONS=1`; bots' reactions never
arrive).

## Packs

```bash
awdiscord pack install ./packs/dgg_pm                       # a plugin pack
awdiscord pack install ~/my-agent-pack                      # an awdk agent pack
awdiscord pack install https://github.com/org/repo#packs/x  # from git
awdiscord pack list | remove NAME
```

An **agent pack** (`brain_pack.yaml` / `agent.yaml` / `skills/`, the awdk and
awpack shape) becomes a plugin: its system prompt rides along on `/ask` where it
is on, its skills are listed to the agent, and `/<pack> ask` answers in its voice.
`packs/dgg_pm` is an example plugin pack: Digital Ground Game's org board with
their own status flow.

## Run it under a supervisor

`awdiscord run --inbox-port 8371` opens `GET /health` for a supervisor. The awdk
addon manifest does exactly that, so `adk addon` (and awsh) can start, health-check
and restart the bot like any other local component.

## Rails

- **Bot tokens only.** Every request is sent as `Authorization: Bot <token>`, and the
  runtime asks Discord who the token belongs to before anything else. A user account
  is refused. Driving a person's account ("self-bot") is against Discord's terms.
- **Least permission.** The install link asks for `bot` + `applications.commands` and
  four permissions: view channel, send messages, embed links, read message history.
  `install_url()` refuses administrator, manage-server, manage-roles, manage-webhooks,
  ban, kick and mention-everyone.
- **Rate limits.** Token buckets per user, per server and for the whole bot; a request
  must find room in all three, and a refusal says how long to wait.
- **Mentions off.** Every answer goes out with `allowed_mentions: {parse: []}`, so an
  agent cannot ping `@everyone`.
- **Approvals.** A card with Approve / Deny buttons. The button id carries an HMAC of
  the approval id and the verb, only user ids in `AWDISCORD_OPERATORS` count, a
  decision is taken exactly once, and the result is POSTed to your callback with an
  `X-Awdiscord-Signature` header (HMAC-SHA256 of the body under the same secret).

## Approvals from your own code

Run the bot with an inbox, then raise cards from anything on the same machine:

```bash
export AWDISCORD_OPERATORS=<your user id>
export AWDISCORD_APPROVAL_SECRET=$(python -c "import secrets;print(secrets.token_hex(32))")
export AWDISCORD_INBOX_PORT=8371 AWDISCORD_INBOX_TOKEN=<a long random string>
awdiscord run &
awdiscord approval "Publish the release notes?" "v1.4 notes are drafted" \
    --callback http://127.0.0.1:9000/decided
```

Without `AWDISCORD_APPROVAL_CHANNEL` the card is sent to each operator by DM.

## DM the owner from a service

`POST /notify/owner` with `{"message": "..."}` DMs every owner (`AWDISCORD_OWNERS`,
defaulting to `AWDISCORD_OPERATORS`). It accepts the inbox bearer, or the
`X-Internal-Key` header when the bot has `AITHER_INTERNAL_SECRET` set. That key opens
this route only, never `/approvals` or `/post`. With no owners configured it answers
409 rather than dropping the message.

```bash
curl -X POST http://127.0.0.1:8371/notify/owner \
  -H "Authorization: Bearer $AWDISCORD_INBOX_TOKEN" \
  -H "Content-Type: application/json" -d '{"message":"New signup"}'
```

`POST /notify/channel` takes the same body and the same two credentials and posts to
`AWDISCORD_OPS_CHANNEL` (the team's ops room). It never takes a channel id from the
caller; with no ops channel configured it answers 409 with `"reason": "no_ops_channel"`
(the one signal a caller may treat as "skipped").

## Use the pieces alone

```python
from awdiscord import install_url, RateLimiter, ApprovalDesk, post_webhook, Dispatcher

install_url("123456789012345678")
post_webhook("https://discord.com/api/webhooks/...", "deploy finished")
```

`Dispatcher` takes the interaction JSON Discord sends and returns the response JSON,
with no network, so the bot's behaviour is testable with plain dicts.

## Configuration

| variable | meaning |
|---|---|
| `DISCORD_BOT_TOKEN` / `AWDISCORD_TOKEN` | the bot token |
| `DISCORD_APP_ID` / `AWDISCORD_APP_ID` | application id (looked up from the token if unset) |
| `AWDISCORD_AGENT_URL`, `AWDISCORD_AGENT_MODEL`, `AWDISCORD_AGENT_KEY` | the agent |
| `AWDISCORD_PERSONAS_DIR` | `<name>.md` or `<name>/SOUL.md` files become `/ask persona:` choices |
| `AWDISCORD_DEFAULT_PERSONA` | persona used when none is chosen |
| `AWDISCORD_A2A_URL` | speak A2A v0.3 (JSON-RPC `message/send`) to this agent endpoint; takes precedence over `AWDISCORD_AGENT_URL` |
| `AWDISCORD_A2A_TOKEN` | bearer for the A2A endpoint |
| `AWDISCORD_CA_BUNDLE` | CA file for an agent served over a private CA |
| `AWDISCORD_OPERATORS`, `AWDISCORD_APPROVAL_SECRET` | enable approvals |
| `AWDISCORD_APPROVAL_CHANNEL` | post cards here instead of DMing operators |
| `AWDISCORD_OPS_CHANNEL` | channel id `POST /notify/channel` posts to |
| `AWDISCORD_INBOX_PORT`, `AWDISCORD_INBOX_TOKEN`, `AWDISCORD_INBOX_HOST` | local inbox |
| `AWDISCORD_DEV_GUILD` | also register commands on one server (instant, for testing) |
| `AWDISCORD_STATE_DIR` | where approvals are stored (default `~/.awdiscord`) |
| `AWDISCORD_TOOL_MODEL` | model for turns that offer plugin tools (default: `AWDISCORD_AGENT_MODEL`) |
| `AWDISCORD_OWNER_AGENT_URL` | operators' mentions go to this fleet agent (Genesis `/chat`) |
| `AWDISCORD_OWNER_TOOL_LOOP=1` | operators get the plugin tool loop too; the fleet agent becomes the `ask_fleet` tool |
| `AWDISCORD_SERVED_GUILDS` | server ids allowed to answer with the owner's model (comma or space separated). The home guild (`AITHER_DISCORD_HOME_GUILD_ID`) is always served; an unset, empty or unparseable value serves the home guild only |
| `AWDISCORD_SETUP_REPLY_MINUTES` | how often an unserved server may receive the bring-your-own-inference card (default 10) |
| `AWDISCORD_STANDALONE=1` | self-hosted mode: the env agent answers every guild the bot is in. Without it an empty allowlist serves NOBODY. `awdiscord setup` writes it |
| `AWDISCORD_HOSTED=1` | server managers may use platform-hosted inference. **OFF unless set**: with it unset a server whose mode is `hosted` resolves to `none`, exactly as before |
| `AWDISCORD_HOSTED_BASE` | the hosted gateway's OpenAI-compatible base (default `https://mcp.aitherium.com/v1/hosted`). `https://` required; `http://` accepted only for a loopback gateway |
| `AWDISCORD_HOSTED_MODEL` | route name sent to the hosted gateway (default `aither-bonsai`; the gateway accepts only its published route names) |
| `AWDISCORD_GENESIS_URL` | Genesis base used to read a server's hosted record (`AWDISCORD_OWNER_AGENT_URL` is the fallback); `AITHER_DISCORD_BOT_KEY` authenticates it |

### Which servers may use your model

A bot you publish answers with YOUR inference, so a stranger who installs it must
not be able to spend it. Only the **home guild** and the servers listed in
`AWDISCORD_SERVED_GUILDS` reach the agent; any other server gets one short card
telling it to run its own model (`pip install awdiscord` + `awdiscord setup`), and
the card is rate-limited per server. DMs are served only to `AWDISCORD_OWNERS`.

To add a server, append its id:

```bash
AWDISCORD_SERVED_GUILDS=1470279244113510466,1451725348181119093
```

Leave the variable unset to serve the home guild alone. The gate lives in
`Dispatcher.chat` / `Dispatcher.handle`, before any agent is resolved, so an
unserved server never reaches a model.

### Bring your own model (`/model`)

A server manager can put a server on its own OpenAI-compatible endpoint (llama.cpp,
Ollama, LM Studio, vLLM, a hosted API) with `/model setup`. The command opens a
modal for the endpoint, model, API key and optional vision model(s); the endpoint
must answer a health check (`GET /models`, or a one-token completion) before
anything is saved. `/model test` re-checks it, `/model status` shows what answers
(never the key, only a 12-character fingerprint), and `/model off` deletes the key
and returns the server to the bot's default model. `/model` works even on a server
the bot does not serve, so that server can bootstrap itself; it writes only its own
guild's row, and the admin check reads the permissions on Discord's signed
interaction, never message text.

Every model call goes through one predicate, `BrainResolver.resolve(guild, user)`
(`awdiscord/brain.py`). `owner` = the bot's configured agent, `byo` = the server's
own endpoint, `none` = nothing answers here. `Dispatcher.agent_for(guild, user)`,
`/status`, the image seer and a plugin's `Ctx.agent` all read it, so a plugin cannot
borrow the owner's model for an unserved server.

The key is written to `{AWDISCORD_STATE_DIR}/brains/<guild>.secret` with
`os.open(..., 0o600)` inside a `0o700` directory, never to `guilds.db`; the database
keeps only the endpoint, model, vision list and fingerprint. On Windows the NTFS
ACL inherited from the state directory applies instead of the POSIX mode bits, so a
shared Windows host should use the Aitherium vault for keys.

A **hosted** bot (one with a `served` allowlist or a Genesis owner agent) accepts
only public `https://` endpoints on port 443: private, loopback, link-local and
metadata addresses are refused, redirects are never followed, and every request
re-resolves and pins the IP, so DNS rebinding cannot reach an internal address. A
**standalone** bot may use `http://`, loopback and LAN addresses, because that is
exactly what a model server on the same machine looks like.

### Platform-hosted inference (off by default)

With `AWDISCORD_HOSTED=1`, a server whose brain mode is `hosted` gets an
`OpenAICompatAgent` pointed at the platform gateway, authenticated with that
server's own key from `{AWDISCORD_STATE_DIR}/brains/<guild>.secret`. The server
never shares the bot owner's model or key. Genesis remains the authority: on a
60-second cache miss the bot reads `GET /v1/workspace/discord-guild-binding/inference`
and only an `active` record becomes a brain. A non-active record, a missing key,
a lookup error or any exception resolves to `none` (fail closed); an error is
never cached as hosted. A gateway 401/402/403 drops the record and re-asks Genesis
on the next valid window, and a 429 answers with "this server's daily allowance is
used up -- top up or bring your own model with /model" without retrying.


`awdiscord --self-test` proves every rail offline and exits non-zero if one does not hold.

## License

Apache-2.0

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awpack](https://github.com/Aitherium/awpack) | that the pack you want shipped inside somebody's SDK, under whatever licence that SDK happens to carry | the pack as its own versioned artifact, with its own licence, that any agent runtime can install |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awdesk](https://github.com/Aitherium/awdesk) | that the agent is somewhere behind a browser tab | a tray icon, a face on your desktop, and the decision card that pops when it needs you |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awdelphi](https://github.com/Aitherium/awdelphi) | one agent's confident take on a decision | the round trace, the anonymity, and who dissents |
| [awclassify](https://github.com/Aitherium/awclassify) | a filename, a folder, or whoever last touched it | doc_type, visibility, audience and topics, with the evidence lines that decided each |
| [awdecide](https://github.com/Aitherium/awdecide) | a hosted classifier's probability that never learns whether it was right | the decision, its probability, and the calibration curve from your own resolved outcomes |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awsuite](https://github.com/Aitherium/awsuite) | that an agent holding your mailbox will not send on its own | every send, draft, upload and create returns a dry-run until confirm is true |
| [awpermit](https://github.com/Aitherium/awpermit) | that an admitted agent will only do what its owner meant | an Ed25519 permit naming the agent, the principal, the door, the actions and the cap |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awrena](https://github.com/Aitherium/awrena) | a leaderboard someone can edit, and votes nobody counted | a scored duel with both answers kept, and a result bound to them |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awstorage](https://github.com/Aitherium/awstorage) | a du you ran last month, and a peers file that says 3 TB free | an inventory snapshot per node with a diff since the last one, and each tree classified re-fetchable or not |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awask](https://github.com/Aitherium/awask) | that anyone read the paragraph where you asked | the ask itself, with a button that steers the session that raised it |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| **awdiscord** _(you are here)_ | a self-bot on someone's account, or a bot that acts without asking | slash commands, rate limits, and approval buttons only your operators can press |
| [awswarm](https://github.com/Aitherium/awswarm) | that a model either fits your GPU or it doesn't run at all | a placement plan and an acquisition-probability estimate before you spend on a run |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [awvoice](https://github.com/Aitherium/awvoice) | that a cloud vendor may hold your audio | a transcript and a wav from a service you host |
| [awvision](https://github.com/Aitherium/awvision) | a filename and a caption somebody wrote | what a model actually reports about the pixels |
| [awscreen](https://github.com/Aitherium/awscreen) | a selector that was true when the page was written | the elements actually rendered, by what they look like |
| [awbeads](https://github.com/Aitherium/awbeads) | that a layout your users built survives the next deploy | the arrangement as data you can read back, diff, and hand to another surface |
| [awsprite](https://github.com/Aitherium/awsprite) | a chatbot with a face that resets every session | a knowledge base you can list, edit, and watch shape what it becomes |
| [awbonsai](https://github.com/Aitherium/awbonsai) | that inference always means a request left the machine | a WebGPU model answering on the tab's own GPU, with a consent record logged before it ever loaded |
| [gawbbonet](https://github.com/Aitherium/gawbbonet) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [awrtifact](https://github.com/Aitherium/awrtifact) | a hand-rolled split script and a hand-edited worker manifest | byte-verified parts in a release, served with Range + CORS, sizes asserted by a live gate |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awreport](https://github.com/Aitherium/awreport) | that the report you pasted carried no token in it | a redacted report, and the duplicate it merged into instead of filing twice |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awfocus](https://github.com/Aitherium/awfocus) | twelve terminal tabs and a bad memory | one command that names every session, finds any transcript, and opens or steers the one you want |
| [awgym](https://github.com/Aitherium/awgym) | that a world model learned anything from the games it saw | transitions captured from real play, fed back, and the retrodiction score falling on grids it never saw |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awevolve](https://github.com/Aitherium/awevolve) | that your optimisation loop is finding anything | every version it kept, the score that version earned, and the edit that produced it |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awmine](https://github.com/Aitherium/awmine) | that a session's lesson survived the session | a row per outcome, a candidate per lesson, and the transcript line each one came from |
| [awrise](https://github.com/Aitherium/awrise) | that a scheduled agent ran at all, and ran exactly once | a durable record of every wake -- fired, skipped, overlapped or timed out -- each with its reason |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |
| [awwall](https://github.com/Aitherium/awwall) | that a service only talks to the hosts you think it talks to | an explicit egress allowlist, where a denial names the rule that denied it |
| [awembed](https://github.com/Aitherium/awembed) | a general-purpose embedder that has never seen your code | a held-out split of whole directories, scored teacher vs student vs int8 |
| [awtax](https://github.com/Aitherium/awtax) | a closed tax app's sealed file you can never read again | a plain, provider-neutral schema of every figure, with the page it came from |
| [awsettings](https://github.com/Aitherium/awsettings) | that you will remember to re-approve the same thing on every box you work from | one profile, unioned rather than overwritten, with the credentials left behind |
| [awavatar](https://github.com/Aitherium/awavatar) | a cloud 3D vendor's opaque task id | a manifest with a sha256, a licence and a rig-audit verdict per file |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awpack](https://github.com/Aitherium/awpack) | First-party agent packs — the ones we build, versioned and installable on their own | [docs](https://aitherium.github.io/awpack/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awdesk](https://github.com/Aitherium/awdesk) | Aither World Desk -- the desktop body of AitherOS Online: tray, avatars, decision cards, the Living Desktop as an overlay | [docs](https://aitherium.github.io/awdesk/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awpool](https://github.com/Aitherium/awpool) | The free half of the elastic work pool: dispatches public-safe work onto the free GitHub-hosted runners of the public aw* mirrors, so their idle CI minutes become compute. awrun remains the paid/self-hosted lane; this is its free twin | [docs](https://aitherium.github.io/awpool/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awdelphi](https://github.com/Aitherium/awdelphi) | Anonymous multi-round expert panels — a converged answer with a trace | [docs](https://aitherium.github.io/awdelphi/) |
| [awclassify](https://github.com/Aitherium/awclassify) | Classify any document -- what it is, who may read it, who it is for, what it is about | [docs](https://aitherium.github.io/awclassify/) |
| [awdecide](https://github.com/Aitherium/awdecide) | One typed-decision contract -- choice / score / bool with a probability -- over a ladder of backends you already run (rules, tiny local models, an LLM's logprobs), fail-closed, with a Brier ledger that resolves every decision against its outcome | [docs](https://aitherium.github.io/awdecide/) |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awsuite](https://github.com/Aitherium/awsuite) | Your Google Workspace as agent tools, and no write happens without a yes | [docs](https://aitherium.github.io/awsuite/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awpermit](https://github.com/Aitherium/awpermit) | Let an agent through your door with a scope, a spend cap, and someone accountable | [docs](https://aitherium.github.io/awpermit/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awrena](https://github.com/Aitherium/awrena) | Put two agents head to head and get a verdict you can check | [docs](https://aitherium.github.io/awrena/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awstorage](https://github.com/Aitherium/awstorage) | Every drive on every node, indexed, classified and diffed -- so you can see what you own before you delete it | [docs](https://aitherium.github.io/awstorage/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awask](https://github.com/Aitherium/awask) | Your agent asks you a question — and acts on your answer | [docs](https://aitherium.github.io/awask/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| **awdiscord** _(you are here)_ | Put any agent on Discord as a real bot, with an operator in the loop | [docs](https://aitherium.github.io/awdiscord/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awswarm](https://github.com/Aitherium/awswarm) | Run one model too big for any single GPU across a pool of small ones | [docs](https://aitherium.github.io/awswarm/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awvoice](https://github.com/Aitherium/awvoice) | Hear and speak — transcribe audio, synthesize a voice | [docs](https://aitherium.github.io/awvoice/) |
| [awvision](https://github.com/Aitherium/awvision) | See an image — describe it, ask it a question, compare two | [docs](https://aitherium.github.io/awvision/) |
| [awscreen](https://github.com/Aitherium/awscreen) | See this machine — what is on screen, and where to click it | [docs](https://aitherium.github.io/awscreen/) |
| [awkit](https://github.com/Aitherium/awkit) | Render an agent panel from a tool result — one component, any React app | [docs](https://aitherium.github.io/awkit/) |
| [awbeads](https://github.com/Aitherium/awbeads) | A spatial canvas for a page — arrange things, connect them, and keep the arrangement | [docs](https://aitherium.github.io/awbeads/) |
| [awsprite](https://github.com/Aitherium/awsprite) | Hatch a companion that grows only from what you teach it, then take it home | [docs](https://aitherium.github.io/awsprite/) |
| [awbonsai](https://github.com/Aitherium/awbonsai) | Run a real model in the visitor's own browser — no server round trip, no upload | [docs](https://aitherium.github.io/awbonsai/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [awbrain](https://github.com/Aitherium/awbrain) | Your history as a wiki of linked markdown — claims pinned to the evidence | [docs](https://aitherium.github.io/awbrain/) |
| [gawbbonet](https://github.com/Aitherium/gawbbonet) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | [docs](https://aitherium.github.io/gawbbonet/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrtifact](https://github.com/Aitherium/awrtifact) | Deliberately chunk artifacts into GitHub release assets — the productized aitherkvcache mirror lane | [docs](https://aitherium.github.io/awrtifact/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awreport](https://github.com/Aitherium/awreport) | File a bug report that has already scrubbed your secrets and collapsed the duplicate | [docs](https://aitherium.github.io/awreport/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awfocus](https://github.com/Aitherium/awfocus) | See, search and steer every Claude session from one command | [docs](https://aitherium.github.io/awfocus/) |
| [awgym](https://github.com/Aitherium/awgym) | An ARC training gym — a game a world model can watch, and six roles that play through it | [docs](https://aitherium.github.io/awgym/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awevolve](https://github.com/Aitherium/awevolve) | Point an agent at a file and a command that scores it, and let it improve | [docs](https://aitherium.github.io/awevolve/) |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | [docs](https://aitherium.github.io/awsh/) |
| [awmine](https://github.com/Aitherium/awmine) | Mine what your agents did -- outcomes, lessons and procedures out of the transcripts they left behind | [docs](https://aitherium.github.io/awmine/) |
| [awrise](https://github.com/Aitherium/awrise) | Wake an agent on a schedule, let it do one thing, and put it back to sleep | [docs](https://aitherium.github.io/awrise/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |
| [awwall](https://github.com/Aitherium/awwall) | Say what a workload may reach, and watch everything else fail closed | [docs](https://aitherium.github.io/awwall/) |
| [awrouter](https://github.com/Aitherium/awrouter) | OpenRouter for your own fleet: pick a model backend by cost/latency/ capability, fail over, fit the context window, stream. Standalone, OpenAI-compatible, no Aither-specifics required to be valuable | [docs](https://aitherium.github.io/awrouter/) |
| [awembed](https://github.com/Aitherium/awembed) | Train an embedding model that knows your corpus, and prove it beats the big one | [docs](https://aitherium.github.io/awembed/) |
| [awtax](https://github.com/Aitherium/awtax) | Turn any tax PDF -- returns, W-2, 1099, statements, even scans -- into structured data you can check | [docs](https://aitherium.github.io/awtax/) |
| [awflow](https://github.com/Aitherium/awflow) | A deterministic workflow runtime — chain agent calls with journal replay and budget control | [docs](https://aitherium.github.io/awflow/) |
| [awsettings](https://github.com/Aitherium/awsettings) | Your agent's permissions and config, following you to the next machine | [docs](https://aitherium.github.io/awsettings/) |
| [awavatar](https://github.com/Aitherium/awavatar) | One character spec in, a rigged, animated, multi-style avatar pack out | [docs](https://aitherium.github.io/awavatar/) |

**Built on** [REA (Reverse Engineer Anything)](https://github.com/morluto/rea) · [llama.cpp](https://github.com/ggml-org/llama.cpp) · [vLLM](https://github.com/vllm-project/vllm) · [ComfyUI](https://github.com/comfyanonymous/ComfyUI) · [CentOS Stream](https://www.centos.org/centos-stream/) · [Podman](https://github.com/containers/podman) · [Docker](https://github.com/moby/moby) · [LanceDB](https://github.com/lancedb/lancedb) · [WireGuard](https://www.wireguard.com/) · [FFmpeg](https://ffmpeg.org/) · [Blender + Rigify](https://www.blender.org/) · [headroom](https://github.com/headroomlabs-ai/headroom) · [SANA](https://github.com/NVlabs/Sana) · [Hunyuan3D](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1) · [repowise](https://github.com/repowise-dev/repowise) · [Playwright](https://github.com/microsoft/playwright) · [Chromium](https://www.chromium.org/) · [Next.js](https://github.com/vercel/next.js) · [React](https://github.com/facebook/react).

<div id="aither-constellation" data-self="awdiscord"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
