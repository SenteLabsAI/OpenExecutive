from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from openexecutive import mcp_server
from openexecutive.api.routes import (
    agents,
    alerts,
    architecture,
    artifacts,
    audit,
    chat,
    clients,
    company_profile,
    decisions,
    departments,
    documents,
    episodic,
    evals,
    fixtures,
    guide,
    health,
    knowledge,
    onboarding,
    people,
    personas,
    review,
    scheduled,
    sessions,
    skills,
    staff_onboarding,
    talent,
    today,
    watchlist,
    workflows,
)
from openexecutive.api.routes import (
    auth as auth_route,
)
from openexecutive.integrations.google_chat import router as google_chat_router
from openexecutive.integrations.telegram_bot import router as telegram_router


class _OELogFormatter(logging.Formatter):
    """Compact, scannable formatter for openexecutive logs.

    - Strips the redundant `openexecutive.` prefix from logger names.
    - Right-pads the module column so the message gutter aligns.
    - ANSI-colors level + module when stdout is a TTY; plain otherwise.
    - Honours `extra={"turn_break": True}` / `extra={"iter_marker": True}`
      to draw visual separators between chat turns and iteration loops.
    """

    _LEVEL_COLORS = {
        "DEBUG": "\033[2;37m",   # dim gray
        "INFO": "\033[36m",       # cyan
        "WARNING": "\033[33m",    # yellow
        "ERROR": "\033[31m",      # red
        "CRITICAL": "\033[1;31m", # bold red
    }
    _DIM = "\033[2m"
    _RESET = "\033[0m"
    _MODULE_WIDTH = 28

    def __init__(self, *, color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)
        name = record.name
        if name.startswith("openexecutive."):
            name = name[len("openexecutive."):]
        elif name == "openexecutive":
            name = "app"
        if len(name) > self._MODULE_WIDTH:
            name_col = name[: self._MODULE_WIDTH - 1] + "…"
        else:
            name_col = name.ljust(self._MODULE_WIDTH)
        level = record.levelname.ljust(7)
        msg = record.getMessage()
        if record.exc_info:
            msg = msg + "\n" + self.formatException(record.exc_info)

        if self._color:
            lvl_c = self._LEVEL_COLORS.get(record.levelname, "")
            line = (
                f"{self._DIM}{ts}{self._RESET}  "
                f"{lvl_c}{level}{self._RESET}  "
                f"{self._DIM}{name_col}{self._RESET} │ {msg}"
            )
        else:
            line = f"{ts}  {level}  {name_col} │ {msg}"

        if getattr(record, "turn_break", False):
            rule = "─" * 60
            sep = f"{self._DIM}{rule}{self._RESET}" if self._color else rule
            return f"\n{sep}\n{line}"
        if getattr(record, "iter_marker", False):
            return f"\n{line}"
        return line


