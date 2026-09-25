# Open Executive — Company Fixtures

Pre-built company data suites for demos, evals, and development. Each fixture contains a full company context — profile, documents, and episodic memory seed data.

## Available Fixtures

| Fixture | Company | Industry | Stage | Revenue |
|---------|---------|----------|-------|---------|
| `tandem_robotics` | Tandem Robotics | Humanoid Robotics / Warehouse Automation | Series C | $90M |
| `halcyon_motors` | Halcyon Motors | Electric Vehicles / Automotive | Series C | $140M |
| `meridian_petroleum` | Meridian Petroleum | Oil & Gas — Refining / Crude Trading | Private / PE-backed | $3.2B |

`tandem_robotics`, `halcyon_motors`, and `meridian_petroleum` are clean-baseline
research demo fixtures (see the callouts below). All three are fictional companies that
share one synthetic roster — placeholder emails (`example.com`) and non-routable
Discord IDs — so nothing in these fixtures maps to a real person.

> **`halcyon_motors`** is the reference fixture for demoing the **research feature**
> from a **clean baseline**. It's a fictional affordable-EV startup whose competitive
> landscape names real, frequently-changing players (Tesla, Rivian, Lucid, BYD, Ford,
> GM, Hyundai/Kia) plus real policy and supply-chain entities (IRA §30D tax credit,
> EV tariffs, CATL / Panasonic / LG Energy Solution, NHTSA). On load it seeds the
> company's **memory** (decisions, initiatives, prior advice) but **no pre-baked
> alerts or in-flight actions** — the Today page reads "all clear." A `+30s`
> `watchlist_research_scan` then auto-fires the executive research workflow: the 7
> specialists web-search current news, the Executive proposes what we should be
> watching and **adds the highest-signal sources to the watchlist**, and only routes
> something to a department head if it's genuinely urgent. From there the page builds
> up **organically** rather than starting pre-populated. The roster is synthetic
> (placeholder emails + non-routable Discord IDs); to actually deliver routed DMs in a
> live demo, replace them with real contact IDs and supply live Slack/Discord tokens.
> You can also trigger the scan manually by asking the Executive "what should we be
> looking into this week?"

> **`meridian_petroleum`** is a second clean-baseline research demo fixture — a Gulf
> Coast independent oil refiner, crude trader, and fuels marketer whose P&L is driven
> by geopolitics. Its docs and watchlist scan name real, fast-moving entities (the
> Israel–Iran conflict, Strait of Hormuz shipping, OPEC+ / Saudi Aramco, Brent/WTI,
> OFAC Iranian-crude sanctions, VLCC war-risk insurance, EIA/IEA reports, and refiner
> peers Valero / Marathon / Phillips 66 / PBF). Like `halcyon_motors` it loads with
> **memory but a clean Today page**, then the same `+30s` `watchlist_research_scan`
> researches the **geopolitical and political** landscape and proposes what to watch —
> building the watchlist organically instead of pre-seeding every lane. The roster is
> synthetic (placeholder emails + non-routable Discord IDs), shared with the other demo
> fixtures.

## Loading a Fixture

### Via the UI

Open `/demo` in the Open Executive UI. Click "Load" on any company card.

### Via the API

```bash
# List available fixtures
curl http://localhost:8000/fixtures

# Load a specific fixture (replaces active company data)
curl -X POST http://localhost:8000/fixtures/halcyon_motors/load
```

### What Gets Replaced

Loading a fixture replaces:
1. `packages/core/company/profile.yaml` — company profile
2. `packages/core/company/docs/` — company documents (re-indexed into ChromaDB)
3. Episodic memory rows — decisions, initiatives, advice_given (cleared and seeded)
4. **People** — leadership and key employees with channels, authority scopes, availability windows
5. **Departments** — company-specific org shape, charters, OKRs, authority levels (including informational departments with no specialist agent — useful for nonprofits with "Volunteer Coordination" or "Family Services")
6. **Workspace settings** — reset to the defaults (team mode, no time zone of its own), then taken from the fixture's optional `workspace.yaml`

## Running Fixture-Specific Evals

Each fixture ships with 2 eval scenarios in `fixtures/companies/<name>/scenarios/`.

```bash
cd evals
python run_evals.py \
  --scenarios ../fixtures/companies/halcyon_motors/scenarios/ \
  --output results/halcyon_motors/
```

## Fixture Structure

```
fixtures/companies/<name>/
  profile.yaml        # CompanyProfile schema (see openexecutive/memory/company_profile.py)
  docs/               # Company documents indexed into ChromaDB
    *.md
  memory.json         # Episodic seed data (decisions, initiatives, advice_given)
  people.yaml         # Leadership + key employees as Person records
  departments.yaml    # Company-specific departments + OKRs + authority levels
  workspace.yaml      # Optional: solo/team mode + the user's time zone
  scenarios/
    <name>_001.yaml   # Eval scenario 1
    <name>_002.yaml   # Eval scenario 2
```

### Authoring `departments.yaml`

A department can be specialist-aligned (mapped to one of the 8 specialist agents) or informational-only:

```yaml
departments:
  - slug: marketing
    title: Growth & Brand Marketing
    specialist_key: cmo           # → routes to the CMO specialist
    head_person_name: Maya Torres
    authority_level: propose_only # auto_execute | propose_only | escalate
    charter:
      mission: ...
      scope: [...]
      out_of_scope: [...]
    headcount: 12
    budget_usd: 4200000
    okrs:
      - quarter: Q2 2026
        key_result: Reduce CAC from $28 to $22
        target: <$22 CAC
        current: $28
        status: at_risk   # on_track | at_risk | off_track

  - slug: volunteer_coordination
    title: Volunteer Coordination
    specialist_key: null          # informational only — no specialist agent
    ...
```

### Authoring `workspace.yaml` (optional)

Both keys are optional; a fixture without the file loads in team mode with no
time zone of its own (the server's `USER_TIMEZONE`, else UTC, applies).

```yaml
mode: solo                 # solo | team — solo: one person using Open Executive
                           # just for themselves, so department check-ins don't run
timezone: America/Chicago  # IANA zone for the brief times, "tomorrow at 9", quiet hours
```

A value that doesn't validate (including a region like `America` rather than a
zone like `America/Chicago`) is logged and ignored; it never stops the load.
Loading a fixture while your own company is live writes your current settings
to `workspace.yaml` in the backup — even when an earlier backup is reused — so
unloading the fixture restores them. Unloading from an older backup that has no
`workspace.yaml` leaves the current settings as they are.

## Adding a New Fixture

1. Create `fixtures/companies/<your_company>/`
2. Add `profile.yaml` following the `CompanyProfile` schema
3. Add `docs/*.md` files with company context
4. Add `memory.json` with seed data (can be empty: `{"decisions":[],"initiatives":[],"advice_given":[],"scheduled_actions":[]}`)
5. Optionally add `scenarios/*.yaml` for evals

The fixture will appear automatically in `GET /fixtures` and the UI Demo page.
