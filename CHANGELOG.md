# Changelog

## [Unreleased]

- Fact floor for answers built from a search (`awdiscord.factfloor`). After a turn
  whose `web_search` / `deep_research` / `fetch_url` / `news_headlines` returned
  results, every proper name in the answer must appear in those results or the
  conversation: a near miss is corrected to the results' spelling ("YACI" -> "YaCy"),
  a name found nowhere ("Demetri") drops its sentence. One extra call to the turn's
  own model lists the sentences the results do not support or contradict, and those
  are dropped too ("DuckDuckGo operates its own search backend rather than proxying
  Google"). A trimmed answer says it is unsure; every such answer ends with its
  sources. `AWDISCORD_FACT_CHECK=0` skips the model check (the name check stays).

## [0.6.51] - 2026-11-18

- Several persona bots in one voice channel answer a named list: "Atlas, Aither and
  Lyra, quick take on X" gets ONE answer from each named bot, in the order named, one
  at a time. Every bot strips all the leading names (Atlas no longer answers "Aither,
  yadda" as his question) and takes its turn from the room itself: it waits until the
  bots named before it have spoken and the floor is quiet, with a deterministic
  `AWDISCORD_VOICE_SLOT_S` fallback per slot that skips a bot when the floor stays
  busy. No shared server: one bot, one process.
- Quick council: "Atlas, Demi, Lyra, discuss X" (discuss/debate/argue/council/weigh in)
  is one round -- each named bot says one line after reading the earlier lines from
  the voice channel's text chat, then Aither gives a one-line verdict. One round, at
  most `AWDISCORD_VOICE_COUNCIL_MAX_AGENTS` (5) agents, inside
  `AWDISCORD_VOICE_COUNCIL_BUDGET_S` (45) seconds. "Enough" / "wrap it up" / "stop"
  from a human ends it at once and the lead sums up.
- Bots still never answer another bot's speech: the sink drops all bot audio and only
  speaking start/stop is observed (for the floor).
- `awdiscord.voice_council` holds the parsers (multi-name, STT spellings and aliases
  such as "demi"), the cue/stop words, the caps and the slot policy.

## [0.6.50] - 2026-11-18

- An owner-listed server whose `hosted` row cannot answer (no active paid record, no key,
  lookup down) now answers on the owner's model even when hosted inference is ON. A server
  the operator did not list still never reaches the owner's model.

## [0.6.49] - 2026-11-17

- A server the operator lists on the owner's lane (home or `AWDISCORD_SERVED_GUILDS`) that
  carries a stale `hosted` brain row now answers on the owner's model while hosted inference
  is off, instead of showing members the "bring your own model" card. With hosted on, a
  paid server still never falls back to the owner's model.

## [0.6.48] - 2026-11-17

- Conversation wrap-ups (`awdiscord.wrapup`, plugin `wrapup`, on by default).
  `/aither wrapup now [since] [post_to] [preview]` reads the channel since a message link or
  a duration and posts ONE top-level message (default #general): each person's position
  credited by name, points of agreement, open questions, proposed resolutions (numbered,
  vote by reaction), due-outs (owner, action, date) and the links people shared, ending
  with an invitation to chime in. `preview:True` shows it only to you. The message menu
  "Wrap up from here" previews from the clicked message (it takes the slot of
  "Aither: summarize thread": Discord allows five message commands), and
  "@Aither wrap this up" works from a mention.
- Grounded, never invented: every line cites the messages it came from, a named person must
  be the author of a cited message, a due date survives only when the owner wrote it
  (otherwise `date: ask owner of item`), and first-person or editorial model prose is
  dropped. First-person commitments ("I should email them") always become due-outs, and
  with no model the wrap-up is built from quoted lines alone.
- Due-outs are tracked: Claim / Done buttons (closing is the named owner's or staff's),
  `/aither wrapup status`, and cards on the platform task board when the bot is configured
  for it. Posting is staff-only; a preview is anyone's who can read the channel.
- The interaction is acknowledged at once and the wrap-up is written in the deferred work,
  with a progress edit while the model queue is busy (it waits instead of failing).
- `awdiscord wrapup --channel ID [--since ...]`: a dry run that prints, never posts.

## [0.6.47] - 2026-11-16

- Busy-lane fast fail: before a text turn the bot reads MicroScheduler's `/llm/queue/detail`;
  when work for the turn model's lane has waited `AWDISCORD_BUSY_WAIT_S` (5 s), the turn goes
  to `AWDISCORD_BUSY_MODEL` (a separate lane, still through MicroScheduler). Off unless set;
  a BYO endpoint is never probed.

## [0.6.46] - 2026-11-16

- A turn with images tries the turn model itself as the last vision lane (it is often the
  same multimodal lane), and a tool turn takes ONE tool-free look at the images first,
  then runs its tool loop on the description. A busy lane cools down 30 s, not 5 minutes.
- The hosted brain reads images (`AWDISCORD_HOSTED_VISION_MODELS`; `none` turns it off).
- An operator's turn is told the operator's word settles their own affairs: one caution at
  most, no evidence requests, the work starts with at most one question. Names of the
  servers the bot is in ride along.
- Mentioned people reach the model by name (`<@id> (Display, @username)`).
- Inbox `POST /rehandle {channel_id, message_id}`: the bot answers an operator's message
  again, itself.
- More than 100 chat commands: the overflow is registered in the home server instead of
  failing the boot with Discord 30032.

## [0.6.45] - 2026-11-16

- Redact a phrase on request (`awdiscord.redact`; `/redact phrase days`, and the
  `redact_phrase` tool for @mentions). The bot EDITS its own messages that contain the
  phrase to `[redacted]`; other members' matches are listed and DELETED only after the
  asker confirms, and only where the bot holds Manage Messages (without it they are listed,
  nothing is deleted). Staff only: Manage Messages / Manage Server / Administrator, the
  server owner, or a bot operator. Scans back up to 90 days / 1000 messages.
- `Tool.secret_args`: a tool argument named there is masked (sha256 prefix + length) in the
  tool-call log, so a redaction never writes the phrase it removes.

## [0.6.44] - 2026-11-16

- A face for the voice call (`awdiscord.hangout`). Discord does not let a bot send video, so
  every clip the bot puts on the wire (16 kHz mono WAV) and every gesture is handed to ONE
  configured receiver (`AWDISCORD_AVATAR_URL` / `AWDISCORD_AVATAR_TOKEN`, or the inbox
  `POST /voice/avatar` while the bot runs) that draws a body, which is then shared into the
  call from a person's own client. A dead receiver drops frames and is counted in
  `/voice/status`; it never delays the audio.
- Spoken gestures: "Aither, do a little dance" / "wave" / "take a bow" / "celebrate" /
  "do finger guns" play a gesture on the receiver. A question that merely mentions dancing
  stays a question.
- Inbox controls for a host that runs the call: `POST /voice/notes` (start / stop the notes
  session; stopping posts the summary), `POST /voice/emote`, `GET /voice/transcript` (the live
  transcript), `POST /voice/inject` (rehearsal: speak a line in another voice into the call's
  worker; the speech-to-text, agent, TTS and playback legs run for real).

## [0.6.43] - 2026-11-15

- Forwarded messages are read. A Discord "Forwarded" message has empty `content`; its
  text lives in `message_snapshots`, which the bot never read, so a forward to a persona
  got the canned "Hi - ask me anything here". The snapshot's text and embeds are now the
  turn text (`forwarded: true`).
- "Solve this" (`awdiscord.solve`, opt-in via `AWDISCORD_SOLVE_CONFIG` +
  `AWDISCORD_HARNESS_URL`): the owner's "solve this" / "fix it" / "do it" in a persona's
  DM starts a harness-daemon session on what the DM is about (a forward, an alert page,
  a card), and the persona's own bot posts the 3-line report (cause, fix, verification)
  in the same DM when it ends. No fresh item (6 h) means ordinary chat. An alert that
  matches the `owner_only` rules (credential, money, public post, grant) raises a
  decision card and starts nothing. A daemon failure says "Nothing was changed".
  Ledger `/state/solve.json` resumes the watchers after a restart.

## [0.6.42] - 2026-11-15

- "What is the misunderstanding" answers come in a fixed shape: one line per person's
  position (what they want, not their role), the crux, the fix. A small set of
  reference notes rides along when the conversation is on their topic (first: GitHub's
  noreply commit email), so a fallback model that lacks the fact still names it.

## [0.6.41] - 2026-11-15

- `/ask` and @mentions read the recent conversation on their own: the channel's (or
  thread's) last 40 messages (`AWDISCORD_CONTEXT_MESSAGES`), bounded by a token budget
  (`AWDISCORD_CONTEXT_TOKENS`, default 4000; the oldest lines drop first), each with its
  time, author, who it replies to, and link titles. Only a channel the asker can read
  (View Channel + Read Message History); another channel only through a linked message
  the asker can read there. The answer ends with "(read the last N messages here)".
- `/ask context:recent|thread|since-link|none` (default `recent`) and `link:`.
- `/ask` with no question opens a form with a 4000-character field; the `question`
  option takes 6000. A mention is no longer cut at 1500 characters.
- "Ask Aither about this": a message context-menu command any member may use. It reads
  the conversation around the message and opens the form (empty = "what is going on
  here, and what is the disagreement?").
- Questions about the conversation itself (misunderstanding, disagreement, what is going
  on) are answered as each side's position, the crux, and the concrete fix.

## [0.6.40] - 2026-11-15

- `Plugin(dm_buttons=True)`: a plugin's buttons may be pressed in a DM with the bot (a
  preview DMed to the owner). A DM has no server, so the plugin is reached even when it
  is not default-enabled, and it must check who pressed itself.

## [0.6.39] - 2026-11-15

- `/poll` looks good now. It posts Discord's OWN poll (live percentages, emoji answers,
  a duration, optional multi-select) whenever it fits: 2-10 answers of up to 55
  characters and the bot holds SEND_POLLS in that channel (read from the interaction's
  `app_permissions`). Anything else -- anonymous, role-weighted (`vote_weights` in the
  community store), 11-20 answers, long answers, no SEND_POLLS -- gets the bot's own card:
  fixed-width Unicode block bars with right-aligned percent and count, the leader marked
  and its button green, each choice's emoji, vote counts on the buttons, "closes in"
  and "tap again to change your vote". When it closes the same message is edited into
  the final result with the winner called out; nothing else is posted. Input takes
  `option1`..`option10`, `q | a | b`, `q: a, b, c`, or opens a form when choices are
  missing instead of a usage error.

- A turn with an image always lands. A vision lane now gets a short probe
  (`AWDISCORD_VISION_TIMEOUT`, default 25 s) instead of the full agent timeout; a lane
  that failed is skipped by every turn in the process for `AWDISCORD_VISION_COOLDOWN_S`
  (default 300 s); when no vision lane answers, the text model answers and the reply's
  first line says the image was not read. A describe-this-image call
  (`ask(..., text_fallback=False)`) raises instead, so a text model never invents a
  description. A mention whose turn crashes replaces the "working on it" ack with a
  short failure line instead of leaving it unanswered.

- `awdiscord.contrib_events`: one shared shape for a contribution event (`ContribEvent`:
  kind, repo, number, head sha, GitHub login, member, agent, size bucket, clawback
  target) with an idempotent `id`, `size_bucket()` and `is_bot_login()`. Producers (a
  bounty poller, a pull-request intake bot) emit it; a points service pays it once.
- Orientation is now each server's own, and it reaches newcomers. Members of a
  partner server joined #general, posted "I'm a software engineer, willing to
  contribute", and nobody answered. Once a server admin runs `/orientation set`
  (Manage Server or Administrator in THAT server; an operator without it cannot):
  - `/orientation text` (a form) stores what the community is plus links, and
    `/orientation set repos:owner/repo claim:#channel` names its projects and the
    channel where a plan is claimed. With no repos set, the project coordinator's
    `AWDISCORD_PROJECT_REPOS` map (`guild=owner/repo`) is used. The text says where
    the projects are, to read their CONTRIBUTING.md and roadmap, and to claim a plan
    before starting. Only a server with none of that gets the generic text.
  - New members (the members intent, or Discord's "X joined" line in that channel)
    get ONE welcome: batched after a five-minute gather, at most one an hour, up to
    ten names, only those members mentionable, each member once. It replies under the
    join line when that line is in the orientation channel.
  - A member's first messages in that channel that read as an intro or an offer to
    help (keyword prefilter, then the server's model when it has one) get one short
    reply; a member already welcomed gets a link to the welcome instead.
  - `/orientation preview which:welcome|intro|post` shows the exact text privately,
    before anything is on. `welcome:` and `intros:` on `set` turn either part off.
  - Only NEW members are judged as intros: one the plugin saw join, or whose
    Discord join time is under a day old. "I'm a ..." needs a role noun ("I'm a
    bit confused" is not an intro) and a bare greeting must be the whole message.
  - Welcomes and intro replies need consent from this version's `/orientation set`
    (`schema` 2, `newcomers_by`); a config written by an earlier set, by anyone,
    keeps only its periodic post. A missing `welcome`/`intros` key is off.
  - At most one immediate intro reply per server every ten minutes; over budget the
    member is named in the next batched welcome instead. With welcomes off the
    member stays eligible, so their next message can take the budget once it frees.
  - The intro judge runs only after the reply budget is checked and is capped at
    three model turns per server every ten minutes (a raid of fresh accounts costs
    at most that). Its turn is a `partner`-surface, non-agentic turn scoped to the
    guild and channel, never the owner's private DM surface.
  - `GenesisAgent.ask(agentic=False)` sends a plain answer turn with no tool loop.
  - The welcome slot and each named member are claimed atomically before the post:
    a timeout after Discord accepted it never reposts, a 4xx rolls back for a retry,
    and an intro arriving mid-post gets no second message.
- `Plugin.wants_messages(host, guild)`: an optional gate for `on_message`. A plugin
  that is on but has no work in a server no longer moves that server's messages
  onto the hook route (orientation uses it, so unconfigured servers stay IGNORE).
- A plugin hook's message now carries Discord's message `type` (7 = member joined)
  and the author's `joined_at` (epoch seconds, 0 unknown).
- `Ctx.modal` fields take `value`, so a form opens with what is saved.

- The agent knows the server's own custom emoji and stickers (`awdiscord.expressions`):
  a small per-guild prompt block (40 entries max, most relevant first, role-restricted
  and unavailable ones skipped) with usage rules, cached 10 min, empty on any REST
  failure. A `[sticker:name]` marker in a mention reply sends that sticker as
  `sticker_ids` (one max; unknown names dropped; a refused sticker never loses the
  answer). Operators add meanings under plugin store `expressions`/`meanings`.
- Mentions are answered where they were asked, as a reply (`awdiscord.quiet`);
  `AWDISCORD_CONTAIN_MENTIONS=1` brings back the bot-channel relay. A durable mute per
  channel or thread ("remove yourself from this chat" / "mute"; "unmute" by the setter
  or an admin) survives restarts; a muted mention gets one private notice, no public
  reply. Replies drop curt stock lines and, in public, the bot describing its own model
  or serving stack. A "make a room called #x" ask runs the `server_room` plugin tool.
- `mirror.feed()` itself refuses an event whose guild is not in `AWDISCORD_MIRROR_GUILDS`
  (default the home guild); DMs never cross. `mirror.stats()` reports refused counts per
  guild.
- The Discord<->Relay mirror is fed from the gateway: every message, edit, delete,
  reaction, thread and channel update of a mirrored guild is queued for the relay
  bridge (`awdiscord.mirror`). Only guilds in `AWDISCORD_MIRROR_GUILDS` (default: the
  home guild, `AITHER_DISCORD_HOME_GUILD_ID`) cross; DMs never do; `off` disables it.
- A plugin can add a message context-menu command ("Apps" on a right-click of a
  message): `Command("Save to my Space", "", handler, message=True)`. The handler reads
  the message from `ctx.target_message` (Discord's signed copy, so the member could see
  it). Any member may use it; the plugin's per-server on/off switch and the rate limit
  apply as for a slash command. The three built-in "Aither: ..." message commands are
  unchanged and stay operator-only; a plugin cannot take their names.
- `awdiscord.intake`: a plugin can take a message addressed to the bot (a DM, or a
  mention) before the chat brain does. `intake.register(fn)`; `fn(event)` posts its own
  answer and returns True when the message was its. A DM from someone the operator
  serves but who is not the operator used to end at the bring-your-own-model card and
  then at silence; a plugin that knows the sender can now acknowledge and file it.
  With nothing registered, nothing changes.
- `/orientation post` no longer shows "The application did not respond". The command
  made three Discord calls before answering, which overran Discord's 3-second limit:
  the post landed and the admin was told it had failed. It now acknowledges first and
  fills in the private reply when the post is done; a failure is reported there too.

## [0.6.30] - 2026-11-12

- `/aither settings`: one private view of what the bot hears and answers in this
  server - whether it answers here, whether it joins voice calls on its own, whether
  it reads messages that do not mention it, the wake rule in voice, who sees its
  answers, plus three more behind "+3 more". Every row is read through the function
  that enforces it and says who can change it. Buttons change the two a server owns:
  who sees answers (managers), and calls (managers can turn them off; only the bot's
  owner can turn them on). **History** shows who changed what and when; changes are
  kept per server (newest 200) and logged as `hearing_setting`. Nothing new listens:
  no setting was added and every default is unchanged.
- A command group can be shared: `Command("aither", ..., shared=True)` lets a later
  plugin's `/aither <other>` join it instead of colliding, so a pack's
  `/aither feedback` and the core's `/aither settings` are one Discord command. Each
  subcommand still obeys its own plugin's on/off state; overlapping subcommand names
  are still refused.

## [0.6.29] - 2026-11-12

- Calls that run themselves, opt-in per server (`/calls mode:on` by the bot's owner,
  or `AWDISCORD_VOICE_AUTO_GUILDS`; off everywhere else): the bot joins a voice
  channel when a person does, leaves when the last person does, and keeps notes for
  the whole call. The channel's chat is told in one line when notes start, with how
  to pause. `/calls` shows anyone what is on.
- Notes and answers coexist: while notes are taken, a sentence that starts with
  "Aither" is answered and recorded (it used to be transcribed instead of answered).
  "Aither, off the record" pauses the notes and "Aither, back on the record" resumes.
- Notes survive a restart: every line is appended to a journal under
  `AWDISCORD_STATE_DIR/callnotes`. A restart mid-call resumes the session; a call that
  ended during the restart is posted at the next start. Notes that cannot be posted
  stay on disk and are retried.
- At the end of a call the action items are filed through `callnotes.register_filer`
  and posted as a numbered list with owner and link (five, then "+N more"). An item
  that spends money, deletes data or publishes outside the organisation is marked
  for a decision and never done. The call's record goes to `register_keeper`.
- "Aither, create a task to ...", "remind Ann to ...", "ask Atlas to ..." file the
  instruction during the call and post the result in the chat.

## [0.6.28] - 2026-11-11

- Every tool call is logged as one JSON line on stdout (`event: tool_call`, turn id,
  tool, args summary, ok/error, ms), and the answered events and the reply's turn
  record which tools ran.
- A sentence claiming a search or tool failed is removed from the answer when no tool
  call in that turn failed; a real failure keeps it. New ground rules: blame a tool
  only when one returned an error, and never state hardware nobody gave.
- `PluginHost.tools_for(question=...)`: a `wants_caller` tool sees the asker's own
  words as `caller["question"]`, so it can tell what the person stated from what the
  model filled in.

## [0.6.27] - 2026-11-11

- `web_search`: a default search that FAILS (a timeout, then a 405 from the SearXNG-shape
  retry) now reaches the keyed-provider fallback too, instead of raising before it.

## [0.6.26] - 2026-11-11

- Who sees a command's answer is now a choice. Every answering slash command (`/ask`,
  `/share`, and every member plugin command) takes `private:true|false`, and that
  always wins. Otherwise the server default applies: `/settings visibility
  public|private|bot_channel` (Manage Server), stored as `core/reply_visibility`. The
  default is `public`, which answers where it was asked, for everyone; the bot-wide
  default is `AWDISCORD_REPLY_VISIBILITY`. `/settings` shows the current value.
- In `bot_channel` mode, `/ask` and `/share` asked elsewhere are posted publicly in the
  bot channel as their own message, mentioning the asker, who gets one private pointer.
  An ephemeral deferred reply makes every followup ephemeral, so the public answer is
  never a followup. With no bot channel, the answer is public where it was asked.
  Operator and admin commands stay contained.
- `web_search`: when the search service's default (keyless) pick comes back empty, the
  keyed providers in `AWDISCORD_SEARCH_FALLBACK` (default `exa,parallel`) are tried
  next. The keyless scrapers are bot-blocked upstream, and every search was returning
  nothing.

## [0.6.25] - 2026-11-11

- Voice: with two or more people in a channel only "Aither, ..." starts a turn (STT
  spellings "ether", "either", "aether" and a "hey" lead-in tolerated;
  `AWDISCORD_VOICE_WAKE=auto|on|off`, `AWDISCORD_VOICE_WAKE_WORDS`). Alone with one
  person every utterance is still answered.
- Voice notes: "Aither, take notes" / "record" transcribes every speaker (name + time)
  instead of answering and tells the text chat; "Aither, stop" posts a summary
  (decisions, action items with owners, open questions) and the full transcript as a
  file to the voice channel's chat. Notes live only in memory.
- Voice lookups: "Aither, look up X" / "check the knowledge base for X" runs off the
  voice loop and posts the answer to the chat; the call keeps going.
- A model with no answer after `AWDISCORD_VOICE_STUCK_S` (8) seconds is said out loud
  instead of left as silence; a failed turn says so. The turn log adds queue_ms,
  plan_ms and first_token_ms.
- Streaming voice turns no longer die with TypeError on an older awvoice whose
  SentenceChunker lacks `first_clause_chars`.

## [0.6.24] - 2026-11-11

- A busy model queue is waited out instead of asking the member to ask again.
  `/ask`, message actions and mentions retry an agent timeout or a "lane is full /
  retry after" refusal for up to 14 minutes (`AWDISCORD_PATIENCE_S`); a deferred
  `/ask` edits in one "still working" line, then the answer. Only a real failure,
  or a queue full for the whole wait, gets a message, and it says what failed.

## [0.6.23] - 2026-11-11

- Every chat turn names the asker: the agent's context carries the author's Discord
  id and display name, plus what any plugin's new `turn_context(caller)` hook knows
  (an account-link pack names the linked account). Asked "do you know who I am?",
  the bot no longer says it cannot tell who sent the message.

## [0.6.22] - 2026-11-11

- Asked "what model are you?", the bot named nothing ("I don't have a specific
  model name to share"): the ground rules forbid inventing specifics and the
  system prompt never named the model. The system prompt now carries one line
  naming the model this turn was sent to, what the scheduler last reported
  serving (`last_route`) when that differs, and the image models.

## [0.6.21] - 2026-11-09

- The join flow's next step no longer tells a new member to type `/claim` or
  `/find-work`, which were never commands: it points at `/gh untracked` and
  `/gh board` and says to comment on the issue to take it. The template guide
  lists the real GitHub commands. A test now fails if member-facing text names
  a command that is not registered.

- Startup logs `version_drift` (code vs installed metadata) when an overlay
  image updated the code but not its dist-info, so `pip show` misreports.
  The overlay build's `sync_dist_info.py` step fixes the metadata as the
  last step of every overlay build.

## [0.6.20] - 2026-11-08

- A verified project card can carry the badge with no public image host:
  `AWDISCORD_VERIFIED_BADGE_FILE` (a PNG under 1 MB) is attached to the post and
  shown as the card image. `Rest.request_files` sends any JSON call with files.

## [0.6.19] - 2026-11-08

- `join` follows the published join contract (occupyxaitherium `join/README.md`):
  a code-of-conduct button and an optional time zone; availability options are
  the contract's (a few hours, evenings, weekends, full time). The bot-channel
  card carries name, number and archetype only, never a member's answers; the
  full card goes to their DM once. Optional private join log
  (`/enlist action:set key:join_log`): one line, who/squad/number. A failed
  number allocation still assigns every role and says the number is pending.
  An archetype role is only removed on a retake if the bot granted it. Squad
  channels are created only for squads with a Squad Lead.
- `awp:join:project:<slug>` opens the form with that squad preselected (for
  project cards that a server template posts).

## [0.6.18] - 2026-11-08

- Three shipped layouts turn a server into a software dev and deployment business:
  `dev-business-minimal`, `dev-business-full`, and `partner-aitherium-section` (ONE
  "Aitherium" category inside somebody else's server: #bot, #join and a tagged
  verified-projects forum; nothing else there is touched). `/template
  plan|apply|names` (a server's own admins, or the bot's operators) and a one-time
  arrival card show the plan first and create it on Confirm. Additive only; a missing Manage Roles or
  Manage Channels is named before anything is written. `/template project` (operators only) posts a
  project's card ONLY when the Aitherium Verified table lists it
  (`AWDISCORD_VERIFIED_PROJECTS`), with that table's fields (repo, verified date,
  report, looking for contributors) and the badge from `AWDISCORD_VERIFIED_BADGE_URL`;
  a project seeking contributors also gets a "+ Join as contributor" button
  (`awp:join:project:<slug>`), a squad role with no permissions and a squad in the
  join form. See `TEMPLATE.md`.
- Layout role names come from `awdiscord.orders` and the server's choice is the join
  config's `name_set` (`professional` by default, `themed` opt-in). A partner section
  never uses the themed names.
- The starter layout no longer creates generic roles: Founders / Builders / Friends
  are now Aitherium Steward / Engineer / Partner (Fifth Ideal / Knight Radiant /
  Ally of the Order when the server chose themed), and
  `#founders` is `#stewards`. Existing servers are not renamed.
- `guildspec`: a forum's `tags` are created with it (`available_tags`), and
  `validate_spec` refuses more than 20 tags or a tag over 20 characters.

## [0.6.16] - 2026-11-08

- The bot makes its own `#bot` channel: at startup and when added to a server,
  if containment finds none, it creates `bot` (under a "bot"/"aither" category
  when there is one) and re-resolves. Only in servers the owner owns unless the
  server opts in (`core`/`auto_bot_channel`); off with
  `AWDISCORD_AUTO_BOT_CHANNEL=0`. Without Manage Channels it posts one line to
  the system channel and logs it. It never renames, moves or deletes a channel.
- `join`: a 10-scenario archetype assessment (select menus, 3 pages). Each
  answer weights two capability axes; the top pair is one of ten archetypes.
  The archetype role and the umbrella (agent-access) role are given together.
  Retake after 30 days; stored with the join record and in the CSV export.
- `join`: the five-ideal ladder (Member, Contributor, Certified, Lead,
  Steward) read from an append-only Oath Log with evidence links. 2nd = a
  merged PR by a VERIFIED GitHub account (`/verify-github`) or a proven deploy
  (`/prove-deploy`); 3rd = 2 peer approvals at the 3rd ideal+ or an operator
  (`/review approve`, `/certify`); 4th = a squad nomination + 3 mentees at the
  2nd ideal + an operator (`/nominate`, `/mentor`, `/ideal confirm`); 5th = a
  council of 3 + 6 months + adoption evidence (`/council vote`). `/progress`
  shows the next requirements with a bar; 90 quiet days = dormant, never a
  demotion. Nothing is self-granted.
- Two name sets per server (`name_set`): `professional` (default; Aitherium
  Academy: Agentic Engineering, `ACAE: <Track>`, umbrella Aitherium Engineer)
  and `themed` (Order of Aitherium, First..Fifth Ideal, Knight Radiant). All
  strings live in `awdiscord.orders.NAMES`. `radiant` gains the Fourth and
  Fifth Ideal roles and exposes the professional archetypes.

## [0.6.15] - 2026-11-08

- New built-in plugin `join` (off until a server enables both the plugin and
  its join config): `/enlist` opens a two-step form (four pickers, then a
  modal), gives every joiner a member number and the first `cohort_cap`
  (default 100) the Founding Cohort role, creates and assigns track and squad
  roles, and posts a welcome card to the bot channel and the member's DMs.
  `Certified <track>` and `Squad Lead` are granted only by `/certify` and
  `/lead` (server managers, audited). Managers get `status` (permission gaps
  in words), `setup`, `post`, `export` (CSV by DM), `digest`, `layout`, `set`.
- `GuildStore.update` / `ScopedStore.update`: an atomic read-modify-write of
  one key, used for the cohort number.

## [0.6.14] - 2026-11-08

- The bot channel's name matches tolerantly: case, `#`, emoji and punctuation
  are ignored, and failing an exact `bot` a channel whose name holds the word
  (`bot-chat`) counts, the shortest first. Categories and voice channels never
  count. A created, renamed or deleted channel drops the server's cached answer.

## [0.6.13] - 2026-11-08

- Containment hardening: a relayed answer's header and first part travel as one
  message (no orphan "asked in" line, a long answer overflows instead of being
  cut); a failing later DM part or continuation is logged, never re-posted in
  the original channel; a network error on the reaction or typing call never
  loses the answer; a reaction answer falls back in place when the bot channel
  refuses; a thread inside the bot channel counts even when its parent is not
  cached; a server without a bot channel still gets its setup card in the system
  channel.

## [0.6.12] - 2026-11-08

- Output containment: the bot talks in each server's bot channel (the text
  channel named `bot`; `AWDISCORD_BOT_CHANNEL` = an id or name; a server's
  `core`/`bot_channel` store setting wins). Outside it a slash command's reply is
  ephemeral, and a mention is answered in the bot channel (the asker mentioned,
  with a jump link) while the original message gets a reaction. A server with no
  bot channel gets the answer by DM; DMs closed = the original channel. The setup
  card goes to the bot channel instead of the system channel. An operator who
  sets `/ask private:false` explicitly keeps a public reply.
  `AWDISCORD_CONTAIN=0` turns it off.

## [0.6.11] - 2026-11-08

- The host's address is replaced by a decoy instead of a "[redacted address]"
  marker: one address per process start, drawn only from the documentation
  ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), so it
  never points at a real third party. `fetch_url` on a "what is my IP" service
  answers with that decoy (never contacting the service) instead of refusing.

## [0.6.10] - 2026-11-08

- The bot no longer hands out its host's own address. Every reply has the host's
  public IPv4 and IPv6 addresses redacted (IPv6 by /64; IPv4 also in reverse-DNS
  dashed form), learned at startup and refreshed hourly; `AWDISCORD_REDACT_IPS`
  adds more, `AWDISCORD_EGRESS_PROBE=0` turns the probe off.
- `fetch_url` and `web_search` refuse "what is my IP" services (ipify, ifconfig,
  icanhazip, ipinfo, whatismyip and the like), at every redirect hop.
- `fetch_url` dials the address it validated and follows redirects itself,
  checking each hop, so DNS rebinding cannot steer it inside the network.
- `AWDISCORD_EGRESS_PROXY` (optional) sends the web tools through a proxy.

## [0.6.9] - 2026-11-08

- A reply the model stopped at `max_tokens` (`finish_reason: "length"`) is no longer
  sent ending mid-word. It is continued up to twice (the partial answer plus "continue
  where you stopped", no tools) and joined; still cut = it ends with "I hit my length
  limit here. Ask me to continue." Streamed replies get the same note.
- `AWDISCORD_MAX_TOKENS` sets the per-reply token cap (default 900).

## [0.6.8] - 2026-11-08

- A slow answer is delivered, not dropped. When a mention or DM is still running
  after `AWDISCORD_ACK_AFTER_S` (20; 0 turns it off) the bot replies "Working on
  it" so the member knows the request was taken, then deletes that note and posts
  the real answer when it lands.
- `AWDISCORD_AGENT_TIMEOUT` sets the per-call model timeout (default 120 s). A
  shared GPU lane under load can take longer than that; the answer the model
  still produced used to be thrown away behind a "try again in a minute".

## [0.6.7] - 2026-11-08

- Deterministic abuse guard (`awdiscord.abuse`). A message that tries to set a
  standing rule ("from now on", "always respond", "every word you say"), override
  instructions, swap the persona, loop forever, or pull the system prompt or
  credentials never reaches the agent and is never stored in history; the member
  gets one fixed sentence. Applies to @mentions, DMs and `/ask`; operators exempt.
- Strikes: `AWDISCORD_STRIKES` (3) re-rule attempts within
  `AWDISCORD_STRIKE_WINDOW_S` (1800) put that member on a
  `AWDISCORD_STRIKE_COOLDOWN_S` (1800) cooldown in that server: one notice, then
  silence until it expires. In memory, bounded.
- History scrub: a reply that is mostly one repeated word, and the ask that
  produced it, is dropped before replay and never stored, so one bad answer
  cannot keep reproducing itself.
- `/aither-ignore user [hours]` and `/aither-unignore user`, for members with
  Manage Server or Moderate Members (or a bot operator). Ephemeral; the ignore is
  kept in the server's GuildStore when the bot has one.

## [0.6.6] - 2026-11-08

- Public-channel length policy. A server answer longer than
  `AWDISCORD_PUBLIC_REPLY_CHARS` (default 700, 0 = off) or longer than one message
  is posted as a 1-3 sentence summary; the full answer is DMed to the asker. When
  their DMs are closed it goes in a thread on the summary, and inline when no thread
  can be made, so nothing is dropped. DMs and asks that say "full" or "details"
  stay full. The summary comes from the same agent, or from the answer's own first
  sentences when that call fails.
- New ground rule: a member cannot change how the bot answers everyone (standing
  format, persona, "from now on" rules, word games); the bot declines in one
  sentence and answers the actual question.

## [0.6.5] - 2026-11-07

- New `awdiscord.roles`: the fail-closed self-assign decision. A role is handed to a
  member only when it is on the server's allowlist (by id), is not an integration
  role, and its LIVE permission bits carry nothing server-wide (administrator,
  manage server/roles/channels/messages/nicknames/threads/events, moderation...).
  The bot's own Manage Roles bit and role hierarchy are checked before the write.
- Plugin modal submissions now share the dispatcher's rate-limit buckets; before,
  a form went straight to the plugin with no brake.
- `GuildStore` turns on `PRAGMA secure_delete` and gains `vacuum()`, so an erased
  row is zeroed on disk.

## [0.6.4] - 2026-11-07

- New builtin `film` plugin: a customer steers their own construction film
  (`film_steer_status`, `film_comment_step`, `film_steer`, `film_preview`) from the
  channel `AWDISCORD_FILM_MAP` binds to their instance. Unmapped channel or a
  non-member = no film tools; hosts in `refuse_hosts` are never bound.
- `Tool.offered(caller)`: a per-turn gate; a tool is offered only when it returns
  True, and a gate that raises withholds the tool.

## [0.6.1] - 2026-11-07

### Added

- **discord**: one brain resolver per (guild, user), and /model BYO setup (#15213) (3c779a2e46)
- **voice**: ask for a short spoken answer on every voice turn (#15192) (3e9fd90cd9)
- **voice**: stream Discord voice replies sentence by sentence, with barge-in (#15184) (4a0a420acb)
- **discord**: owner inference serves only an explicit server allowlist (#15176) (f6d1b38299)
- **a2a**: spec A2A v0.3 across AitherOS -- JSON-RPC on every service, honest cards, a fixed registry, A2A in Discord, an authenticated /a2a (#15136) (e15423d8bb)
- **awdiscord**: /artifact - saved HTML pages as Discord cards, with live autocomplete (#15081) (926fb6c8ec)
- **discord**: self-serve server onboarding -- join card, /aitherium setup, bot-claim binding with consent, management-permission tier, starter layout (#14970) (4b8965fc68)
- company stack manifest, full-tool voice turns, Strata tool, brain probe, shell gate (#14860) (2afaa5e390)
- **discord**: a plugin starts its own daemon at boot (#14833) (7d48d81843)
- **discord**: a founder's voice note files itself -- transcript, tasks, agent, thread (#14814) (0f787d8154)
- **awdiscord**: two-stage trigger filter, CANDIDATE route, silent speculative hand-off (#14790) (27fd232746)
- **voice**: one shared listen-think-speak loop; Aither joins Discord voice (#14787) (a17e2c1bc7)
- **discord**: read every attachment in, and hand files back out (#14777) (c5c53f9dec)
- **models**: reported == served -- page a dead model lane, park posture-owned rows, Bonsai 2 defaults (#14728) (2b534e5fb5)
- **artifacts**: publish_artifact + share_file -- content-addressed links, never host paths (#14701) (03f9685624)
- **discord**: a deep_research tool, and the persona stops asking for short answers (#14697) (dc570aa26f)
- **discord**: the learning loop -- rate/correct/teach Aither's answers in Discord (aitherium_feedback pack, awdiscord 0.4.0) (#14675) (6c111b5f40)
- **discord**: Aither orchestrates -- delegate media to Iris + media-forge, tasks, reminders (#14671) (6df8ff795d)
- **discord**: all 48 platform agents as webhook personas on the one bot (#14635) (e0f3a2ce20)
- **discord**: Collective onboarding -- welcome DM, signed signup link, newsletter opt-in (#14623) (e56fa04bec)
- **discord**: land the collective work board on develop, mount every collective pack, fail closed (3d9bc7acc3)
- **awdiscord**: opt-in AWDISCORD_AGENT_PRIORITY sends metadata.priority (3be4fac2d1)
- **discord**: Aitherium Collective server layout lives in the repo (#14599) (af6d2ccf89)
- **discord**: collective core - /link, /whoami, channel map, role sync for the official bot (a9a84231ba)
- **awdiscord**: operator ops commands (/today /alerts /signups /issues /reports) + ops channel (#14596) (a5961e7fe5)
- **awdiscord**: Knights Radiant role provisioner (10 orders, 3 ideals, umbrella) (#14591) (dbf61f3747)
- **awdiscord**: read images, videos and text files sent to the bot (#13333) (98855289a6)
- **awdiscord**: the official Discord bot's source lands on develop (#12978) (d272ee24ed)

### Fixed

- **awdiscord**: no fleet container names in a shipped test (AWDST007) (#15211) (1adab06d03)
- **awdiscord**: no customer name in a shipped test comment (last sync blocker) (#15206) (29b0c9f9f5)
- **awdiscord**: no monorepo path in a shipped test (unblocks the public sync again) (#15204) (217d2d604a)
- **awdiscord**: drop a customer name from the system prompt (unblocks the public sync) (#15188) (a1b8ec96ad)
- **voice**: a second join no longer leaves Aither deaf in the channel (#15180) (fd6da414d0)
- **voice**: close an utterance when Discord stops sending, so Aither answers in voice (#15120) (d4431d16e0)
- **awdiscord**: voice actually hears -- davey <0.2, unmapped speakers, plaintext silence frames (#15110) (f0d693a789)
- **awdiscord**: voice join awaited a sync bool; /artifact index survives restarts (#15092) (6b7a985296)
- **discord**: brain carries product docs; search before asking; capability discovery (#14953) (1afccff6f4)
- **awdiscord**: retry a transient 502/503/504 or refused connection instead of dropping the question (#14918) (4add81ee04)
- **awdiscord**: drop a peer agent-voices hunk that #14860 swept in (awvoice.voices is not on develop) (#14864) (c276f757e1)
- **discord**: a voice note that times out is queued, never lost (#14841) (5040fd57df)
- **awdiscord**: DAVE-decrypt inbound voice before Opus decode -- bots heard nothing (#14834) (8fea947642)
- **awdiscord**: Plugin declares on_ready beside on_reply/on_reaction (#14825) (a1e24c20d5)
- **aw***: gen_aw_doctor --check clean -- awask/awpermit get the intercepts, awdiscord/awnode lose the stale one (#14821) (9ce1eccccf)
- **awdiscord**: answer from what the tools found instead of "kept calling tools" (#14819) (050ce4f73c)
- **discord**: long replies continue instead of truncating; never print host paths (#14694) (89e8d8c17a)
- **discord**: orchestrator least privilege -- per-author history, fail-closed roles, local-only member forge runs, job cap (#14677) (5722e936e8)
- **chat**: never post tool-call markup, fragments or raw search dumps as replies (#14674) (24fe1f3302)
- **brain**: company brain answers linked members only, notes are data, sources must be retrieved, seed from HEAD behind an approved manifest (#14659) (08bacfafe1)
- **discord**: gate the agent loop and cockpit platform reads on operators; tenant + human actor on CRM writes (#14658) (c4d50dbe42)
- **discord**: owner @mentions answered from the company brain; /draft reads the Collective (#14648) (527a009cc7)
- **growth**: signup/subscribe owner DMs reach Discord again via awdiscord /notify/owner (#14558) (56ad50f096)
- **discord**: ground rules on every public turn — no invented specifics, no action offers to non-owners (#13920) (1115cfe0f3)
- **awdiscord**: the owner agent presents the fleet key, so Genesis stops metering it as a free user (#13419) (0182e14f25)
