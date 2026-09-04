# Running Open Executive for a real organization

The onboarding wizard (`/onboard`) is the quickest way to get a company profile
in front of the Executive, but it parses free text and only covers the profile.
When you want an instance to mirror a real org — the actual people, who approves
what, the departments and their goals, and the documents the team works from —
seed it from files you keep **outside the repo** and re-apply them whenever
something changes.

This is what `openexecutive seed-org` does. It talks to a running API over
HTTP, so it works the same against `make dev`, a Docker container, or a Fly
deployment, and it never touches the demo-fixture path (which wipes state and
only reads fixtures baked into the image).

## 1. Prepare the environment

Before seeding a deployed instance you need, on the API app:

| Secret | Why |
|---|---|
| `ANTHROPIC_API_KEY` | model access |
| `BACKEND_SHARED_SECRET` | the API gate; `seed-org` sends it as `x-api-key` |
| `EXEC_EMAIL_ADDRESS` | the mailbox the Executive operates as — required to boot |

and on the UI app `AUTH_SECRET`, `AUTH_GOOGLE_ID/SECRET`, `AUTH_URL`,
`AUTH_TRUST_HOST=true`, the same `BACKEND_SHARED_SECRET`, and
`ALLOWED_EMAILS=<your own Google email>`. `ALLOWED_EMAILS` only matters until the
roster has people in it — after that, **a Person's email is the login allowlist**
(`GET /auth/allowed-emails`), and archiving a Person revokes their web login and
their Discord/Telegram/email access in one step. See [auth.md](auth.md) and
[deployment.md](deployment.md).

## 2. Lay out the seed directory

Anything under a directory named `company/` is gitignored at every depth, so a
good home is `company/seed/<org>/` at the repo root:

```
company/seed/acme/
  profile.yaml        # {company: {...}} — every CompanyProfile field
  people.yaml         # {people: [...]}
  departments.yaml    # {departments: [...]}
  docs.yaml           # {docs: [{file, domain}, ...]}   (optional)
  docs/               # *.md *.txt *.pdf *.docx *.doc     (optional)
```

The YAML shapes are the same ones the curated demo fixtures use, so
`fixtures/companies/*/` are working examples and
[fixtures/README.md](../fixtures/README.md) documents `departments.yaml` in
detail. Two additions over the fixture format:

- `people[].reports_to: <full name>` sets the reporting line (resolved to
  `reports_to_person_id` after everyone exists).
- `docs.yaml` assigns a knowledge domain per file (`strategy | finance | hr |
  legal | operations | marketing | board | product | general`). Files not listed
  land in `general`, which specialist retrieval does not filter on — so list the
  ones that matter.

### profile.yaml

```yaml
company:
  name: Acme Robotics
  industry: Warehouse automation
  stage: Series A
  founding_year: 2023
  headcount: 42
  mission: ...
  target_customer: {profile: ..., pain_points: [...]}
  competitive_landscape: {primary_competitors: [...], competitive_advantages: [...]}
  org_structure: {departments: [...], leadership_team: [...]}
  strategic_priorities: {current_year: [...], north_star_metric: ...}
  culture: {values: [...], operating_principles: [...]}
  financials: {burn_rate_monthly: 250000, runway_months: 14, key_metrics: {...}}
```

The profile is applied with `PUT /company-profile`, which creates or **fully
replaces** the profile (no merge), so the file is the source of truth.

### people.yaml

```yaml
people:
  - full_name: Jordan Avery
    role: CEO
    is_principal: true          # exactly one principal; gets wildcard approval
    email: jordan@acme.example  # Google-account email = login allowlist entry
    discord_user_id: "1000..."  # optional channel ids: slack_user_id, telegram_chat_id
    department_slugs: [product]
    preferred_channel: any      # email | slack | telegram | discord | any
    response_sla_hours: 8
    authority_scope: [wildcard]
    availability:
      - {weekdays: [0, 1, 2, 3, 4], start_local: "09:00", end_local: "18:00", timezone: America/New_York}
  - full_name: Sam Rivera
    role: Head of Finance
    email: sam@acme.example
    authority_scope: [spend_lt_10k, vendor_onboarding]
    reports_to: Jordan Avery
```

