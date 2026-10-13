"""Keeping the MCP gateway up: start with retries, re-check on demand, report.

The gateway (``orchestrator/mcp_gateway.py``) is one extensible-mcp subprocess
started at boot. It used to get a single try with a 120 s cap and no second
chance: after a slow cold start, a transient fault in the child or a package
cache that had to be rebuilt, the process ran until its next restart with no
MCP tools. No email went out or came in, and the daily brief recorded "no
deliverable channel configured" as if nothing had been set up.

``GatewaySupervisor`` owns the attempts:

* ``start`` makes the first attempt at boot, under the same cap as before.
* When it fails, the supervisor keeps trying in the background with a growing
  wait between attempts (``BACKOFF_S``, capped at its last value), for as
  long as the process runs.
* ``ensure`` is the lazy re-check for work that needs the gateway now (a
  scheduled brief, an email follow-up, the poller): while the gateway is
  down and no attempt is running, one is made right away instead of waiting
  for the next retry; an attempt already running is awaited.
* A successful attempt does what boot did: the gateway becomes the active
  one (``set_active_gateway``), its pinned Google tools are primed and the
  email poller starts.
* ``status`` is what ``/health`` reports under ``gateway`` and what the Setup
  status page and the brief's "not sent" notice read.

The supervisor never raises out of an attempt except for a real cancellation
of the task awaiting it: the failure modes are the ones ``api/main.py``
contained in #122 (anyio exception groups, a silent child), and they are
contained here the same way.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger("openexecutive.orchestrator.gateway_boot")

# Seconds to wait before each retry after a failed attempt: the first retry
# after 30 s, then doubling, then every 10 minutes for good. A cold start
# that has to clone extensible-mcp can genuinely take a couple of minutes, so
# the early retries are not wasted on a child that just needed longer.
BACKOFF_S: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0, 600.0)
# How long one attempt may take. The child is `uvx --from git+…`, which
# resolves and may clone on a cold start, so this is generous; what it must
# not be is unbounded: `ClientSession.initialize()` waits forever on a child
# that is alive but silent.
START_TIMEOUT_S = 120.0
# Tearing down a gateway that just failed walks the same stdio machinery that
# failed, so it gets a clock too.
CLOSE_TIMEOUT_S = 10.0
# How much of a failure's text /health and the Setup page carry.
ERROR_TEXT_MAX = 300

GatewayState = Literal["off", "no_servers", "starting", "up", "down"]


@dataclass(frozen=True)
class GatewayStatus:
    """The gateway as /health reports it.

    ``state``: ``off`` (MCP is not enabled), ``no_servers`` (enabled, but the
    config names none), ``starting`` (an attempt is running: the first one,
    or a retry), ``up`` or ``down`` (every attempt so far failed; the next
    one is at ``next_retry_at``). ``attempts`` counts starts tried, so an
    operator can tell a gateway that came up first time from one that needed
    three tries. ``last_error`` is the last failure's text, kept after a
    recovery so the log line is still findable.
    """

    state: GatewayState
    attempts: int = 0
    last_error: str | None = None
    last_failed_at: datetime | None = None
    up_since: datetime | None = None
    next_retry_at: datetime | None = None
    email_poller: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_failed_at": self.last_failed_at.isoformat() if self.last_failed_at else None,
            "up_since": self.up_since.isoformat() if self.up_since else None,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "email_poller": self.email_poller,
        }


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:ERROR_TEXT_MAX]


class GatewaySupervisor:
    """Starts the gateway, retries while it is down, and reports on it.

    ``app_state`` is the FastAPI app's ``state`` (or anything with settable
    attributes): the supervisor keeps ``mcp_gateway`` and ``mcp_prime_task``
    on it, as boot always did, so ``/health`` and the Setup page read the
    gateway from the app whether it came up at boot or later. ``servers`` is
    what the config names (``configured_server_names``), already checked to
    be non-empty by the caller, which also logs the reason MCP is on.
    """

    def __init__(
        self,
        *,
        config_path: Path,
        servers: Sequence[str],
        app_state: Any = None,
        start_timeout_s: float = START_TIMEOUT_S,
        backoff_s: Sequence[float] = BACKOFF_S,
        why: str = "",
    ) -> None:
        self._config_path = config_path
        self._servers = tuple(servers)
        self._app_state = app_state
        self._start_timeout_s = start_timeout_s
        self._backoff_s = tuple(backoff_s) or BACKOFF_S
        self._why = why
        self.gateway: MCPGateway | None = None
        self.poller_task: asyncio.Task[None] | None = None
        self.prime_task: asyncio.Task[Any] | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._attempt: asyncio.Task[bool] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self.attempts = 0
        self.last_error: str | None = None
        self.last_failed_at: datetime | None = None
        self.up_since: datetime | None = None
        self.next_retry_at: datetime | None = None

    # -- state ---------------------------------------------------------------

    @property
    def is_up(self) -> bool:
        return self.gateway is not None

    def status(self) -> GatewayStatus:
        if self.gateway is not None:
            state: GatewayState = "up"
        elif self._attempt is not None and not self._attempt.done():
            state = "starting"
        else:
            state = "down"
        return GatewayStatus(
            state=state,
            attempts=self.attempts,
            last_error=self.last_error,
            last_failed_at=self.last_failed_at,
            up_since=self.up_since,
            next_retry_at=self.next_retry_at if state == "down" else None,
            email_poller=self.poller_task is not None and not self.poller_task.done(),
        )

    def problem(self) -> str | None:
        """Why the gateway is down, in one line, or None while it is up."""
        if self.gateway is not None:
            return None
        if self.attempts == 0:
            return "the MCP gateway has not started yet"
        if self._attempt is not None and not self._attempt.done():
            return f"the MCP gateway is starting (attempt {self.attempts + 1})"
        detail = f": {self.last_error}" if self.last_error else ""
        return f"the MCP gateway failed to start ({self.attempts} attempt{'s' if self.attempts != 1 else ''}){detail}"

    # -- attempts ------------------------------------------------------------

    async def start(self) -> bool:
        """The boot attempt. On failure the background retries begin."""
        ok = await self.attempt()
        if not ok and not self._closed:
            self._schedule_retry()
        return ok

    async def attempt(self) -> bool:
        """One start, serialized: a second caller waits for the running one
        and gets its outcome rather than starting a second child."""
        if self.gateway is not None:
            return True
        if self._attempt is not None and not self._attempt.done():
            return await asyncio.shield(self._attempt)
        self._attempt = asyncio.ensure_future(self._attempt_once())
        return await asyncio.shield(self._attempt)

    async def ensure(self) -> MCPGateway | None:
        """The gateway, starting it now if it is down and no attempt is
        running. For work that needs it at this moment: a brief about to go
        out should not wait ten minutes for the next scheduled retry when the
        child may well come up now."""
        if self.gateway is not None or self._closed:
            return self.gateway
        try:
            await self.attempt()
        except asyncio.CancelledError:
            raise
        except BaseException:
            logger.exception("MCP gateway re-check failed unexpectedly")
        return self.gateway

    async def _attempt_once(self) -> bool:
        async with self._lock:
            if self.gateway is not None:
                return True
            if self._closed:
                return False
            from openexecutive.orchestrator import mcp_gateway as gw_mod

            self.attempts += 1
            n = self.attempts
            gateway = gw_mod.MCPGateway()
            try:
                await asyncio.wait_for(gateway.start(self._config_path), timeout=self._start_timeout_s)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.last_error = _error_text(exc)
                self.last_failed_at = datetime.now(UTC)
                logger.exception(
                    "MCP gateway failed to start within %.0fs (attempt %d); continuing WITHOUT "
                    "MCP tools and without the email poller until a retry succeeds. "
                    "config=%s servers=[%s] (%s)",
                    self._start_timeout_s, n, self._config_path, ", ".join(self._servers), self._why,
                )
                await _close_quietly(gateway)
                return False
            self.gateway = gateway
            self.up_since = datetime.now(UTC)
            self.next_retry_at = None
            self._wire(gateway, attempt=n)
            return True

    def _wire(self, gateway: MCPGateway, *, attempt: int) -> None:
        """What boot did on a successful start, now on any successful start."""
        from openexecutive.orchestrator import mcp_gateway as gw_mod

        if self._app_state is not None:
            self._app_state.mcp_gateway = gateway
        gw_mod.set_active_gateway(gateway)
        if "google_workspace" in self._servers:
            # Discover the pinned Google tools now, so the model's first
            # direct call_tool doesn't wait on the search. Held here: a bare
            # create_task is only weakly referenced.
            self.prime_task = asyncio.create_task(gateway.prime_pinned_tools())
            if self._app_state is not None:
                self._app_state.mcp_prime_task = self.prime_task
        from openexecutive.integrations.email_poller import run_email_poller

        self.poller_task = asyncio.create_task(run_email_poller(gateway))
        if attempt > 1:
            logger.warning(
                "MCP gateway came up on attempt %d; MCP tools and the email poller are on",
                attempt,
            )

    def _schedule_retry(self) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._set_next_retry(self._delay_for(self.attempts))
        self._retry_task = asyncio.create_task(self._retry_loop(), name="mcp-gateway-retry")

    def _delay_for(self, failures: int) -> float:
        index = min(max(failures - 1, 0), len(self._backoff_s) - 1)
        return self._backoff_s[index]

    def _set_next_retry(self, delay: float) -> None:
        self.next_retry_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() + delay, tz=UTC)

    async def _retry_loop(self) -> None:
        try:
            while self.gateway is None and not self._closed:
                delay = self._delay_for(self.attempts)
                self._set_next_retry(delay)
                logger.warning("MCP gateway retry in %.0fs", delay)
                await asyncio.sleep(delay)
                if self.gateway is not None or self._closed:
                    return
                try:
                    await self.attempt()
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    logger.exception("MCP gateway retry failed unexpectedly")
        except asyncio.CancelledError:
            raise
        finally:
            self.next_retry_at = None

    # -- shutdown ------------------------------------------------------------

    async def close(self) -> None:
        """Stop retrying, stop the poller and priming, and close the gateway."""
        self._closed = True
        for task in (self._retry_task, self._attempt, self.poller_task, self.prime_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        gateway, self.gateway = self.gateway, None
        if gateway is not None:
            from openexecutive.orchestrator import mcp_gateway as gw_mod

            if gw_mod.get_active_gateway() is gateway:
                gw_mod.set_active_gateway(None)
            if self._app_state is not None:
                self._app_state.mcp_gateway = None
            await gateway.close()


async def _close_quietly(gateway: Any) -> None:
    """Reap a gateway that failed to start. Never raises, never hangs.

    ``MCPGateway.close`` suppresses ``Exception`` around each step, which is
    not enough here: anyio raises ``BaseExceptionGroup`` as soon as one
    sub-exception is a ``BaseException`` such as ``CancelledError``, and that
    is not an ``Exception``. Since this runs from inside an ``except`` block,
    anything it raises replaces the actionable log just written with the crash
    that log exists to prevent.
    """
    try:
        await asyncio.wait_for(gateway.close(), timeout=CLOSE_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except BaseException:
        logger.warning(
            "MCP gateway teardown after a failed start did not finish cleanly; "
            "continuing without the failed gateway anyway",
            exc_info=True,
        )
