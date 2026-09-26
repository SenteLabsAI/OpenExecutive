# Open Executive — Claude Code Context

## Project Overview

Open Executive is a multi-agent AI system acting as a virtual corporate executive. Python backend (FastAPI) + Next.js 15 frontend. The "Executive" is a single coherent persona backed by 8 specialist sub-agents, all powered by the Anthropic Claude API.

## Repository Layout

```
packages/core/          Python: all agent logic, API, CLI
packages/ui/            Next.js 15 web UI
knowledge/              Curated MBA knowledge base (git-tracked Markdown)
evals/                  Eval scenarios + LLM-as-judge runner
docker/                 Dockerfile + docker-compose.yml
.github/workflows/      CI + eval pipeline
```

## Key Commands

```bash
make dev          # Start FastAPI (port 8000) + Next.js (port 3000)
make test         # Run pytest
make lint         # ruff check + mypy
make check        # everything CI checks: lint, unit tests, UI build (if touched), PR rules
make eval         # Run eval suite against localhost
make docker       # docker compose up --build
```

## Python Setup

Uses `uv` for package management.

```bash
cd packages/core
uv sync
source .venv/bin/activate
```

## Architecture — How the Agent System Works

1. User message arrives at `Executive` (orchestrator in `orchestrator/executive.py`)
2. Executive uses Anthropic tool use to call `consult_specialist` for relevant domains
3. For cross-domain questions, multiple specialists are called in parallel
4. Each specialist:
   - Gets its domain system prompt from `prompts/domain_prompts.py`
   - Retrieves relevant chunks from ChromaDB (built-in knowledge + company docs)
   - Returns analysis to the Executive
5. Executive synthesizes all specialist input into one coherent response
6. The internal agent architecture is NEVER exposed to the user

## Prompt Caching — Critical

The system is designed around Anthropic prompt caching. Breaking caching = 10x cost increase.

**Never put dynamic content in system prompt blocks that have `cache_control`.**

Build order in `prompts/cache_manager.py`:
1. Tool definitions (sorted by name — MUST be sorted)
2. Executive persona constant (from `prompts/executive_persona.py` — NEVER f-stringed)
3. Company profile block (from `memory/company_profile.py`)
4. Knowledge index summary

RAG context goes in the **user turn**, not the system prompt.

## Adding a New Specialist Agent

1. Create `packages/core/openexecutive/agents/your_agent.py`:
   ```python
   from openexecutive.agents.base import BaseAgent
   
   class YourAgent(BaseAgent):
       name = "your_agent"
       domain = "your_domain"
       model = "claude-sonnet-5"
       
       def get_system_prompt(self) -> str:
           from openexecutive.prompts.domain_prompts import YOUR_AGENT_PROMPT
           return YOUR_AGENT_PROMPT
   ```

2. Add `YOUR_AGENT_PROMPT` constant to `prompts/domain_prompts.py`

3. Register in `orchestrator/router.py`:
   - Add to `SPECIALIST_REGISTRY` dict
   - Add tool enum value to `SPECIALIST_TOOLS[0]["input_schema"]["properties"]["specialist"]["enum"]`
   - Add its area in plain words (e.g. `"sales": "sales"`) to `_AREAS` in `orchestrator/answer_sources.py`. The web chat names that area when the specialist can't answer, and a test checks every registered specialist has one

4. Add knowledge docs to `knowledge/your_domain/`

5. Add `packages/core/openexecutive/evals/_scenarios/your_domain_001.yaml` and `your_domain_002.yaml`

6. If the agent introduces a new pattern (new tool, new routing path, new memory contract), update `packages/core/openexecutive/architecture/architecture-facts.yaml`. Pure additions to `SPECIALIST_REGISTRY` are auto-reflected in the `agents` section without YAML edits.

7. Submit PR — must include all of the above

## Company Data

Company-specific data lives in `packages/core/company/` — **gitignored**. Never commit company data. The `.env` file is also gitignored.

Structure:
- `company/profile.yaml` — structured company profile (populated by onboarding wizard)
- `company/docs/` — uploaded documents (indexed into ChromaDB)

## Code Style

