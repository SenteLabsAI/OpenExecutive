# Open Executive — Technical Architecture

## Overview

Open Executive is a multi-agent AI system that acts as a single coherent virtual executive team. The user always interacts with one voice — the Executive — which internally routes to domain specialist agents, retrieves relevant knowledge, and synthesizes everything into one response. The internal agent architecture is never exposed.

Built on the Anthropic Claude API with native tool use. No LangGraph, no CrewAI — just Python, FastAPI, and direct Anthropic SDK calls.

---

## Repository Layout

```
openexecutive/
├── packages/
│   ├── core/                         # Python backend
│   │   └── openexecutive/
│   │       ├── orchestrator/         # Executive persona + routing loop
│   │       ├── agents/               # 8 specialist agents + triage
│   │       ├── knowledge/            # ChromaDB store + RAG pipeline
│   │       ├── memory/               # Company profile + episodic memory
│   │       ├── onboarding/           # Wizard state machine + profile builder
│   │       ├── prompts/              # Persona + domain prompts + cache manager
│   │       ├── api/                  # FastAPI app + routes
│   │       ├── integrations/         # Slack, Email, Telegram, Google Chat
│   │       ├── scheduler/            # Background job runner (single-instance)
│   │       ├── alerts/               # Proactive alert triage + dispatch
│   │       ├── workflows/            # Multi-step workflow engine
│   │       ├── audit/                # SQLite-backed audit log
│   │       ├── architecture/         # Self-documenting architecture module
│   │       └── cli.py                # Click CLI
│   └── ui/                           # Next.js 15 web UI
├── evals/                            # Eval scenarios + LLM-as-judge runner
├── docker/                           # Dockerfile + docker-compose.yml
└── docs/                             # This file + deployment, setup guides
```

---

## Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| LLM backbone | Anthropic Claude API | Native tool use, prompt caching, streaming |
| Default model | `claude-sonnet-4-6` | Best speed/quality ratio for most queries |
| Deep reasoning | `claude-opus-4-7` | Strategy, finance, legal, board — high-stakes decisions |
| Routing / extraction | `claude-haiku-4-5-20251001` | Intent routing and background memory extraction |
| Backend | Python + FastAPI | Async-native, Pydantic models, auto OpenAPI docs |
| Package manager | `uv` | Fast, reproducible |
| Vector store | ChromaDB (local) | Zero-config embedded DB |
| Episodic memory | SQLite | Decision continuity + alerts + audit in one file |
| Web UI | Next.js 15 (App Router) + Tailwind | Streaming-ready, TypeScript |
| License | Apache 2.0 | Permissive for commercial internal deployments |

---

## Agent Architecture

### The Executive (Orchestrator)

`orchestrator/executive.py`

The Executive is the only agent the user ever sees. Every user message goes through it. It decides which specialists to consult, runs them in parallel via Anthropic tool use, and synthesizes the results into one coherent executive response.

The internal routing is implemented as a `consult_specialist` tool — the model decides when and which specialists to call, not hardcoded logic.

### Specialist Agents

`agents/`

| Agent | Key | Model | Activates on |
|---|---|---|---|
| Chief Strategy Officer | `cso` | `claude-opus-4-7` | Competitive analysis, M&A, OKRs, scenario planning |
| Chief Financial Officer | `cfo` | `claude-opus-4-7` | Financial modeling, fundraising, unit economics |
| Chief HR/People Officer | `chro` | `claude-sonnet-4-6` | Hiring, comp, performance, culture, org design |
| General Counsel | `gc` | `claude-opus-4-7` | Contracts, IP, employment law, compliance |
| Chief Operating Officer | `coo` | `claude-sonnet-4-6` | Process, vendor management, operational scaling |
| Chief Marketing Officer | `cmo` | `claude-sonnet-4-6` | GTM, brand, messaging, PR, crisis comms |
| Chief Product Officer | `cpo` | `claude-sonnet-4-6` | Roadmap, prioritization, product strategy |
| Board Comms Director | `board_comms` | `claude-opus-4-7` | Board decks, investor relations, governance |

CSO, CFO, GC, and Board Comms use `extended-thinking` (`budget_tokens: 8000`) for deeper reasoning on high-stakes decisions.

### Request Flow

