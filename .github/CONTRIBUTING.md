# Contributing to Open Executive

## Getting Started

1. Fork the repo and clone your fork
2. Set up the development environment: `make install`
3. Copy `.env.example` to `.env` and add your `ANTHROPIC_API_KEY`
4. Start the dev server: `make dev`
5. Run the tests: `make test`

## Branch Naming

- `feat/` — new features
- `fix/` — bug fixes
- `agent/` — new specialist agents
- `eval/` — new eval scenarios
- `docs/` — documentation changes

## PR Requirements

All PRs must:
1. Pass CI (ruff, mypy, unit tests)
2. Include working code — no stubs, no placeholders
3. Include tests for new behavior
4. For new or modified agents: include at least 2 eval scenarios
5. **Architecture docs**: verify `/architecture` reflects your change (see below)
6. **A title in the form `type(scope): what changed`**, in the imperative —
   `fix(chat): bind the session for the whole SSE turn`. Types are `fix`,
   `feat`, `docs`, `chore`, `refactor`, `test` and `perf` — a new specialist
   agent is a `feat` and a new eval scenario is a `test`, so the `agent/` and
   `eval/` branch prefixes above have no type of their own. The scope is the
   subsystem (`chat`, `memory`, `alerts`, `ui`, `deps`, …), not a file path,
   and is dropped only when a change genuinely spans the repo.
   Say what changed rather than what it is about — `fix(memory): stop the
   extractor dropping short approvals`, not `fix(memory): extractor bug`.
7. **A completed PR description using the template** — what changed, why, and
   how it works, with the checklist filled in. PRs submitted with an empty
   template will be closed; you're welcome to resubmit with the sections
   completed.

## Adding a New Specialist Agent

See [CLAUDE.md](../CLAUDE.md#adding-a-new-specialist-agent) for the step-by-step guide.

## Improving the Knowledge Base

The `knowledge/` directory contains Markdown files with executive expertise. Contributions here are very welcome.

Requirements:
- Accurate and up-to-date information
- Cite sources for specific claims
- Domain-tagged with the correct folder
- Practical, not academic — this is for practitioners

## Architecture Docs (`/architecture` page)

The `/architecture` page in the UI is served from **static, hand-authored
content**: one `packages/core/openexecutive/architecture/prebuilt/<section_id>.json`
file per section listed in `architecture/sections.py`. Nothing on that path
calls an LLM at runtime, so nothing updates itself — if your PR changes
behavior a section describes and you don't re-author the section, the page
silently goes stale.

The deep source-of-truth notes behind the page live in
`packages/core/openexecutive/architecture/architecture-facts.yaml`.

**When your PR materially changes a documented topic, update BOTH in the same
PR**: the relevant `architecture-facts.yaml` key, and the affected
`prebuilt/<section_id>.json`. This applies equally to *changes* under an
existing topic (e.g. adding a new integration channel, changing a documented
endpoint's response shape) — not just brand-new topics. The topic → section-id
map and full procedure are in [CLAUDE.md](../CLAUDE.md#architecture-docs);
common cases:

- New or changed integration channel → `integrations`
- New workflow primitive or routing pattern → `workflows` / `agents` / `lifecycle`
- Cache layout change → `caching`
- Endpoint added/removed/renamed or response shape changed → `api`
- New top-level module under `packages/core/openexecutive/` → new `SectionSpec`
  in `architecture/sections.py`, matching entry in
  `packages/ui/src/app/architecture/page.tsx`, and a new `prebuilt/<id>.json`

Each `prebuilt/<id>.json` carries `section_id`, `title`, `markdown`, `mermaid`
(string or `null`), and `generated_at`; validate edits with
`python -m json.tool`. Pure additions to `SPECIALIST_REGISTRY` are
auto-reflected in the `agents` facts and need no YAML edit.

## Prompt Changes

Prompt changes to `executive_persona.py` or `domain_prompts.py` require:
1. A before/after comparison in the PR description
2. Eval suite run showing no regression (score drop ≤10% on existing scenarios)
3. At least 2 new eval scenarios if adding new behavior

## Reporting Issues

Use GitHub Issues. Include:
- What you asked the Executive
- What you expected
- What you got
- Your company profile context (anonymized)
