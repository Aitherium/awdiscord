# awdiscord

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
| `/plugins list\|info\|enable\|disable` | what this server has on (enable/disable: Manage Server) |

## Plugins

Built in:

| plugin | default | commands |
|---|---|---|
| `community` | on | `/poll` with vote buttons, `/faq ask\|list\|set\|remove` (the agent answers from the FAQ only), `/rules` |
| `projects` | off | `/task add\|list\|start\|done\|assign\|drop`, `/standup` written by the agent from the board |
| `github` | off | `/gh board\|untracked\|issue\|connect` for a GitHub Projects board (`AWDISCORD_GITHUB_TOKEN`) |

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
| `AWDISCORD_CA_BUNDLE` | CA file for an agent served over a private CA |
| `AWDISCORD_OPERATORS`, `AWDISCORD_APPROVAL_SECRET` | enable approvals |
| `AWDISCORD_APPROVAL_CHANNEL` | post cards here instead of DMing operators |
| `AWDISCORD_INBOX_PORT`, `AWDISCORD_INBOX_TOKEN`, `AWDISCORD_INBOX_HOST` | local inbox |
| `AWDISCORD_DEV_GUILD` | also register commands on one server (instant, for testing) |
| `AWDISCORD_STATE_DIR` | where approvals are stored (default `~/.awdiscord`) |

`awdiscord --self-test` proves every rail offline and exits non-zero if one does not hold.

## License

Apache-2.0
