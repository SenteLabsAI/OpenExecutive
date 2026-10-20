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
  environment variables / platform secrets and are gitignored locally. If you find a
  committed secret, report it privately rather than opening an issue.
- **Access control.** The deployed UI is gated by Google or SSO (OpenID
  Connect) sign-in with an allow-list (`ALLOWED_EMAILS` plus the People
  roster), and the API by a shared-secret header between the UI proxy and the
  FastAPI backend, optionally with signed callers so the API checks who is
  signed in rather than trusting a header (see [docs/auth.md](docs/auth.md)).
  Local login, with no sign-in, is for one person on their own computer only.
- **What each person can see.** It is one company's workspace: everyone on the
  roster shares the company's knowledge, alerts and briefs. Some data is
  private to one person, owner included: their People card, the artifacts
  they create, and their own mailbox under Act as me. A path where one
  signed-in person can read or act on another's private data is in scope.
- **Outbound actions.** The Executive can send messages and call external tools
  (MCP). Reports about prompt-injection paths that lead to unintended outbound
  actions or data egress are in scope and appreciated.