People are matched by email (case-insensitive), then by full name, and patched
in place. Because a Person's email is their sign-in identity, a name-only match
that would change an existing email is refused unless you pass
`--allow-email-change` (use it when someone genuinely switches addresses).
`is_principal` cannot be changed with a patch — archive and recreate the person
if the principal changes.

### departments.yaml

Open Executive seeds eight default departments (`strategy`, `finance`, `hr`,
`legal`, `operations`, `marketing`, `product`, `board_comms`), each wired to a
specialist agent. **Only those default slugs carry specialist routing**;
departments you create are informational (no `specialist_key`) and are skipped
by specialist-routing workflows. So reuse and retitle the default slugs for the
functions you want the specialists to run, and add new slugs only for teams
that don't map to a specialist:

```yaml
departments:
  - slug: operations                      # keep the slug → keeps the COO specialist
    title: Engineering
    head_person_name: Jordan Avery
    authority_level: auto_execute         # auto_execute | propose_only | escalate
    charter: {mission: ..., scope: [...], out_of_scope: [...]}
    cadences: {check_in: "weekly@mon@14:00"}   # daily@HH:MM | weekly@dow@HH:MM (UTC)
    headcount: 12
    budget_usd: 1800000
    goals:
      - {period_type: quarter, period_value: Q4 2026, key_result: ..., target: ..., current: ..., status: on_track}
  - slug: field-ops                       # new: created via POST, informational
    title: Field Operations
    charter: {mission: ...}
```

For new departments the server derives the slug from the title (`Field
Operations` → `field-operations`); `seed-org` remaps the slug you wrote and any
`department_slugs` that reference it, and matches the same department again on
later runs as long as the title is unchanged. To retitle a department the seed
created, first set its YAML `slug` to the server slug it received (shown in the
first run's warning), otherwise the new title creates a second department.
Goals match by `(period_value, key_result)`, so rewording a key result creates
a new goal — delete the old one in the UI. Pass `--prune-departments` to delete
defaults you don't use (`hr`, `board_comms`, …); they are not re-seeded, and the
flag refuses to run without a `departments.yaml`.

## 3. Apply it

```bash
cd packages/core
export BACKEND_SHARED_SECRET=...            # the API's value; unset for `make dev`
uv run openexecutive seed-org --api http://localhost:8000 --dir ../../company/seed/acme --dry-run
uv run openexecutive seed-org --api http://localhost:8000 --dir ../../company/seed/acme --prune-departments
```

`--dry-run` lists every write it would send and sends none. The command is
idempotent: re-running after editing the YAML patches what changed and creates
nothing twice. Docs are uploaded once per filename; `--reindex-docs` deletes and
re-uploads them (use after editing a document).

Verify:

```bash
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/health | jq .company_name
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/people | jq '.[] | {full_name, email, is_principal}'
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/departments | jq '.[] | {slug: .config.slug, head: .config.head_person_id, goals: (.goals | length)}'
curl -s -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/auth/allowed-emails | jq
```

Then sign in to the UI with one of the seeded emails.

## 4. Protect it

Once the real org is loaded, snapshot it so a demo can always be undone:

```bash
curl -sX POST -H "x-api-key: $BACKEND_SHARED_SECRET" $OE_API/fixtures/snapshot
```

Loading a demo fixture from `/demo` replaces the live company; `/fixtures/unload`
restores the snapshot. `POST /fixtures/reset` wipes everything, including the
snapshot — re-run `seed-org` afterwards.

## 5. Bringing in documents from Google Drive

`seed-org` uploads whatever is in `docs/`. For a team whose material lives in
Drive, export the documents you want the Executive grounded in (Google Docs →
Markdown, Sheets → CSV, Slides → text), trim anything that shouldn't be in the
knowledge base (personal details, NDA text, third-party confidential material),
drop the files in `docs/`, list their domains in `docs.yaml`, and re-run with
`--reindex-docs` when they change.

For live access on top of the snapshot, give the Executive's Google account
(`EXEC_EMAIL_ADDRESS`) viewer access to the shared drive and complete the
Google Workspace setup in [deployment.md](deployment.md); the Executive then
searches and reads Drive on demand through its Workspace tools.
