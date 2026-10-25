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

### Pages' security headers

The UI's middleware also sets a Content-Security-Policy on every page, so it now runs on
`/signin` and Auth.js's routes too; they still skip the session check
([`middlewarePaths.ts`](../packages/ui/src/lib/middlewarePaths.ts)):

- **Scripts** run only with the response's nonce (`script-src 'self' 'nonce-…' 'strict-dynamic'`),
  plus `'unsafe-eval'` on the dev server for React's dev build. A script injected into a page
  (text from an outside source rendered as HTML, a dependency gone wrong) doesn't run.
- **No plugins and no foreign `<base>`** (`object-src 'none'`, `base-uri 'self'`).
- **No framing by other sites** (`frame-ancestors 'self'`), so no other page can show the app's
  Approve and Send buttons under its own. Auth.js's own pages get this header alone.

Every page renders per request (the root layout awaits `connection()`), because a page built
ahead of time would carry no nonce and the policy would stop its scripts. See
[`lib/csp.ts`](../packages/ui/src/lib/csp.ts).

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
   `<issuer>/.well-known/openid-configuration`. It must be an `https://`
   address: the ID token is trusted because it arrives over TLS from the
   provider, so over plain http anyone on the network path could forge one.
   `http://localhost` is accepted for testing on your own computer. All
   three of issuer, id and secret are needed; Settings → Setup status says
   which is missing.
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
  `ask_executive` asks as no one, and the owner's memory and activity, the
  briefing and the roster are refused. `/mcp` reads the caller the gate
  verified, so a sign-in in front of it that sends a signed assertion gets
  that person's answers.
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
- **A card's Approve button in Telegram doesn't need them.** In a private Telegram chat on
  a webhook with `TELEGRAM_WEBHOOK_SECRET` set, a tap is tied to the person by their chat
  id on the People roster. Whoever can edit someone's Telegram chat id there can approve
  their cards, so on a server without signed callers keep the shared secret to the owner.

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

### Host origins (showing Open Executive inside another app)

Another web app can show Open Executive's pages in a frame and call a short list of its
routes from its own pages, as the signed-in person: a *host origin*. List each one,
exactly, on the **UI**:

```bash
OE_HOST_ORIGINS=https://app.example.com            # comma- or space-separated
```

Unset (the default), nothing changes apart from the policies under **Every page** below.
A malformed entry refuses the whole setting (logged at startup), so a typo never widens
what is trusted.

**Same site, two origins.** Serve the UI on a sibling host of the host app, under one
registrable domain (`exec.example.com` beside `app.example.com`; locally two names under
one domain such as `exec.example.localhost` and `app.example.localhost`, never
`exec.localhost` beside `app.localhost`, which browsers treat as two sites). The browser
then sends the UI's `SameSite=Lax` session cookie with the host's calls and inside its
frame, while keeping the two origins' pages, storage and responses apart: the host's
scripts can't read or drive Open Executive's pages. An entry must be `https://` (plain
`http://` only for a name under `localhost`). `docs/deployment.md` has the hosting rules.

**Every page**, host origins set or not, sends a `Content-Security-Policy`
(`src/lib/csp.ts`, set by `src/middleware.ts`):

- `script-src 'self' 'nonce-…' 'strict-dynamic'`: a fresh nonce per response, which
  Next.js puts on its own scripts; no `'unsafe-inline'`, and no `'unsafe-eval'` outside
  the dev server. An injected script doesn't run. Every page renders on request for this
  (the root layout waits for it).
- `object-src 'none'; base-uri 'self'`.
- `frame-ancestors 'self'` plus the host origins: no other site can frame the Approve and
  Send buttons.

Auth.js's own pages get `frame-ancestors 'self'`, and whatever the API answers through
the proxy without a policy of its own (JSON, its HTML docs, an error page) gets
`default-src 'none'; frame-ancestors 'none'; sandbox`, so nothing in it runs on this
origin. Downloads keep the API's own sandbox policy.

