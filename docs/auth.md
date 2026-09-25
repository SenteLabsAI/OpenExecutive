# Authentication & Access Control

Open Executive is gated behind Google sign-in plus an email allow-list — except in [one-person mode](#one-person-mode-no-google-sign-in), for trying it on your own computer. This doc explains what's protected, how, and how to operate it (add/remove users, rotate secrets, debug failures).

---

## What's protected, by what

Two independent layers. Either one alone would be insufficient; together they fail closed.

| Layer | What it does | Where |
|---|---|---|
| **UI: Auth.js v5 + Google OAuth** | Anyone hitting the public UI is redirected to `/signin`. Only Google accounts on the allow-list can complete sign-in — the **union** of `ALLOWED_EMAILS` and the People roster (see below). | [packages/ui/src/auth.ts](../packages/ui/src/auth.ts), [packages/ui/src/middleware.ts](../packages/ui/src/middleware.ts), [packages/ui/src/app/signin/page.tsx](../packages/ui/src/app/signin/page.tsx) |
| **API: shared-secret header** | The FastAPI backend is reachable over the network. It rejects every request whose `x-api-key` header doesn't match `BACKEND_SHARED_SECRET`. The UI proxy stamps this header on every upstream call. | [packages/core/openexecutive/api/main.py](../packages/core/openexecutive/api/main.py), [packages/ui/src/app/api/backend/[...path]/route.ts](../packages/ui/src/app/api/backend/%5B...path%5D/route.ts) |

### Who is on the allow-list

Two **additive** sources. An email is admitted if it appears in *either*:

1. **`ALLOWED_EMAILS`** — the comma-separated env var on the UI, read once at startup.
   Checked first, and a hit short-circuits: a configured operator signs in even when the
   API is completely down, and pays no roster-fetch latency.
2. **The People roster** — every non-archived Person with an email, served by
   `GET /auth/allowed-emails` and cached for 5 minutes per UI instance.

Neither one overrides the other. Adding people to the roster cannot revoke an
`ALLOWED_EMAILS` entry, and leaving `ALLOWED_EMAILS` blank is fine once the roster is
populated.

Membership is re-checked on every request the middleware gates, not just at sign-in, so a
roster removal takes effect within the cache window rather than waiting out the 24h JWT.

If the roster fetch fails, `ALLOWED_EMAILS` users are unaffected (their check never
consults the roster). Roster-only users with an existing session keep working — the UI
fails open rather than signing everyone out over a brief backend hiccup, since the strict
sign-in gate already vetted them — but a *new* roster-only sign-in is denied until the
backend answers.

### Request flow

```
Browser ──► exec.example.com (UI) ──► (middleware: session check)
                │
                ├── no session  ──► redirect to /signin → Google → callback → cookie set
                │
                └── has session ──► /api/backend/[...path] (proxy)
                                        │ stamps x-api-key
                                        ▼
                                 api.example.com (FastAPI)
                                        │ middleware verifies x-api-key (constant-time)
                                        ▼
                                    route handler
```

### Exempt paths (API)

These bypass the shared-secret check because they're hit by external services that authenticate themselves:

- `/health` — the platform's health checker
- `/webhook/telegram` — verifies Telegram's own secret token
- `/webhook/google-chat` — verifies the GCP project's signed JWT
- `OPTIONS *` — CORS preflight (no auth headers possible)

See [`_UNAUTHENTICATED_PATHS`](../packages/core/openexecutive/api/main.py) — any new webhook from an external service must be added here.

---

## One-person mode (no Google sign-in)

While `AUTH_GOOGLE_ID` is blank, `make dev` starts the web app in one-person
mode. The sign-in page shows an **Open** button instead of Google, and whoever
clicks it is the owner (the principal). This is for trying Open Executive on
your own computer; it never runs anywhere else.

There is no password, so the protection is *where* a request can come from:

- **Only `make dev` turns it on.** The same command starts the UI on
  `127.0.0.1`, so other machines on your network cannot connect. It sets
  `OE_LOCAL_OWNER_MODE=1` for the UI; don't set that yourself. It reads the
  same settings the app does (the root `.env` and `packages/ui/.env.local`),
  so if either one sets up Google sign-in, `make dev` starts as usual.
- **Never on a server.** A production build (`next build`, the deploy image)
  compiles the mode out, and it stays off whenever `AUTH_GOOGLE_ID` or
  `OE_PUBLIC_DEPLOYMENT` is set. `make docker` publishes port 3000 to your
  network, so it keeps Google sign-in.
- **Only from this computer's browser.** Signing in, and every request after,
  must be addressed to `localhost`, `127.0.0.1` or `[::1]`, and in this mode
  the API on port 8000 applies the same rule. That blocks a malicious website
  that tries to reach either one through your browser (DNS rebinding). The
  web app also refuses form posts that another page, including another
  `localhost` port, makes your browser send.

The session has no email. The UI proxy then sends no `x-caller-email`, and the
API treats the request as the principal's, the same way it treats the CLI. If
`AUTH_SECRET` is blank in both files, `make dev` uses a temporary one for that
run, so you click **Open** again after a restart. `AUTH_TRUST_HOST` isn't
needed in this mode.

To invite your team, or to run on a server, set up Google sign-in (below). As
soon as `AUTH_GOOGLE_ID` is set, one-person mode is off and any session it
created stops working.

---

## Required configuration

### One-time: Google Cloud Console

1. Create or pick a Google Cloud project.
2. Set up the consent screen (the **Google Auth Platform** page — formerly "OAuth consent screen"). External user type is fine; you don't need to add test users or publish the app because we only request basic scopes (`openid email profile`).
3. **APIs & Services → Clients → + Create Client → Web application**:
   - **Authorized JavaScript origins**: `http://localhost:3000`, plus your deployed UI origin (e.g. `https://exec.example.com`)
   - **Authorized redirect URIs**: `http://localhost:3000/api/auth/callback/google`, plus `<your UI origin>/api/auth/callback/google`
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

### Production

Generate both secrets once and set them on the two containers. `BACKEND_SHARED_SECRET` **must be byte-identical on the UI and the API** — a mismatch silently breaks every API call with `401`, so generate it once and paste the same value, rather than running the generator twice.

```bash
SHARED=$(openssl rand -hex 32)
AUTH=$(openssl rand -base64 32)
```

**UI:**

```
AUTH_SECRET=$AUTH
AUTH_GOOGLE_ID=<your client id>
AUTH_GOOGLE_SECRET=<your client secret>
ALLOWED_EMAILS=alice@x.com,bob@y.com
AUTH_TRUST_HOST=true
AUTH_URL=https://exec.example.com
BACKEND_SHARED_SECRET=$SHARED
```

**API** — the same `$SHARED` value:

```
BACKEND_SHARED_SECRET=$SHARED
BACKEND_ALLOWED_ORIGINS=https://exec.example.com
OE_PUBLIC_DEPLOYMENT=1
```

> **Why `AUTH_URL` is required (not just `AUTH_TRUST_HOST`)** — behind a reverse proxy or load balancer, Auth.js builds the post-OAuth-callback redirect URL from the container's bind address (`0.0.0.0`) unless told the public origin explicitly. `AUTH_TRUST_HOST=true` is necessary but not sufficient. Symptom if missing: sign-in succeeds at Google, then the browser tries to load `http://0.0.0.0/...` and fails with `ERR_CONNECTION_REFUSED`.

> **Production fails closed.** With `OE_PUBLIC_DEPLOYMENT` set and no `BACKEND_SHARED_SECRET`, [api/main.py](../packages/core/openexecutive/api/main.py) raises `RuntimeError` at startup rather than serve traffic without auth. Set it on every internet-reachable instance — see [deployment.md](deployment.md).

---

## Operations

### Add or remove a user

Access comes from two additive sources, so there are two ways in — and removal means
taking the person out of **both**.

**Add via the roster (no restart).** Create a Person with that email in People. It goes
live within the 5-minute roster cache window. This is the normal path for teammates.

**Add via `ALLOWED_EMAILS` (restart).** Append the email and restart the UI:

```
ALLOWED_EMAILS=alice@x.com,bob@y.com,carol@z.com
```

The list is read once at startup, so the new entry goes live when the restart finishes.
Recommended for your own operator/break-glass account: nothing that writes the roster —
a fixture load, an onboarding run, someone editing People — can take it away.

Rules: comma-separated, case-insensitive, whitespace around entries is stripped, trailing commas are harmless.

**Remove a user.** Archive or delete their Person **and** drop them from
`ALLOWED_EMAILS`. Removing only one leaves the other still granting access. The roster
half lands on their next gated request once the cache expires; the env half needs a UI
restart.

### Rotate the shared secret

Do this if anyone with deployment access leaves, or on a regular cadence. Generate one value and set it on both containers, then restart both:

```bash
NEW=$(openssl rand -hex 32)
```

There is a window during a rolling restart where one side has the new value and the other still has the old one; calls in that window return `401`. If that matters, stop the UI first, rotate both, then bring it back up.

### Rotate `AUTH_SECRET`

Invalidates all existing sessions (everyone is signed out and must re-auth). Use this if the secret may be compromised.

```bash
openssl rand -base64 32     # set as AUTH_SECRET on the UI, then restart it
```

### Revoke OAuth client

If `AUTH_GOOGLE_SECRET` is leaked, regenerate in Google Cloud Console (Clients → your client → **Reset Secret**), then update both your local root `.env` (and `packages/ui/.env.local` if you use one) and the deployed value. Old issued tokens stop working immediately.

---

## Debugging

| Symptom | Likely cause |
|---|---|
| `OAuth client was not found` / `invalid_client` | `AUTH_GOOGLE_ID` typo, swapped with `AUTH_GOOGLE_SECRET`, or the client lives in a different GCP project |
| `redirect_uri_mismatch` | The Authorized redirect URI in Google Console doesn't exactly match `<origin>/api/auth/callback/google`. Wait 5 min for Google to propagate after edits |
| Browser tries to load `0.0.0.0` after sign-in | `AUTH_URL` not set on the UI |
| `AccessDenied` page after Google login | Email is in neither `ALLOWED_EMAILS` nor the People roster (the two are unioned), or Google returned `email_verified !== true`. Check the `auth_login` audit row's `source`: `no_match` = checked against both lists and genuinely not on either; `env_only_roster_unavailable` = the roster fetch failed and the email isn't in the env list |
| A removed teammate can still sign in | Their email is still in `ALLOWED_EMAILS`. The roster is additive, so archiving the Person alone doesn't revoke access |
| API returns `401` for every request | UI and API have different `BACKEND_SHARED_SECRET` values (very common after rotating in two separate terminal sessions) |
| API refuses to start with `RuntimeError: BACKEND_SHARED_SECRET is required` | `OE_PUBLIC_DEPLOYMENT` is set and the secret is missing. Set it; the next restart will boot |
| Sign-in works but the chat stays empty | Backend is auth'd but `ANTHROPIC_API_KEY` is missing on the API. Its logs will show the error |
| Signed in, but no past chats are listed | Your email isn't on your own People entry, so the API can't tell who you are. Setup saves it from the **Your sign-in email** field; to fix it afterwards, add the email to your row on the People page |
| **Open** says it only works on this computer | You opened the app by a network address. Use `http://localhost:3000` in a browser on the machine running `make dev` |
| Sign-in page shows Google, but you wanted one-person mode | `AUTH_GOOGLE_ID` is set (in the root `.env` or `packages/ui/.env.local`), or the app was started some way other than `make dev` |

### Useful commands

```bash
# Live logs
docker compose logs -f ui
docker compose logs -f api

# Probe the API directly, without going through the UI
curl -sv https://api.example.com/health                                     # 200
curl -sv https://api.example.com/sessions                                   # 401
curl -sv -H "x-api-key: $SHARED" https://api.example.com/sessions           # 200
```

---

## Threat model — what this does and does not protect against

**Mitigates:**
- Random internet visitors reaching the UI or the API
- A leaked UI URL being usable by anyone with a Google account (allowlist)
- Direct API hits bypassing the UI (shared secret)
- Cookie theft from one session leaking *another* user's data (each session is independent JWT; no shared state)
- Missing-secret deploys silently exposing the API (the `OE_PUBLIC_DEPLOYMENT` fail-closed guard)
- Other pages on the same site — to a browser, every `localhost` port is one site — posting to the UI proxy with your cookie (it refuses writes whose `Sec-Fetch-Site` isn't `same-origin`)

**Does not mitigate:**
- A compromised `BACKEND_SHARED_SECRET` — anyone who learns it can hit the API as if they were the UI. Rotate if leaked.
- A compromised Google account on the allow-list — that user has full access to all shared data. The product is currently a **shared workspace**; there is no per-user data isolation.
- A compromised deploy credential — attacker can change secrets, redeploy, or read logs. Rotate deploy credentials if a CI workflow is compromised.
- Browser-side XSS — Auth.js sessions are httpOnly cookies, so JS can't read them, but a successful XSS could make authenticated requests from the victim's browser. Standard same-origin protections apply.
- Other users or programs on the same computer in one-person mode — anything that can reach `127.0.0.1:3000` can click **Open**. Use that mode only on a computer that is yours alone.

If/when per-user data isolation matters (e.g. private sessions per teammate), the change is non-trivial — see the original plan note in [PR #86](https://github.com/SenteLabsAI/OpenExecutive/pull/86) about adding `user_id` to the sessions table.
