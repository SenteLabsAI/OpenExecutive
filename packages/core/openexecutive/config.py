from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Walk up from this file to find the repo root .env. If no .env exists
# (CI, fresh checkouts), `_ROOT` becomes `cwd` so file-path defaults stay
# inside the working tree instead of resolving to filesystem root —
# previously `_ROOT / "chroma_db"` became `/chroma_db` in CI, which is
# unwritable and produced `chromadb.InternalError: Permission denied`.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE
_FOUND_ENV = False
while _ROOT.parent != _ROOT:
    if (_ROOT / ".env").exists():
        _FOUND_ENV = True
        break
    _ROOT = _ROOT.parent
if not _FOUND_ENV:
    _ROOT = Path.cwd()
_ENV_FILE = _ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optional: a deployment can run entirely on local / OpenRouter models
    # with no Anthropic key. The `_validate_provider_available` model
    # validator below ensures at least one backend is reachable, and the
    # registry raises an actionable error if a Claude model is requested
    # while this is unset.
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")

    default_model: str = Field("claude-sonnet-4-6", alias="DEFAULT_MODEL")
    deep_reasoning_model: str = Field("claude-opus-4-7", alias="DEEP_REASONING_MODEL")
    routing_model: str = Field("claude-haiku-4-5-20251001", alias="ROUTING_MODEL")
    # Model for the executive_research specialist fan-out (research-mode turn
    # only — the chat path still uses each agent's deep_reasoning_model). The
    # research turn is retrieve-from-web-search + summarize, which does not
    # need Opus-tier reasoning; running 7 specialists on Sonnet (deep reasoning
    # off) instead of Opus 4.7 is the dominant cost lever for the workflow.
    # Set RESEARCH_MODEL=claude-opus-4-7 to restore the prior behavior.
    research_model: str = Field("claude-sonnet-4-6", alias="RESEARCH_MODEL")

    vector_store_path: Path = Field(_ROOT / "chroma_db", alias="VECTOR_STORE_PATH")
    company_profile_path: Path = Field(
        _ROOT / "company" / "profile.yaml", alias="COMPANY_PROFILE_PATH"
    )

    enable_caching: bool = Field(True, alias="ENABLE_CACHING")

    # ---- Knowledge retrieval (RAG) tuning ------------------------------
    # Defaults mirror the historical hard-coded values in
    # knowledge/retriever.py. Exposed as settings so the relevance gate and
    # per-collection chunk counts can be tuned without code edits — and so a
    # RAG ablation run can disable builtin retrieval by setting
    # KNOWLEDGE_BUILTIN_N_RESULTS=0 (see openexecutive/evals/ablation.py).
    knowledge_distance_threshold: float = Field(
        0.55, alias="KNOWLEDGE_DISTANCE_THRESHOLD"
    )
    knowledge_builtin_n_results: int = Field(5, alias="KNOWLEDGE_BUILTIN_N_RESULTS")
    knowledge_company_n_results: int = Field(3, alias="KNOWLEDGE_COMPANY_N_RESULTS")

    # Max parallel `consult_specialist` calls dispatched in one chat turn.
    # 0 (default) is inert: resolve_fanout_cap() falls back to the specialist
    # roster size, which no real cross-domain turn exceeds. Set a positive
    # value to bound worst-case turn cost — consult_specialist tool calls past
    # the cap are skipped with a tool_result the model can react to, instead of
    # silently fanning out (each specialist call carries its own RAG + memory
    # prefetch, so unbounded fan-out is the main per-turn cost driver).
    max_parallel_specialists: int = Field(0, alias="MAX_PARALLEL_SPECIALISTS")

    # ---- OpenRouter routing --------------------------------------------
    # Toggle that routes Claude calls through OpenRouter (so usage is
    # billed to your OpenRouter account) and unlocks the curated set of
    # non-Anthropic models in the Council UI. Default OFF so a fresh
    # checkout's behavior is identical to before.
    openrouter_enabled: bool = Field(False, alias="OPENROUTER_ENABLED")
    openrouter_api_key: str | None = Field(None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        "https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    # Surfaced in your OpenRouter dashboard alongside the cost data.
    openrouter_app_title: str = Field("Open Executive", alias="OPENROUTER_APP_TITLE")
    openrouter_referer: str | None = Field(None, alias="OPENROUTER_REFERER")
    openrouter_timeout_s: float = Field(180.0, alias="OPENROUTER_TIMEOUT_S")

    # Per-call wall-clock cap for the utility_fast paths (Discord response
    # gate, wait_for_human decision parser, inbound_resolver disambiguation).
    # Anthropic-direct haiku usually returns in <1s, but routing utility_fast
    # to a slow OpenRouter model (a BYO non-Claude slug) can take 5-15s.
    # Default 10s covers the latter while still failing fast enough that a
    # stuck request doesn't pile up tasks.
    utility_fast_timeout_s: float = Field(10.0, alias="UTILITY_FAST_TIMEOUT_S")

    @model_validator(mode="after")
    def _validate_openrouter(self) -> "Settings":
        # Enabling the toggle without a key would silently 401 every call.
        # Fail loud at startup instead.
        if self.openrouter_enabled and not self.openrouter_api_key:
            raise ValueError(
                "OPENROUTER_ENABLED=true requires OPENROUTER_API_KEY to be set"
            )
        return self

    # ---- Local / self-hosted models ------------------------------------
    # Route selected model slugs to a local OpenAI-compatible server
    # (Ollama, LM Studio, vLLM, llama.cpp, …) instead of the Anthropic API.
    # Default OFF so a fresh checkout's behavior is identical to before.
    #
    # To run with NO Anthropic key, also point the model settings at local
    # slugs, e.g.:
    #   LOCAL_MODELS_ENABLED=true
    #   LOCAL_BASE_URL=http://localhost:11434/v1   # Ollama
    #   LOCAL_MODELS=llama3.3,qwen2.5
    #   DEFAULT_MODEL=llama3.3
    #   DEEP_REASONING_MODEL=llama3.3
    #   ROUTING_MODEL=llama3.3
    local_models_enabled: bool = Field(False, alias="LOCAL_MODELS_ENABLED")
    # Base URL of the local OpenAI-compatible server, including the version
    # path, e.g. http://localhost:11434/v1 (Ollama) or http://localhost:1234/v1
    # (LM Studio). Required when LOCAL_MODELS_ENABLED is on.
    local_base_url: str | None = Field(None, alias="LOCAL_BASE_URL")
    # Optional bearer token. Ollama / LM Studio need none; vLLM or a gateway
    # in front of it may. Omitted from requests entirely when unset.
    local_api_key: str | None = Field(None, alias="LOCAL_API_KEY")
    # Comma-separated model slugs to surface in the Council UI and route to
    # the local backend, e.g. "llama3.3,qwen2.5:14b". These are sent to the
    # server verbatim, so they must match the names it serves.
    local_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="LOCAL_MODELS"
    )
    # Local generation (especially CPU inference) can be far slower than a
    # hosted API. Default generous so a slow first token doesn't time out.
    local_timeout_s: float = Field(300.0, alias="LOCAL_TIMEOUT_S")

    # Dedicated credential/config root for OpenAI's official Codex App Server.
    # When unset, providers.codex_auth derives a private sibling directory from
    # COMPANY_PROFILE_PATH, keeping OE's managed login isolated from any
    # developer-level ~/.codex session. Docker's company path is under /data,
    # so the derived directory also survives machine restarts.
    codex_home_path: Path | None = Field(None, alias="CODEX_HOME_PATH")

    # Optional deployment-admin override for legacy installations whose
    # canonical principal was created before browser email binding existed.
    principal_email: str | None = Field(None, alias="PRINCIPAL_EMAIL")

    @field_validator("local_models", mode="before")
    @classmethod
    def _parse_local_models(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [x.strip() for x in v.split(",") if x.strip()]
        return []

    @model_validator(mode="after")
    def _validate_local_models(self) -> "Settings":
        if self.local_models_enabled and not self.local_base_url:
            raise ValueError(
                "LOCAL_MODELS_ENABLED=true requires LOCAL_BASE_URL to be set "
                "(e.g. http://localhost:11434/v1 for Ollama)"
            )
        return self

    @model_validator(mode="after")
    def _validate_provider_available(self) -> "Settings":
        # At least one backend must be reachable, or every model call fails.
        if not (
            self.anthropic_api_key
            or self.openrouter_enabled
            or self.local_models_enabled
        ):
            raise ValueError(
                "No LLM provider configured. Set ANTHROPIC_API_KEY, or enable "
                "OpenRouter (OPENROUTER_ENABLED=true + OPENROUTER_API_KEY), or "
                "enable local models (LOCAL_MODELS_ENABLED=true + LOCAL_BASE_URL)."
            )
        return self

    # ---- Honcho memory provider ----------------------------------------
    # External per-person memory layer (https://honcho.dev). When enabled,
    # the Executive queries Honcho for a `<peer_memory>` block keyed off the
    # inbound user's Person.id (so Slack-Alice and Discord-Alice share one
    # peer card) and syncs each completed turn back to Honcho. Default OFF
    # so a fresh checkout's behavior is unchanged.
    honcho_enabled: bool = Field(False, alias="HONCHO_ENABLED")
    honcho_api_key: str | None = Field(None, alias="HONCHO_API_KEY")
    # Self-hosted Honcho lives at whatever URL the operator deploys it to.
    # The SDK's hosted default is the Plastic Labs cloud — we leave it
    # explicit here so the env var must be set for either path.
    honcho_base_url: str | None = Field(None, alias="HONCHO_BASE_URL")
    honcho_workspace_id: str = Field("openexec", alias="HONCHO_WORKSPACE_ID")
    # Hard ceiling on the prefetch call so a Honcho outage can't stall the
    # turn. 3s is generous for a local-network self-host; on timeout we
    # silently degrade to no peer_memory block and continue.
    honcho_prefetch_timeout_s: float = Field(3.0, alias="HONCHO_PREFETCH_TIMEOUT_S")

    @model_validator(mode="after")
    def _validate_honcho(self) -> "Settings":
        # Enabling without a key gets you 401s on every prefetch. Fail loud.
        if self.honcho_enabled and not self.honcho_api_key:
            raise ValueError(
                "HONCHO_ENABLED=true requires HONCHO_API_KEY to be set"
            )
        # HONCHO_BASE_URL is optional. The honcho-ai SDK (v2.1.1)
        # accepts ``base_url=None`` and falls back to its built-in
        # production endpoint, which is the right behavior for the
        # hosted-Honcho setup. Earlier we required it explicitly here,
        # but that crashed the openexec-api-dev deploy on 2026-05-25
        # because hosted Honcho doesn't need an operator-set URL.
        return self

    chat_stream_timeout_s: float = Field(120.0, alias="CHAT_STREAM_TIMEOUT_S")

    # Extra wall-clock allowance added to chat_stream_timeout_s when a request
    # opts in to Committee review. Committee adds three reviewer calls + one
    # full-pass revision on top of the draft, typically 5–12s.
    committee_extra_timeout_s: float = Field(60.0, alias="COMMITTEE_EXTRA_TIMEOUT_S")

    # Reasoning effort for deep-reasoning specialists. Opus 4.7 only supports
    # `thinking.type=adaptive` paired with `output_config.effort`. Valid
    # values: "low", "medium", "high", "xhigh", "max". `low` is ~3x faster
    # and much cheaper; bump to `medium` when answers feel shallow.
    specialist_effort: str = Field("low", alias="SPECIALIST_EFFORT")

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        # Resolve relative paths against cwd, not _ROOT. _ROOT can walk all the
        # way to / when no .env is present (e.g. CI), making relative paths
        # like "./chroma_db" resolve to unwritable system paths.
        base = Path.cwd()
        if not self.vector_store_path.is_absolute():
            self.vector_store_path = base / self.vector_store_path
        if not self.company_profile_path.is_absolute():
            self.company_profile_path = base / self.company_profile_path
        if self.codex_home_path is not None and not self.codex_home_path.is_absolute():
            self.codex_home_path = base / self.codex_home_path
        return self

    slack_bot_token: str | None = Field(None, alias="SLACK_BOT_TOKEN")
    slack_app_token: str | None = Field(None, alias="SLACK_APP_TOKEN")
    # Company-wide broadcast channels — when set, OE can post to "the
    # whole team" on a given integration without picking a specific
    # human or department. Used by `send_company_broadcast`. Each is
    # independently optional: a deployment can wire up just Slack
    # broadcast and leave Discord/Telegram unconfigured.
    slack_default_channel_id: str | None = Field(
        None, alias="SLACK_DEFAULT_CHANNEL_ID"
    )
    discord_default_channel_id: str | None = Field(
        None, alias="DISCORD_DEFAULT_CHANNEL_ID"
    )
    telegram_default_chat_id: str | None = Field(
        None, alias="TELEGRAM_DEFAULT_CHAT_ID"
    )

    # Required: the Executive's own Google Workspace address. No default —
    # we never want the Executive to operate as some other user's account
    # because an env var silently fell through. The email poller, alert
    # dispatcher, and the persona's identity addendum all read this.
    exec_email_address: str = Field(..., alias="EXEC_EMAIL_ADDRESS")
    # Display name the Executive signs messages with. Pinned into the
    # identity addendum so the model has a concrete self-name and never
    # falls back to signing as a person from the company People roster.
    exec_display_name: str = Field("Open Executive", alias="EXEC_DISPLAY_NAME")
    email_poll_interval_seconds: int = Field(60, alias="EMAIL_POLL_INTERVAL_SECONDS")

    # Telegram + Discord channel access is roster-driven: a sender's
    # channel ID must be present on a non-archived Person row. The old
    # TELEGRAM_ALLOWED_CHAT_IDS / DISCORD_ALLOWED_USER_IDS / EMAIL_ALLOWED_SENDERS
    # env vars have been removed — manage access via the /people UI.
    telegram_bot_token: str | None = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_webhook_secret: str | None = Field(None, alias="TELEGRAM_WEBHOOK_SECRET")

    discord_bot_token: str | None = Field(None, alias="DISCORD_BOT_TOKEN")
    discord_app_id: str | None = Field(None, alias="DISCORD_APP_ID")
    discord_guild_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list, alias="DISCORD_GUILD_IDS"
    )
    discord_notify_channel_id: int | None = Field(None, alias="DISCORD_NOTIFY_CHANNEL_ID")
    discord_thread_response_gate_enabled: bool = Field(
        True, alias="DISCORD_THREAD_RESPONSE_GATE_ENABLED"
    )

    # @mention auto-thread router. The default mode promotes nearly every
    # plain-channel @mention into a fresh auto-titled thread (with a
    # one-line pointer left behind in the channel) — except when the
    # user's message is a bare greeting like "hi" or "good morning",
    # which stays inline so a casual hello doesn't clutter the channel
    # with a new thread. The legacy length-based heuristic (`auto` mode)
    # is preserved for callers that want the older behavior.
    #
    # Kill switch via DISCORD_MENTION_REPLY_MODE:
    #   - "thread_unless_greeting" (default) — promote unless the user's
    #     incoming text is a bare greeting (see _is_simple_greeting in
    #     integrations/discord_bot.py for the curated list).
    #   - "auto"           — length-only heuristic on the generated reply
    #     (promote when len(response) >= DISCORD_MENTION_THREAD_THRESHOLD_CHARS).
    #   - "always_thread"  — restore legacy behavior (every @mention opens a thread).
    #   - "always_inline"  — never promote, even for long replies.
    discord_mention_thread_threshold_chars: int = Field(
        1500, alias="DISCORD_MENTION_THREAD_THRESHOLD_CHARS", ge=0
    )
    # Pydantic v2 enforces the Literal natively — a typo'd env var fails at
    # startup with a clear message instead of silently falling through to
    # one of the branches.
    discord_mention_reply_mode: Literal[
        "thread_unless_greeting", "auto", "always_thread", "always_inline"
    ] = Field("thread_unless_greeting", alias="DISCORD_MENTION_REPLY_MODE")

    @field_validator("discord_guild_ids", mode="before")
    @classmethod
    def _parse_guild_ids(cls, v: Any) -> list[int]:
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, (int, float)):
            return [int(v)]
        if isinstance(v, str) and v.strip():
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return []

    google_chat_service_account_file: str | None = Field(
        None, alias="GOOGLE_CHAT_SERVICE_ACCOUNT_FILE"
    )
    google_chat_service_account_email: str | None = Field(
        None, alias="GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL"
    )
    google_chat_project_number: str | None = Field(None, alias="GOOGLE_CHAT_PROJECT_NUMBER")

    mcp_servers_config_path: Path = Field(
        _ROOT / "company" / "mcp_servers.json", alias="MCP_SERVERS_CONFIG_PATH"
    )
    mcp_enabled: bool = Field(False, alias="MCP_ENABLED")

    # ---- Calendar booking (first-climb autonomy, Build 1) ------------------
    # When true, the `create_calendar_event` / `cancel_calendar_event` tools
    # are surfaced to the Executive and calendar operations route through the
    # Google Workspace MCP (workspace-mcp at --tool-tier complete).
    # Fail-soft: disabled when unconfigured. No hard validator — the project
    # was burned by required-when-X validators crashing boot (honcho).
    calendar_booking_enabled: bool = Field(False, alias="CALENDAR_BOOKING_ENABLED")
    # Code-enforced caps applied regardless of trust-ledger promotion state.
    calendar_business_hours_start: str = Field("09:00", alias="CALENDAR_BUSINESS_HOURS_START")
    calendar_business_hours_end: str = Field("18:00", alias="CALENDAR_BUSINESS_HOURS_END")
    # Maximum days in the future an event can be booked.
    calendar_horizon_days: int = Field(30, alias="CALENDAR_HORIZON_DAYS")
    # Hard ceiling on bookings created per calendar-day for this class.
    calendar_max_events_per_day: int = Field(10, alias="CALENDAR_MAX_EVENTS_PER_DAY")
    # Maximum number of attendees per event (inclusive of organizer).
    calendar_max_attendees: int = Field(8, alias="CALENDAR_MAX_ATTENDEES")
    # When true, every booking requests a Google Meet video link
    # (add_google_meet on the manage_event MCP call). The model can still
    # opt out per-event via the tool's add_google_meet=false.
    calendar_meet_links_enabled: bool = Field(True, alias="CALENDAR_MEET_LINKS_ENABLED")
    # Default duration (minutes) for an impromptu create_instant_meeting that
    # doesn't specify one.
    calendar_instant_meeting_minutes: int = Field(30, alias="CALENDAR_INSTANT_MEETING_MINUTES")
    # When true, a successful booking schedules a post-meeting follow-up that
    # DMs a human attendee for the recap (decisions + action items).
    calendar_post_meeting_followup_enabled: bool = Field(
        True, alias="CALENDAR_POST_MEETING_FOLLOWUP_ENABLED"
    )

    # Anthropic native web_search server tool. Enabled by default — the
    # Executive needs live lookups to fulfill briefing proposals that ask
    # for research (e.g. "skim Ford IR", "check Lucid news"). Set
    # ENABLE_WEB_SEARCH=false to opt out and avoid per-search charges.
    enable_web_search: bool = Field(True, alias="ENABLE_WEB_SEARCH")
    # Cap on web searches per specialist/Executive turn. Each search is billed,
    # so the fan-out cost scales with this (7 specialists x N searches). Most
    # findings come from the first one or two searches; default 2 keeps the
    # bulk of the value at a fraction of the search spend. Raise via
    # WEB_SEARCH_MAX_USES for deeper digs.
    web_search_max_uses: int = Field(2, alias="WEB_SEARCH_MAX_USES")
    web_search_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="WEB_SEARCH_ALLOWED_DOMAINS"
    )
    web_search_blocked_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="WEB_SEARCH_BLOCKED_DOMAINS"
    )

    # ── xcrawl (external scrape / SERP API) ───────────────────────────────
    # Off by default. xcrawl's scrape API reads JS-rendered / bot-blocked
    # pages the keyless RSS adapter cannot, so the watchlist can monitor
    # sources that have NO usable RSS feed (modern marketing / news SPAs)
    # via scrape-backed change detection, and validate / repair a feed
    # target at insert time so dead rows never reach the scan loop.
    #
    # No "required when enabled" validator: a flag set without a key
    # degrades to disabled in the client rather than crash-looping boot.
    xcrawl_enabled: bool = Field(False, alias="XCRAWL_ENABLED")
    xcrawl_api_key: str | None = Field(None, alias="XCRAWL_API_KEY")
    xcrawl_base_url: str = Field(
        "https://run.xcrawl.com/v1", alias="XCRAWL_BASE_URL"
    )
    xcrawl_timeout_s: float = Field(30.0, alias="XCRAWL_TIMEOUT_S")

    # ── research finding verification (xcrawl scrape deep-read) ───────────
    # When on (also requires xcrawl_enabled), the executive_research workflow
    # runs a post-dedup pass that scrapes each surviving finding's cited URL
    # and asks a cheap model whether the page actually supports the claim —
    # demoting / dropping findings whose source is dead or doesn't back them,
    # before the Executive routes anything. web_search gives specialists
    # discovery (snippets); this gives them the deep-read the finding contract
    # already assumes. Off by default; a no-op when xcrawl is disabled.
    external_research_verify_enabled: bool = Field(
        False, alias="EXTERNAL_RESEARCH_VERIFY_ENABLED"
    )
    # Cheap model for the per-finding verify call (read scraped page → verdict).
    research_verify_model: str = Field(
        "claude-haiku-4-5-20251001", alias="RESEARCH_VERIFY_MODEL"
    )
    # Hard cap on findings verified per research run (each = 1 scrape + 1 cheap
    # LLM call). Bounds added cost; verified in severity order.
    research_verify_max_findings: int = Field(
        8, alias="RESEARCH_VERIFY_MAX_FINDINGS"
    )

    # ── agentic research (read-before-cite scrape loop) ───────────────────
    # When on (also requires xcrawl_enabled), each executive_research
    # specialist runs a bounded search→scrape_url→emit tool-use loop: it
    # reads the FULL article behind its best web_search hits (xcrawl scrape)
    # and grounds claims in real content, instead of the single-shot
    # snippet-only call that produced findings whose cited sources didn't
    # back them. Off by default (single-shot path unchanged); the verify
    # pass remains the safety net.
    research_agentic_scrape_enabled: bool = Field(
        False, alias="RESEARCH_AGENTIC_SCRAPE_ENABLED"
    )
    # Max scrape_url calls a specialist may make per run (each = 1 xcrawl
    # scrape + the page's tokens). Past this the tool refuses and tells the
    # model to emit. Bounds per-run cost across the 7-specialist fan-out.
    research_scrape_max_per_specialist: int = Field(
        3, alias="RESEARCH_SCRAPE_MAX_PER_SPECIALIST"
    )
    # Hard ceiling on tool-use turns per specialist loop (search/scrape/emit).
    # Guards against a specialist that never emits.
    research_loop_max_iterations: int = Field(
        5, alias="RESEARCH_LOOP_MAX_ITERATIONS"
    )

    @field_validator(
        "web_search_allowed_domains", "web_search_blocked_domains", mode="before"
    )
    @classmethod
    def _parse_domain_list(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [x.strip() for x in v.split(",") if x.strip()]
        return []

    @model_validator(mode="after")
    def _validate_web_search(self) -> "Settings":
        if self.web_search_allowed_domains and self.web_search_blocked_domains:
            raise ValueError(
                "Set WEB_SEARCH_ALLOWED_DOMAINS or WEB_SEARCH_BLOCKED_DOMAINS, not both"
            )
        if self.web_search_max_uses < 1:
            raise ValueError("WEB_SEARCH_MAX_USES must be >= 1")
        return self

    # Base URL of the UI, used when the Executive composes deep links
    # (e.g., proactive nudges that suggest running a workflow). No trailing
    # slash. Override for production deployments. Must be an http(s) URL —
    # validated at startup so a misconfigured value cannot produce a deep
    # link that points at a non-web scheme or an arbitrary attacker host.
    ui_base_url: str = Field("http://localhost:3000", alias="UI_BASE_URL")

    @field_validator("ui_base_url")
    @classmethod
    def _validate_ui_base_url(cls, v: str) -> str:
        from urllib.parse import urlparse
        v = v.strip()
        if not v:
            raise ValueError("UI_BASE_URL must not be empty")
        if len(v) > 512:
            raise ValueError("UI_BASE_URL must be 512 characters or fewer")
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                f"UI_BASE_URL scheme must be http or https (got {parsed.scheme!r})"
            )
        if not parsed.netloc:
            raise ValueError("UI_BASE_URL must include a host")
        return v

    # Proactive nudges / scheduler
    user_timezone: str = Field("UTC", alias="USER_TIMEZONE")
    max_scheduled_horizon_days: int = Field(30, alias="MAX_SCHEDULED_HORIZON_DAYS")
    max_pending_per_channel_ref: int = Field(20, alias="MAX_PENDING_PER_CHANNEL_REF")
    max_pending_global: int = Field(500, alias="MAX_PENDING_GLOBAL")
    scheduler_poll_interval_seconds: int = Field(30, alias="SCHEDULER_POLL_INTERVAL_SECONDS")
    scheduler_enabled: bool = Field(True, alias="SCHEDULER_ENABLED")
    scheduled_admin_token: str | None = Field(None, alias="SCHEDULED_ADMIN_TOKEN")

    # Outbound DM anti-spam guard — applied at the send-tool chokepoint
    # (orchestrator.outbound_guard, enforced inside the telegram/slack/discord
    # send handlers). Suppresses a proactive DM that would exceed the
    # per-recipient rate cap, duplicate a recently-sent message, or land while
    # the recipient is on leave / outside their availability windows.
    outbound_max_per_recipient_per_window: int = Field(
        5, alias="OUTBOUND_MAX_PER_RECIPIENT_PER_WINDOW"
    )
    outbound_rate_window_minutes: int = Field(60, alias="OUTBOUND_RATE_WINDOW_MINUTES")
    outbound_dedup_window_minutes: int = Field(360, alias="OUTBOUND_DEDUP_WINDOW_MINUTES")
    outbound_respect_quiet_hours: bool = Field(True, alias="OUTBOUND_RESPECT_QUIET_HOURS")

    # Overnight client rotation (multi-client practice mode) — during a quiet
    # window, activate each parked client slot in turn, generate its morning
    # brief and run its monitors, save it back, then restore the original
    # client and deliver a cross-client digest. OFF by default: automated
    # context switching plus per-client LLM cost must be a conscious opt-in.
    # The time of day comes from CLIENT_ROTATION_TIME (HH:MM UTC, default
    # 03:30), read by the scheduler like the principal-brief times.
    client_rotation_enabled: bool = Field(False, alias="CLIENT_ROTATION_ENABLED")

    # Proactive nudge engine — heartbeat that scans for stalled workflows,
    # stale commitments, and idle initiatives and emits per-channel nudges
    # routed via Person.preferred_channel + availability windows.
    nudge_scan_enabled: bool = Field(True, alias="NUDGE_SCAN_ENABLED")
    nudge_scan_interval_minutes: int = Field(15, alias="NUDGE_SCAN_INTERVAL_MINUTES")
    nudge_stalled_lead_hours: int = Field(24, alias="NUDGE_STALLED_LEAD_HOURS")
    nudge_stalled_min_quiet_hours: int = Field(24, alias="NUDGE_STALLED_MIN_QUIET_HOURS")
    nudge_stalled_cooldown_hours: int = Field(24, alias="NUDGE_STALLED_COOLDOWN_HOURS")
    nudge_commitment_stale_days: int = Field(3, alias="NUDGE_COMMITMENT_STALE_DAYS")
    nudge_commitment_cooldown_hours: int = Field(48, alias="NUDGE_COMMITMENT_COOLDOWN_HOURS")
    nudge_initiative_idle_days: int = Field(7, alias="NUDGE_INITIATIVE_IDLE_DAYS")
    nudge_initiative_cooldown_days: int = Field(7, alias="NUDGE_INITIATIVE_COOLDOWN_DAYS")
    nudge_max_defer_days: int = Field(3, alias="NUDGE_MAX_DEFER_DAYS")
    nudge_max_per_scan: int = Field(10, alias="NUDGE_MAX_PER_SCAN")
    nudge_max_per_person_per_scan: int = Field(2, alias="NUDGE_MAX_PER_PERSON_PER_SCAN")

    # External-condition monitoring — heartbeat that polls source adapters
    # (vendor_status in PR-A; RSS + stock in PR-B) and emits external_signals
    # rows, then promotes qualifying signals into the existing alerts pipeline
    # via monitoring.pipeline.promote_signal_to_alert. Mirrors nudge_scan.
    external_monitor_enabled: bool = Field(True, alias="EXTERNAL_MONITOR_ENABLED")
    external_monitor_scan_interval_minutes: int = Field(
        5, alias="EXTERNAL_MONITOR_SCAN_INTERVAL_MINUTES"
    )
    # Cost guard: a misbehaving source returning 1000 events per tick must
    # not flood the alert pipeline. Excess signals beyond this cap are
    # logged-but-dropped at scan time; never queued.
    external_monitor_max_signals_per_scan: int = Field(
        50, alias="EXTERNAL_MONITOR_MAX_SIGNALS_PER_SCAN"
    )
    # Adapter-fetch ceiling (bytes). Caps the body we read from any single
    # external feed — defence against runaway sources (e.g. malformed RSS
    # that streams forever) and a soft guard against XML-bomb shapes.
    external_monitor_max_fetch_bytes: int = Field(
        2_000_000, alias="EXTERNAL_MONITOR_MAX_FETCH_BYTES"
    )
    # Kill switch for the BILLED standing-query adapter, independent of the
    # keyless feed adapters. When false, ``query`` watchlist rows are skipped
    # at poll time (no LLM call, no web search) but other sources keep running.
    external_monitor_query_enabled: bool = Field(
        True, alias="EXTERNAL_MONITOR_QUERY_ENABLED"
    )
    # Capture-time relevance enrichment: one cheap LLM call per NEW (post-dedup,
    # pre-promotion) signal that scores it against the company profile +
    # initiatives and writes a one-line "why this matters" into the alert.
    external_monitor_enrichment_enabled: bool = Field(
        True, alias="EXTERNAL_MONITOR_ENRICHMENT_ENABLED"
    )
    # Optional relevance gate: signals whose enrichment relevance_score is below
    # this threshold are recorded (outcome=suppressed_low_relevance) but not
    # promoted. Default 0.0 = OFF — surfacing is unchanged until an operator
    # calibrates a threshold against real traffic.
    external_monitor_enrichment_min_relevance: float = Field(
        0.0, alias="EXTERNAL_MONITOR_ENRICHMENT_MIN_RELEVANCE"
    )
    # User-Agent sent to SEC EDGAR by the `edgar` source adapter. SEC's fair-
    # access policy asks callers to identify with a descriptive UA INCLUDING a
    # contact email; requests with a missing/generic UA may be throttled or
    # blocked. Operators SHOULD override this with a real contact.
    edgar_user_agent: str = Field(
        "OpenExecutive-Monitor/1.0", alias="EDGAR_USER_AGENT"
    )
    # Per-source poll cadence comes from the adapter's
    # `default_poll_interval_minutes` attribute (see
    # monitoring.pipeline._poll_floor_minutes_for). PR-B may introduce
    # env overrides if a real need surfaces, but central dispatch on
    # signal_type was a premature abstraction at PR-A's scope.

    # Watchlist research workflow — periodic cron that re-runs the
    # 7-specialist research fan-out. The skip-if-unchanged pre-check
    # keeps the cost ~zero on quiet days; first tick after a profile /
    # initiative / watchlist change runs immediately.
    watchlist_research_enabled: bool = Field(
        True, alias="WATCHLIST_RESEARCH_ENABLED"
    )
    watchlist_research_interval_minutes: int = Field(
        120, alias="WATCHLIST_RESEARCH_INTERVAL_MINUTES"
    )
    # Staleness floor: even when the skip-if-unchanged fingerprint matches,
    # force a fresh research run once this many hours have elapsed since the
    # last successful run. This bounds how long the council can stay blind to
    # purely-external developments (a competitor move, a regulation change)
    # that the internal-state fingerprint can't see. Set <= 0 to disable the
    # floor and rely solely on skip-if-unchanged.
    watchlist_research_max_staleness_hours: int = Field(
        24, alias="WATCHLIST_RESEARCH_MAX_STALENESS_HOURS"
    )

    @field_validator("user_timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"USER_TIMEZONE {v!r} is not a known IANA zone") from exc
        return v

    @model_validator(mode="after")
    def _resolve_mcp(self) -> "Settings":
        if not self.mcp_servers_config_path.is_absolute():
            self.mcp_servers_config_path = Path.cwd() / self.mcp_servers_config_path
        if not self.mcp_enabled and self.mcp_servers_config_path.exists():
            self.mcp_enabled = True
        return self


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
