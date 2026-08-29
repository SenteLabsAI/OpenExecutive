# Security Policy

## Supported Versions

Open Executive is under active development. Security fixes are applied to the
`main` branch; there are no long-term support branches at this time. Always run
the latest `main`.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **"Report a vulnerability"** to open a private advisory.

This routes your report privately to the maintainers. Please use this channel
rather than email so reports are tracked and not missed.

Please include:

- A description of the issue and its potential impact.
- Steps to reproduce (proof-of-concept if possible).
- Affected component(s) (API, UI, an integration, a deployment config).
- Any suggested remediation.

We aim to acknowledge reports within a few business days and will keep you
informed as we work on a fix. We ask that you give us a reasonable opportunity
to remediate before any public disclosure.

## Scope & Handling Notes

A few things worth knowing when assessing or reporting:

- **Secrets** never belong in the repo. Runtime secrets (`ANTHROPIC_API_KEY`,
  `BACKEND_SHARED_SECRET`, `AUTH_*`, integration tokens) are injected via
  environment variables / Fly secrets and are gitignored locally. If you find a
  committed secret, report it privately rather than opening an issue.
- **Access control.** The deployed UI is gated by Google sign-in with an email
  allow-list, and the API is protected by a shared-secret header between the UI
  proxy and the FastAPI backend (see [docs/auth.md](docs/auth.md)). The product
  is currently a shared workspace with no per-user data isolation — treat all
  allow-listed users as trusted.
- **Outbound actions.** The Executive can send messages and call external tools
  (MCP). Reports about prompt-injection paths that lead to unintended outbound
  actions or data egress are in scope and appreciated.
- **Defense-in-depth.** An optional, deterministic execution-guard layer
  (`grimdall_guard`, off by default) adds pre-execution checks on tool calls —
  secret denial, an egress allowlist, a destructive-command block, and a
  per-session spend budget — with signed audit receipts. Enable it with
  `GRIMDALL_ENABLED=true` (Shadow Mode) and `GRIMDALL_ENFORCE=true` (hard
  blocks); see [README → Execution Security](#execution-security-optional) and
  the architecture [security section](packages/core/openexecutive/architecture/prebuilt/security.json).
