---
name: steward
description: Repo rules for driving an Open Executive pull request to green - CI failures, the pr-checks job, and findings from the automated Claude review. Use when watching, fixing or babysitting a PR in this repo.
---

# Steward: driving an Open Executive PR

Only what is specific to this repo. `CLAUDE.md` covers the code rules.

## Before every push
- Run `make check`. It runs what CI runs (lint, unit and integration tests,
  the UI build if `packages/ui` changed, `scripts/pr_checks.py`) with the
  `BACKEND_SHARED_SECRET` / `OE_PUBLIC_DEPLOYMENT` traps handled.
- If a test fails only in the full run, rerun it serially (no `-n`). The
  usual cause is a ContextVar or audit-log leak from another module (see
  `CLAUDE.md` -> Testing), not a flake.
- Batch every fix for a round into one push. Each push re-runs the paid
  Claude review.

## `PR rules` job red
- `arch-doc-drift`: re-author the named
  `architecture/prebuilt/<section>.json` and the matching
  `architecture-facts.yaml` notes. Waive with `Arch-Docs: n/a - <reason>`
  only when the change doesn't alter what the section describes. Never
  waive just to get green.
- `eval-scenarios`: add the `evals/_scenarios/*.yaml` files the
  new-agent checklist in `CLAUDE.md` asks for.
- `no-stubs`: finish the code; don't reword the marker.

## Claude review findings
The `Claude Code Review` workflow starts each inline comment with a marker:
- 🔴 must fix before merge (bug, security, `CLAUDE.md` invariant). Fix it,
  or reply with evidence that it doesn't reproduce.
- 🟡 optional. Reply in one line; fold it into the next push only if it is
  plainly right. It never starts a push on its own.
- 🟣 pre-existing, not introduced by this PR. Reply in one line and leave it.

## Branch and title
- PRs are squash-merged with the title as the commit subject, which
  release-please reads. Keep it `type(scope): what changed`.
- Resolve conflicts by merging `main` into the branch, not by force-pushing.
- Regenerate lockfiles with `uv lock` / `npm install`, never by hand. The
  `uv lock --check` step fails on a stale `uv.lock`.
