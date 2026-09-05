# Authentication & Access Control

Open Executive is gated behind Google sign-in plus an email allow-list. This doc explains what's protected, how, and how to operate it (add/remove users, rotate secrets, debug failures).

---

## What's protected, by what

Two independent layers. Either one alone would be insufficient; together they fail closed.

| Layer | What it does | Where |
|---|---|---|
| **UI: Auth.js v5 + Google OAuth** | Anyone hitting the public UI is redirected to `/signin`. Only Google accounts in `ALLOWED_EMAILS` can complete sign-in. | [packages/ui/src/auth.ts](../packages/ui/src/auth.ts), [packages/ui/src/middleware.ts](../packages/ui/src/middleware.ts), [packages/ui/src/app/signin/page.tsx](../packages/ui/src/app/signin/page.tsx) |
| **API: shared-secret header** | The FastAPI backend is on a public Fly URL. It rejects every request whose `x-api-key` header doesn't match `BACKEND_SHARED_SECRET`. The UI proxy stamps this header on every upstream call. | [packages/core/openexecutive/api/main.py](../packages/core/openexecutive/api/main.py), [packages/ui/src/app/api/backend/[...path]/route.ts](../packages/ui/src/app/api/backend/%5B...path%5D/route.ts) |

### Request flow

```
Browser ──► openexec-ui-dev.fly.dev ──► (middleware: session check)
                │
                ├── no session  ──► redirect to /signin → Google → callback → cookie set
                │
                └── has session ──► /api/backend/[...path] (proxy)
                                        │ stamps x-api-key
                                        ▼
                                 openexec-api-dev.fly.dev (FastAPI)
                                        │ middleware verifies x-api-key (constant-time)
                                        ▼
                                    route handler
```

### Exempt paths (API)

These bypass the shared-secret check because they're hit by external services that authenticate themselves:

- `/health` — Fly's health checker
- `/webhook/telegram` — verifies Telegram's own secret token
- `/webhook/google-chat` — verifies the GCP project's signed JWT
- `OPTIONS *` — CORS preflight (no auth headers possible)

See [`_UNAUTHENTICATED_PATHS`](../packages/core/openexecutive/api/main.py) — any new webhook from an external service must be added here.

---

## Required configuration

### One-time: Google Cloud Console

1. Create or pick a Google Cloud project.
2. Set up the consent screen (the **Google Auth Platform** page — formerly "OAuth consent screen"). External user type is fine; you don't need to add test users or publish the app because we only request basic scopes (`openid email profile`).
3. **APIs & Services → Clients → + Create Client → Web application**:
   - **Authorized JavaScript origins**: `http://localhost:3000`, `https://openexec-ui-dev.fly.dev`
   - **Authorized redirect URIs**: `http://localhost:3000/api/auth/callback/google`, `https://openexec-ui-dev.fly.dev/api/auth/callback/google`
4. Copy the Client ID and Client secret immediately — the secret is shown only once.

### Local dev (repo-root `.env`, gitignored)

Put everything in the repo-root `.env` (the file the README quickstart has you
create from `.env.example`). Both `make dev` and `make docker` load it into the
API **and** the UI:

Generate the two random secrets first and paste their **output** — never put
`$(...)` inside the file itself: the file is parsed as plain text by Docker
Compose and the backend's dotenv loader, so command substitutions become the
literal (publicly known) string instead of a secret.

```bash
openssl rand -base64 32   # → paste as AUTH_SECRET
openssl rand -hex 32      # → paste as BACKEND_SHARED_SECRET
```

```bash
AUTH_GOOGLE_ID=<from google>
AUTH_GOOGLE_SECRET=<from google>
AUTH_SECRET=<paste the base64 output>
AUTH_TRUST_HOST=true
# AUTH_URL stays blank for local dev — set it only on public deployments.
ALLOWED_EMAILS=you@example.com,teammate@example.com
BACKEND_SHARED_SECRET=<paste the hex output>
ANTHROPIC_API_KEY=sk-ant-...
```

Then `make dev` and visit http://localhost:3000.

#### Skipping Google locally

