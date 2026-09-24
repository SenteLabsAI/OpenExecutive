<!--
Thanks for contributing to Open Executive! Keep it to these three sections.
See .github/CONTRIBUTING.md for the full contribution guide.

Detail belongs in the commit message, not here. Open questions belong in the
review thread.

TITLE: type(scope): what changed, in the imperative.

  fix(chat): bind the session for the whole SSE turn
  feat(alerts): let the Executive clear a card from the Discuss handoff
  docs(architecture): describe the committee review pass
  chore(deps): bump anyio to 4.14.2

Types: fix, feat, docs, chore, refactor, test, perf.
Scope is the subsystem, not a file path — chat, memory, alerts, briefing,
orchestrator, integrations, email, slack, ui, providers, knowledge, workflows,
scheduler, deps. Drop it only when the change genuinely spans the repo.

Say what changed, not what it is about: "fix(memory): stop the extractor
dropping short approvals", not "fix(memory): extractor bug". Lowercase after
the colon, no trailing period, and keep it under ~70 characters where you can.
-->

## Problem

<!-- What is broken or missing, and how does it show up? -->

## Approach

<!-- What the change does. Call out anything non-obvious; keep it short. -->

## Checklist

- [ ] Working implementation — no stubs or TODO placeholders
- [ ] Tests added/updated for new behavior (`pytest packages/core/tests/unit/`)
- [ ] `ruff check` and `mypy` pass (`make lint`)
- [ ] UI builds if touched (`cd packages/ui && npm run build`)
- [ ] Eval scenarios added for a new agent or prompt change (if applicable)
- [ ] Architecture docs updated if a documented topic changed
      (see the "Architecture Docs" section in `CLAUDE.md`)
- [ ] No secrets, credentials, or personal data committed
