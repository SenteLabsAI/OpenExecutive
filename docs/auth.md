# Authentication & Access Control

Open Executive is gated behind Google sign-in or [SSO sign-in](#sso-sign-in-openid-connect) (any OpenID Connect provider: Keycloak, Okta, Entra ID, …) plus an email allow-list — except with [local login](#local-login-no-google-sign-in), for trying it on your own computer. This doc explains what's protected, how, and how to operate it (add/remove users, rotate secrets, debug failures).

---

## What's protected, by what

Two independent layers. Either one alone would be insufficient; together they fail closed.

| Layer | What it does | Where |
|---|---|---|
| **UI: Auth.js v5 + Google OAuth and/or OpenID Connect** | Anyone hitting the public UI is redirected to `/signin`. Only accounts with a verified email on the allow-list can complete sign-in — the **union** of `ALLOWED_EMAILS` and the People roster (see below). | [packages/ui/src/auth.ts](../packages/ui/src/auth.ts), [packages/ui/src/middleware.ts](../packages/ui/src/middleware.ts), [packages/ui/src/app/signin/page.tsx](../packages/ui/src/app/signin/page.tsx) |
| **API: shared-secret header** | The FastAPI backend is reachable over the network. It rejects every request whose `x-api-key` header doesn't match `BACKEND_SHARED_SECRET`. The UI proxy stamps this header on every upstream call. | [packages/core/openexecutive/api/main.py](../packages/core/openexecutive/api/main.py), [packages/ui/src/app/api/backend/[...path]/route.ts](../packages/ui/src/app/api/backend/%5B...path%5D/route.ts) |
| **API: signed callers** (optional, recommended on a server) | The proxy also signs *who* is signed in, with a key only the UI holds; the API checks the signature instead of taking `x-caller-email` from whoever holds the shared secret. See [Signed callers](#signed-callers). | [packages/core/openexecutive/api/caller.py](../packages/core/openexecutive/api/caller.py), [packages/ui/src/lib/callerAssertion.ts](../packages/ui/src/lib/callerAssertion.ts) |

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

While the owner's People entry has no email, someone let in by `ALLOWED_EMAILS` who is
on no entry can re-run the setup interview and add the address they signed in with to
the owner's entry. That is how an owner who set up with local login links theirs, and
it makes them the owner. So keep `ALLOWED_EMAILS` to people you would trust as the owner, or
make sure the owner's entry has an email. Only the owner can change the People list itself.

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
                ├── no session  ──► redirect to /signin → Google or SSO → callback → cookie set
                │
                └── has session ──► /api/backend/[...path] (proxy)
                                        │ stamps x-api-key and x-caller-email,
                                        │ and signs x-caller-assertion (signed callers)
                                        ▼
                                 api.example.com (FastAPI)
                                        │ middleware verifies x-api-key (constant-time)
                                        │ and, with signed callers on, the assertion
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

## Local login (no Google sign-in)

While `AUTH_GOOGLE_ID` and `AUTH_OIDC_ISSUER` are blank, `make dev` starts the web app with local
login. The sign-in page shows an **Open** button instead of Google, and whoever
clicks it is the owner (the principal). This is for trying Open Executive on
your own computer; it never runs anywhere else.

There is no password, so the protection is *where* a request can come from:

- **Only `make dev` turns it on.** The same command starts the UI on
  `127.0.0.1`, so other machines on your network cannot connect. It sets
  `OE_LOCAL_LOGIN=1` for the UI; don't set that yourself. It reads the
  same settings the app does (the root `.env` and `packages/ui/.env.local`),
  so if either one sets up Google or SSO sign-in, `make dev` starts as usual.
- **Never on a server.** A production build (`next build`, the deploy image)
  compiles the mode out, and it stays off whenever `AUTH_GOOGLE_ID`,
  `AUTH_OIDC_ISSUER` or `OE_PUBLIC_DEPLOYMENT` is set. `make docker` publishes port 3000 to your
  network, so it keeps Google sign-in.
- **Only from this computer's browser.** Signing in, and every request after,
  must be addressed to `localhost`, `127.0.0.1` or `[::1]`, and the API on
  port 8000 applies the same rule. That blocks a malicious website that
  re-points its own name at your computer to reach either one (DNS
  rebinding). Both also refuse writes that another page makes your browser
  send, such as a form posted from another site or another `localhost` port.
  A Telegram webhook arriving through a tunnel is accepted only when
  `TELEGRAM_WEBHOOK_SECRET` is set to a value Telegram can send, so Telegram
  can prove it sent it.

The session has no email. The UI proxy then sends no `x-caller-email`, and the
API treats the request as the principal's, the same way it treats the CLI.
That includes **Send** on a reply Act as me drafted in your own Gmail: under
local login, any program on this computer that can reach port 8000 can tap it
for you (it sends only a draft already waiting on Today, to the people shown on
it). On a computer other people or untrusted programs use, set up Google
sign-in with signed callers instead. If
`AUTH_SECRET` is blank in both files, `make dev` uses a temporary one for that
run, so you click **Open** again after a restart. `AUTH_TRUST_HOST` isn't
needed in this mode.

To invite your team, or to run on a server, set up Google or SSO sign-in
(below). As soon as `AUTH_GOOGLE_ID` or `AUTH_OIDC_ISSUER` is set, local login
is off and any session it created stops working.

---

## Required configuration

### One-time: Google Cloud Console

1. Create or pick a Google Cloud project.
2. Set up the consent screen (the **Google Auth Platform** page — formerly "OAuth consent screen"). External user type is fine; you don't need to add test users or publish the app because we only request basic scopes (`openid email profile`).
3. **APIs & Services → Clients → + Create Client → Web application**:
   - **Authorized JavaScript origins**: `http://localhost:3000`, plus your deployed UI origin (e.g. `https://exec.example.com`)
   - **Authorized redirect URIs**: `http://localhost:3000/api/auth/callback/google`, plus `<your UI origin>/api/auth/callback/google`
4. Copy the Client ID and Client secret immediately — the secret is shown only once.

### SSO sign-in (OpenID Connect)

For a company whose people sign in through their own identity provider —
Keycloak, Okta, Auth0, Authentik, JumpCloud, GitLab, Microsoft Entra ID, or
any other OpenID Connect provider — instead of (or as well as) Google. The
sign-in page then shows **Sign in with SSO** (or the name you give it); when
Google is set up too, both buttons are there.

SSO only adds a way in. The provider returns the person's email, and that
email goes through the same allow-list (`ALLOWED_EMAILS` plus the People
roster), the same per-request re-check and the same audit log as a Google
sign-in. One provider per install.

1. At your provider, register a **confidential web client** (one with a
   client secret) using the authorization-code flow, with the redirect URI
   `<your UI origin>/api/auth/callback/oidc` (and
   `http://localhost:3000/api/auth/callback/oidc` for local testing). It needs
   the `openid email profile` scopes.
2. Set on the UI:

   ```
   AUTH_OIDC_ISSUER=https://sso.example.com/realms/company
   AUTH_OIDC_ID=<client id>
   AUTH_OIDC_SECRET=<client secret>
   AUTH_OIDC_NAME=Okta        # optional: the button says "Sign in with Okta"
   ```

   `AUTH_OIDC_ISSUER` is the provider's issuer, exactly as its tokens state
   it; the app reads everything else from
   `<issuer>/.well-known/openid-configuration`. All three of issuer, id and
   secret are needed; Settings → Setup status says which is missing.
3. Restart the UI.

**The email must be verified.** Access is granted by email address, so an
SSO sign-in is refused (`email not verified` in the audit log) unless the
provider marks the email verified with the standard `email_verified` claim.
At many providers people can edit their own profile, and an unverified
address could be anyone's, including the owner's. How to satisfy it:

| Provider | What to do |
|---|---|
| Keycloak | Tick **Email verified** on each user, or for users imported from LDAP / Active Directory turn on **Trust Email** on the user federation provider. Also check that users can't change their own email in the account console, or that changing it requires re-verification. |
| Okta, Auth0, Authentik, JumpCloud | Sends `email_verified` for verified addresses; nothing to do. |
| Microsoft Entra ID | Entra doesn't send `email_verified`. Use the tenant's issuer `https://login.microsoftonline.com/<tenant-id>/v2.0` (not `common`), and under **Token configuration** add the optional ID-token claims `email` and `xms_edov`. The app accepts `xms_edov` (Microsoft's "email domain verified") from an Entra issuer only. |

If your provider can't say an email is verified and people **cannot** change
their own email there, you can opt in with
`AUTH_OIDC_TRUST_UNVERIFIED_EMAIL=true`. Setup status then shows an amber
light as a reminder. Don't set it on a provider where people can edit their
own email or sign themselves up.

An email means the same person whichever button they used: a Google sign-in
and an SSO sign-in with the same address are one user. Your provider decides
which addresses it hands out, so only connect one you trust as much as the
allow-list itself.

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
# …and/or SSO (see "SSO sign-in" above):
# AUTH_OIDC_ISSUER=https://sso.example.com/realms/company
# AUTH_OIDC_ID=<client id>
# AUTH_OIDC_SECRET=<client secret>
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

### Signed callers

The shared secret proves a request came from the UI, but not *who* is signed in: the proxy
says so in `x-caller-email`, and the API believes whoever holds the secret. Anyone with it
could name the owner and use owner-only routes. Signed callers close that gap. The proxy
signs who is calling with an Ed25519 key that only the UI holds, and the API checks the
signature with the public key. **Setup** warns while a shared secret is set without them.

Make the pair once:

```bash
uv run --with cryptography python scripts/make-caller-keys.py
```

It prints two lines. Put `CALLER_ASSERTION_PRIVATE_KEY` on the **UI** only and
`CALLER_ASSERTION_PUBLIC_KEYS` on the **API** only, then restart both, UI first. The UI
keeps sending `x-caller-email` as well, so it works against an API without the keys.

With the keys set:

- **Each assertion covers one request.** It holds only for the method and the exact path
  and query it was signed for, expires within 30 seconds, and works once.
- **No caller header at all is a service.** A script, `curl` with only `x-api-key`, or an
  MCP client can still use the API, but it is **never the owner**. For MCP,
  `ask_executive` asks as no one, and its `caller_email` is ignored.
- **The UI's local-login session is the operator.** It is signed, names no one and runs as
  the owner, as before.
- **Refusals are 401.** You get one for:
  - an `x-caller-email` with no assertion (`code: caller_assertion_required`);
  - a bad, expired, reused or mismatched assertion (`caller_assertion_invalid`).

  The API logs the reason, never the assertion.
- **A bad key stops startup.** A public key that can't be read stops the API at boot. A
  private key that can't be read makes the UI refuse every call with `500`. Neither falls
  back to trusting the header.
- **Send on a drafted reply needs them.** Act as me's **Send** (a reply the Executive drafted
  in the owner's own Gmail, waiting on Today) sends mail as the owner, so it works only when
  the API can tell it is the owner asking: with the keys set, or under local login. On a
  server without them it answers `409 caller_signing_required`, and the owner sends the
  draft from Gmail instead.

**To call an owner-only route from a terminal**, sign that one request with the UI's key:

```bash
export CALLER_ASSERTION_PRIVATE_KEY=...   # the UI's; never print or store it
curl -H "x-api-key: $SHARED" \
     -H "x-caller-assertion: $(uv run --with cryptography python scripts/mint-caller-assertion.py GET /today)" \
     https://api.example.com/today
```

It signs as the operator. Pass `--email` to sign as a signed-in person. Sign the path
exactly as `curl` sends it, query included.

**Rotate** without downtime:

1. Make a new pair.
2. Add its public key to the API's comma-separated list, then restart the API.
3. Move the UI to the new private key.
4. Drop the old public key.

The API remembers used assertions in memory, so it must run as one process. It does
already, because the scheduler runs inside it.

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
| `AccessDenied` page after Google or SSO login | Email is in neither `ALLOWED_EMAILS` nor the People roster (the two are unioned), or the provider didn't mark it verified (audit `reason`: `email_not_verified`; for SSO see [the table above](#sso-sign-in-openid-connect)). Check the `auth_login` audit row's `source`: `no_match` = checked against both lists and genuinely not on either; `env_only_roster_unavailable` = the roster fetch failed and the email isn't in the env list |
| A removed teammate can still sign in | Their email is still in `ALLOWED_EMAILS`. The roster is additive, so archiving the Person alone doesn't revoke access |
| API returns `401` for every request | UI and API have different `BACKEND_SHARED_SECRET` values (very common after rotating in two separate terminal sessions) |
| API refuses to start with `RuntimeError: BACKEND_SHARED_SECRET is required` | `OE_PUBLIC_DEPLOYMENT` is set and the secret is missing. Set it; the next restart will boot |
| Every call is `401` with `caller_assertion_required` | The API has `CALLER_ASSERTION_PUBLIC_KEYS` but the UI has no `CALLER_ASSERTION_PRIVATE_KEY`. Set it on the UI and restart it |
| Calls are `401` with `caller_assertion_invalid` | The UI's private key isn't the pair of any public key on the API, or a clock is off by more than a minute. The API log gives the reason. `reason=path` means something between the two rewrote the path, such as a proxy that strips a prefix |
| Every UI call fails with `caller signing is misconfigured` | `CALLER_ASSERTION_PRIVATE_KEY` on the UI isn't `<key id>:<key>` as `scripts/make-caller-keys.py` prints it |
| API refuses to start with `CALLER_ASSERTION_PUBLIC_KEYS can't be used` | The list is malformed. Paste the line the script printed |
| An owner-only route refuses your `curl` | With signed callers on, a request carrying only `x-api-key` is a service and never the owner. Sign it with `scripts/mint-caller-assertion.py` |
| Sign-in works but the chat stays empty | Backend is auth'd but `ANTHROPIC_API_KEY` is missing on the API. Its logs will show the error |
| Signed in, but no past chats are listed | Your email isn't on your own People entry, so the API can't tell who you are. Setup saves it from the **Your sign-in email** field; to fix it afterwards, add the email to your row on the People page |
| **Open** says it only works on this computer | You opened the app by a network address. Use `http://localhost:3000` in a browser on the machine running `make dev` |
| SSO sign-in fails with `Configuration` | `AUTH_OIDC_ISSUER` isn't the provider's exact issuer (the UI log shows the discovery error; open `<issuer>/.well-known/openid-configuration` and compare its `issuer`), or the client id / secret is wrong. For Entra ID, use the tenant issuer, not `common` |
| SSO login comes back with `redirect_uri` errors at the provider | The client's redirect URI must be exactly `<origin>/api/auth/callback/oidc` |
| No SSO button | One of `AUTH_OIDC_ISSUER`, `AUTH_OIDC_ID`, `AUTH_OIDC_SECRET` is blank. Settings → Setup status names it |
| Sign-in page shows Google, but you wanted local login | `AUTH_GOOGLE_ID` or `AUTH_OIDC_ISSUER` is set (in the root `.env` or `packages/ui/.env.local`), or the app was started some way other than `make dev` |

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
- Someone who holds the shared secret acting as another user or as the owner (signed callers, when set: only the UI's private key can say who is calling)
- Cookie theft from one session leaking *another* user's data (each session is independent JWT; no shared state)
- Missing-secret deploys silently exposing the API (the `OE_PUBLIC_DEPLOYMENT` fail-closed guard)
- Other pages on the same site — to a browser, every `localhost` port is one site — posting to the UI proxy with your cookie (it refuses writes whose `Sec-Fetch-Site` isn't `same-origin`)

**Does not mitigate:**
- A compromised `BACKEND_SHARED_SECRET` — anyone who learns it can hit the API as if they were the UI. Rotate if leaked. With signed callers on, they can use it only as a service, never as a signed-in user or the owner.
- A compromised `CALLER_ASSERTION_PRIVATE_KEY` — whoever holds it can sign as anyone. It lives only on the UI; rotate it (above) if the UI's environment leaks.
- A compromised Google account on the allow-list — that user has full access to all shared data. The product is currently a **shared workspace**; there is no per-user data isolation.
- A compromised deploy credential — attacker can change secrets, redeploy, or read logs. Rotate deploy credentials if a CI workflow is compromised.
- Browser-side XSS — Auth.js sessions are httpOnly cookies, so JS can't read them, but a successful XSS could make authenticated requests from the victim's browser. Standard same-origin protections apply.
- Other users or programs on the same computer while local login is on — anything that can reach `127.0.0.1:3000` can click **Open**. Use it only on a computer that is yours alone.

If/when per-user data isolation matters (e.g. private sessions per teammate), the change is non-trivial — see the original plan note in [PR #86](https://github.com/SenteLabsAI/OpenExecutive/pull/86) about adding `user_id` to the sessions table.