```
User message
    │
    ▼
Executive (_run_agent_loop)
    │
    ├── Anthropic streams response
    │       │
    │       └── stop_reason == "tool_use"?
    │               │
    │               ▼
    │           Extract tool_use blocks
    │               │
    │               ▼
    │           route_parallel(specialist_calls)
    │               │
    │               ├── StrategyAgent.analyze()  ─┐
    │               ├── FinanceAgent.analyze()    ─┼── asyncio.gather (parallel)
    │               └── ...                       ─┘
    │               │
    │               ▼
    │           Inject tool_results → continue loop
    │
    └── stop_reason == "end_turn"
            │
            ▼
        Yield full_response → SSE stream → UI
```

For cross-domain questions (e.g. "Should we raise a Series B?"), multiple specialists are called in parallel. `route_parallel` returns results as an ordered list matched to tool_use IDs — duplicate specialist calls are handled correctly.

---

## Prompt Caching Strategy

Prompt caching is built in from the start. Cache misses on a busy session cost ~10x more per token.

**Render order** (enforced in `prompts/cache_manager.py`):

```
[system]
  1. Executive persona constant          ← cache_control: ephemeral (1h TTL)
  2. Company profile block               ← cache_control: ephemeral (1h TTL)
  3. Knowledge index summary             ← cache_control: ephemeral (1h TTL)

[messages]
  ... conversation history ...
  penultimate assistant turn             ← cache_control: ephemeral (5m TTL, rolling)
  current user turn:
    <past_decisions>...</past_decisions>  ← NOT cached (fresh each turn)
    <retrieved_context>...</retrieved_context>  ← NOT cached (fresh each turn)
    user message
```

**Rules that must never be broken:**
- No `datetime.now()` in any cached block — time is injected as a user message if needed
- Tool list sorted by name before every API call
- Company profile serialized with `sort_keys=True`
- `EXECUTIVE_PERSONA_PROMPT` is a frozen constant — never f-stringed

Expected cache hit rate: 70–85% of input tokens after the first few turns of a session.

---

## Knowledge Architecture

Two-layer retrieval system. Both layers are queried per specialist call and injected into the **user turn** (never the system prompt, which would bust the cache).

### Layer 1 — Built-in Executive Knowledge

`knowledge/builtin/` (inside the Python package, shipped with the app)

Curated MBA-level content seeded into ChromaDB at startup:

| Domain | Content |
|---|---|
| Strategy | Competitive analysis frameworks, OKR methodology |
| Finance | Unit economics, LTV/CAC, fundraising playbooks |
| HR | Hiring scorecards, comp philosophy, performance management |
| Legal | Startup legal basics, employment, IP, contracts |
| Operations | Scaling frameworks, vendor management |
| Marketing | GTM playbooks, brand strategy |
| Board | Board communication, investor relations, governance |

Collection: `builtin_knowledge`. Seeded once at startup, idempotent.

### Layer 2 — Company-Specific Knowledge

**Structured** (`company/profile.yaml`, gitignored):
Loaded as a cached system prompt block. The Executive always has this context without a retrieval step — name, industry, stage, headcount, ARR, mission, competitors, priorities, values, financials.

**Unstructured** (uploaded documents, `company/docs/`):
PDFs, DOCX, and Markdown files ingested via `POST /documents`. Chunked at 512 words with 50-word overlap, embedded, and stored in the `company_docs` ChromaDB collection.

### Retrieval at Runtime

`knowledge/retriever.py`

Each specialist call triggers a domain-filtered semantic search:

```python
# For a CSO query:
retrieve(query=user_message, specialist_name="cso")

# Internally:
builtin_chunks = store.query(collection="builtin_knowledge",
                              query=query,
                              domain_filter=["strategy", "marketing"],
                              n_results=5)
company_chunks = store.query(collection="company_docs",
                              query=query,
                              domain_filter=["strategy", "marketing"],
                              n_results=3)
```

Domain aliases per specialist (`DOMAIN_ALIASES` in `knowledge/retriever.py`):

| Specialist | Domain filter |
|---|---|
| cso | strategy, marketing |
| cfo | finance, operations |
| chro | hr, legal |
| gc | legal, hr |
| coo | operations, finance |
| cmo | marketing, strategy |
| cpo | product, strategy |
| board_comms | board, finance, strategy |

---

## Memory System

### Short-term (in-context)
`orchestrator/session.py`

Conversation history for the current session stored in `Session.conversation_history`. `get_recent_history(max_turns=20)` returns the last 40 messages, always starting on a user turn.

### Episodic (SQLite)
`memory/episodic.py`

Persists key decisions, initiatives, advice, and scheduled actions across sessions. The same SQLite file (`episodic_memory.db`) also backs the alerts store and the audit log.

