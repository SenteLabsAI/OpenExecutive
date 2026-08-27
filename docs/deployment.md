# Dev Deployment — Fly.io + GitHub Actions

The `main` branch is continuously deployed to a shared dev environment on Fly.io. This doc covers how it works, how to set it up from scratch, and how to operate it.

---

## Topology

Two Fly apps in the same org:

| App | Source | Public URL | State |
|---|---|---|---|
| `openexec-api-dev` | [docker/Dockerfile](../docker/Dockerfile) — Python 3.11 + FastAPI + scheduler | `https://openexec-api-dev.fly.dev` | Persistent volume `executive_data` mounted at `/data` |
| `openexec-ui-dev` | [docker/Dockerfile.ui](../docker/Dockerfile.ui) — Next.js 16 standalone build | `https://openexec-ui-dev.fly.dev` | Stateless |

The UI talks to the API only through its server-side proxy route at [packages/ui/src/app/api/backend/[...path]/route.ts](../packages/ui/src/app/api/backend/[...path]/route.ts), which reads `BACKEND_BASE_URL` at request time. Currently set to the public API URL — see "Why not `.flycast`?" below.

### Why the API is pinned to one machine

The scheduler in [packages/core/openexecutive/scheduler](../packages/core/openexecutive/scheduler) claims due actions with `UPDATE … RETURNING`. Two machines running the same scheduler would double-fire (and double-bill the Anthropic API). The API app is configured to keep this from happening:

```toml
# fly.api.toml
[deploy]
  strategy = "immediate"        # replace in place; no overlap during rollout

[http_service]
  min_machines_running = 1
  max_machines_running = 1
```

Do **not** `flyctl scale count 2` on the API app. If horizontal scale is ever needed, gate the scheduler on machine identity first.

### Persistent state

The volume `executive_data` is mounted at `/data`. Three things live there:

- `/data/chroma_db/` — ChromaDB vector index (built-in knowledge + uploaded company docs)
- `/data/episodic_memory.db` — SQLite for episodic memory, alerts, knowledge review, scheduled actions
- `/data/company/profile.yaml` + `/data/company/docs/` — onboarding output + uploaded docs

Path mappings come from these Fly env vars (set in [fly.api.toml](../fly.api.toml)):

```toml
VECTOR_STORE_PATH      = "/data/chroma_db"
EPISODIC_DB_PATH       = "/data/episodic_memory.db"
COMPANY_PROFILE_PATH   = "/data/company/profile.yaml"
MCP_SERVERS_CONFIG_PATH = "/data/company/mcp_servers.json"
```

On first boot, the volume is empty — ChromaDB rebuilds the built-in knowledge index from files shipped inside the Python package at `openexecutive/knowledge/builtin/`. The episodic SQLite is created on demand. The company profile stays empty until you run the onboarding wizard against the dev URL.

---

## How a push becomes a deploy

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. **`changes` job** — runs `dorny/paths-filter@v3` against the pushed commit and computes two booleans:

   ```
   api: packages/core/**, knowledge/**, docker/Dockerfile, fly.api.toml, .github/workflows/deploy.yml
   ui:  packages/ui/**,   docker/Dockerfile.ui, fly.ui.toml, .github/workflows/deploy.yml
   ```

   Editing the workflow itself triggers both, by design.

2. **`deploy-api`** runs `flyctl deploy --remote-only --config fly.api.toml` if `api == true`. Authenticated with the `FLY_API_TOKEN_API` repo secret (an app-scoped deploy token).

3. **`deploy-ui`** runs `flyctl deploy --remote-only --config fly.ui.toml` if `ui == true`. Uses `FLY_API_TOKEN_UI`.

Each job uses a `concurrency` group so two pushes can't deploy the same app simultaneously. The workflow does **not** block on CI — branch protection on `main` is the gate; if it's off, broken pushes can deploy.

### Manually trigger a deploy

```
gh workflow run "Deploy (dev)" -f target=api      # api only
gh workflow run "Deploy (dev)" -f target=ui       # ui only
gh workflow run "Deploy (dev)" -f target=both     # both
```

Useful when you want to redeploy without a code change (e.g., after rotating a secret).

---

## One-time setup (from scratch)

These steps were done once to bootstrap the environment. They are documented here for the next person.

### 1. Install flyctl

```
brew install flyctl
flyctl auth login          # or: flyctl auth signup
```

### 2. Create both apps

```
flyctl apps create openexec-api-dev
flyctl apps create openexec-ui-dev
```

### 3. Create the volume

```
flyctl volumes create executive_data --region iad --size 1 -a openexec-api-dev
```

Answer `y` to the "two-volumes-for-HA" warning — single-instance is intentional.

