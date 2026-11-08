# Changelog

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
