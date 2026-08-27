# Open Executive — Claude Code Context

## Project Overview

Open Executive is a multi-agent AI system acting as a virtual corporate executive. Python backend (FastAPI) + Next.js 16 frontend. The "Executive" is a single coherent persona backed by 8 specialist sub-agents, all powered by the Anthropic Claude API.

## Repository Layout

```
packages/core/          Python: all agent logic, API, CLI
packages/ui/            Next.js 16 web UI
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

4. Add knowledge docs to `knowledge/your_domain/`

5. Add `evals/scenarios/your_domain_001.yaml` and `your_domain_002.yaml`

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

## Live Hosts

- **API** — FastAPI backend, Fly app `openexec-api-dev`: https://openexec-api-dev.fly.dev
  - Sections list + availability: `GET /architecture/sections`
  - Per-section content (static, pre-authored): `GET /architecture/sections/{id}`
  - SSH for SQLite / log inspection: `flyctl ssh console -a openexec-api-dev`
- **UI** — Next.js frontend, Fly app `openexec-ui-dev`: https://openexec-ui-dev.fly.dev
  - Architecture page: https://openexec-ui-dev.fly.dev/architecture

## Environment Variables

See `.env.example`. Required: `ANTHROPIC_API_KEY`. Optional integrations: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST`.

## Testing

> **Local gotcha:** if `BACKEND_SHARED_SECRET` is set in your shell/container
> (e.g. for the `openexec-api` skill), full-app `TestClient` tests return `401`
> instead of their expected status. Run the suite with the var unset —
> `env -u BACKEND_SHARED_SECRET uv run pytest tests/unit/` — to match CI (CI
> does not set it).

> **UI lint:** `packages/ui` has no ESLint config — `npm run lint` opens an
> interactive setup prompt. `npm run build` (`next build`) is the UI's
> lint/type gate.

```bash
# Unit tests (no API calls)
pytest packages/core/tests/unit/ -v

# Integration tests (requires ANTHROPIC_API_KEY)
pytest packages/core/tests/integration/ -v

# Eval suite
cd evals && python run_evals.py --scenarios scenarios/ --output results/
```

## PR Requirements

- No stubs — working code only
- Tests for new behavior
- Eval scenarios for new agents or prompt changes
- `ruff check` and `mypy` must pass
- Architecture docs updated per `## Architecture Docs` above (when integrations, scheduler, departments/people, caching, invariants, routing patterns, or top-level modules change)
- Descriptive PR description explaining the change and rationale

## Workflow

- For any task that writes, modifies, refactors, fixes, or plans code changes
  in this repo, invoke the `anvil` skill before editing. This applies to bug
  fixes, new features, refactors, and config changes — including small edits.
- Research-only tasks (read, search, explain, summarize) do not require Anvil.