def _configure_logging() -> None:
    """Send `openexecutive.*` logs to stdout at INFO level.

    Attaches the handler directly to the `openexecutive` logger (not root)
    with propagate=False, so uvicorn's `dictConfig` clobbering the root
    handler list doesn't silence us.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    app_logger = logging.getLogger("openexecutive")
    if not any(getattr(h, "_oe_configured", False) for h in app_logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_OELogFormatter(color=sys.stdout.isatty()))
        handler._oe_configured = True  # type: ignore[attr-defined]
        app_logger.addHandler(handler)
    app_logger.setLevel(level)
    app_logger.propagate = False
    # Quiet down a few chatty libraries.
    for noisy in ("httpx", "httpcore", "chromadb", "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()


# Paths that bypass the shared-secret gate. /health is hit by Fly's checker;
# the /webhook/* routes are called by external services (Google, Telegram) and
# carry their own verification.
_UNAUTHENTICATED_PATHS: frozenset[str] = frozenset(
    {"/health", "/webhook/telegram", "/webhook/google-chat"}
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from openexecutive.alerts.store import initialize_db as initialize_alerts_db
    from openexecutive.audit import AuditLogger, set_audit_logger
    from openexecutive.config import get_settings
    from openexecutive.knowledge.loader import seed_builtin_knowledge, seed_failures
    from openexecutive.knowledge.skills_index import seed_builtin_skills
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.memory.episodic import initialize_db

    # Re-assert logging config AFTER uvicorn's own dictConfig, then announce.
    _configure_logging()
    logging.getLogger("openexecutive").info(
        "openexecutive API starting (LOG_LEVEL=%s) — chat turn logs will appear below",
        os.environ.get("LOG_LEVEL", "INFO").upper(),
    )

    settings = get_settings()

    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    app.state.store = store
    # Hand the warm store to the MCP server's resource/tool handlers, which
    # have no FastAPI Request to reach app.state through.
    mcp_server.set_store(store)

    await seed_builtin_knowledge(store=store)
    await seed_builtin_skills(store=store)
    await seed_failures(store=store)

    initialize_db()
    initialize_alerts_db()

    # User-generated company fixtures (DB-backed; persists on the Fly volume).
    from openexecutive.fixtures.store import initialize_db as initialize_fixtures_db
    initialize_fixtures_db()

    from openexecutive.agents.overrides import initialize_overrides_db
    initialize_overrides_db()

    # People: init BEFORE departments so the people table exists when
    # departments code references person IDs (FK ordering).
    from openexecutive.people.store import initialize_db as initialize_people_db
    initialize_people_db()

    # Talent / executive-search core (clients, engagements, candidates).
    # Self-contained tables in the same DB; no FK ordering constraint with
    # the other subsystems.
    from openexecutive.talent.store import initialize_db as initialize_talent_db
    initialize_talent_db()

    # Staff-onboarding framework (templates, plans, tasks). Self-contained tables
    # in the same DB; no FK ordering constraint with the other subsystems.
    from openexecutive.staff_onboarding.store import (
        initialize_db as initialize_staff_onboarding_db,
    )
    initialize_staff_onboarding_db()
    # Seed default onboarding templates (idempotent — operator edits are kept).
    from openexecutive.staff_onboarding.seed import seed_default_templates
    seed_default_templates()

    # Departments: persistent state layer over the 8 specialist agents. Init
    # AFTER episodic_db so the additive ALTERs (department column on decisions,
    # initiatives, advice_given, scheduled_actions) have already run by the
    # time anything else writes to those tables.
    from openexecutive.departments.store import (
        initialize_db as initialize_departments_db,
    )
    from openexecutive.departments.store import seed_default_departments
    initialize_departments_db()
    seed_default_departments()

    from openexecutive.departments.completeness import check_org_completeness
    _org_warnings = check_org_completeness()
    for _w in _org_warnings:
        logging.getLogger("openexecutive").warning("org-completeness: %s", _w)

    from openexecutive.departments.cadence import (
        bootstrap_cadences,
        cancel_orphaned_cadences,
    )
    # Sweep first: cancel cadence actions left behind by deleted departments
    # so they stop firing check-in alerts, then enqueue for live departments.
    cancel_orphaned_cadences()
    bootstrap_cadences()

    if settings.nudge_scan_enabled:
        from openexecutive.scheduler.nudge_engine import bootstrap_nudge_scan
        bootstrap_nudge_scan()

    # External-condition monitor — init the watchlist / external_signals
    # tables, then seed the heartbeat row so the scheduler picks it up
    # on its next tick. Same shape as nudge_scan above.
    from openexecutive.monitoring.store import (
        initialize_db as initialize_monitoring_db,
    )
    initialize_monitoring_db()
    if settings.external_monitor_enabled:
        from openexecutive.monitoring.pipeline import (
            bootstrap_external_monitor_scan,
        )
        bootstrap_external_monitor_scan()

    # Watchlist research cron — periodic re-run gated by a state-hash
    # check so quiet days cost nothing. Mirrors the external_monitor
    # heartbeat above.
    if settings.watchlist_research_enabled:
        from openexecutive.monitoring.research.scheduler import (
            bootstrap_watchlist_research_scan,
        )
        bootstrap_watchlist_research_scan()

    if settings.notion_sync_enabled:
        from openexecutive.knowledge.notion_sync import bootstrap_notion_sync_scan
        bootstrap_notion_sync_scan()

    audit_logger = AuditLogger()
    app.state.audit = audit_logger
    set_audit_logger(audit_logger)

    # Honcho per-fixture workspace reconcile: if a previous process
    # crashed mid-demo, the active-workspace override may persist past
    # the fixture-active sentinel and route Honcho traffic to a stale
    # demo workspace forever. Clear it on boot.
    from openexecutive.cli.fixture_loader import reconcile_honcho_workspace_on_startup
    reconcile_honcho_workspace_on_startup(settings)

    from openexecutive.knowledge.external_sources import load_manifest
    from openexecutive.knowledge.review_store import ReviewStore

    ReviewStore.initialize_db()
    ReviewStore.sync_builtin_registrations()

    # Register any OER sources that were already ingested before this PR deployed.
    ingested_external = [
        {"id": src.id, "domains": src.domains}
        for src in load_manifest()
        if src.cache_dir.exists() and any(src.cache_dir.iterdir())
    ]
    if ingested_external:
        ReviewStore.sync_external_registrations(ingested_external)

    from openexecutive.evals.persistence import (
        initialize_eval_runs_db,
        initialize_user_scenarios_db,
    )
    from openexecutive.workflows.dynamic_store import initialize_dynamic_workflows_db
    from openexecutive.workflows.persistence import initialize_runs_db

    initialize_runs_db()
    initialize_dynamic_workflows_db()
    initialize_eval_runs_db()
    initialize_user_scenarios_db()

    email_poller_task: asyncio.Task[None] | None = None
    scheduler_task: asyncio.Task[None] | None = None
    resumer_task: asyncio.Task[None] | None = None
    discord_bot: Any = None
    discord_bot_task: asyncio.Task[None] | None = None

    if settings.mcp_enabled:
        from openexecutive.orchestrator.mcp_gateway import MCPGateway, set_active_gateway

        gateway = MCPGateway()
        await gateway.start(settings.mcp_servers_config_path)
        app.state.mcp_gateway = gateway
        set_active_gateway(gateway)

        from openexecutive.integrations.email_poller import run_email_poller
        email_poller_task = asyncio.create_task(run_email_poller(gateway))
    else:
        app.state.mcp_gateway = None

    if settings.scheduler_enabled:
        from openexecutive.scheduler import run_scheduler

        scheduler_task = asyncio.create_task(
            run_scheduler(
                gateway=getattr(app.state, "mcp_gateway", None),
                poll_interval_seconds=settings.scheduler_poll_interval_seconds,
            )
        )

    # Start the WaitForHuman resumer alongside the scheduler (same single-worker
    # constraint — do not run in more than one process against the same DB).
    from openexecutive.workflows.resumer import run_resumer
    resumer_task = asyncio.create_task(run_resumer())

    # Discord gateway bot. Embedded in the lifespan (rather than a sibling Fly
    # app) because the bot needs direct access to the same SQLite + ChromaDB
    # under /data, and Fly volumes attach to a single machine. Same pattern as
    # email_poller above. Skipped when no token is configured.
    if settings.discord_bot_token:
        _discord_log = logging.getLogger("openexecutive")
        try:
            from openexecutive.integrations.discord_bot import create_discord_bot
        except ImportError:
            _discord_log.exception(
                "Discord bot enabled but discord.py is not installed; skipping bot"
            )
        else:
            try:
                discord_bot = create_discord_bot()
                discord_bot_task = asyncio.create_task(
                    discord_bot.start(settings.discord_bot_token)
                )

                # Surface bot crashes (invalid token, gateway 4004, network)
                # immediately instead of waiting for shutdown to discover them.
                def _on_discord_done(task: asyncio.Task[None]) -> None:
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None:
                        _discord_log.error(
                            "Discord bot exited unexpectedly", exc_info=exc
                        )

                discord_bot_task.add_done_callback(_on_discord_done)
            except Exception:
                _discord_log.exception(
                    "Failed to start Discord bot; continuing without it"
                )
                discord_bot = None
                discord_bot_task = None

    # Run the MCP Streamable-HTTP session manager for the life of the app.
    # Mounting the sub-app does NOT run its lifespan, so without this every
    # /mcp request 500s. The session manager was created when create_app()
    # called mcp_server.mount() → streamable_http_app(). The run-once guard
    # tolerates repeated create_app() lifespans in the test suite (the manager
    # can only be run once per instance).
    async with mcp_server.run_session_manager():
        yield

    # Shut Discord down FIRST so the gateway stops accepting new events before
    # we tear down email_poller/scheduler/resumer that handlers might call into.
    # Bounded timeout: discord.py's close handshake can stall during a reconnect,
    # and a hung shutdown blocks the FastAPI lifespan and risks SIGKILL on Fly.
    if discord_bot is not None or discord_bot_task is not None:
        async def _shutdown_discord() -> None:
            if discord_bot is not None:
                with contextlib.suppress(Exception):
                    await discord_bot.close()
            if discord_bot_task is not None:
                with contextlib.suppress(
                    asyncio.CancelledError, Exception
                ):
                    await discord_bot_task

        try:
            await asyncio.wait_for(_shutdown_discord(), timeout=10.0)
        except TimeoutError:
            logging.getLogger("openexecutive").warning(
                "Discord shutdown exceeded 10s; cancelling task"
            )
            if discord_bot_task is not None and not discord_bot_task.done():
                discord_bot_task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, Exception
                ):
                    await discord_bot_task

    if email_poller_task is not None:
        email_poller_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await email_poller_task

    if scheduler_task is not None:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await scheduler_task

    if resumer_task is not None:
        resumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await resumer_task

    if getattr(app.state, "mcp_gateway", None) is not None:
        from openexecutive.orchestrator.mcp_gateway import set_active_gateway
        set_active_gateway(None)
        await app.state.mcp_gateway.close()

    # Cleanup if needed (ChromaDB handles persistence)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Open Executive API",
        description="AI-powered virtual executive team",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Allowed UI origins: localhost for `make dev`, plus any production
    # origins listed in BACKEND_ALLOWED_ORIGINS (comma-separated, e.g.
    # "https://openexec-ui-dev.fly.dev,https://exec.mycompany.com").
    extra_origins = [
        o.strip()
        for o in os.environ.get("BACKEND_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", *extra_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Shared-secret gate. If BACKEND_SHARED_SECRET is set, every non-exempt
    # request must include a matching x-api-key header. If unset, the gate is
    # off (intended for local dev only — production deploys MUST set it).
    # Fail closed when running under Fly (FLY_APP_NAME is set on every machine):
    # no secret on a publicly reachable instance is never acceptable.
    shared_secret = os.environ.get("BACKEND_SHARED_SECRET", "").strip()
    if not shared_secret and os.environ.get("FLY_APP_NAME"):
        raise RuntimeError(
            "BACKEND_SHARED_SECRET is required when running on Fly. "
            "Set it with: flyctl secrets set -a $FLY_APP_NAME "
            "BACKEND_SHARED_SECRET=$(openssl rand -hex 32)"
        )
    if shared_secret:
        @app.middleware("http")
        async def _shared_secret_gate(request: Request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path in _UNAUTHENTICATED_PATHS or request.method == "OPTIONS":
                return await call_next(request)
            provided = request.headers.get("x-api-key", "")
            if not provided or not hmac.compare_digest(provided, shared_secret):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)
    else:
        logging.getLogger("openexecutive").warning(
            "BACKEND_SHARED_SECRET is unset — API is open. Acceptable for local "
            "dev only; set this secret in any environment reachable from the "
            "public internet."
        )

    app.include_router(auth_route.router, tags=["auth"])
    app.include_router(fixtures.router, tags=["fixtures"])
    app.include_router(clients.router, tags=["clients"])
    app.include_router(agents.router, tags=["agents"])
    app.include_router(personas.router, tags=["personas"])
    app.include_router(chat.router, tags=["chat"])
    app.include_router(sessions.router, tags=["sessions"])
    app.include_router(onboarding.router, tags=["onboarding"])
    app.include_router(company_profile.router, tags=["company-profile"])
    app.include_router(documents.router, tags=["documents"])
    app.include_router(knowledge.router, tags=["knowledge"])
    app.include_router(skills.router, tags=["skills"])
    app.include_router(workflows.router, tags=["workflows"])
    app.include_router(evals.router, tags=["evals"])
    app.include_router(episodic.router, tags=["memories"])
    app.include_router(review.router, tags=["review"])
    app.include_router(alerts.router, tags=["alerts"])
    app.include_router(artifacts.router, tags=["artifacts"])
    app.include_router(decisions.router, tags=["decisions"])
    app.include_router(audit.router, tags=["audit"])
    app.include_router(departments.router, tags=["departments"])
    app.include_router(people.router, tags=["people"])
    app.include_router(talent.router, tags=["talent"])
    app.include_router(staff_onboarding.router, tags=["staff-onboarding"])
    app.include_router(today.router, tags=["today"])
    app.include_router(scheduled.router, tags=["scheduled"])
    app.include_router(watchlist.router, tags=["watchlist"])
    app.include_router(google_chat_router, tags=["google-chat"])
    app.include_router(telegram_router, tags=["telegram"])
    app.include_router(architecture.router, tags=["architecture"])
    app.include_router(guide.router, tags=["guide"])
    app.include_router(health.router, tags=["health"])

    # Expose Open Executive as an MCP server at /mcp (Streamable-HTTP). Gated
    # by the same shared-secret middleware as every other route — clients pass
    # x-api-key. Mounting also lazily creates mcp.session_manager, which the
    # lifespan runs (see above).
    mcp_server.mount(app)

    return app


app = create_app()
