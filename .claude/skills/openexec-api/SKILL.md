---
name: openexec-api
description: Interact with the Open Executive FastAPI backend via curl. Use this skill when the user asks to hit /chat, /today, /people, /scheduled_actions, /architecture/*, /health/*, /fixtures/*, /audit/*, or any HTTP endpoint on the Open Executive API, local or deployed. Authenticates via $BACKEND_SHARED_SECRET in the x-api-key header. Tiered safety: GET runs freely, mutating POSTs need explicit confirmation.
---

# openexec-api

Curl recipes against the FastAPI backend — for hitting HTTP routes, not for inspecting SQLite or tailing logs on a host.

## Base URLs

```bash
export OE_API=http://localhost:8000   # local (`make dev` must be running) — the default
export OE_API=https://<your-host>     # a deployed environment
```

All recipes below use `$OE_API`. If it is unset and the user doesn't specify, assume `http://localhost:8000`.

## Auth

```bash
-H "x-api-key: $BACKEND_SHARED_SECRET"
```

Rules:

- **Never expand `$BACKEND_SHARED_SECRET`** into the literal value in commands you print, in PRs, or in commit messages.
- **Never write the value to a file.** Do not read from `.env` to "help" — if the env var isn't exported, ask the user to export it before proceeding.
- For routes that vary by caller, optionally add `-H "x-caller-email: <user's-own-email>"`. Never put someone else's email there — the backend uses that header to resolve the caller's Person row and pull their per-person Honcho memory.
- **Signed callers.** When the API has `CALLER_ASSERTION_PUBLIC_KEYS` set, a bare `x-caller-email` is refused (`401 caller_assertion_required`), and a request with only `x-api-key` is a service that is never the owner. Owner-only routes refuse it. Then ask the user to export the UI's `CALLER_ASSERTION_PRIVATE_KEY` and sign each request instead of sending `x-caller-email`. Treat that key like the shared secret: never expand, print or store it.

  ```bash
  -H "x-caller-assertion: $(uv run --with cryptography python scripts/mint-caller-assertion.py GET /today)"
  ```

  Each assertion covers one request: the same method, the exact path and query, within 60 seconds, once. So mint one per call. It signs as the operator (the owner); `--email <user's-own-email>` signs as that user.

## Safety tiers

- **Green — auto-run.** All `GET` requests, and the cheap idempotent regenerate POSTs.
- **Yellow — confirm once per session.** Writes that change durable state but are reversible (e.g. archiving a Person, deleting a scheduled action).
- **Red — print the curl, wait for explicit "go," no chaining.** `POST /fixtures/reset`, `POST /fixtures/{name}/load`, `POST /fixtures/unload`, `POST /fixtures/snapshot`, `POST /chat` (impersonates the user — generally don't invoke from Claude).

Each red command needs its own "go" — a prior approval does not carry over.

## Green: health & introspection

```bash
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/health | jq
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/health/honcho | jq
```

`/health/honcho` returns one of:

- `{"status": "disabled"}` — Honcho is off
- `{"status": "ok", "latency_ms": ...}` — workspace reachable
- `{"status": "error", "error_type": "...", "error_msg": "..."}` — wrapper or network failure

## Green: architecture page

```bash
# All sections with freshness
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/architecture/sections | jq '.sections[] | {id, fresh, generated_at}'

# One section
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/architecture/sections/honcho | jq

# Cache key debug (per-component hashes feeding FactsBundle.core_hash)
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/architecture/debug/bundle | jq
```

## Green/Yellow: force regenerate a section

Cheap, idempotent, but it does call Claude and write to disk — treat as **yellow** (one confirm per session is enough).

```bash
curl -sX POST -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/architecture/sections/honcho/regenerate | jq
```

Replace `honcho` with any section id from `sections.py`: `overview`, `lifecycle`, `agents`, `caching`, `rag`, `review`, `memory`, `honcho`, `org`, `audit`, `schemas`, `workflows`, `scheduler`, `integrations`, `today`, `api`.

## Green: today / org / scheduler views

```bash
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/today | jq
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/people | jq
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/people/by-scope/CEO | jq
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" "$OE_API/scheduled_actions?status=pending" | jq
```

## Green: audit log

```bash
# Tail recent events
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" "$OE_API/audit/logs?limit=20" | jq

# Filter by event_type (peer_memory rows are Honcho ops)
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" "$OE_API/audit/logs?event_type=peer_memory&limit=10" | jq

# One event by id (returns full details_json)
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" "$OE_API/audit/logs/12345" | jq
```

## Green: fixtures (demo companies)

```bash
# List available demos
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures | jq

# Current load state
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures/status | jq
```

## Red: fixtures (mutating)

Each of these is **destructive or significantly state-altering**. Print the exact curl, wait for "go," do not chain.

```bash
# Snapshot current local state before loading a demo (one-time setup before first demo load)
curl -sX POST -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures/snapshot

# Load a demo company (wipes current local state, switches Honcho workspace)
curl -sX POST -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures/<name>/load

# Unload demo (restores the snapshot, deletes the demo Honcho workspace)
curl -sX POST -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures/unload

# Full factory reset (wipes profile.yaml, company/, ChromaDB, ALL episodic tables,
# people store, departments, Honcho workspace, snapshot dir). Effectively a fresh install.
curl -sX POST -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures/reset
```

## Red: /chat

`POST /chat` runs the orchestrator end-to-end. Calling it from Claude would impersonate the user, log spurious turns to the audit trail, and (if `x-caller-email` matches the user) pollute their Honcho peer card. Document, don't invoke. If the user explicitly asks for a chat test from curl, treat as red and confirm.

## Output handling

- Always pipe JSON to `jq` so the transcript stays readable.
- For long responses, project early: `jq '.sections | length'`, `jq '.[] | {id, status}'`, `jq '.audit_events[0:3]'`. Don't dump full bodies into Claude's context.
- For text/streaming endpoints, redirect to a temp file and `head`/`grep`.

## Endpoint discovery

If you need a route this skill doesn't list, the authoritative source is `packages/core/openexecutive/api/routes/`. One file per router. Grep for `@router.get|@router.post|@router.delete|@router.patch` to enumerate. Do not guess endpoints — the backend's middleware will 401 you on the wrong path with the api-key header.

## Common gotchas

- `localhost:8000` requires `make dev` running. If curl returns "Connection refused," the dev server is down.
- `x-api-key` middleware applies to nearly all routes. A bare `GET /health` works without it, but most others 401 without the header.
- The UI proxies through `/api/backend/...` and re-stamps `x-caller-email` from the verified session. Hitting the API directly bypasses that — useful for testing, but means you control the caller identity (and have to set it accurately).
- After `POST /fixtures/reset`, the Honcho workspace is wiped. The next chat turn rebuilds it lazily; don't expect prior conversation memory.
