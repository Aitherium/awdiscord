# Changelog

## [Unreleased]

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
  The monorepo's `.DEPLOYMENT/awdiscord/sync_dist_info.py` fixes the
  metadata as the last step of every overlay build.

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