**What a host origin may call: the host list** (`packages/ui/src/lib/host-list.json`).
Each entry is a method and an API path. It holds: reading who is signed in (`GET /auth/me`,
so the host can warn when that isn't its own user), the briefing, cards,
conversations and the watch list; acknowledging, snoozing, reopening and reviewing
alerts; chatting (send, stop, add a message, attach, dictate); and the watch list
(describe, add, approve or decline a proposal, edit, delete). Never approving, rejecting
or cancelling a card, preparing a draft, Act as me, delegation, settings or People.
`tests/unit/test_host_list.py` checks every entry against the API's routes and against
that rule. The proxy enforces it:

- A request whose `Origin` is exactly a listed host origin, with the browser's
  `Sec-Fetch-Site: same-site`, may call only the host list, and gets CORS for that exact
  origin with credentials. Anything else naming a host origin gets `403` (with CORS, so
  the host's page can read why), as does a call off the list.
- The middleware answers a host origin's preflight (`OPTIONS`, which carries no cookie)
  before the session check, for listed calls only. A listed call with no session gets
  `401` with CORS, not the sign-in redirect, so the host can offer sign-in.
- Once judged, the proxy drops the call's `Origin`, `Referer` and `Sec-Fetch-*` before
  the API: the API's own local-login rule (`_OWN_PAGE_FETCH_SITES`) would refuse a
  same-site write, and the call's surface (below) tells the API what it needs.
- Every other request is judged as before: `lib/crossSite.ts` still refuses every
  cross-site and same-site write that isn't a host call.

**The `host` surface.** The proxy marks a host origin's call as surface `host`: in the
signed caller assertion (an optional `surface` claim) and as `x-caller-surface`. A
client's own copy is stripped with the rest of `x-caller-*`. With signed callers on, the
API reads it from the verified assertion only, and the header must agree with it (`401`
otherwise); without them it reads the header, as it does `x-caller-email`. A chat turn on
that surface (`Session.from_host`) only reads and advises: it is offered the specialists,
the read tools and the gateway's reads, never anything that sends, posts, books,
schedules, runs or changes stored state (`MCP_CLIENT_WITHHELD_TOOLS`), the principal-only
tools, Act as me, `recall_history`, scripts or extension tools. A call the model makes
anyway is refused, telling the person to start a new chat in Open Executive itself. Its
words are never taken as the person's own: no memory extraction, open loop, working
style, History note, conversation review or peer memory learns from it, and it is never
a verified speaker. It can stop or add to its own turns only.
A host's page writes only in a conversation it started or already wrote in (`409`
otherwise), and **a conversation a host's page wrote into stays that way on every page:** each user line
records the surface that sent it (`chat_messages.sender_surface`), and every later turn in
that conversation, from Open Executive's own pages too, gets the same read-only rules,
since the host's line stays in what the model reads and could be an instruction planted
for the person's next turn. The mark is the conversation's and sticks
(`sessions.host_wrote`): taking the line back (Try again, Edit) and sending its words
from Open Executive's page is still read-only. `tests/unit/test_host_surface_turns.py` lists what such a turn
is offered; `tests/integration/test_chat_host_surface.py` checks the conversation rule.

**Embedded mode.** A page opened as `…?embed=1` in a frame whose parent is a listed host
origin sends that parent `ready`; once the parent answers with a valid `init`, the page
leaves out its own sidebar, top bar, bottom bar and Ask OE panel and takes the host's
colours, font and name. Until then it renders as it would anywhere. The messages
(`oe-embed/v1`: `ready`, `init`, `theme`, `route`, `open` and `badge`) are in
`src/lib/embed.ts`, with a fixed set of examples in
`src/lib/embed-examples.json` that a host's own tests check too. They carry no secret,
token or identity anyone acts on; every message is checked for the exact origin and the
parent window, and every `init` field is validated. In a frame:

- **Sign-in opens in a tab of its own, in any frame.** Most sign-in providers refuse to be
  framed, and a sign-in button under a page this app doesn't control can be pressed by a
  lured click. So `/signin` in a frame shows no sign-in forms, only a link that opens it in
  a new tab; when the person comes back signed in, the frame goes on to where it was headed.
- **The host's user hint** (an email in `init`, display only) shows a warning with Sign out
  when it isn't who is signed in here, so the next person at a shared computer doesn't
  work on as the last one without knowing. A host's own screens can do the same with
  `GET /auth/me`.
- **Changes ask inside the frame, and the browser must have seen the question, in any
  frame** (embedded or not, since a host page can frame these pages without saying so).
  Approving, rejecting or cancelling a card, a proposal, a workflow sign-off or a roster
  request, every chat message sent from the frame (the host's are read-only, but one sent
  from this app's page is the person's own turn), and any other change the host list
  doesn't already allow with the same effect (a setting, Handle it for me, People, an
  upload) made within 10 seconds of a click, key, drop or paste in the frame, or of a Yes,
  first asks in a panel inside the frame naming what will happen (for a chat message, the
  message). The frame checks every request it sends (`lib/cardActions.ts`), so no button
  can skip it. The page around a frame can make it invisible, cover it and lure clicks,
  so the panel's Yes counts only when all of these hold (`lib/seen.ts`):
  - IntersectionObserver v2 (`trackVisibility: true`) has reported the panel whole on
    screen, uncovered, untransformed, unfiltered and at full opacity, without a break, for
    at least 700 ms. The browser computes that answer; the page around can't fake it. A
    cover that lets clicks through (`pointer-events: none`) and a frame cropped to show the
    Yes without the words above it fail it too. The browser reports at most every 100 ms;
    reports it hasn't delivered yet are read at each look (`takeRecords`);
  - the real pointer has been inside the panel, without leaving, for at least 500 ms, and
    has moved there (a cursor drawn by the page around doesn't move it). A frame slid under
    the pointer between the two clicks of a lured double-click fails this. Leaving the
    panel, or the frame losing focus, starts it again;
  - the press began on Yes after all of that held, and the click that follows is trusted
    and on Yes too. A key press on a focused Yes has no pointer press, so it never counts;
  - 250 ms after the click, the panel is still in the same unbroken run of "seen" and the
    pointer still inside, so a cover laid on just before the click shows up first.

  The panel keeps Open Executive's own colours and face (`.consent` in `globals.css`),
  whatever the host's theme: otherwise a host could draw the question white on white, or in
  a symbol font, while the browser still reports it seen. Anyone who can't point (a
  keyboard, a touch screen) answers on this app's own page, which the panel links to.

  A browser without v2 (Firefox, Safari) can't tell, so the panel offers no Yes there: it
  links to this app's own page for it, top level in the same tab (the card's page,
  `/cards/<id>`, or else the page that asked, without `embed`), where nothing can frame
  it; Back returns to the host. It never fails open. The answer stays in the browser:
  what it guards against is code on the host's origin, which can't run in this app's
  origin, and the change itself is still this app's same-origin request with its usual
  checks.