### 4. Set secrets on the API app

Minimum to boot — all three are required. Without any one the app fails startup and crash-loops (uvicorn exit 3): missing `ANTHROPIC_API_KEY` or `EXEC_EMAIL_ADDRESS` is a pydantic `ValidationError`; missing `BACKEND_SHARED_SECRET` raises a `RuntimeError` on Fly.

```
flyctl secrets set -a openexec-api-dev \
  ANTHROPIC_API_KEY=sk-ant-... \
  BACKEND_SHARED_SECRET=$(openssl rand -hex 32) \
  EXEC_EMAIL_ADDRESS=exec@yourcompany.com
```

(`BACKEND_SHARED_SECRET` must match the UI app's value — see the auth note below; `EXEC_EMAIL_ADDRESS` is a required `Settings` field with no default.)

Optional integrations (add only the ones you want active on dev — use *dev* credentials, not personal/prod):

```
flyctl secrets set -a openexec-api-dev \
  USER_TIMEZONE=America/Los_Angeles \
  TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 16) \
  GOOGLE_OAUTH_CLIENT_ID=... \
  GOOGLE_OAUTH_CLIENT_SECRET=... \
  SLACK_BOT_TOKEN=xoxb-... \
  SLACK_APP_TOKEN=xapp-... \
  SCHEDULED_ADMIN_TOKEN=$(openssl rand -hex 32)
```

> Channel access (who can DM the bot via Email / Telegram / Discord /
> Slack) is roster-driven now — manage allowed senders via the /people
> UI by adding their channel ID to a Person row. The old env-var
> allowlists (EMAIL_ALLOWED_SENDERS, TELEGRAM_ALLOWED_CHAT_IDS,
> DISCORD_ALLOWED_USER_IDS) have been removed.

See [.env.example](../.env.example) for the full list and what each one does.

> **Auth secrets are not optional in production.** The UI app needs `AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`, `AUTH_URL`, `AUTH_TRUST_HOST`, `ALLOWED_EMAILS`, and `BACKEND_SHARED_SECRET`. The API app needs `BACKEND_SHARED_SECRET` (same value) and `BACKEND_ALLOWED_ORIGINS`. The API will refuse to start on Fly without `BACKEND_SHARED_SECRET` set. See [auth.md](auth.md) for the full setup including Google Cloud Console steps.

### 5. Create per-app deploy tokens and add them to GitHub

```
flyctl tokens create deploy -a openexec-api-dev -x 999999h
flyctl tokens create deploy -a openexec-ui-dev  -x 999999h
```

Each command outputs `FlyV1 <macaroon>`. The full string (including the `FlyV1 ` prefix and space) is the token value.

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `FLY_API_TOKEN_API` | token from the `openexec-api-dev` command |
| `FLY_API_TOKEN_UI`  | token from the `openexec-ui-dev` command |

### 6. First deploy

Either merge a PR to `main` or trigger manually:

```
gh workflow run "Deploy (dev)" -f target=both
```

### 7. Configure external webhooks

Once the API is reachable, point any external services at it:

- **Telegram**: `curl -F "url=https://openexec-api-dev.fly.dev/webhook/telegram" "https://api.telegram.org/bot<DEV_TOKEN>/setWebhook"`
- **Google OAuth**: in the GCP console, add `https://openexec-api-dev.fly.dev/oauth/google/callback` as an authorized redirect URI on the OAuth client.

---

## QA environment

QA is a second, **more stable** environment that updates only when you deliberately promote a known-good commit. Dev moves on every push to `main`; QA moves only on the `qa` branch.

| App | Config | Public URL | State |
|---|---|---|---|
| `openexec-api-qa` | [fly.api.qa.toml](../fly.api.qa.toml) — same image as dev | `https://openexec-api-qa.fly.dev` | Persistent volume `executive_data` at `/data` |
| `openexec-ui-qa` | [fly.ui.qa.toml](../fly.ui.qa.toml) — `BACKEND_BASE_URL` points at the QA API | `https://openexec-ui-qa.fly.dev` | Stateless |

The QA apps are byte-for-byte the same Docker images as dev — only the app name, the API URL the UI proxies to, the secrets, and the deploy trigger differ.

### Promotion flow

[`.github/workflows/deploy-qa.yml`](../.github/workflows/deploy-qa.yml) deploys QA on push to the `qa` branch (path-filtered, same logic as dev) or via manual `workflow_dispatch`. To promote a vetted commit:

```
git push origin main:qa                          # fast-forward qa to a known-good main
# or open and merge a main → qa pull request
gh workflow run "Deploy (qa)" -f target=both     # redeploy without a code change
```

Dev is untouched by any of this — it still tracks `main`. There is no QA Honcho job: Honcho is hosted, and QA is isolated from dev by a QA-specific `HONCHO_WORKSPACE_ID` (see secrets below).

### One-time setup

```
flyctl apps create openexec-api-qa
flyctl apps create openexec-ui-qa
flyctl volumes create executive_data --region iad --size 1 -a openexec-api-qa
flyctl tokens create deploy -a openexec-api-qa -x 999999h   # → FLY_API_TOKEN_API_QA
flyctl tokens create deploy -a openexec-ui-qa  -x 999999h   # → FLY_API_TOKEN_UI_QA
```

Add the two tokens as GitHub repo secrets `FLY_API_TOKEN_API_QA` and `FLY_API_TOKEN_UI_QA`.

QA-specific secrets (differences from dev):

```
# API — distinct shared secret, QA-scoped hosted-Honcho workspace, separate bot creds
flyctl secrets set -a openexec-api-qa \
  ANTHROPIC_API_KEY=sk-ant-... \
  BACKEND_SHARED_SECRET=$(openssl rand -hex 32) \
  BACKEND_ALLOWED_ORIGINS=https://openexec-ui-qa.fly.dev \
  HONCHO_ENABLED=true HONCHO_API_KEY=... HONCHO_BASE_URL=... \
  HONCHO_WORKSPACE_ID=openexec-qa \
  TELEGRAM_BOT_TOKEN=<QA bot> SLACK_BOT_TOKEN=<QA bot> ...   # separate QA bot apps only

# UI — same BACKEND_SHARED_SECRET as the API; QA OAuth redirect URI added in GCP
flyctl secrets set -a openexec-ui-qa \
  AUTH_SECRET=$(openssl rand -base64 32) \
  AUTH_GOOGLE_ID=... AUTH_GOOGLE_SECRET=... ALLOWED_EMAILS=... \
  AUTH_TRUST_HOST=true AUTH_URL=https://openexec-ui-qa.fly.dev \
  BACKEND_SHARED_SECRET=<same value as the API app>
```

Then add `https://openexec-ui-qa.fly.dev/api/auth/callback/google` as an authorized redirect URI on the OAuth client, create separate QA bot apps for any channels QA should run (so QA and dev never double-reply), and complete the onboarding wizard against the QA URL — QA starts with an empty volume by design.

---

## Google Workspace (co-located in the API)

The Executive's Gmail/Calendar/Drive tools come from `workspace-mcp`
(taylorwilsdon/google_workspace_mcp) via the MCP gateway. It runs **co-located
inside the API** as a stdio child of the gateway — not a separate service —
because the product is single-tenant (one install = one company = one exec Google
account), so the server is inherently one-per-install. `workspace-mcp` is baked
into the API image and launched by [docker/workspace-mcp-launch.sh](../docker/workspace-mcp-launch.sh)
from the `google_workspace` entry in `/data/company/mcp_servers.json`. Its OAuth
token (if any) persists at `/data/google_credentials` on the API's existing
`executive_data` volume. No second Fly app, deploy token, or deploy job.

### Configure per install

Everything is set on the **API** app. Pick one auth mode — `GWORKSPACE_AUTH_MODE`
selects it (default `oauth`, set in [fly.api.toml](../fly.api.toml) `[env]`; a
secret overrides the env value). The gateway forwards these to the
`workspace-mcp` child.

**Option A — `oauth` (single-user).** Set the client credentials on the API:

```
flyctl secrets set -a openexec-api-dev \
  GOOGLE_OAUTH_CLIENT_ID=... \
  GOOGLE_OAUTH_CLIENT_SECRET=...
```

There is no Google OAuth callback served by the API, so **seed the token**:
complete the OAuth flow once **locally** (`uvx workspace-mcp` with
`WORKSPACE_MCP_CREDENTIALS_DIR` pointed at a local folder), then copy the
resulting credential file(s) onto the API volume:

```
flyctl ssh console -a openexec-api-dev -C "mkdir -p /data/google_credentials"
flyctl ssh sftp shell -a openexec-api-dev
#   put <local-credentials-dir>/<token-file> /data/google_credentials/<token-file>
```

**Option B — `service_account` (domain-wide delegation).** No browser flow —
needs Workspace **admin** to authorize the service account's client ID for the
Gmail/Calendar/Drive scopes:

```
flyctl secrets set -a openexec-api-dev \
  GWORKSPACE_AUTH_MODE=service_account \
  USER_GOOGLE_EMAIL=exec@yourcompany.com \
  GOOGLE_SERVICE_ACCOUNT_KEY_JSON="$(cat service-account.json)"
```

(`GOOGLE_SERVICE_ACCOUNT_KEY_FILE` — a path to a key on the volume — works too.
The launcher fails fast if neither the key nor `USER_GOOGLE_EMAIL` is set.)

### Wire the gateway config

The live gateway config is on the API volume at `/data/company/mcp_servers.json`
(gitignored). The `google_workspace` entry must point `command` at the launcher
(see [packages/core/mcp_servers.json.example](../packages/core/mcp_servers.json.example)
for the exact block). After setting secrets or editing the config:

```
flyctl apps restart openexec-api-dev   # gateway reads the config + secrets at startup
```

Confirm: `flyctl logs -a openexec-api-dev` shows `MCPGateway started`, and a
calendar/email/drive request resolves a `google_workspace__*` tool. Egress is
gated — the Executive can only email/invite/share with People on the roster.

### Migrating off the old separate app

If a previous setup created `openexec-gworkspace-dev`, retire it after the
co-located path works: revert the `google_workspace` entry in
`/data/company/mcp_servers.json` to the launcher form, then
`flyctl apps destroy openexec-gworkspace-dev` (this also removes its
`gworkspace_creds` volume) and delete the `FLY_API_TOKEN_GWORKSPACE` repo secret.

---

## Operations

> The commands below use the dev app names. For QA, swap `-a openexec-api-dev` → `-a openexec-api-qa` (and likewise for the UI app).

### Tail logs

```
flyctl logs -a openexec-api-dev
flyctl logs -a openexec-ui-dev
```

### Status

```
flyctl status -a openexec-api-dev      # machine state, volume attach, last release
flyctl releases -a openexec-api-dev    # release history
```

### Smoke checks

```
curl https://openexec-api-dev.fly.dev/health
# {"status":"ok","builtin_knowledge_chunks":...,"version":"0.1.0"}

curl https://openexec-ui-dev.fly.dev/api/backend/health
# Same JSON, proxied through the UI
```

### Rotate a secret

```
flyctl secrets set -a openexec-api-dev SOME_KEY=newvalue
```

Fly automatically rolls the API machine. No manual deploy needed.

### Rollback

```
flyctl releases -a openexec-api-dev
flyctl release rollback <version> -a openexec-api-dev
```

### Roll the volume forward / get a shell on it

```
flyctl ssh console -a openexec-api-dev
# inside: ls /data
```

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Actions log: `failed to calculate checksum of ref … "/knowledge": not found` | Stale Dockerfile reference | Already fixed in PR #64; if it returns, ensure `docker/Dockerfile` doesn't `COPY` non-existent top-level dirs |
| Deploy step says success, `/health` times out | App crashed during boot — usually a missing env var | `flyctl logs -a openexec-api-dev`; look for a Pydantic `ValidationError` and add the missing secret |
| UI loads but every request errors | UI proxy can't reach API | Check `BACKEND_BASE_URL` in [fly.ui.toml](../fly.ui.toml); we use the public `.fly.dev` URL because alpine's resolver had trouble with `.flycast` IPv6 |
| Two scheduled actions firing at the same time | Two API machines running | `flyctl status -a openexec-api-dev` — if more than one machine, `flyctl scale count 1 -a openexec-api-dev` |
| Onboarding wizard fails with "no company profile" | Volume empty (first boot) | Expected. Complete the wizard via the UI; output lands at `/data/company/profile.yaml` |

### Why not `.flycast`?

`http://openexec-api-dev.flycast:8000` is Fly's private IPv6 anycast routing — same org, no public hop. We tried it; node 22 on alpine inside the UI container failed to resolve the AAAA record reliably, surfacing as opaque 500s on every `/api/backend/*` call. Public `https://openexec-api-dev.fly.dev` adds ~50ms but is reliable. If you confirm IPv6 resolution works in a future image, switching back to flycast is a one-line change in [fly.ui.toml](../fly.ui.toml).

---

## What's not done yet

- **Volume backups** — single 1GB volume, no snapshot cron. Fly's `Scheduled snapshots: true` (5 retained) covers the basics, but there's no off-site copy. For dev this is fine.
- **CI gate on the deploy workflow** — currently relies on branch protection. If you want a hard pre-deploy gate, switch the trigger to `workflow_run` on CI completion.
- **Per-PR preview environments** — out of scope; everything ships to a single shared dev env on merge to `main`.
- **Prod split** — still out of scope. The QA environment above is the worked example of the pattern: copy `fly.api.toml`/`fly.ui.toml` to a `*.<env>.toml` pair, create separate Fly apps, and add a parallel deploy workflow gated on a dedicated branch. Repeat for `prod` (gated on a `prod` branch or tag) when needed.
