# Deployment

Open Executive ships as two containers — the FastAPI backend and the Next.js UI —
plus one persistent volume. Anything that can run a Docker image and attach a
disk will host it: `docker compose` on a single box, a container platform, or a
Kubernetes deployment.

[docker/docker-compose.yml](../docker/docker-compose.yml) is the reference
topology. It is also what `make docker` runs locally, so the local and deployed
shapes are the same.

---

## ⚠️ Single instance only

**The API must run exactly one replica.** The scheduler claims due rows with
`UPDATE … RETURNING`, which is safe against concurrent claims *within* a process
but not across processes: a second API container fires every scheduled action a
second time — duplicate emails, duplicate Slack messages, duplicate briefings.

There is no leader election. Whatever you deploy on, pin the API to one instance
and use a replace-in-place rollout rather than one that briefly runs two
containers. The UI is stateless and scales freely.

---

## Topology

| Component | Image | State |
|---|---|---|
| API | [docker/Dockerfile](../docker/Dockerfile) | One persistent volume at `/data` |
| UI | [docker/Dockerfile.ui](../docker/Dockerfile.ui) | Stateless |

The UI never talks to the API directly from the browser. It proxies through its
own server (`/api/backend/*`), stamping the shared secret on each upstream call,
so the UI origin is the only one that *needs* to be public. See [auth.md](auth.md).

> **The compose file binds the API to `127.0.0.1` on purpose.** The UI reaches it
> over the compose network, so nothing needs it on `0.0.0.0`. If you change that
> binding or put the API behind a proxy on its own hostname, you have made it
> internet-reachable — set `BACKEND_SHARED_SECRET` **and** `OE_PUBLIC_DEPLOYMENT=1`
> before you do. Neither is set by default, and without them the API serves every
> route unauthenticated with only a log line to say so.

---

## Persistent state

One volume, mounted at `/data`:

- `/data/chroma_db/` — ChromaDB vector index (built-in knowledge + uploaded company docs)
- `/data/episodic_memory.db` — SQLite: episodic memory, people, alerts, scheduled actions, audit log
- `/data/company/profile.yaml` + `/data/company/docs/` — onboarding output + uploaded docs
- `/data/company/mcp_servers.json` — MCP gateway config
- `/data/google_credentials/` — Google Workspace OAuth token, if that integration is enabled

Nothing hardcodes those paths. Each is an env var, and the defaults are
repo-relative so a local checkout works with no configuration:

```
VECTOR_STORE_PATH             = /data/chroma_db
EPISODIC_DB_PATH              = /data/episodic_memory.db
COMPANY_PROFILE_PATH          = /data/company/profile.yaml
MCP_SERVERS_CONFIG_PATH       = /data/company/mcp_servers.json
WORKSPACE_MCP_CREDENTIALS_DIR = /data/google_credentials
```

On first boot the volume is empty. ChromaDB rebuilds the built-in knowledge index
from files shipped inside the Python package (`openexecutive/knowledge/builtin/`),
and the SQLite database is created on demand. The company profile stays empty
until you run the onboarding wizard against the deployed URL — the wizard failing
with "no company profile" on a fresh volume is expected, not a fault.

---

## Required configuration

| Variable | Why |
|---|---|
| `ANTHROPIC_API_KEY` | Every agent call. The app will not start without it. |
| `BACKEND_SHARED_SECRET` | Gates every API route via `x-api-key`. Generate with `openssl rand -hex 32`; the UI needs the same value. |
| `OE_PUBLIC_DEPLOYMENT=1` | **Set this on every internet-reachable instance.** See below. |
| `BACKEND_ALLOWED_ORIGINS` | Comma-separated UI origins allowed through CORS, e.g. `https://exec.example.com`. |
| `AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`, `AUTH_URL`, `ALLOWED_EMAILS` | UI sign-in. See [auth.md](auth.md). |

### Operating-data feeds (optional)

The CFO, COO and partner seats read live figures from three read-only feeds.
Each is off until configured and shows up to the agents as a labelled gap, never
as an estimate.

| Variable | Source | Notes |
|---|---|---|
| `STRIPE_READONLY_KEY` | Stripe restricted key | Create a **restricted** key with read access to Balance, Subscriptions and Invoices only. Secret Manager: `colette-exec-stripe-readonly-key`. |
| `LINEAR_API_KEY` | Linear personal API key | Read-only use. Secret Manager: `colette-exec-linear-api-key`. |
| `BILLING_EXPORT_TABLE` | BigQuery table of the Cloud Billing export | `project.dataset.gcp_billing_export_v1_XXXX`. Not a secret. Uses the VM service account (ADC), which needs `roles/bigquery.jobUser` on the project (or `BILLING_QUERY_PROJECT`) and `roles/bigquery.dataViewer` on the export dataset. |

The Terraform creates the two secret containers; populate them yourself with
`gcloud secrets versions add <name> --data-file=-`. `infra/deploy.sh` includes a
key in `.env` only when it has a version.

Integrations (Slack, Discord, email, Google Workspace) are all optional and off
unless their variables are set. [.env.example](../.env.example) is the full list.

### `OE_PUBLIC_DEPLOYMENT`

If `BACKEND_SHARED_SECRET` is unset the API serves every route unauthenticated.
That is the intended default for local development and a serious incident
anywhere else, so a deployment declares itself public:

