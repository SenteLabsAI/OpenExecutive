# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From 0.3.0 on, entries are written by release-please from the titles of the
merged pull requests (`feat` → Added, `fix` → Fixed). The open release PR holds
the next entry; edit it there before merging if a line needs rewording.

## [0.3.0] - 2026-09-23

### Added
- **Attunement: the Executive follows up on what people owe** (#175). When
  anyone on the roster commits to something in chat ("I'll send the vendor
  quote Thursday"), asks for something, or the principal says a teammate will
  do something, it becomes an open loop. Loops come due, get chased by the
  nudge engine through the usual routing and outbound checks, and close when
  their owner says it's done. The Executive can list them ("what is Sara
  waiting on?") and close one on request. Each person's page lists their open
  loops with Mark done. Every chat message now records who actually sent it,
  so nothing an outsider writes is read as someone on the roster.
- **👍/👎 on replies** (#175), in the main chat and the Ask OE panel.
- **Attunement: learning which proactive messages land** (#176). Every
  proactive DM (nudges, follow-ups, reflection, research and alert-review
  messages) is resolved as replied, acted on, ignored after 72 hours, or void
  when it stopped mattering. Credit only goes to the person who acted. A kind
  of nudge a person's last five resolved sends all went unanswered on ranks
  last for them on a longer cooldown until they answer one. The morning
  reflection gets a "What lands" summary, and each person's page a "How they
  respond" card.
- **Attunement: per-person working style** (#177). Up to four short rules on
  how to write replies for each person ("lead with the recommendation, then
  the numbers"), learned only from their own messages and 👍/👎 and pinned
  into their own conversations. Rules are checked before they are stored and
  every time they are used, and must be about how replies read, never an
  action. A "How I work with them" card lets the principal or the person
  edit, lock or reset them.
- New settings, all with defaults: `ATTUNEMENT_*` in `.env.example`.
  `ATTUNEMENT_ENABLED=false` turns off open-loop tracking and style learning;
  the outcome ledger has no switch and always records. None of it needs
  Honcho.

## [0.2.2] - 2026-09-22

### Changed
- **The MCP gateway runs a pinned extensible-mcp commit.** It was launched from
  the repo's default branch, so every container start ran whatever that
  branch held that day: a versioned image did not pin its gateway, rolling
  back an image did not roll the gateway back, and a start without network
  failed. The gateway and the image's pre-warm now launch the same commit
  with the same `--exclude-newer` cutoff, which also freezes extensible-mcp's
  own dependencies (uvx re-resolved those against PyPI on every start), and a
  unit test keeps the two in step. An image whose pre-warm succeeded now
  starts the gateway without network; the pre-warm is still best-effort, so
  a build during a GitHub outage ships an image that fetches it at first
  start. Updating the gateway is a deliberate bump of the commit and cutoff.

### Fixed
- **The compose health check no longer fails on a healthy API.** It runs
  `curl -f http://localhost:8000/health` inside the API container, but the
  image never installed `curl`, so the check failed on every run and compose
  reported a working API as unhealthy. The API image now includes `curl`.
- **The People tab shows the newest notes about a person** (#174). Its
  "recent" list and "learned" date asked Honcho for conclusions with
  `reverse=True`, which returns the oldest page, so a person's correction
  made in chat never appeared there. It now reads the newest page.

## [0.2.1] - 2026-09-22

### Changed
- **The API image installs the CPU-only build of torch.** torch is only
  present because `sentence-transformers` needs it, and the container runs on
  CPU hosts, but the default Linux wheel is the CUDA build and pulled in
  nineteen packages the image never used — fifteen `nvidia-*` libraries,
  three `cuda-*` shims and triton, about 2.2 GB of compressed wheels. torch
  now resolves from PyTorch's CPU index (2.13.0 → 2.14.0+cpu on Linux, plain
  2.14.0 on macOS, both from that index) and the Dockerfile installs from the
  lock with `uv sync` instead of an exported requirements file, so each
  package comes from the index the lock names. Expect the API image to shrink
  by several GB and cold builds and pulls to get much faster. `uv sync` gets
  the same wheels locally; a GPU deployment would need to override the index,
  and a plain `pip install` of the package (which ignores uv sources) still
  gets the CUDA build.

## [0.2.0] - 2026-09-22

### Security
- **The sign-in allow-list is now the union of `ALLOWED_EMAILS` and the People
  roster, not the roster alone** (#132). The UI previously treated the roster
  as authoritative as soon as it held one email, so any Person row with an
  address silently disabled `ALLOWED_EMAILS` — a fixture load, which wipes
  `people` and inserts its own addresses, could lock the configured operator
  out of their own instance. **This widens access on upgrade:** every address
  still sitting in `ALLOWED_EMAILS` regains sign-in even if that person is not
  on the roster, so audit the env var before deploying. Removing someone now
  means removing them from *both* the roster and `ALLOWED_EMAILS`. The
  `authorized` callback's fail-open also narrows: it used to admit any valid
  session whenever the roster fetch failed, and now admits only a session that
  is not in `ALLOWED_EMAILS` and whose roster membership is unreadable.
- `packages/ui`: force `lodash-es` to 4.18.1 via an npm `overrides` entry
  (GHSA-r5fr-rjxr-66jc code injection in `_.template`, GHSA-f23m-r3pf-42rh
  prototype pollution in `_.unset`/`_.omit`). The vulnerable 4.17.23 was pinned
  exactly by `chevrotain@11.1.2` underneath `mermaid@12.0.0`, and no mermaid
  release moves off it.

### Removed
- **Talent / executive search.** The whole vertical is gone: the `talent`
  specialist, `openexecutive/talent/` (engagements, candidates, offers, the
  ChromaDB matching graph), its 21 REST routes, 12 chat tools, 4 MCP tools and
  the `/talent` UI, plus the five recruiting workflows (`candidate_screen`,
  `candidate_outreach`, `interview_coordination`, `reference_check`,
  `offer_approval`). The `chro` specialist and its `comp_refresh` /
  `org_design` / `performance_review` / `exec_search_brief` workflows are
  unchanged — `exec_search_brief` is an advisory hiring brief, not pipeline.
- **Staff onboarding.** `openexecutive/staff_onboarding/` (templates, per-hire
  plans, tasks), its REST routes and 8 chat tools, the `new_hire_onboarding` and
  `role_onboarding` workflows, the `/staff-onboarding` UI, and the
  `onboarding_ramp` / `onboarding_kickoff` / `onboarding_checkin` scheduler
  kinds. **The company-setup wizard (`/onboard`) is untouched** — it is a
  different subsystem that happens to share the word.
- `GET /today` no longer returns the `talent` or `onboarding` fields, and the
  per-turn chat briefing no longer includes their digests.

  Existing SQLite tables (`engagements`, `candidates`, `offers`,
  `onboarding_templates`, `onboarding_plans`, `onboarding_tasks`) are **not
  dropped** — nothing reads them, and a blank client slot still wipes them so
  candidate data cannot cross slots. Pending talent reminders the removed
  workflows had scheduled on the principal's DM channel are cancelled by a
  one-time startup sweep (`cancel_orphaned_talent_reminders`, recorded in the
  new `app_migrations` table); the sweep can be deleted in the release after
  next.

### Changed
- **The chat turn ceiling is now 300s (360s with Committee review)**, up from
  120s/180s: deep multi-specialist turns were being cut off mid-answer. A
  ceiling this generous is only reasonable because a turn can now be ended by
  the user, so the two changes ship together. The onboarding interview, which
  previously borrowed `CHAT_STREAM_TIMEOUT_S`, gets its own
  `INTERVIEW_TIMEOUT_S` (still 120s) — it retries twice, so inheriting the new
  ceiling would have meant a 10-minute hang before the wizard surfaced a
  timeout.

### Fixed
- **A turn broken off early is no longer missing from the next turn's
  context.** On the disconnect and timeout paths the route persisted the
  partial turn to SQLite, but the Executive's post-turn block — which mirrors
  it into the live in-memory session — was skipped, and a cached session never
  re-reads its history from the DB. The next turn in the same process then
  prompted as though the turn had never happened, while a page reload showed
  it. The route now mirrors the turn itself on every broken-out path.
- **The UI proxy now forwards client disconnects upstream.** `signal:
  req.signal` was missing from the backend proxy's `fetch`, so the API never
  saw `http.disconnect` and its `request.is_disconnected()` check could not
  fire in production: closing a tab left the turn running to completion against
  Anthropic and the partial reply was never saved.
- **A disconnected turn no longer keeps working after the client is gone.**
  The SSE driver races the stop switch with `asyncio.wait`, which — unlike the
  `asyncio.wait_for` it replaced — does not cancel its futures when the task
  awaiting it is cancelled. Since Starlette cancels the response body on
  `http.disconnect`, the in-flight step is now cancelled in a `finally`, so a
  closed tab cannot leave a specialist or tool round running with no deadline
  and no persistence.

### Added
- **Versioned container images on GitHub Container Registry** (#142). Every
  push to `main` publishes `ghcr.io/sentelabsai/openexecutive-api:main` and
  `…/openexecutive-ui:main`; pushing a `vX.Y.Z` git tag publishes `X.Y.Z`,
  `X.Y` and `latest`. Deployments can pull a pinned version instead of
  building from source. See `docs/deployment.md` → Images.
- **Stop button in chat.** A reply can now be halted mid-stream, from the main
  chat composer and the Ask OE side panel (Escape works too). Whatever the
  Executive had written is kept, persisted and marked *Stopped by you*, so a
  truncated answer is not read back as a complete one after a reload. The
  client mints a `client_turn_id` and sends it with the turn; `POST /chat/stop`
  halts it. Keying on a client-minted id is what makes the button live from the
  moment Send is pressed — the server spends several seconds fetching context
  before the response stream exists, and a stop inside that window costs
  nothing because no model call has been made yet. An unknown id and another
  caller's turn both return 404, so the endpoint cannot be used to probe which
  turns are live.
- **Conversational onboarding.** `/onboard` now opens with "tell me about your
  company" instead of a 12-step form. The user writes a paragraph (and can
  attach a deck, one-pager or brief), the new `onboarding_interviewer` agent
  asks up to 8 clarifying questions, then drafts a company profile, leadership
  roster and department list that the user edits inline and saves. The draft
  review reuses the `/company-profile` section editors, extracted to
  `components/company-profile/ProfileSections.tsx`, so the editing surface is
  the same one the user gets permanently afterwards. New endpoints:
  `POST /onboard/interview/{start,message,draft,commit}` and
  `GET /onboard/interview/{session_id}`.

  Design notes: the interview never writes — `POST /onboard/interview/commit`
  is the single write, ordered so a rejected save leaves the draft editable
  and retryable. Departments are reconciled **additively** (matched ones
  updated, new ones created, none ever deleted), unlike the fixture loader
  which wipes the table and would destroy the eight defaults' `specialist_key`
  wiring. Exactly one person is marked principal and gets `WILDCARD`
  authority; no contact details are imported. Every failure path is asserted
  not to echo the user's input, because these transcripts carry ARR, burn and
  runway. The step-by-step wizard remains at `/onboard?mode=form` and still
  backs the `openexecutive onboard` CLI.
- **Research watch policy grounds in departments and recent decisions, and
  routes to department heads.** Departments gain a `watched_entities` list
  (`PATCH /departments/{slug}`, edited one per line on the department page).
  Named there, an entity is strong grounding for the research watch policy —
  like a profile vendor or ticker — so a proposal about it can be added on
  its own, and the watch is inserted with `route_to_department` /
  `route_to_person_id` (the head). Those columns now actually route: every
  watch alert carries the department's head and a `department:<slug>` tag,
  so it queues on the head's briefing instead of the principal's. Charter
  scope phrases and goal key results ground suggestions only. Suggestions in
  a department's area go to its head as one "Watch suggestions for
  <Department>" card per run (refreshed in place, re-issued weekly after it
  is handled; the principal when the department has no head), and the
  principal's pile-up nudge counts only their own. The ten most recent
  episodic decisions from the last 90 days are rendered to the research
  council and add a point to a proposal whose entity they name (with the
  decision's department as a routing hint). The research fingerprint tracks
  department watch interests and the decisions that name a known entity, so
  a new watched entity or a relevant decision triggers the next scan.
  `/watchlist` shows "for: <department>" on routed rows.
- **Watch proposals are linked to their evidence, and profile entries ground
  by name.** `propose_watch` now requires `finding_index`, and when the model
  omits it the policy links the finding that cites the source itself instead
  of rejecting the proposal for lack of evidence. Profile competitor / vendor
  / ticker entries and department watched entities are parsed to their names
  ("Tesla (TSLA) — Model Y…" grounds as Tesla plus the ticker TSLA; "GM /
  Chevrolet (…)" as both), so short names such as BYD, GM or Kia match and
  the description text can no longer stand in for the entity.
- **Research watchlist policy — grounded watches go straight in, uncertain
  ones become suggestions.** The research council's watchlist pass no longer
  adds watches itself; its only tool is `propose_watch`, and deterministic
  policy (`monitoring.research.watch_policy`) decides from company data. A
  proposal tied to a named competitor, vendor, ticker, initiative or
  priority (the profile gains `vendors` and `tickers` for this) and
  corroborated (own source, high-confidence finding, consensus,
  the policy's own track record) is added on its own — quietly: daily
  cadence, medium severity floor, a keyword trigger for feeds, 5 % for
  stocks — and shows up in the brief's "handled overnight" block as
  *watching*. Anything the Executive is not sure about lands as a dry-run
  **suggestion** (polls, never alerts) in a "Suggested by the Executive"
  section on `/watchlist` with Approve / Decline (reason: not relevant, too
  noisy, wrong source); the morning brief mentions the pending count in one
  line, and a single nudge alert fires only once ≥3 suggestions have waited
  ≥7 days. Declines are remembered by target and enforced in the tool
  handler, so a declined source is never re-proposed under a new slug;
  "Stop watching…" on a research-added card, `remove_watchlist_entry` and
  `DELETE /watchlist/{slug}?reason=` on research rows record one too.
  Approvals, declines, expiries and auto-disables feed the policy's history
  and the research turn ("stock watches grounded in a competitor: 4
  approved / 1 declined"). Research-added watches retire themselves: the
  scheduler sweep disables one that fired ≥5 alerts with ≥2 dismissed and
  low trust, or one with 3 consecutive poll failures (audited; re-enable
  from `/watchlist`; silence alone never retires a watch).
  Endpoints: `POST /watchlist/{slug}/approve`, `POST /watchlist/{slug}/decline`.
  Settings: `WATCHLIST_RESEARCH_MAX_DIRECT_ADDS=2`,
  `WATCHLIST_RESEARCH_MAX_PROPOSALS=2`, `WATCHLIST_MAX_ENABLED=40`,
  `WATCHLIST_PROPOSAL_TTL_DAYS=14`.
- **Self-maintaining alert feed.** Alerts get a per-category time-to-live
  (`ALERT_TTL_DAYS_ACTION=14`, `ALERT_TTL_DAYS_MONITORING=3`); a scheduler
  sweep expires past-TTL rows (audited, reversible via
  `POST /alerts/{id}/reopen`) and every surface reads the same live view.
  A repeat of an open alert with the same `(source, dedup_key)` now
  coalesces into the existing card (`occurrence_count`, `last_seen_at`)
  instead of stacking, and only re-pings when severity rose to high/urgent;
  monitoring alerts carry a stable `watch:<slug>` key so one watch means
  one card. A distinct `resolved` status keeps Executive closes separate
  from `ack` (user approved).
- **Executive alert review** (`alert_review_scan`, every 6 h and right before
  the morning brief): re-examines each open alert with evidence — newer
  signals from the same watch, related alerts, activity since, the roster
  with SLAs, department authority — and, through deterministic policy,
  routes + DMs the owner (`propose_via_alert` for propose-only departments),
  nudges, escalates to the principal with a deadline, drafts an artifact,
  suggests a workflow, folds duplicates, or resolves with evidence (high
  confidence only, citing a server-minted evidence ref — free text never
  closes a card; otherwise annotates "likely stale"). Alert text is rendered
  as inert data inside the review prompt (injection boundary); board / comp /
  legal matters only ever go to a scope-holder or the principal and keep
  their text; route/escalate are idempotent across passes; DMs carry an
  "[Alert review] Re: …" header. Capped per pass
  (`ALERT_REVIEW_MAX_MOVES_PER_SCAN`), single-flight, off for every caller
  with `ALERT_REVIEW_ENABLED=false`, every move audited with its evidence and
  prior state, every close reversible. `POST /alerts/review` runs it on
  demand.
- Briefing cards read as decisions: what changed since you last looked, the
  recommended move as the primary control, why-now / due chips, provenance
  (age, seen ×N), "N folded in", a "Handled overnight" rail with Undo, an
  "Executive handled N overnight" pill, a capped "Needs you" list with
  Show more, "Dismiss older than 7 days" (explicit ids), "Re-check
  relevance", and "Mute this topic" on dismiss.
- `POST /alerts/bulk-ack`, `POST /alerts/{id}/reopen`, `POST /alerts/review`.
- Dismissing a watch-sourced alert lowers that watch's `trust_score` (and
  bumps `dismiss_count`); approving recovers it. Trust now discounts the
  ranking and is shown to the review as evidence.

### Changed
- **"Handled overnight" now says what the Executive actually did, and says
  it with some pride.** The briefing rail no longer lists the review's
  `changed` verdict (an "Updated '<old headline>' — …" row): that rewrite
  leaves the alert open in "Needs you", where the card already shows the
  note, so the row double-reported it and called it done. Rewritten items
  now render in the morning brief and end-of-day digest as a REWRITTEN block
  under "what changed" (`brief_state.rewritten_since`), and a rewrite alone
  still un-suppresses the brief. Each remaining `/today.handled_overnight`
  row is structured (headline, target, detail, outcome, evidence_ref,
  event_type, and the alert's `status` now) instead of one truncated
  sentence; routed / nudged audit summaries name the person rather than
  "person 12", and the nudge bookkeeping marker is stripped for display.
  The rail is now one line by default — a count-only first-person heading
  ("Since your last brief: N off your plate, N in others' hands, N waiting
  on you", plus drafts / suggestions / watch changes / other moves / back
  on your plate when present) — with the rows behind a "details"
  disclosure, and nothing at all on a night with nothing handled. The
  grouping and summary logic lives in `packages/ui/src/lib/handled.ts`
  and is covered by `npm test`. Expanded, rows read first-person
  ("Resolved *X* — evidence", "Dismissed *X* as stale — …", "Handed *X* to
  Dana Kim", "Chased Dana Kim on *X*"), escalations first, one row per
  alert, every row linking its evidence to a pre-filtered `/audit` (the
  audit page now reads `event_type` / `q` from the query string) and a
  still-open row jumping to its card. Undo is only offered while the close
  still stands; a close already undone shows "Reopened" after a reload.
- **Specialists are told what departments watch.** The research context
  every specialist receives now carries a `DEPARTMENT WATCH INTERESTS`
  block (each department's watched entities), and the grounding rule
  admits a department interest or a company named in a recent decision.
  A Finance head who lists Brex now gets Brex findings; before, the
  specialists were never told and their grounding rule dropped them. The
  per-specialist finding cap is enforced in the tool schema and the
  parser, not only in prompt text.
- **The research run has its own controls.** `RESEARCH_WEB_SEARCH_MAX_USES`
  (default 3) caps searches per specialist independently of the chat knob;
  `RESEARCH_SPECIALISTS` picks which of the seven specialists run; the
  routing pass can no longer start a workflow (`run_workflow` is withheld,
  `suggest_workflow` remains); the periodic run's default interval moves
  from 120 to 360 minutes.
- **Web search reaches every OpenRouter model.** The provider feature gate
  stripped the web-search tool for non-Claude models, so a research
  specialist or the Executive pinned to Gemini, GPT, Llama, DeepSeek or
  Grok ran without search (and the research prompt then told it to emit
  nothing). The tool is now kept for any OpenRouter model and replaced by
  OpenRouter's own `openrouter:web_search` server tool (the deprecated web
  plugin is no longer used); only a self-hosted OpenAI-compatible backend
  still strips it. The server tool carries the same contract as
  Anthropic's: the model decides when to search, `max_uses` caps searches
  server-side, the domain allow/block lists apply, and the search count is
  reported, so `RESEARCH_WEB_SEARCH_MAX_USES` and `WEB_SEARCH_MAX_USES`
  mean the same thing on both deployment types.
- **Per-run watch budgets go to the strongest proposals.** The policy
  classifies every proposal first and applies them by tier (direct adds
  before suggestions) and descending score, so when the model files more
  candidates than the budgets or the ceiling allow, a better-grounded
  proposal filed later no longer loses its slot to weaker ones filed
  earlier, and a duplicate target keeps its strongest filing.
- **Every model call is recorded.** Research specialists, the research
  routing and watchlist passes, triage and the chat memory extractor now
  write the same `cache_event` audit row the Executive's chat turns always
  did (tokens, cache hits, cost, and the server-side web searches the call
  made). A research run's result and its `watchlist_research_periodic_ran`
  audit row carry a `usage` block summing its calls per source, and
  `GET /audit/usage` (and the usage page) gain a by-source breakdown.

### Removed
- **The xcrawl scrape service and everything that depended on it.** The
  research run no longer has a post-dedup verify pass or an optional
  read-before-cite scrape loop, and the watch policy no longer scores a
  "verified" point; the `XCRAWL_*`, `EXTERNAL_RESEARCH_VERIFY_*`,
  `RESEARCH_AGENTIC_*`, `RESEARCH_SCRAPE_*` and `RESEARCH_LOOP_*` settings
  are gone (a deployment that still sets them is unaffected). Every
  watchlist fetch is the keyless, SSRF-guarded httpx fetcher: a page watch
  written under the old `fetch: xcrawl` marker re-captures its baseline on
  the next poll without reporting a change, and an `rss` target that is not
  a feed becomes a `page_watch` when the page already fetched by the feed
  check has readable text (otherwise it is rejected, as before).

### Changed
- The research routing pass no longer carries the watchlist write tools
  (`add_watchlist_entry`, `tune_watchlist_entry`, `remove_watchlist_entry`):
  every watch a research run creates goes through the watchlist policy.
  `tune_watchlist_entry` refuses `mode` / `enabled` changes on a pending
  suggestion, URL targets are SSRF-checked at insert time for every kind,
  and deleting a watch now removes its signal history in the same
  transaction (the foreign key previously made any polled watch
  undeletable).
- Watchlist rows carry an `origin` (`manual` | `executive` | `research` |
  `research_proposed`); the research skip-if-unchanged fingerprint hashes
  only enabled, active rows so approving or declining a suggestion never
  triggers a fresh 7-specialist run; the research turn shows each watch's
  origin, fired/dismissed counts and trust, plus declined targets.
- Daily briefs are bounded to "since the last delivered brief": what's new,
  what the Executive handled overnight, who we're waiting on, one top call,
  and a one-line carried-over count. An unchanged day sends
  "Nothing new since yesterday's brief — N items still waiting on you." and
  skips the model call (`PRINCIPAL_BRIEF_SUPPRESS_UNCHANGED`; `force_full`
  on a manual run bypasses it). The EoD digest now excludes monitoring noise.
- The daily reflection sees yesterday's standup and is told not to repeat
  it; the watchlist research council's forced re-run moves from daily to
  weekly (`WATCHLIST_RESEARCH_MAX_STALENESS_HOURS=168`); stalled-workflow
  nudges stop after `NUDGE_MAX_PER_SCOPE=3` per item.
- Alerts created by the Executive's `create_alert` tool now land with their
  `routed_to_person_id` and `department:<slug>` tag (previously every
  triage-born alert fell to the principal as "unrouted").

### Upgrading
- All schema changes are additive columns with defaults (`alerts` table;
  `watchlist.origin`, plus two new tables `watchlist_declines` and
  `watchlist_policy_outcomes`); rolling the image back is safe. No config
  changes are required.
- The research council now adds a watch on its own only when it is grounded
  in your company data. Name your vendors and tickers on the Company
  Profile page ("External Dependencies") so status pages and filings for
  them land without asking; everything else arrives as a suggestion on
  `/watchlist`. Existing watches are untouched (`origin=manual`) and never
  auto-disabled.
- After upgrading, the first scheduler sweep expires alerts past their TTL
  (audited as `alert_sweep`, reversible with `reopen`); raise
  `ALERT_TTL_DAYS_*` (or set `0`) beforehand to keep an old backlog. The
  review job starts acting within `ALERT_REVIEW_MIN_AGE_HOURS` (2 h) — set
  `ALERT_REVIEW_ENABLED=false` to turn it off, or
  `ALERT_REVIEW_MAX_MOVES_PER_SCAN=0` to keep it annotate-only.
- Briefs get shorter and send a one-liner on unchanged days
  (`PRINCIPAL_BRIEF_SUPPRESS_UNCHANGED=false` restores daily full briefs).
- `docker/docker-compose.yml` now binds the API to `127.0.0.1:8000` instead of
  every host interface. The UI reaches the API over the compose network, so
  nothing in the stack needed the public port. If you were calling `:8000`
  directly from another host, put a reverse proxy in front of it and set
  `BACKEND_SHARED_SECRET` and `OE_PUBLIC_DEPLOYMENT=1`.

## [0.1.0] - 2026-06-30

Initial public release.

### Added
- Multi-agent "Executive" system: a single coherent executive persona backed by
  eight specialist sub-agents, powered by the Anthropic Claude API.
- Python backend (`packages/core`) — FastAPI service, orchestrator, specialist
  agents, ChromaDB-backed RAG, CLI, and prompt-caching layer.
- Next.js 15 web UI (`packages/ui`), including the static `/architecture` page.
- Curated MBA knowledge base (`knowledge/`) and eval suite (`evals/`).
- Optional integrations: Slack, Discord, and email.
- Docker deployment configuration.
- Open-source project setup: Apache-2.0 license, contribution guide, code of
  conduct, security policy, issue/PR templates, and CI.

[0.3.0]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SenteLabsAI/OpenExecutive/releases/tag/v0.1.0