- Python: `ruff` for linting, `mypy` for type checking, `pytest` for tests
- Pydantic v2 throughout — use `model_config = ConfigDict(...)` not `class Config`
- All Anthropic API calls: use `anthropic.AsyncAnthropic()`
- No dynamic content in cached system prompt blocks
- All agent `analyze()` calls are async

## Architecture Docs

The `/architecture` page is served from **static, hand-authored content** under `packages/core/openexecutive/architecture/prebuilt/<section_id>.json` — one file per section in `architecture/sections.py` (`SECTIONS`). The backend (`api/routes/architecture.py`) only reads these files; **nothing on this path calls an LLM**. The files ship in the Docker image, so they redeploy automatically with any `packages/core/**` change.

`architecture/architecture-facts.yaml` is the curated, deep source-of-truth reference for the *why* behind the system (integrations, scheduler behavior, departments/people structure, caching layout, invariants, committee review, authority gates). It is **no longer fed to a runtime generator** — treat it as the authoritative notes you (or Claude Code) read when re-authoring a section.

The most common failure mode is **new behavior added under an existing topic** — e.g. adding Discord to integrations, or changing the response shape of an endpoint described under `today`. Nothing forces an update, so the page silently goes stale. Treat any change that alters what a section already describes as a required content update — same bar as adding a brand-new topic.

When your PR materially changes a documented topic, **re-author the affected `prebuilt/<section_id>.json` in the same PR** (the simplest path: ask Claude Code to re-author that section from the updated facts), and update the corresponding `architecture-facts.yaml` notes. Topics → section ids:

