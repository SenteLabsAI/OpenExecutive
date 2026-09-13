# Observability: watching a running Executive

Open Executive acts on its own. It answers in Slack and email, consults
specialists, calls tools, and its scheduler sends messages later without anyone
in the loop. This page explains what the Executive records about all of that,
where it lives, and how to watch it from **outside** the process, without
touching the code or the prompts.

Everything below reads the same SQLite file the API writes. Nothing here needs
a code change, a restart, or a new dependency.

## Where the record lives

One SQLite file, set by `EPISODIC_DB_PATH`:

| How you run it | Path |
|---|---|
| `make dev` | `packages/core/episodic_memory.db` (the default `./episodic_memory.db`, relative to the API's working directory) |
| Docker (`docker/docker-compose.yml`) | `/data/episodic_memory.db` inside the `executive_data` volume |

A Docker named volume is not visible from the host. To read it from outside the
container, bind-mount `/data` to a host directory instead, or copy the file out
with `docker compose cp api:/data/episodic_memory.db .`.

Open it read-only so you can never lock or modify the live database:

```sh
sqlite3 "file:packages/core/episodic_memory.db?mode=ro"
```

## Live dashboard with ClawMetry

The sections below show how to read the database yourself. If you would rather
watch it live, [ClawMetry](https://clawmetry.com/runtimes/openexecutive) reads
this same file read-only, with no change to Open Executive:

1. Install it. On macOS or Linux:

   ```sh
   curl -fsSL https://clawmetry.com/install.sh | bash
   ```

   (Windows and desktop installers are on the
   [ClawMetry page](https://clawmetry.com/runtimes/openexecutive).)

2. Run `clawmetry`. It finds a clone under your home directory on its own. For
   Docker, point it at the file first:

   ```sh
   export CLAWMETRY_OPENEXECUTIVE_DB=/path/to/episodic_memory.db
   clawmetry
   ```

3. Open `http://localhost:8900` and pick OpenExecutive in the runtime switcher.

You get every conversation, including the ones that started in Slack or email;
each specialist consult as a step with its question and answer; failed tool
calls; token usage and cost per conversation; and every send the scheduler has
queued, with its status, so a pending message is visible before it goes out.
The OpenExecutive integration is part of ClawMetry's paid plans.

## What is recorded

| Table | What it holds |
|---|---|
| `sessions`, `chat_messages` | Web chat conversations and their transcript |
| `audit_log` | One row per event, with `session_id`, `turn_id`, `actor`, a searchable `summary`, and structured `details_json` |
| `scheduled_actions` | Every outbound send the Executive queued: `channel`, `channel_ref`, `run_at`, `intent_text`, `status` (`pending`, `running`, `done`, `failed`, `cancelled`) |

Useful `audit_log.event_type` values:

| `event_type` | Meaning |
|---|---|
| `chat_turn` | A user message (`actor = 'user'`) or the Executive's reply (`actor = 'executive'`) |
| `specialist_consult` | The Executive consulted a specialist; `actor` is the specialist, `full_json` has the question and the answer |
| `tool_invocation` | A tool call; `details_json.ok` is `false` when it failed |
| `cache_event` | One model call, written by `audit/usage.py::log_model_usage`: `model`, `input_tokens`, `output_tokens`, cache read/write tokens, `cost_usd`, `web_search_requests`. `actor` names the source (`executive`, `specialist_research`, `research_synthesis`, `research_watchlist`, `triage`, `memory_extractor`) |
| `integration_inbound` | A message arrived from Slack, email, Telegram, Discord or Google Chat |
| `scheduled_action` | The scheduler delivered, deferred, dropped or failed a queued send (`details_json.phase`) |

Slack and email conversations write `audit_log` rows but no `sessions` row, so
to list every conversation, read session ids from `audit_log`, not only from
`sessions`.

## Four questions you can answer today

**What is about to be sent, and to whom?**

```sql
SELECT run_at, channel, channel_ref, intent_text
FROM scheduled_actions
WHERE status = 'pending'
  AND channel != '__internal__'
ORDER BY run_at;
```

`__internal__` rows are the scheduler's own recurring jobs (department
check-ins, monitoring scans, briefs being prepared). They never reach a
person as written, so they are left out here.

**What did each conversation cost?**

```sql
SELECT session_id,
       SUM(json_extract(details_json, '$.input_tokens'))  AS input_tokens,
       SUM(json_extract(details_json, '$.output_tokens')) AS output_tokens,
       SUM(json_extract(details_json, '$.cost_usd'))      AS reported_usd
FROM audit_log
WHERE event_type = 'cache_event'
GROUP BY session_id
ORDER BY reported_usd DESC;
```

**Where did the spend come from?**

```sql
SELECT COALESCE(actor, 'unknown') AS source,
       COUNT(*)                                            AS calls,
       SUM(json_extract(details_json, '$.input_tokens'))  AS input_tokens,
       SUM(json_extract(details_json, '$.output_tokens')) AS output_tokens,
       SUM(json_extract(details_json, '$.cost_usd'))      AS reported_usd
FROM audit_log
WHERE event_type = 'cache_event'
GROUP BY source
ORDER BY input_tokens DESC;
```

**What failed?**

```sql
SELECT ts, event_type, summary
FROM audit_log
WHERE (event_type = 'tool_invocation' AND json_extract(details_json, '$.ok') = 0)
   OR (event_type = 'scheduled_action' AND json_extract(details_json, '$.phase') = 'failed')
ORDER BY ts DESC;
```

## Reading the cost figures correctly

- `cost_usd` is the real charge when calls are routed through OpenRouter. On
  the Anthropic-direct path it is `NULL`, and a dollar figure has to be worked
  out from the token counts and the model's published price.
- Most model calls write a `cache_event` (see the `actor` values above). One
  path does not: the specialist consults the Executive makes during a chat
  turn (`consult_specialist`, which runs `BaseAgent.analyze`) record no usage
  row. Specialist calls made through `analyze_with_tools`, such as monitoring
  research, are recorded under `specialist_research`. So a conversation's total
  from this table is a **lower bound** whenever the Executive consulted a
  specialist.
- On a local model (`LOCAL_MODELS_ENABLED=true`, e.g. Ollama), each call still
  writes a `cache_event` with the model name, but the token fields are `0`
  unless the server reports usage on its stream. A zero there means "not
  measured", not "free".
- Rows written by background work (research runs, triage) may carry no
  `session_id`; research runs tag theirs with `details.run_id` instead.