```sql
decisions         (id, timestamp, domain, summary, rationale, outcome, tags)
initiatives       (id, title, status, created_at, updated_at, summary)
advice_given      (id, timestamp, domain, query_summary, advice_summary)
scheduled_actions (id, title, prompt, scheduled_for, status, channel_ref, ...)
```

**Read path:** `format_for_prompt()` returns the 8 most recent decisions and all active initiatives as a `<past_decisions>` block, injected into the user turn at session start.

**Write path — LLM extraction:** After every Executive response, `schedule_extraction()` fires a background task that calls `claude-haiku-4-5-20251001` with a structured `store_memories` tool. Haiku decides whether the turn contains anything worth remembering and extracts it into typed fields. Results are written to SQLite without blocking the response stream.

```
User message + Executive response
        │
        ▼ (background, non-blocking)
claude-haiku-4-5-20251001 (tool_choice: auto)
        │
        └── store_memories tool call (if anything worth keeping)
                ├── decisions[]   → store_decision()
                ├── initiatives[] → store_initiative()  (upserts by title)
                └── advice[]      → store_advice()
```

Key design decisions:
- `tool_choice: "auto"` — Haiku skips the tool entirely for idle turns (greetings, clarifying questions), avoiding noise in the DB
- `asyncio.create_task()` in async context; daemon `threading.Thread` in sync/CLI context — never blocks
- Strong task reference held in `_background_tasks` set to prevent GC cancellation mid-flight
- Input capped at 20,000 chars per side to bound cost on long responses

### Company Profile (structured long-term)
`memory/company_profile.py`

`CompanyProfile` is a Pydantic v2 model serialized to `company/profile.yaml`. Loaded at startup, cached in the system prompt. Updated through the onboarding wizard or natural-language corrections.

---

## Scheduler

`scheduler/runner.py`

A background polling loop that wakes the Executive when a `scheduled_actions` row comes due.

**Claim pattern:** `claim_due_actions()` uses `UPDATE … RETURNING` — the action transitions from `pending` to `running` atomically. This prevents double-firing if a stale loop somehow overlaps.

**Startup sweep:** `requeue_orphaned_running()` resets any `running` rows left by a previous crash back to `pending`, so no action is permanently lost.

**Single-instance constraint:** Do not run the scheduler in more than one process against the same database. The Fly.io API app is pinned to `max_machines_running = 1` for exactly this reason.

```
SQLite scheduled_actions (status=pending, scheduled_for ≤ now)
        │
        ▼ claim_due_actions() UPDATE … RETURNING
        │
        ▼ asyncio.create_task(_execute_action)
        │
        ▼ Executive.chat(prompt=action.prompt, ...)
        │
        ▼ mark_action_done() / mark_action_failed_or_retry()
```

---

## Alerts System

`alerts/`

A proactive event triage pipeline that routes incoming signals (emails, Slack messages, documents) through a `TriageAgent` before deciding whether to surface them as alerts.

### Components

| File | Role |
|---|---|
| `pipeline.py` | Entry point — `evaluate_and_dispatch()`. Applies rate limiting (60 events/min), deduplication, runs triage |
| `models.py` | `AlertEvent`, `TriageDecision`, `Alert`, `AlertSeverity` (low/medium/high/urgent), `AlertChannel` (web/slack_dm/email/persisted) |
| `store.py` | SQLite read/write for alert records (same `episodic_memory.db`) |
| `dispatcher.py` | Fans out to configured delivery channels |
| `sse_bus.py` | In-process SSE bus — pushes web-channel alerts to connected UI clients without polling |
| `preferences.py` | Per-channel user preferences (min severity, quiet hours) |

### Triage flow

```
AlertEvent (source, subject, body, external_id)
        │
        ▼ _rate_limited() → drop if > 60/min
        │
        ▼ store.is_duplicate(external_id) → skip if seen
        │
        ▼ TriageAgent.evaluate(event)  [claude-haiku-4-5-20251001]
        │   → TriageDecision(alert=bool, severity, summary, suggested_action)
        │
        ▼ alert=True → store.create_alert() → dispatcher.send(channels)
                                              └── sse_bus.publish() (web channel)
```

---

## Workflows

`workflows/`

Pre-built multi-step executive deliverables. Each workflow is a subclass of `WorkflowBase` that streams `WorkflowEvent` objects — plan steps first, then intermediate summaries, then a final Markdown artifact.

### Available workflows (18)

