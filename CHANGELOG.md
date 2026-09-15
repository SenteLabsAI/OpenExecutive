# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

[Unreleased]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SenteLabsAI/OpenExecutive/releases/tag/v0.1.0