No OAuth client yet? Set `AUTH_DEV_BYPASS=true` in the root `.env` and run
`make dev` (the `make docker` UI service does not forward the flag). `/signin`
then signs you in automatically as the first `ALLOWED_EMAILS` entry (via an
Auth.js Credentials provider registered only when the flag is on), and every
downstream check — middleware, backend proxy, `x-caller-email`, the allow-list
callbacks — runs unchanged. Two guards keep it local: the provider is
registered only when `NODE_ENV` is not `production` (`next build` hard-sets
`production`, so the flag is inert on Fly or any other built deployment), and
`make dev` binds the UI to `127.0.0.1` instead of the usual `0.0.0.0` while the
flag is on, so nothing else on your network can reach the dev server. (The
provider also rejects non-loopback `Host` headers, but that is a tripwire, not
a boundary — `Host` is client-supplied.) If you start `next dev` by hand with
the flag on, pass `-H 127.0.0.1` yourself. "Sign out" in the UI lands on
`/signin`, which signs you straight back in while the flag is on.

For Docker, use `make docker` (not a bare `docker compose -f
docker/docker-compose.yml up`): the Makefile passes `--env-file .env`, which
is what feeds the UI container's `AUTH_*` / `BACKEND_SHARED_SECRET` values.
If you invoke compose directly, add `--env-file .env` yourself.

A `packages/ui/.env.local` (also gitignored) still works, but note the
precedence: under `make dev` / `make docker` the root `.env` is exported into
the process environment before Next.js starts, and Next never overrides an
already-set variable — so **for any key present in both files, the root `.env`
wins — including keys left blank in the root file** (a blank export still
counts as set). Use `.env.local` only for keys absent from the root `.env`
entirely.
Plain `npm run dev` in `packages/ui` (without `make dev`) reads only
`packages/ui/.env*`, not the root `.env`.

### Production (Fly secrets)

```bash
# Generate once, reuse across both apps
SHARED=$(openssl rand -hex 32)
AUTH=$(openssl rand -base64 32)

# UI
flyctl secrets set -a openexec-ui-dev \
  AUTH_SECRET="$AUTH" \
  AUTH_GOOGLE_ID="<your client id>" \
  AUTH_GOOGLE_SECRET="<your client secret>" \
  ALLOWED_EMAILS="alice@x.com,bob@y.com" \
  AUTH_TRUST_HOST=true \
  AUTH_URL="https://openexec-ui-dev.fly.dev" \
  BACKEND_SHARED_SECRET="$SHARED"

# API — same $SHARED value
flyctl secrets set -a openexec-api-dev \
  BACKEND_SHARED_SECRET="$SHARED" \
  BACKEND_ALLOWED_ORIGINS="https://openexec-ui-dev.fly.dev"
```

Run both `flyctl` commands in the **same terminal session** so `$SHARED` doesn't get re-generated between them — a mismatch silently breaks every API call with `401`.

> **Why `AUTH_URL` is required (not just `AUTH_TRUST_HOST`)** — Behind Fly's edge, Auth.js builds the post-OAuth-callback redirect URL using the container's bind address (`0.0.0.0`) unless told the public origin explicitly. `AUTH_TRUST_HOST=true` is necessary but not sufficient on Fly. Symptom if missing: sign-in succeeds at Google, then the browser tries to load `http://0.0.0.0/...` and fails with `ERR_CONNECTION_REFUSED`.

> **Production fails closed.** If `BACKEND_SHARED_SECRET` is unset and `FLY_APP_NAME` is set (i.e. running on Fly), [api/main.py](../packages/core/openexecutive/api/main.py) raises `RuntimeError` at startup rather than serve traffic without auth.

---

## Operations

### Add or remove a user

```bash
flyctl secrets set -a openexec-ui-dev ALLOWED_EMAILS="alice@x.com,bob@y.com,carol@z.com"
```

Fly redeploys automatically after a secret change. The list is read once at startup, so the new entry is live as soon as the rolling restart finishes (~30s).

Rules: comma-separated, case-insensitive, whitespace around entries is stripped, trailing commas are harmless.

### Rotate the shared secret