```
OE_PUBLIC_DEPLOYMENT=1
```

With it set and no shared secret, `create_app()` raises at startup instead of
booting an open API. The check is deliberately fail-safe — any value other than
`0`/`false`/`no`/`off`/empty arms it, so a typo requires the secret rather than
skipping the check.

---

## Health checks

`GET /health` is exempt from the shared-secret gate so a platform health checker
can reach it unauthenticated. It returns:

```json
{"status": "ok", "builtin_knowledge_chunks": 1234, "version": "0.1.0"}
```

**Give it a startup grace period of about 5 minutes.** A cold container builds
the MCP tool-discovery vector index and loads Chroma before it serves. The
embedding model is baked into the image, so no network fetch is involved, but
the work is real — a short grace period will kill the container mid-boot in a
crash loop that looks like a deploy failure.

## Resources

**2 GB of memory for the API.** 1 GB out-of-memories during ingest, where the
ONNX embedder and Chroma writes run concurrently. One shared CPU is sufficient;
the workload is I/O-bound on the Anthropic API.

---

## Google Workspace credentials

The Gmail/Calendar/Drive tools come from `workspace-mcp`, which runs co-located
inside the API as a stdio child of the MCP gateway rather than as its own
service — the product is single-tenant, so the server is inherently one per
install. It is baked into the API image and launched by
[docker/workspace-mcp-launch.sh](../docker/workspace-mcp-launch.sh) from the
`google_workspace` entry in `/data/company/mcp_servers.json` (see
[packages/core/mcp_servers.json.example](../packages/core/mcp_servers.json.example)
for the exact block).

`GWORKSPACE_AUTH_MODE` picks the auth mode; it defaults to `oauth`.

**Option A — `oauth` (single user).** Set `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET`. The API serves no OAuth callback, so the token has
to be seeded: complete the flow once locally with `WORKSPACE_MCP_CREDENTIALS_DIR`
pointed at a local folder, then copy the credential file onto the volume.

```bash
docker compose exec api mkdir -p /data/google_credentials
docker compose cp <local-credentials-dir>/<token-file> api:/data/google_credentials/
docker compose restart api        # the gateway reads config and credentials at startup
```

**Option B — `service_account` (domain-wide delegation).** No browser flow, but a
Workspace admin must authorize the service account's client ID for the
Gmail/Calendar/Drive scopes:

```
GWORKSPACE_AUTH_MODE=service_account
USER_GOOGLE_EMAIL=exec@yourcompany.com
GOOGLE_SERVICE_ACCOUNT_KEY_JSON=<contents of service-account.json>
```

(`GOOGLE_SERVICE_ACCOUNT_KEY_FILE`, a path to a key already on the volume, works
too. The launcher fails fast if neither the key nor `USER_GOOGLE_EMAIL` is set.)

Outbound egress is gated either way: the Executive can only email, invite, or
share with People on the roster.

---

## Operations

```bash
# Logs
docker compose logs -f api

# Smoke the API directly
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" https://api.example.com/health

# Same JSON, proxied through the UI (exercises the shared secret end to end)
curl -s https://exec.example.com/api/backend/health

# Inspect state on the volume — SELECT only unless you mean it
docker compose exec api sqlite3 /data/episodic_memory.db \
  "SELECT id, kind, status, run_at FROM scheduled_actions WHERE status='pending' LIMIT 10;"
```

**Rollback** is an image-tag rollback: redeploy the previous tag. The volume is
not versioned with the image, so a release that migrates schema forward is not
undone by rolling the image back — check what changed under `*/store.py` before
relying on it. Additive column migrations (the common case — every column has a
default and older builds name their columns explicitly) are safe to roll back
over; a rolled-back build simply ignores the newer columns.

**Backups.** There is no snapshot cron in this repo. `/data/episodic_memory.db`
is the irreplaceable part (Chroma rebuilds from source documents), so back it up
with `sqlite3 /data/episodic_memory.db ".backup /tmp/backup.db"` and copy it off
the host — a plain file copy of a live SQLite database can be torn.

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Deploy reports success, `/health` never responds | Crash during boot, usually a missing env var | Read the API logs; look for a Pydantic `ValidationError` naming the variable |
| Container is killed and restarted repeatedly during startup | Health-check grace period too short | Raise it to ~5 minutes (see above) |
| API refuses to start: `BACKEND_SHARED_SECRET is required` | `OE_PUBLIC_DEPLOYMENT` is set with no secret | Working as intended — set the secret |
| Every UI request errors, API is healthy | UI proxy can't reach the API, or the shared secret differs between them | Check `BACKEND_BASE_URL` on the UI and that both sides carry the same `BACKEND_SHARED_SECRET` |
| Browser console shows CORS errors | UI origin missing from `BACKEND_ALLOWED_ORIGINS` | Add the exact scheme + host |
| Scheduled actions firing twice | More than one API replica | Scale the API to exactly 1 (see the warning at the top) |
| Onboarding wizard says "no company profile" | Empty volume on first boot | Expected — complete the wizard; output lands at `/data/company/profile.yaml` |

---

## Optional: self-hosted Honcho

Per-person memory can run against hosted Honcho (set `HONCHO_API_KEY` and
`HONCHO_BASE_URL`) or a self-hosted instance. [docker/honcho/](../docker/honcho/)
carries the image and configuration for the self-hosted path.