- New integration channel OR changed integration behavior → `integrations`
- New workflow primitive (e.g., `wait_for_human`) OR new `WORKFLOW_REGISTRY` entry with a new pattern → `workflows`
- Cache layout change (block count, TTLs, what's cached) → `caching`
- New invariant or guardrail → the affected section (often `overview` / `agents`)
- New routing pattern (e.g., committee review) OR changed specialist routing → `routing`-adjacent sections (`agents`, `lifecycle`)
- Schema change to a documented table → `schemas`
- Endpoint added, removed, renamed, or response-shape changed → `api` (and any section that names it)
- New top-level module under `packages/core/openexecutive/` → add a `SectionSpec` in `architecture/sections.py`, a matching entry in `packages/ui/src/app/architecture/page.tsx` (IDs must match), AND a new `prebuilt/<id>.json`

Each `prebuilt/<id>.json` has the keys `section_id`, `title`, `markdown`, `mermaid` (a Mermaid string or `null`), and `generated_at`. The Markdown must not include the section heading (the UI renders the title). Validate edits with `python -m json.tool`.

CI enforces this with `scripts/pr_checks.py` (also run by `make check`): a change under a documented module fails unless one of that module's `prebuilt/<section>.json` files changed too. The module → section map is `SECTIONS_FOR` in that script; update it when you add a module or section. When a change genuinely does not alter what a section describes, waive it with a line `Arch-Docs: n/a - <reason>` in a commit message or the PR description.

## Local Hosts

`make dev` serves both:

- **API** — FastAPI backend on http://localhost:8000
  - Sections list + availability: `GET /architecture/sections`
  - Per-section content (static, pre-authored): `GET /architecture/sections/{id}`
  - SQLite inspection: `sqlite3 ./episodic_memory.db` (or `$EPISODIC_DB_PATH`)
- **UI** — Next.js frontend on http://localhost:3000
  - Architecture page: http://localhost:3000/architecture

Deployment topology and operations live in `docs/deployment.md`. Any host URLs
and deploy configuration for a specific environment are kept outside this repo.

## Environment Variables

See `.env.example`. Required: `ANTHROPIC_API_KEY`, `EXEC_EMAIL_ADDRESS` (no default). Optional integrations: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_WEBHOOK_SECRET` (`docs/telegram_setup.md`), `DISCORD_BOT_TOKEN` + `DISCORD_APP_ID`, `GOOGLE_CHAT_PROJECT_NUMBER` + one of `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` / `_EMAIL` (`docs/google_chat_setup.md`). Email has no IMAP/SMTP settings: the poller (`integrations/email_poller.py`) reads and sends through the Gmail tools of the Google Workspace MCP (`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`), signed in as `EXEC_EMAIL_ADDRESS`. Act as me (optional) reads the owner's *own* Gmail directly (`delegation/gmail.py`, never the MCP gateway) from a per-person credential in `DELEGATION_GOOGLE_CREDENTIALS_DIR`, minted by `scripts/connect-own-gmail.py`; `DELEGATION_COMPOSER_MODEL` / `DELEGATION_MAX_DRAFTS_PER_DAY` tune the drafts.

## Testing

> **Local gotcha:** if `BACKEND_SHARED_SECRET` is set in your shell/container
> (e.g. for the `openexec-api` skill), full-app `TestClient` tests return `401`
> instead of their expected status. Run the suite with the var unset —
> `env -u BACKEND_SHARED_SECRET uv run pytest tests/unit/` — to match CI (CI
> does not set it).

> **`OE_PUBLIC_DEPLOYMENT` is the same trap, harder:** `api/main.py` runs
> `app = create_app()` at module level, and with that var set and no
> `BACKEND_SHARED_SECRET` the guard raises at **import** time — so the failure
> is a collection error, not a test failure. `tests/conftest.py` pops it
> defensively; keep it unset in your shell anyway.

> **Ad-hoc scripts:** `get_settings()` requires `EXEC_EMAIL_ADDRESS` (no
> default), so a one-off `uv run python` snippet needs it exported alongside
> `ANTHROPIC_API_KEY` — the test suite sets both in `tests/conftest.py`.

> **uv gotchas (learned on #87):** `uv export/sync --frozen` uses `uv.lock`
> as-is and does NOT detect a stale lock — `--locked` is the flag that fails
> on staleness. `uv sync` in CI silently rewrites a stale lock before tests
> run, so lock freshness is gated by the `uv lock --check` step in `ci.yml`.
> `uv export -o FILE` still echoes the full export to stdout unless `-q`.

> **UI lint:** `packages/ui` has no ESLint config — `npm run lint` opens an
> interactive setup prompt. `npm run build` (`next build`) is the UI's
> lint/type gate. CI's UI job also runs `npx tsc --noEmit` and `npm test`,
> and some of those `scripts/*.test.mjs` parity tests parse Python source
> (`api/main.py`'s `_LOOPBACK_HOST_RE` / `_OWN_PAGE_FETCH_SITES`,
> `utils/deployment.py`'s `FALSEY_ENV`) — moving or renaming one of those
> breaks the UI job, so run `npm test` too when you touch them.

> **Audit-log test pollution:** `audit.log_event` writes to the default
> `./episodic_memory.db` unless the test isolates it. A test module that
> exercises audited code (alerts review, sweeps) without monkeypatching
> `openexecutive.audit.log_event` (or the audit logger's DB) leaks rows that
> break *other* modules' assertions only in a full run (e.g. the brief's
> "handled overnight" block). Patch it in an autouse fixture, and delete a
> stray `packages/core/episodic_memory.db` (gitignored) if one appears.

> **Patching `episodic.DB_PATH` isn't enough:** `memory/episodic.py` reads it
> at call time, but the audit logger keeps its own path, and modules that bind
> `DB_PATH` as a default argument at import (`memory/session_store.py`,
> `knowledge/review_store.py`, …) never see the patch, so they still use
> `./episodic_memory.db`. If a stray copy of that file lacks a table they read,
> the test fails with "no such table" depending on what ran before it. Point
> them at the test DB too, as the `db` fixture in
> `tests/integration/test_scheduler_runner.py` does for the audit logger.

> **ContextVar leaks between tests:** a *sync* fixture or test that sets
> `current_session` (or any module-level ContextVar) and doesn't reset it
> leaves that value bound for every later test in the process.
> pytest-asyncio isolates only async tests and fixtures. Use
> `token = var.set(...)`, `yield`, `var.reset(token)`. CI's `-n auto --dist
> loadfile` usually puts the leaking file and the one it breaks on different
> workers, so only a serial run (no `-n`) shows it.

> **Pre-existing ruff hits in `tests/unit/test_attachments.py`** (unsorted
> imports, unused `asyncio`): `make lint` only checks `openexecutive/`, so CI
> is unaffected — lint the specific test files you touched rather than
> `tests/` as a whole.

> **Integration tests need no API key:** `tests/integration/` drives the
> FastAPI routes against a temp SQLite DB with the model calls stubbed, and
> CI runs it next to `tests/unit/`. Stub `utils.session_title.generate_session_title`
> and `knowledge.retriever.retrieve` in any new route test, or it reaches the
> live API (see `patched_deps` in `test_chat_committee.py`).

```bash
# Unit tests (no API calls)
pytest packages/core/tests/unit/ -v

# Integration tests (route-level, stubbed model calls, no API key)
pytest packages/core/tests/integration/ -v

# Eval suite
make eval   # runs packages/core/openexecutive/evals/_scenarios/*.yaml, writes evals/results/
```

## PR Requirements

- No stubs — working code only
- Tests for new behavior
- Eval scenarios for new agents or prompt changes
- `ruff check` and `mypy` must pass — `make check` runs these, the unit tests and `scripts/pr_checks.py` (no stubs, eval scenarios, arch-doc drift) the way CI does
- Architecture docs updated per `## Architecture Docs` above (when integrations, scheduler, departments/people, caching, invariants, routing patterns, or top-level modules change)
- PR title is `type(scope): what changed`, in the imperative — e.g.
  `fix(chat): bind the session for the whole SSE turn`. Types: `fix`, `feat`,
  `docs`, `chore`, `refactor`, `test`, `perf`. Scope is the subsystem
  (`chat`, `memory`, `alerts`, `briefing`, `orchestrator`, `integrations`,
  `ui`, `deps`, …), not a file path; drop it only when the change genuinely
  spans the repo. Say what changed rather than what it is about, lowercase
  after the colon, no trailing period. See `.github/PULL_REQUEST_TEMPLATE.md`.
  The type sets the next version (release-please, before 1.0: `feat` and `fix`
  → patch, a breaking change (`!` / `BREAKING CHANGE:`) → minor;
  `chore`/`docs`/`test`/`refactor` release nothing on their own), and
  `feat`/`fix` titles become the changelog lines, so pick the type by what the
  change is, not by how big it is. PRs are squash-merged with the PR title as
  the commit subject, which is what release-please reads — branch commit
  messages do not reach `main`.
- `!` / `BREAKING CHANGE:` only for a real break (a removed or reshaped
  endpoint, a new required env var, a migration an operator must run) — never
  to get a bigger bump. To release a larger version for any other reason, put
  a `BEGIN_COMMIT_OVERRIDE` / `END_COMMIT_OVERRIDE` block at the end of one
  PR's description holding that PR's title, a blank line, then
  `Release-As: X.Y.Z`. release-please reads the block in place of the squash
  message. A bare `Release-As:` line in the description does not work:
  release-please only reads footers in the commit's final paragraph, and
  GitHub or the Claude footer appends text after it (#210). It applies to the
  next release only. The same block with a corrected message un-marks a
  merged PR (e.g. drops a wrong `!`). Either takes effect on the next push to
  `main`. Write the begin marker only once in a PR description, and never in
  prose: release-please takes the text after its first occurrence, so a
  backticked mention earlier in the body becomes the "message", fails to
  parse, and drops that commit from the release (#210, #211).
- PR description is three sections and nothing else: **Problem**, **Approach**,
  **Checklist** (see `.github/PULL_REQUEST_TEMPLATE.md`). Rationale, review
  findings and alternatives go in the commit message; open questions go in the
  review thread. Keep the body short enough to read in one screen.

## Definition of Done

Before calling a code change done or opening a PR:

- `make check` passes. It unsets `BACKEND_SHARED_SECRET` / `OE_PUBLIC_DEPLOYMENT` for the tests, so the Testing gotchas above don't bite.
- New behavior has tests; a new agent or prompt change has eval scenarios.
- A change to what a documented topic describes updates its `prebuilt/<section>.json` (see Architecture Docs), or carries an `Arch-Docs: n/a - <reason>` waiver.
- A change touching `api/`, `integrations/`, `mcp_server/`, auth, `orchestrator/outbound_guard.py`, the cached prompt blocks or `.gitignore` gets a pass from the `security-reviewer` agent (or `/security-review`) before the PR opens.
- Every non-draft PR also gets an automated Claude review (`.github/workflows/claude-code-review.yml`) as inline comments marked 🔴 must-fix / 🟡 optional / 🟣 pre-existing. Fix or answer each finding.
- When driving a PR to green, the `steward` skill (`.claude/skills/steward/SKILL.md`) covers CI failures and review findings.