| Workflow | Section |
|---|---|
| `board_prep` | Board |
| `investor_update` | Board / Capital & Investors |
| `fundraising_prep` | Capital & Investors |
| `ma_evaluation` | Capital & Investors |
| `annual_plan` | Operating Cadence |
| `quarterly_plan` | Operating Cadence |
| `mbr` (monthly business review) | Operating Cadence |
| `competitive_teardown` | Growth & GTM |
| `gtm_launch` | Growth & GTM |
| `pricing_review` | Growth & GTM |
| `product_strategy` | Product |
| `performance_review` | People |
| `comp_refresh` | People |
| `org_design` | People |
| `exec_search_brief` | People |
| `churn_deep_dive` | Growth & GTM |
| `crisis_comms` | Risk, Legal & Crisis |
| `risk_register` | Risk, Legal & Crisis |

### Execution model

```python
class WorkflowBase(ABC):
    @abstractmethod
    async def run(self, ...) -> AsyncIterator[WorkflowEvent]: ...
```

Events are SSE-streamed to the UI. `persistence.py` stores run state in SQLite so partially-completed runs survive a server restart. The `WorkflowSection` enum drives the UI's section grouping and order.

---

## Audit Log

`audit/logger.py`

SQLite-backed append-only audit trail. Writes to the same `episodic_memory.db` file. Swallows exceptions — an audit failure must never break a chat turn.

**Recorded event types:**

| Type | When |
|---|---|
| `chat_turn` | Every Executive response |
| `specialist_consult` | Each specialist agent called |
| `tool_invocation` | MCP tool calls |
| `scheduled_action` | Scheduler fires an action |
| `alert` | Alert triaged and dispatched |
| `integration_inbound` | Slack/Email/Telegram/Google Chat message received |

`redaction.py` strips PII patterns (email addresses, phone numbers, credit card numbers) from summaries before storage.

---

## Onboarding Wizard

`onboarding/wizard.py`

A 9-step state machine. Works as both a CLI flow and an API-driven web wizard.

| Step | Field | Required |
|---|---|---|
| 0 | Company name | Yes |
| 1 | Industry & stage | Yes |
| 2 | Team size & founding year | Yes |
| 3 | Business model & ARR | Yes |
| 4 | Competitors & advantages | Yes |
| 5 | Strategic priorities & north star metric | Yes |
| 6 | Culture & values | Optional |
| 7 | Monthly burn & runway | Optional |
| 8 | Mission & vision | Optional |

`WizardState` tracks `current_step`, `answers`, `completed`, and `skipped_steps`. `process_answer()` advances the state. `build_profile_from_answers()` parses free-text answers into structured fields.

Output: `company/profile.yaml` (gitignored).

---

## API Endpoints

`api/routes/`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check + ChromaDB status |
| `POST` | `/chat` | SSE streaming chat |
| `GET/POST/PATCH` | `/onboard/*` | Onboarding wizard |
| `GET/PATCH` | `/company-profile` | Read / update company profile |
| `POST/GET/DELETE` | `/documents` | Upload, list, delete company documents |
| `GET/PATCH/DELETE` | `/agents/*` | List agents, inspect or override model |
| `GET/POST/PATCH/DELETE` | `/memories/*` | Episodic decisions and initiatives |
| `GET` | `/sessions/*` | Session history and messages |
| `GET/POST/PUT` | `/builtin`, `/external` | Built-in knowledge management |
| `GET/PATCH/POST` | `/workflows/*` | List workflows, run, stream, manage runs |
| `GET/POST/DELETE` | `/alerts/*` | Alert list, ack, feedback, preferences |
| `GET/POST/DELETE` | `/scheduled/*` | Scheduled actions |
| `GET/PATCH/POST` | `/items/*` | Knowledge review queue |
| `GET` | `/audit/logs` | Audit log read |
| `GET/POST` | `/architecture/sections/*` | Self-documented architecture sections |
| `GET/POST/PUT` | `/skills/*` | Skill registry |
| `POST` | `/webhook/telegram` | Telegram webhook receiver |
| `POST` | `/webhook/google-chat` | Google Chat webhook receiver |

### Chat SSE format

```
data: {"type": "chunk", "content": "...", "session_id": "..."}
data: {"type": "chunk", "content": "..."}
...
data: {"type": "done", "session_id": "..."}
```

On error:
```
data: {"type": "error", "message": "..."}
```

---

## Integrations

### Slack Bot
`integrations/slack_bot.py`

Slack Bolt app in socket mode. Listens for `@OpenExecutive` mentions and direct messages. Calls `Executive.chat()` synchronously via `asyncio.run()`. Runs as a separate process alongside FastAPI.