Do this if anyone with access to either Fly app leaves, or on a regular cadence.

```bash
NEW=$(openssl rand -hex 32)
flyctl secrets set -a openexec-ui-dev  BACKEND_SHARED_SECRET="$NEW"
flyctl secrets set -a openexec-api-dev BACKEND_SHARED_SECRET="$NEW"
```

There's a brief window during the rolling restart where one app has the new value and the other has the old one. Calls during that window will 401. If that matters, take the UI down first (`flyctl scale count 0 -a openexec-ui-dev`), rotate both, then scale back up.

### Rotate `AUTH_SECRET`

Invalidates all existing sessions (everyone is signed out and must re-auth). Use this if the secret may be compromised.

```bash
flyctl secrets set -a openexec-ui-dev AUTH_SECRET="$(openssl rand -base64 32)"
```

### Revoke OAuth client

If `AUTH_GOOGLE_SECRET` is leaked, regenerate in Google Cloud Console (Clients → your client → **Reset Secret**), then update both your local root `.env` (and `packages/ui/.env.local` if you use one) and the Fly secret. Old issued tokens stop working immediately.

---

## Debugging

| Symptom | Likely cause |
|---|---|
| `OAuth client was not found` / `invalid_client` | `AUTH_GOOGLE_ID` typo, swapped with `AUTH_GOOGLE_SECRET`, or the client lives in a different GCP project |
| `redirect_uri_mismatch` | The Authorized redirect URI in Google Console doesn't exactly match `<origin>/api/auth/callback/google`. Wait 5 min for Google to propagate after edits |
| Browser tries to load `0.0.0.0` after sign-in | `AUTH_URL` not set on Fly |
| `AccessDenied` page after Google login | Email not in `ALLOWED_EMAILS`, or Google returned `email_verified !== true` |
| API returns `401` for every request | UI and API have different `BACKEND_SHARED_SECRET` values (very common after rotating in two separate terminal sessions) |
| API refuses to start with `RuntimeError: BACKEND_SHARED_SECRET is required` | You're on Fly and forgot to set the secret. Set it; the next machine restart will boot |
| Sign-in works but the chat stays empty | Backend is auth'd but `ANTHROPIC_API_KEY` is missing on `openexec-api-dev`. `flyctl logs -a openexec-api-dev` will show the error |

### Useful commands

```bash
# What secrets are set on each app (digests only, not values)
flyctl secrets list -a openexec-ui-dev
flyctl secrets list -a openexec-api-dev

# Live logs
flyctl logs -a openexec-ui-dev
flyctl logs -a openexec-api-dev

# Probe the public API without going through the UI
curl -sv https://openexec-api-dev.fly.dev/health           # should be 200
curl -sv https://openexec-api-dev.fly.dev/sessions          # should be 401
curl -sv -H "x-api-key: $SHARED" https://openexec-api-dev.fly.dev/sessions  # should be 200
```

---

## Threat model — what this does and does not protect against

**Mitigates:**
- Random internet visitors reaching the UI or the API
- A leaked UI URL being usable by anyone with a Google account (allowlist)
- Direct API hits bypassing the UI (shared secret)
- Cookie theft from one session leaking *another* user's data (each session is independent JWT; no shared state)
- Missing-secret deploys silently exposing the API (Fly fail-closed guard)

**Does not mitigate:**
- A compromised `BACKEND_SHARED_SECRET` — anyone who learns it can hit the API as if they were the UI. Rotate if leaked.
- A compromised Google account on the allow-list — that user has full access to all shared data. The product is currently a **shared workspace**; there is no per-user data isolation.
- A compromised Fly token — attacker can change secrets, redeploy, or read logs. Rotate Fly tokens if a CI workflow is compromised.
- Browser-side XSS — Auth.js sessions are httpOnly cookies, so JS can't read them, but a successful XSS could make authenticated requests from the victim's browser. Standard same-origin protections apply.

If/when per-user data isolation matters (e.g. private sessions per teammate), the change is non-trivial — see the original plan note in [PR #86](https://github.com/SenteLabsAI/OpenExecutive/pull/86) about adding `user_id` to the sessions table.