`make embed-outputs` writes what a host app checks itself against without running this
app or holding its secrets: `embed-manifest.json` (the navigation and routes),
`host-list.json` and `openapi.json`.

**What remains.** A host origin runs code Open Executive doesn't control: the host app,
everything installed in it, and whatever its administrators add. That code can do what
the host list allows, as whoever has the host open and is signed in here: read their
email, the briefing, alerts, cards, conversations (whose replies can draw on the person's memory and
mail) and the watch list; start a read-only chat turn, in a conversation it started, and
read its reply; handle alerts and the watch list. It can't approve, send, change settings
or People, act through a chat turn (its lines make a conversation read-only everywhere,
so a chat started from the host stays read-only in Open Executive too, and it can't
write in the person's own conversations), or read or click inside the frame. A chat it
starts shows in the person's chat list like their own (held read-only from its first
moment, but not labelled there yet; it is left out of the brief). It can run the alert
review when it likes, which may itself route, nudge or escalate alerts to teammates as
the scheduled review would, and add watches on addresses of its choosing, whose findings
arrive marked as outside content, as any watch's do. A framed page reports its path, query and title on
every navigation, which on a page about one record (a person, a run) names it, and on a
search page carries what was searched for.
It can lure clicks onto a framed page it has made invisible, but every change beyond the
host list asks inside the frame, and its Yes counts only once the browser has seen the
question and the person pointed at it (or, in a browser that can't tell, on this app's
own page). Every host origin you list is
trusted that far: list only apps whose administrators you would trust with it, and read every change
to the host list, the `host` surface's tools and these policies with a security review.

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
| No SSO button | One of `AUTH_OIDC_ISSUER`, `AUTH_OIDC_ID`, `AUTH_OIDC_SECRET` is blank, or the issuer isn't `https://` (only `http://localhost` is allowed). Settings → Setup status names it |
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
- Other sites framing the UI's buttons (clickjacking) and injected scripts running in its pages (the pages' `Content-Security-Policy`: `frame-ancestors` and a nonce per response)
- A host origin's scripts (`OE_HOST_ORIGINS`) approving cards, sending mail, changing settings or People, or acting through a chat turn (the host list; the `host` surface's read-only chat, which a conversation keeps once a host's page wrote in it; changes in a frame refused)

**Does not mitigate:**
- A compromised `BACKEND_SHARED_SECRET` — anyone who learns it can hit the API as if they were the UI. Rotate if leaked. With signed callers on, they can use it only as a service, never as a signed-in user or the owner.
- A compromised `CALLER_ASSERTION_PRIVATE_KEY` — whoever holds it can sign as anyone. It lives only on the UI; rotate it (above) if the UI's environment leaks.
- A compromised Google account on the allow-list — that user has full access to all shared data. The product is currently a **shared workspace**; there is no per-user data isolation.
- A compromised deploy credential — attacker can change secrets, redeploy, or read logs. Rotate deploy credentials if a CI workflow is compromised.
- Browser-side XSS — Auth.js sessions are httpOnly cookies, so JS can't read them, but a successful XSS could make authenticated requests from the victim's browser. Standard same-origin protections apply.
- A listed host origin's scripts reading what the host list allows (the briefing, conversations and their replies, the watch list) and starting chat turns as whoever has the host open: see "Host origins", **What remains**.
- Other users or programs on the same computer while local login is on — anything that can reach `127.0.0.1:3000` can click **Open**. Use it only on a computer that is yours alone.

If/when per-user data isolation matters (e.g. private sessions per teammate), the change is non-trivial — see the original plan note in [PR #86](https://github.com/SenteLabsAI/OpenExecutive/pull/86) about adding `user_id` to the sessions table.