Required env vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`

### Email Poller
`integrations/email_poller.py`

Polls the configured Gmail inbox via Gmail MCP OAuth every `EMAIL_POLL_INTERVAL_SECONDS` (default 60s). Parses email threads and routes them through the triage pipeline before deciding whether to surface them as alerts or respond directly. Outbound replies are sent via Gmail MCP.

Required env var: `EXEC_EMAIL_ADDRESS`  
Optional: `EMAIL_POLL_INTERVAL_SECONDS`

**Access control**: roster-driven. A sender's address must match the `email` field of a non-archived Person row to receive a response; unknown senders are silently dropped and marked read. Manage via the /people UI.

### Telegram Bot
`integrations/telegram_bot.py`

Webhook-based bot registered via FastAPI (`POST /webhook/telegram`). Validates incoming requests against `TELEGRAM_WEBHOOK_SECRET` using HMAC. Splits long responses at paragraph boundaries to stay within Telegram's 4096-char limit.

Required env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`

**Access control**: roster-driven. A sender's chat_id must match the `telegram_chat_id` field of a non-archived Person row to receive a response.

Setup: register the webhook once with Telegram after the API is deployed:
```bash
curl -F "url=https://your-api-host/webhook/telegram" \
  "https://api.telegram.org/bot<TOKEN>/setWebhook"
```

### Google Chat
`integrations/google_chat.py`

Webhook-based integration registered via FastAPI (`POST /webhook/google-chat`). Verifies incoming JWT Bearer tokens from Google (issuer `chat@system.gserviceaccount.com`, audience = GCP project number). Authenticates outbound Chat REST API calls via a service account.

Three auth modes (selected by which env vars are set):
1. **Key file** (`GOOGLE_CHAT_SERVICE_ACCOUNT_FILE`) — standard SA JSON key; blocked by some org policies
2. **Impersonation** (`GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL`, no key file) — uses ADC to impersonate the SA
3. **ADC direct** — neither var set; ambient credential must already be the Chat bot SA

Required env var: `GOOGLE_CHAT_PROJECT_NUMBER`  
Optional: `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE`, `GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL`

See [docs/google_chat_setup.md](google_chat_setup.md) for full setup instructions.

---

## Configuration

All settings via environment variables (`.env` file in `packages/core/`).

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `DEFAULT_MODEL` | No | `claude-sonnet-4-6` | Model for Executive + most specialists |
| `DEEP_REASONING_MODEL` | No | `claude-opus-4-7` | Model for CSO, CFO, GC, Board |
| `ROUTING_MODEL` | No | `claude-haiku-4-5-20251001` | Model for intent routing and memory extraction |
| `VECTOR_STORE_PATH` | No | `./chroma_db` | ChromaDB persistence directory |
| `COMPANY_PROFILE_PATH` | No | `./company/profile.yaml` | Company profile location |
| `EPISODIC_DB_PATH` | No | `./episodic_memory.db` | SQLite for episodic memory, alerts, audit, scheduler |
| `ENABLE_CACHING` | No | `true` | Anthropic prompt caching |
| `SCHEDULER_ENABLED` | No | `true` | Enable background scheduler |
| `SCHEDULER_POLL_INTERVAL_SECONDS` | No | `30` | Scheduler poll frequency |
| `SLACK_BOT_TOKEN` | No | — | Slack bot OAuth token |
| `SLACK_APP_TOKEN` | No | — | Slack socket mode token |
| `EXEC_EMAIL_ADDRESS` | No | — | Executive Gmail address (Gmail MCP OAuth) |
| `EMAIL_POLL_INTERVAL_SECONDS` | No | `60` | Email poll frequency |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token |
| `TELEGRAM_WEBHOOK_SECRET` | No | — | HMAC secret for webhook validation |
| `GOOGLE_CHAT_PROJECT_NUMBER` | No | — | GCP project number |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` | No | — | Path to SA JSON key |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL` | No | — | SA email for ADC impersonation |
| `GOOGLE_OAUTH_CLIENT_ID` | No | — | Google OAuth client ID (Gmail MCP) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | No | — | Google OAuth client secret (Gmail MCP) |
| `MCP_ENABLED` | No | `false` | Enable MCP tool gateway |
| `UI_BASE_URL` | No | `http://localhost:3000` | Base URL for UI links in notifications |
| `USER_TIMEZONE` | No | `UTC` | Timezone for scheduler and alerts |

See [../.env.example](../.env.example) for the full list.

