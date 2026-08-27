---
name: flyctl
description: Operate the two Fly.io apps for Open Executive (openexec-api-dev, openexec-ui-dev). Use this skill whenever the user asks to tail logs, SSH for SQLite inspection, list/set/unset secrets, check deploy status, restart machines, deploy, or rotate AUTH_SECRET. Covers the exact commands, SQLite paths, and known gotchas for this project. Tiered safety: read-only runs freely; destructive needs explicit user confirmation.
---

# flyctl

Quick-reference cheatsheet for operating Open Executive's two Fly.io apps. Pair skill: `openexec-api` (HTTP curl recipes against the FastAPI surface).

## Apps

| App | Stack | Key secrets | URL |
|---|---|---|---|
| `openexec-api-dev` | FastAPI (Python, port 8000) | `ANTHROPIC_API_KEY`, `HONCHO_API_KEY`, `HONCHO_BASE_URL` (optional), `BACKEND_SHARED_SECRET` | https://openexec-api-dev.fly.dev |
| `openexec-ui-dev` | Next.js 16 (NextAuth + Google OAuth) | `AUTH_SECRET`, `AUTH_GOOGLE_ID/SECRET`, `BACKEND_SHARED_SECRET`, `BACKEND_URL` | https://openexec-ui-dev.fly.dev |

No production-only apps exist today. Anything ending `-dev` is the live shared deployment.

## Safety tiers

- **Green — auto-run.** Read-only. `flyctl status / releases / logs / secrets list`; `flyctl ssh console -C` with a `SELECT`-only sqlite query.
- **Yellow — confirm once per session.** `flyctl ssh console` interactive, `flyctl apps restart`, redeploys of a known image.
- **Red — print the exact command and wait for explicit "go" each time, no chaining.** `flyctl secrets set/unset`, `flyctl machine destroy`, `flyctl apps destroy`, any `UPDATE/DELETE/INSERT/DROP` over ssh, `AUTH_SECRET` rotation, `flyctl deploy` from an unknown ref.

If the user previously said "go" for a red command, it does NOT authorize the next red command. Each red command needs its own "go."

## Green: status, logs, secrets list

```bash
flyctl status -a openexec-api-dev
flyctl status -a openexec-ui-dev

flyctl releases -a openexec-api-dev | head -10

# Live tail
flyctl logs -a openexec-api-dev

# One-shot capture (recent only, exits)
flyctl logs -a openexec-api-dev --no-tail

# Secret NAMES only — never echoes values
flyctl secrets list -a openexec-api-dev
```

## Green: SQLite inspection over SSH (SELECT-only)

The api app's `/data` volume layout:

| Path | Contents |
|---|---|
| `/data/episodic_memory.db` | chat messages, sessions, **people** (NOT a separate file), audit_log incl. peer_memory rows, scheduled_actions, alerts, decisions, initiatives, advice |
| `/data/chroma/` | ChromaDB vector store |
| `/data/_user_backup/.honcho_active_workspace.json` | current Honcho workspace override (null = use env default) |
| `/data/_user_backup/` | fixture snapshot before demo loads |

**SSH-with-shell gotcha.** `flyctl ssh console -a … -C "<cmd>"` does NOT spawn a shell, so pipes, redirects, quoted sqlite args fail silently or get mangled. Always wrap multi-token commands as `-C 'sh -c "<cmd>"'`.

```bash
# Recent peer_memory audit rows (Honcho sync_turn / prefetch / workspace_reset)
flyctl ssh console -a openexec-api-dev -C 'sh -c "sqlite3 /data/episodic_memory.db \"SELECT ts, json_extract(details_json,'\''$.op'\''), json_extract(details_json,'\''$.outcome'\'') FROM audit_log WHERE event_type='\''peer_memory'\'' ORDER BY id DESC LIMIT 10;\""'

# People roster
flyctl ssh console -a openexec-api-dev -C 'sh -c "sqlite3 /data/episodic_memory.db \"SELECT id, name, email, archived_at FROM people ORDER BY id;\""'

# Pending scheduled actions
flyctl ssh console -a openexec-api-dev -C 'sh -c "sqlite3 /data/episodic_memory.db \"SELECT id, kind, status, run_at FROM scheduled_actions WHERE status='\''pending'\'' ORDER BY run_at LIMIT 10;\""'

# Active Honcho workspace
flyctl ssh console -a openexec-api-dev -C 'sh -c "cat /data/_user_backup/.honcho_active_workspace.json 2>/dev/null || echo null"'
```

When the query gets gnarly, drop a heredoc into an interactive ssh (yellow tier — confirm once). Avoid the temptation to escape quotes through three layers.

## Red: SQLite writes

Any `UPDATE / DELETE / INSERT / DROP` against `/data/episodic_memory.db` is red. Print the exact `sqlite3` command, wait for "go," do not chain.

## Red: secrets set / unset

`flyctl secrets set` and `unset` trigger an immediate redeploy. Tell the user this before you propose the command.

```bash
# Rotate NextAuth secret — invalidates ALL existing JWTs / logs everyone out
flyctl secrets set AUTH_SECRET="$(openssl rand -base64 33)" -a openexec-ui-dev

# Unset a secret (also redeploys)
flyctl secrets unset HONCHO_BASE_URL -a openexec-api-dev
```

The full reference set lives in `scripts/fly-secrets.sh.example`. Reach for it when bootstrapping a new env, not for one-off changes.

## Yellow: restart / deploy

```bash
# Warm restart all machines for an app
flyctl apps restart openexec-api-dev

# Redeploy current branch — needs a clean tree and CI green; never unattended
flyctl deploy -a openexec-api-dev
```

GitHub Actions handles normal deploys via `.github/workflows/`. Manual `flyctl deploy` is a recovery tool, not the happy path.

## Gotchas

- `-C` bypasses the shell. Always `'sh -c "…"'` for pipes/quoted args.
- macOS lacks `timeout(1)`. For bounded queries, use a `LIMIT` clause — don't try to wrap the ssh in a shell timeout.
- `flyctl secrets list` redacts values. There is no `flyctl secrets get` for a reason — if you need to confirm a value, ask the user.
- Always check whether the branch's PR is still open before pushing a follow-up: `gh pr list --head <branch> --state open --json number,url`. Mirrors global CLAUDE.md.
- If `flyctl ssh console` hangs on connect, check `flyctl status -a <app>` — a machine may be `replacing` mid-deploy.