---

## Evaluation System

`evals/`

29 scenarios covering all 8 domains, scored by `claude-opus-4-7` as an LLM-as-judge.

Each scenario YAML defines:
- `query` — the question posed to the Executive
- `company_context` — simulated company profile
- `expected_topics` — topics the response must cover
- `required_routing` — which specialists must be consulted
- `quality_criteria` — domain-specific rubric

Five scoring dimensions (1–5 scale each):
1. **Persona coherence** — sounds like a seasoned exec, not a generic AI
2. **Domain accuracy** — frameworks, numbers, and legal basics are correct
3. **Company context utilization** — references the specific company situation
4. **Routing quality** — correct specialists consulted; multi-specialist for cross-domain
5. **Actionability** — concrete next steps with timelines

CI gate: all evals must score ≥ 3.5/5 average. Any dimension dropping >10% vs `main` fails the PR.

---

## Adding a New Specialist Agent

1. Create `packages/core/openexecutive/agents/your_agent.py`:

```python
from openexecutive.agents.base import BaseAgent

class YourAgent(BaseAgent):
    name = "your_agent"
    domain = "your_domain"
    model = "claude-sonnet-4-6"

    def get_system_prompt(self) -> str:
        from openexecutive.prompts.domain_prompts import YOUR_AGENT_PROMPT
        return YOUR_AGENT_PROMPT
```

2. Add `YOUR_AGENT_PROMPT` to `prompts/domain_prompts.py`

3. Register in `orchestrator/router.py`:
   - Add to `SPECIALIST_REGISTRY`
   - Add to the `specialist` enum in `SPECIALIST_TOOLS`

4. Add domain alias to `DOMAIN_ALIASES` in `knowledge/retriever.py`

5. Add knowledge docs to `knowledge/builtin/your_domain/`

6. Add at least 2 eval scenarios to `evals/scenarios/`

7. Submit PR — CI requires all of the above.

---

## Company Data Privacy

Everything in `company/` is gitignored. This includes:

- `company/profile.yaml` — structured company profile (wizard output)
- `company/docs/` — uploaded documents
- `chroma_db/` — the vector store (contains embeddings of company documents)
- `episodic_memory.db` — decisions, initiatives, advice, alerts, audit log

None of this leaves the local machine (or your own Fly volume in cloud deployments) except as part of prompts sent to the Anthropic API. Anthropic does not train on API data.

---

## Execution Security (Grimdall) — Optional

Because RAG context and inbound channel messages are injected into user turns,
the model can be steered by malicious content into tool calls that exfiltrate
secrets, destroy state, or run unbounded spend. An **optional** deterministic
guard layer (`openexecutive/security/grimdall_guard.py`, **off by default**)
evaluates every skill/MCP tool call at the `_run_agent_loop` dispatch boundary
and re-checks at `MCPGateway.call_tool` (covering direct callers such as the
scheduler):

1. **Secret denial** — blocks tool arguments naming credential paths (`.env`,
   `~/.ssh`, `~/.aws`, `company/`, `/data`, Google service-account JSON,
   `episodic_memory.db`, `chroma_db`).
2. **Egress allowlist** — bare `http(s)` URL arguments must hit an allowlisted
   host (exact or real subdomain), failing closed. `web_search`, `scrape_url`,
   and `load_mcp_server` are exempt (their data path terminates at allowlisted
   providers / their own URL gates).
3. **Destructive block** — shell-destructive fragments in arguments (`rm -rf`,
   `sudo`, `chmod 777`, `shutdown`, `git reset --hard`, `DROP`/`TRUNCATE
   TABLE`). No tool-name denylist, so legitimate delete tools keep working.
4. **Per-session spend guardrail** — sums the session's `cache_event` audit
   rows and blocks further dispatch past `GRIMDALL_MAX_TOKENS_PER_SESSION` /
   `GRIMDALL_MAX_COST_USD_PER_SESSION`, catching runaway extended-thinking
   loops that recur across turns.

Modes: `GRIMDALL_ENABLED=true` → Shadow (violations written to `audit_log` as
signed `grimdall_block` receipts, execution proceeds); `GRIMDALL_ENFORCE=true`
→ Enforce (violating calls return an error `tool_result`). Receipts are
HMAC-SHA256 signed (`GRIMDALL_SIGNING_KEY` or auto-generated
`company/.grimdall-key`). All checks are deterministic string/ledger
operations — no LLM calls, no network, no shell — and audit writes swallow
exceptions so the guard never breaks a turn.
